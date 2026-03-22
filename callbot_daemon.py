#!/usr/bin/env python3
import io
import json
import logging
import os
import posixpath
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS, all_errors, error_perm
from pathlib import Path
from typing import Any, Dict, List, Optional

import paramiko
import requests
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}
TELEGRAM_TEXT_LIMIT = 3900
DEFAULT_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024


class ReusedSessionFTP_TLS(FTP_TLS):
    def ntransfercmd(self, cmd, rest=None):
        conn, size = super(FTP_TLS, self).ntransfercmd(cmd, rest)
        if not self._prot_p:
            return conn, size

        wrap_kwargs = {"server_hostname": self.host}
        session = getattr(self.sock, "session", None)
        if session is not None:
            wrap_kwargs["session"] = session

        try:
            conn = self.context.wrap_socket(conn, **wrap_kwargs)
        except TypeError:
            wrap_kwargs.pop("session", None)
            conn = self.context.wrap_socket(conn, **wrap_kwargs)
        except ssl.SSLError:
            conn.close()
            raise
        return conn, size


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_optional_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return int(raw)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_remote_path(path: str) -> str:
    normalized = posixpath.normpath((path or "/").replace("\\", "/"))
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def join_remote_path(base: str, name: str) -> str:
    base = normalize_remote_path(base)
    if base == "/":
        return normalize_remote_path(f"/{name}")
    return normalize_remote_path(posixpath.join(base.rstrip("/"), name))


def replace_ext(remote_path: str, new_ext: str) -> str:
    base = posixpath.splitext(remote_path)[0]
    return f"{base}{new_ext}"


def parse_ftp_modify(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def response_to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"Unsupported response type: {type(obj)!r}")


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


@dataclass
class Config:
    ftp_protocol: str
    ftp_host: str
    ftp_port: int
    ftp_user: str
    ftp_password: str
    ftp_remote_root: str
    ftp_archive_dir: str
    ftp_delete_after_success: bool
    ftp_move_to_archive_after_success: bool
    ftp_use_tls: bool
    ftp_timeout_sec: int
    ftp_connect_attempts: int
    ftp_retry_delay_sec: float
    openai_api_key: str
    transcribe_model: str
    transcribe_language: str
    transcribe_chunking_strategy: str
    analysis_model: str
    analysis_reasoning_effort: str
    analysis_store: bool
    analysis_max_output_tokens: int
    instruction_json_path: Path
    state_path: Path
    work_root: Path
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_message_thread_id: Optional[int]
    poll_interval_sec: int
    min_stable_polls: int
    min_audio_bytes: int
    min_dialogue_words: int
    min_duration_min: float
    split_threshold_bytes: int
    target_part_max_bytes: int
    part_export_bitrate: str
    part_export_frame_rate: int
    part_export_channels: int
    max_transcribe_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        ftp_protocol = os.getenv("FTP_PROTOCOL", "ftp").strip().lower() or "ftp"
        ftp_user = (
            os.getenv("FTP_USER")
            or os.getenv("FTP_USERNAME")
            or ""
        ).strip()
        ftp_port_default = "22" if ftp_protocol == "sftp" else "21"
        return cls(
            ftp_protocol=ftp_protocol,
            ftp_host=os.environ["FTP_HOST"],
            ftp_port=int(os.getenv("FTP_PORT", ftp_port_default)),
            ftp_user=ftp_user or os.environ["FTP_USER"],
            ftp_password=os.environ["FTP_PASSWORD"],
            ftp_remote_root=normalize_remote_path(
                (
                    os.getenv("FTP_REMOTE_ROOT")
                    or os.getenv("FTP_REMOTE_DIR")
                    or "/"
                ).strip()
                or "/"
            ),
            ftp_archive_dir=normalize_remote_path(
                os.getenv("FTP_ARCHIVE_DIR", "/archive").strip() or "/archive"
            ),
            ftp_delete_after_success=env_bool("FTP_DELETE_AFTER_SUCCESS", False),
            ftp_move_to_archive_after_success=env_bool(
                "FTP_MOVE_TO_ARCHIVE_AFTER_SUCCESS", False
            ),
            ftp_use_tls=env_bool("FTP_USE_TLS", True),
            ftp_timeout_sec=int(os.getenv("FTP_TIMEOUT_SEC", "60")),
            ftp_connect_attempts=max(
                1, int(os.getenv("FTP_CONNECT_ATTEMPTS", "2"))
            ),
            ftp_retry_delay_sec=max(
                0.0, float(os.getenv("FTP_RETRY_DELAY_SEC", "5"))
            ),
            openai_api_key=os.environ["OPENAI_API_KEY"],
            transcribe_model=os.getenv(
                "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe-diarize"
            ).strip(),
            transcribe_language=os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "ru").strip(),
            transcribe_chunking_strategy=os.getenv(
                "OPENAI_CHUNKING_STRATEGY", "auto"
            ).strip(),
            analysis_model=os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5-mini").strip(),
            analysis_reasoning_effort=os.getenv(
                "OPENAI_ANALYSIS_REASONING_EFFORT", ""
            ).strip(),
            analysis_store=env_bool("OPENAI_ANALYSIS_STORE", False),
            analysis_max_output_tokens=int(
                os.getenv("OPENAI_ANALYSIS_MAX_OUTPUT_TOKENS", "1800")
            ),
            instruction_json_path=Path(
                os.getenv(
                    "INSTRUCTIONS_JSON_PATH",
                    os.getenv("INSTRUCTION_JSON_PATH", "./instructions.json"),
                )
            )
            .expanduser()
            .resolve(),
            state_path=Path(os.getenv("STATE_PATH", "./state.json"))
            .expanduser()
            .resolve(),
            work_root=Path(os.getenv("WORK_ROOT", "./work"))
            .expanduser()
            .resolve(),
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            telegram_message_thread_id=env_optional_int("TELEGRAM_MESSAGE_THREAD_ID"),
            poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", "300")),
            min_stable_polls=int(os.getenv("MIN_STABLE_POLLS", "2")),
            min_audio_bytes=int(os.getenv("MIN_AUDIO_BYTES", str(100 * 1024))),
            min_dialogue_words=int(os.getenv("MIN_DIALOGUE_WORDS", "30")),
            min_duration_min=float(os.getenv("MIN_DURATION_MIN", "0.5")),
            split_threshold_bytes=int(
                os.getenv("SPLIT_THRESHOLD_BYTES", str(4 * 1024 * 1024))
            ),
            target_part_max_bytes=int(
                os.getenv("TARGET_PART_MAX_BYTES", str(4 * 1024 * 1024))
            ),
            part_export_bitrate=os.getenv("PART_EXPORT_BITRATE", "64k").strip(),
            part_export_frame_rate=int(os.getenv("PART_EXPORT_FRAME_RATE", "16000")),
            part_export_channels=int(os.getenv("PART_EXPORT_CHANNELS", "1")),
            max_transcribe_bytes=int(
                os.getenv(
                    "MAX_TRANSCRIBE_BYTES",
                    os.getenv("MAX_API_FILE_SIZE_BYTES", str(DEFAULT_TRANSCRIBE_MAX_BYTES)),
                )
            ),
        )


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"files": {}}
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {"files": {}}
        state = json.loads(raw)
        if not isinstance(state, dict):
            logging.warning("State file root is not an object, starting from empty: %s", path)
            return {"files": {}}
        files = state.get("files")
        if not isinstance(files, dict):
            state["files"] = {}
        return state
    except json.JSONDecodeError:
        logging.exception("Could not parse state file, starting from empty: %s", path)
        return {"files": {}}
    except OSError:
        logging.exception("Could not read state file, starting from empty: %s", path)
        return {"files": {}}


def save_state(path: Path, state: Dict[str, Any]) -> None:
    ensure_parent_dir(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def load_instruction_text(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()

    if isinstance(obj, dict):
        for key in (
            "instructions",
            "instruction",
            "prompt",
            "system_prompt",
            "system",
            "text",
        ):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw.strip()


class RemoteConnectionError(RuntimeError):
    pass


def is_retryable_remote_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, EOFError):
        return True
    if isinstance(exc, paramiko.SSHException):
        message = str(exc).lower()
        return "timeout" in message or "banner" in message
    if isinstance(exc, OSError):
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "timed out",
                "timeout",
                "temporarily unavailable",
                "connection reset",
                "connection aborted",
                "connection refused",
                "network is unreachable",
                "broken pipe",
            )
        )
    return False


def connect_with_retry(cfg: Config, protocol: str, fn):
    last_exc: Optional[Exception] = None
    for attempt in range(1, cfg.ftp_connect_attempts + 1):
        try:
            return fn()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            if not is_retryable_remote_error(exc):
                raise
            last_exc = exc
            if attempt >= cfg.ftp_connect_attempts:
                break
            logging.warning(
                "%s connection attempt %s/%s failed: %s. Retrying in %.1f sec.",
                protocol.upper(),
                attempt,
                cfg.ftp_connect_attempts,
                exc,
                cfg.ftp_retry_delay_sec,
            )
            time.sleep(cfg.ftp_retry_delay_sec)
    raise RemoteConnectionError(
        f"{protocol.upper()} connection to {cfg.ftp_host}:{cfg.ftp_port} "
        f"failed after {cfg.ftp_connect_attempts} attempt(s): {last_exc}"
    ) from last_exc


def ftp_connect_once(cfg: Config):
    ftp = ReusedSessionFTP_TLS() if cfg.ftp_use_tls else FTP()
    ftp.encoding = "utf-8"
    ftp.connect(cfg.ftp_host, cfg.ftp_port, timeout=cfg.ftp_timeout_sec)
    ftp.login(cfg.ftp_user, cfg.ftp_password)
    if cfg.ftp_use_tls and isinstance(ftp, FTP_TLS):
        ftp.prot_p()
    return ftp


def ftp_connect(cfg: Config):
    return connect_with_retry(cfg, "ftp", lambda: ftp_connect_once(cfg))


def ftp_download_file(cfg: Config, remote_path: str, local_path: Path) -> None:
    ensure_parent_dir(local_path)
    with closing(ftp_connect(cfg)) as ftp, local_path.open("wb") as out:
        ftp.retrbinary(f"RETR {remote_path}", out.write)


def ftp_upload_json(cfg: Config, remote_path: str, payload: Dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    directory = posixpath.dirname(remote_path) or "/"
    filename = posixpath.basename(remote_path)
    with closing(ftp_connect(cfg)) as ftp:
        current = ftp.pwd()
        try:
            ensure_ftp_dir(ftp, directory)
            ftp.cwd(directory)
            ftp.storbinary(f"STOR {filename}", io.BytesIO(content))
        finally:
            ftp.cwd(current)


def ensure_ftp_dir(ftp, directory: str) -> None:
    directory = normalize_remote_path(directory)
    current = "/"
    for part in [part for part in directory.split("/") if part]:
        current = join_remote_path(current, part)
        try:
            ftp.cwd(current)
        except all_errors:
            ftp.mkd(current)


def sftp_connect_once(cfg: Config):
    transport = paramiko.Transport((cfg.ftp_host, cfg.ftp_port))
    transport.banner_timeout = cfg.ftp_timeout_sec
    transport.auth_timeout = cfg.ftp_timeout_sec
    try:
        transport.connect(username=cfg.ftp_user, password=cfg.ftp_password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        return transport, sftp
    except Exception:
        transport.close()
        raise


def sftp_connect(cfg: Config):
    return connect_with_retry(cfg, "sftp", lambda: sftp_connect_once(cfg))


def ensure_sftp_dir(sftp: paramiko.SFTPClient, directory: str) -> None:
    directory = normalize_remote_path(directory)
    current = "/"
    for part in [part for part in directory.split("/") if part]:
        current = join_remote_path(current, part)
        try:
            attrs = sftp.stat(current)
            if not stat.S_ISDIR(attrs.st_mode):
                raise RuntimeError(f"Remote path exists and is not a directory: {current}")
        except FileNotFoundError:
            sftp.mkdir(current)


def sftp_walk(cfg: Config, root: str) -> List[Dict[str, Any]]:
    transport, sftp = sftp_connect(cfg)
    result: List[Dict[str, Any]] = []

    def walk(path: str) -> None:
        for entry in sftp.listdir_attr(path):
            name = entry.filename
            if name in (".", ".."):
                continue
            full_path = join_remote_path(path, name)
            modify = None
            if getattr(entry, "st_mtime", None):
                modify = datetime.fromtimestamp(
                    entry.st_mtime,
                    tz=timezone.utc,
                ).strftime("%Y%m%d%H%M%S")
            if stat.S_ISDIR(entry.st_mode):
                walk(full_path)
                continue
            result.append(
                {
                    "name": name,
                    "path": full_path,
                    "size": int(entry.st_size),
                    "modify": modify,
                }
            )

    try:
        walk(normalize_remote_path(root))
        return result
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


def sftp_download_file(cfg: Config, remote_path: str, local_path: Path) -> None:
    ensure_parent_dir(local_path)
    transport, sftp = sftp_connect(cfg)
    try:
        sftp.get(remote_path, str(local_path))
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


def sftp_upload_json(cfg: Config, remote_path: str, payload: Dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    transport, sftp = sftp_connect(cfg)
    try:
        ensure_sftp_dir(sftp, posixpath.dirname(remote_path) or "/")
        with sftp.open(remote_path, "w") as remote_file:
            remote_file.set_pipelined(True)
            remote_file.write(content)
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


def is_mlsd_unsupported(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        message.startswith("500")
        or message.startswith("501")
        or message.startswith("502")
        or message.startswith("504")
        or "mlsd" in message
        or "not understood" in message
        or "not implemented" in message
    )


def is_remote_dir(ftp, remote_path: str) -> bool:
    current = ftp.pwd()
    try:
        ftp.cwd(remote_path)
        return True
    except all_errors:
        return False
    finally:
        try:
            ftp.cwd(current)
        except all_errors:
            pass


def safe_ftp_size(ftp, remote_path: str) -> int:
    try:
        size = ftp.size(remote_path)
        return int(size) if size is not None else 0
    except all_errors:
        return 0


def safe_ftp_modify(ftp, remote_path: str) -> Optional[str]:
    try:
        raw = ftp.sendcmd(f"MDTM {remote_path}")
    except all_errors:
        return None
    if raw.startswith("213 "):
        return raw[4:].strip()
    return None


def ftp_walk_mlsd(ftp, root: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    def walk(path: str) -> None:
        current = ftp.pwd()
        try:
            ftp.cwd(path)
            entries = list(ftp.mlsd())
        finally:
            ftp.cwd(current)

        for name, facts in entries:
            if name in (".", ".."):
                continue
            full_path = join_remote_path(path, name)
            item_type = facts.get("type")
            if item_type == "dir":
                walk(full_path)
                continue
            if item_type != "file":
                continue
            result.append(
                {
                    "name": name,
                    "path": full_path,
                    "size": int(facts.get("size") or 0),
                    "modify": facts.get("modify"),
                }
            )

    walk(root)
    return result


def ftp_walk_nlst(ftp, root: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    visited_dirs = set()

    def walk(path: str) -> None:
        normalized_path = normalize_remote_path(path)
        if normalized_path in visited_dirs:
            return
        visited_dirs.add(normalized_path)

        current = ftp.pwd()
        try:
            ftp.cwd(normalized_path)
            entries = ftp.nlst()
        except error_perm as exc:
            if str(exc).startswith("550"):
                return
            raise
        finally:
            try:
                ftp.cwd(current)
            except all_errors:
                pass

        for raw_entry in entries:
            raw_entry = raw_entry.strip()
            if not raw_entry:
                continue
            name = posixpath.basename(raw_entry.rstrip("/")) or raw_entry
            if name in (".", ".."):
                continue
            full_path = (
                normalize_remote_path(raw_entry)
                if raw_entry.startswith("/")
                else join_remote_path(normalized_path, name)
            )
            if is_remote_dir(ftp, full_path):
                walk(full_path)
                continue
            result.append(
                {
                    "name": name,
                    "path": full_path,
                    "size": safe_ftp_size(ftp, full_path),
                    "modify": safe_ftp_modify(ftp, full_path),
                }
            )

    walk(root)
    return result


def ftp_walk(ftp, root: str) -> List[Dict[str, Any]]:
    try:
        return ftp_walk_mlsd(ftp, root)
    except all_errors as exc:
        if not is_mlsd_unsupported(exc):
            raise
        logging.warning(
            "FTP server does not support MLSD, falling back to NLST scan: %s", exc
        )
        return ftp_walk_nlst(ftp, root)


def remote_walk(cfg: Config) -> List[Dict[str, Any]]:
    if cfg.ftp_protocol == "sftp":
        return sftp_walk(cfg, cfg.ftp_remote_root)
    with closing(ftp_connect(cfg)) as ftp:
        return ftp_walk(ftp, cfg.ftp_remote_root)


def remote_download_file(cfg: Config, remote_path: str, local_path: Path) -> None:
    if cfg.ftp_protocol == "sftp":
        sftp_download_file(cfg, remote_path, local_path)
        return
    ftp_download_file(cfg, remote_path, local_path)


def remote_upload_json(cfg: Config, remote_path: str, payload: Dict[str, Any]) -> None:
    if cfg.ftp_protocol == "sftp":
        sftp_upload_json(cfg, remote_path, payload)
        return
    ftp_upload_json(cfg, remote_path, payload)


def remote_archive_or_delete(cfg: Config, remote_path: str) -> None:
    if cfg.ftp_move_to_archive_after_success:
        destination_name = posixpath.basename(remote_path)
        destination_path = join_remote_path(cfg.ftp_archive_dir, destination_name)
        if cfg.ftp_protocol == "sftp":
            transport, sftp = sftp_connect(cfg)
            try:
                ensure_sftp_dir(sftp, cfg.ftp_archive_dir)
                try:
                    sftp.rename(remote_path, destination_path)
                except OSError:
                    stem, suffix = posixpath.splitext(destination_name)
                    destination_path = join_remote_path(
                        cfg.ftp_archive_dir,
                        f"{stem}__{int(time.time())}{suffix}",
                    )
                    sftp.rename(remote_path, destination_path)
            finally:
                try:
                    sftp.close()
                finally:
                    transport.close()
            return

        with closing(ftp_connect(cfg)) as ftp:
            ensure_ftp_dir(ftp, cfg.ftp_archive_dir)
            try:
                ftp.rename(remote_path, destination_path)
            except all_errors:
                stem, suffix = posixpath.splitext(destination_name)
                destination_path = join_remote_path(
                    cfg.ftp_archive_dir,
                    f"{stem}__{int(time.time())}{suffix}",
                )
                ftp.rename(remote_path, destination_path)
        return

    if not cfg.ftp_delete_after_success:
        return

    if cfg.ftp_protocol == "sftp":
        transport, sftp = sftp_connect(cfg)
        try:
            sftp.remove(remote_path)
        finally:
            try:
                sftp.close()
            finally:
                transport.close()
        return

    with closing(ftp_connect(cfg)) as ftp:
        ftp.delete(remote_path)


def run_cmd(cmd: List[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\nSTDOUT:\n"
            + proc.stdout
            + "\nSTDERR:\n"
            + proc.stderr
        )


def run_cmd_output(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            + " ".join(cmd)
            + "\nSTDOUT:\n"
            + proc.stdout
            + "\nSTDERR:\n"
            + proc.stderr
        )
    return proc.stdout.strip()


def ffprobe_duration_seconds(path: Path) -> Optional[float]:
    try:
        output = run_cmd_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
    except Exception:
        return None
    try:
        return float(output)
    except (TypeError, ValueError):
        return None


def parse_bitrate_to_bps(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", value or "")
    if not match:
        raise ValueError(f"Unsupported bitrate value: {value!r}")
    number = float(match.group(1))
    suffix = match.group(2).lower()
    multiplier = 1
    if suffix == "k":
        multiplier = 1000
    elif suffix == "m":
        multiplier = 1000 * 1000
    return int(number * multiplier)


def strip_dialogue_labels(text: str) -> str:
    return re.sub(r"^[^:\n]{1,80}:\s*", "", text or "", flags=re.MULTILINE)


def shift_segments(
    segments: List[Dict[str, Any]],
    offset_sec: float,
) -> List[Dict[str, Any]]:
    shifted: List[Dict[str, Any]] = []
    for segment in segments or []:
        item = dict(segment)
        for key in ("start", "end"):
            if key not in item or item[key] is None:
                continue
            try:
                item[key] = round(float(item[key]) + offset_sec, 3)
            except (TypeError, ValueError):
                pass
        shifted.append(item)
    return shifted


def prepare_audio_parts(
    input_audio: Path,
    workdir: Path,
    cfg: Config,
) -> List[Path]:
    normalized = workdir / f"{input_audio.stem}__norm.mp3"
    run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_audio),
            "-vn",
            "-ac",
            str(cfg.part_export_channels),
            "-ar",
            str(cfg.part_export_frame_rate),
            "-b:a",
            cfg.part_export_bitrate,
            str(normalized),
        ]
    )
    if normalized.stat().st_size <= cfg.split_threshold_bytes:
        return [normalized]

    segments_dir = workdir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)

    bitrate_bps = parse_bitrate_to_bps(cfg.part_export_bitrate)
    base_segment_sec = max(
        60,
        int((cfg.target_part_max_bytes * 8 / bitrate_bps) * 0.85),
    )

    parts: List[Path] = []
    for factor in (1.0, 0.9, 0.8, 0.7):
        for old_part in segments_dir.glob("part_*.mp3"):
            old_part.unlink()
        segment_time = max(60, int(base_segment_sec * factor))
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(normalized),
                "-vn",
                "-ac",
                str(cfg.part_export_channels),
                "-ar",
                str(cfg.part_export_frame_rate),
                "-b:a",
                cfg.part_export_bitrate,
                "-f",
                "segment",
                "-segment_time",
                str(segment_time),
                "-reset_timestamps",
                "1",
                str(segments_dir / "part_%03d.mp3"),
            ]
        )
        parts = sorted(segments_dir.glob("part_*.mp3"))
        if parts and all(
            part.stat().st_size <= cfg.target_part_max_bytes for part in parts
        ):
            break
    else:
        raise RuntimeError(
            f"Could not split {input_audio.name} into parts <= {cfg.target_part_max_bytes} bytes"
        )

    for part in parts:
        if part.stat().st_size > cfg.max_transcribe_bytes:
            raise RuntimeError(
                f"Audio segment exceeds API limit after split: {part.name}"
            )
    return parts


def map_speaker_name(raw_speaker: Any) -> str:
    if raw_speaker is None:
        return "Неизвестный"
    speaker = str(raw_speaker).strip()
    if not speaker:
        return "Неизвестный"
    mapping = {
        "A": "Человек 1",
        "B": "Человек 2",
        "C": "Человек 3",
        "D": "Человек 4",
        "E": "Человек 5",
    }
    return mapping.get(speaker, f"Человек {speaker}")


def build_dialogue_from_segments(segments: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for seg in segments or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        speaker = map_speaker_name(seg.get("speaker"))
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines).strip()


def parse_filename_metadata(filename: str) -> Dict[str, Optional[str]]:
    stem = Path(filename).stem
    result: Dict[str, Optional[str]] = {
        "original_audio_filename": filename,
        "audio_file_stem": stem,
        "file_date": None,
        "file_time": None,
        "file_phone": None,
        "manager_name": None,
        "call_suffix": None,
    }
    parts = stem.split("__")
    if len(parts) >= 4:
        result["file_date"] = parts[0] or None
        result["file_time"] = parts[1] or None
        result["file_phone"] = parts[2] or None
        manager_and_suffix = parts[3]
        match = re.match(r"^(?P<manager>.+?)_(?P<suffix>[^_]+)$", manager_and_suffix)
        if match:
            result["manager_name"] = match.group("manager").strip() or None
            result["call_suffix"] = match.group("suffix").strip() or None
        else:
            result["manager_name"] = manager_and_suffix.strip() or None
    return result


def build_skip_document(
    remote_file: Dict[str, Any],
    status: str,
    reason: str,
) -> Dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "stage": "done",
        "status": status,
        "error": reason,
        "source": {
            "ftp_path_audio": remote_file["path"],
            "file_name_audio": remote_file["name"],
            "file_size_bytes": remote_file["size"],
            "ftp_modify": remote_file.get("modify"),
            "file_metadata": parse_filename_metadata(remote_file["name"]),
        },
        "transcription": None,
        "analysis": {
            "skipped": True,
            "reason": reason,
            "generated_at": now_iso(),
        },
        "telegram": {
            "sent": False,
            "reason": reason,
            "updated_at": now_iso(),
        },
    }


def transcribe_part(client: OpenAI, audio_path: Path, cfg: Config) -> Dict[str, Any]:
    with audio_path.open("rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            file=audio_file,
            model=cfg.transcribe_model,
            language=cfg.transcribe_language,
            response_format="diarized_json",
            chunking_strategy=cfg.transcribe_chunking_strategy,
        )

    data = response_to_dict(transcription)
    segments = data.get("segments") or []
    full_text = str(data.get("text") or "").strip()
    dialogue_text = build_dialogue_from_segments(segments) or full_text
    duration_sec = data.get("duration")
    try:
        duration_sec = float(duration_sec) if duration_sec is not None else None
    except (TypeError, ValueError):
        duration_sec = None
    if duration_sec is None:
        duration_sec = ffprobe_duration_seconds(audio_path)

    return {
        "file_name": audio_path.name,
        "size_bytes": audio_path.stat().st_size,
        "duration_sec": duration_sec,
        "full_text": full_text,
        "dialogue_text": dialogue_text,
        "segments": segments,
        "usage": data.get("usage") or {},
    }


def build_transcript_document(
    remote_file: Dict[str, Any],
    part_results: List[Dict[str, Any]],
    cfg: Config,
) -> Dict[str, Any]:
    metadata = parse_filename_metadata(remote_file["name"])
    full_parts: List[str] = []
    all_segments: List[Dict[str, Any]] = []
    total_duration = 0.0
    total_usage: Dict[str, Any] = {}

    for item in part_results:
        full_text = item["full_text"].strip()
        shifted_segments = shift_segments(item.get("segments") or [], total_duration)
        all_segments.extend(shifted_segments)
        if full_text:
            full_parts.append(full_text)
        if item["duration_sec"]:
            total_duration += float(item["duration_sec"])
        for usage_key, usage_value in (item.get("usage") or {}).items():
            if isinstance(usage_value, (int, float)):
                total_usage[usage_key] = total_usage.get(usage_key, 0) + usage_value

    full_text_joined = "\n\n".join(full_parts).strip()
    dialogue_text_joined = build_dialogue_from_segments(all_segments) or full_text_joined
    duration_sec_total = round(total_duration, 3) if total_duration else None
    duration_min_total = (
        round(duration_sec_total / 60, 3) if duration_sec_total is not None else None
    )

    return {
        "generated_at": now_iso(),
        "stage": "transcribed",
        "source": {
            "ftp_path_audio": remote_file["path"],
            "file_name_audio": remote_file["name"],
            "file_size_bytes": remote_file["size"],
            "ftp_modify": remote_file.get("modify"),
            "file_metadata": metadata,
        },
        "transcription": {
            "model": cfg.transcribe_model,
            "language": cfg.transcribe_language,
            "duration_sec_total": duration_sec_total,
            "duration_min_total": duration_min_total,
            "word_count": count_words(strip_dialogue_labels(dialogue_text_joined)),
            "full_text": full_text_joined,
            "dialogue_text": dialogue_text_joined,
            "segments": all_segments,
            "usage": total_usage,
            "parts": part_results,
        },
        "analysis": None,
        "telegram": None,
    }


def extract_response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    data = response_to_dict(response)
    collected: List[str] = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text":
                piece = str(content.get("text") or "").strip()
                if piece:
                    collected.append(piece)
    return "\n".join(collected).strip()


def analyze_transcript(
    client: OpenAI,
    instruction_text: str,
    transcript_doc: Dict[str, Any],
    cfg: Config,
) -> str:
    analysis_payload = {
        "task": "Сформируй только готовое итоговое сообщение для Telegram-группы по этой записи разговора.",
        "rules": {
            "language": "ru",
            "return_only_message": True,
            "max_chars": 3500,
            "no_preamble": True,
            "no_code_block": True,
        },
        "transcript_json": transcript_doc,
    }
    request: Dict[str, Any] = {
        "model": cfg.analysis_model,
        "instructions": (
            instruction_text
            + "\n\nДополнительное требование: верни только готовый текст для публикации в Telegram-группу. "
            + "Без вводных слов, без пояснений вне самого сообщения, без markdown-кода."
        ),
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": json.dumps(
                            analysis_payload,
                            ensure_ascii=False,
                            indent=2,
                        ),
                    }
                ],
            }
        ],
        "max_output_tokens": cfg.analysis_max_output_tokens,
        "store": cfg.analysis_store,
    }
    if cfg.analysis_reasoning_effort:
        request["reasoning"] = {"effort": cfg.analysis_reasoning_effort}

    response = client.responses.create(**request)
    text = extract_response_text(response)
    if not text:
        raise RuntimeError("OpenAI analysis returned an empty message")
    return text


def split_text_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> List[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = rest.rfind("\n", 0, limit)
        if cut == -1:
            cut = rest.rfind(" ", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        part = rest[:cut].strip()
        if part:
            parts.append(part)
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def send_telegram_message(cfg: Config, text: str) -> List[Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    pieces = split_text_for_telegram(text)
    results: List[Dict[str, Any]] = []

    for index, piece in enumerate(pieces, start=1):
        payload: Dict[str, Any] = {
            "chat_id": cfg.telegram_chat_id,
            "text": piece if len(pieces) == 1 else f"[{index}/{len(pieces)}]\n{piece}",
            "disable_web_page_preview": True,
        }
        if cfg.telegram_message_thread_id is not None:
            payload["message_thread_id"] = cfg.telegram_message_thread_id
        response = requests.post(url, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {data}")
        results.append(data)

    return results


def process_remote_audio(
    cfg: Config,
    client: OpenAI,
    instruction_text: str,
    state: Dict[str, Any],
    remote_file: Dict[str, Any],
) -> None:
    remote_audio_path = remote_file["path"]
    remote_json_path = replace_ext(remote_audio_path, ".json")
    entry = state["files"].setdefault(remote_audio_path, {})
    entry["stage"] = "processing"
    entry["last_started_at"] = now_iso()
    entry["last_error"] = None
    entry["skip_reason"] = None
    save_state(cfg.state_path, state)

    transcript_doc: Optional[Dict[str, Any]] = None
    try:
        with tempfile.TemporaryDirectory(dir=cfg.work_root) as tmp_dir_name:
            tmp_dir = Path(tmp_dir_name)
            local_audio = tmp_dir / remote_file["name"]

            logging.info("Downloading audio: %s", remote_audio_path)
            remote_download_file(cfg, remote_audio_path, local_audio)

            logging.info("Normalizing audio: %s", local_audio.name)
            audio_parts = prepare_audio_parts(local_audio, tmp_dir, cfg)

            logging.info(
                "Transcribing %s part(s) with %s: %s",
                len(audio_parts),
                cfg.transcribe_model,
                local_audio.name,
            )
            part_results = [transcribe_part(client, part, cfg) for part in audio_parts]

            transcript_doc = build_transcript_document(remote_file, part_results, cfg)
            remote_upload_json(cfg, remote_json_path, transcript_doc)

            word_count = int(transcript_doc["transcription"].get("word_count") or 0)
            duration_min = float(
                transcript_doc["transcription"].get("duration_min_total") or 0.0
            )
            skip_reasons: List[str] = []
            if duration_min < cfg.min_duration_min:
                skip_reasons.append(
                    f"duration shorter than {cfg.min_duration_min} min"
                )
            if word_count < cfg.min_dialogue_words:
                skip_reasons.append(
                    f"dialogue shorter than {cfg.min_dialogue_words} words"
                )

            if skip_reasons:
                transcript_doc["analysis"] = {
                    "skipped": True,
                    "reason": "; ".join(skip_reasons),
                    "word_count": word_count,
                    "duration_min": duration_min,
                    "generated_at": now_iso(),
                }
                transcript_doc["telegram"] = {
                    "sent": False,
                    "reason": "analysis skipped by threshold rules",
                    "updated_at": now_iso(),
                }
                transcript_doc["stage"] = "done"
                remote_upload_json(cfg, remote_json_path, transcript_doc)

                entry["stage"] = "done"
                entry["processed_sig"] = entry.get("last_sig")
                entry["last_finished_at"] = now_iso()
                entry["skip_reason"] = "; ".join(skip_reasons)
                save_state(cfg.state_path, state)
                try:
                    remote_archive_or_delete(cfg, remote_audio_path)
                except Exception:
                    logging.exception(
                        "Could not archive/delete remote audio after analysis skip: %s",
                        remote_audio_path,
                    )
                logging.info(
                    "Skipped GPT analysis because transcript did not pass thresholds: %s",
                    remote_audio_path,
                )
                return

            logging.info(
                "Analyzing transcript with %s: %s",
                cfg.analysis_model,
                remote_audio_path,
            )
            telegram_message = analyze_transcript(
                client,
                instruction_text,
                transcript_doc,
                cfg,
            )
            transcript_doc["analysis"] = {
                "model": cfg.analysis_model,
                "telegram_message": telegram_message,
                "generated_at": now_iso(),
            }
            transcript_doc["stage"] = "analyzed"
            remote_upload_json(cfg, remote_json_path, transcript_doc)

            logging.info("Sending Telegram message: %s", remote_audio_path)
            telegram_results = send_telegram_message(cfg, telegram_message)
            transcript_doc["telegram"] = {
                "sent": True,
                "parts_sent": len(telegram_results),
                "message_ids": [
                    item.get("result", {}).get("message_id") for item in telegram_results
                ],
                "updated_at": now_iso(),
            }
            transcript_doc["stage"] = "done"
            remote_upload_json(cfg, remote_json_path, transcript_doc)

            entry["stage"] = "done"
            entry["processed_sig"] = entry.get("last_sig")
            entry["last_finished_at"] = now_iso()
            save_state(cfg.state_path, state)
            try:
                remote_archive_or_delete(cfg, remote_audio_path)
            except Exception:
                logging.exception(
                    "Could not archive/delete remote audio after success: %s",
                    remote_audio_path,
                )
            logging.info("Done: %s", remote_audio_path)
    except Exception as exc:
        logging.exception("Processing failed for %s", remote_audio_path)
        entry["stage"] = "error"
        entry["last_error"] = str(exc)
        entry["last_failed_at"] = now_iso()
        save_state(cfg.state_path, state)
        if transcript_doc is not None:
            transcript_doc["stage"] = "error"
            transcript_doc["error"] = str(exc)
            try:
                remote_upload_json(cfg, remote_json_path, transcript_doc)
            except Exception:
                logging.exception("Could not upload error JSON for %s", remote_audio_path)


def should_process_file(
    remote_file: Dict[str, Any],
    remote_files_by_path: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    cfg: Config,
) -> bool:
    remote_audio_path = remote_file["path"]
    remote_json_path = replace_ext(remote_audio_path, ".json")
    remote_json_meta = remote_files_by_path.get(remote_json_path)
    entry = state["files"].setdefault(remote_audio_path, {})

    last_sig = entry.get("last_sig")
    current_sig = f'{remote_file["size"]}:{remote_file.get("modify") or ""}'

    if last_sig == current_sig:
        stable_polls = int(entry.get("stable_polls", 0)) + 1
    else:
        stable_polls = 1

    entry["last_sig"] = current_sig
    entry["stable_polls"] = stable_polls
    entry["last_seen_at"] = now_iso()

    modify_dt = parse_ftp_modify(remote_file.get("modify"))
    if modify_dt is not None:
        entry["ftp_modify_iso"] = modify_dt.isoformat()

    if entry.get("stage") == "done" and entry.get("processed_sig") == current_sig:
        return False

    if stable_polls < cfg.min_stable_polls:
        return False

    if remote_json_meta and not entry.get("stage"):
        audio_modify = parse_ftp_modify(remote_file.get("modify"))
        json_modify = parse_ftp_modify(remote_json_meta.get("modify"))
        if json_modify is None or audio_modify is None or json_modify >= audio_modify:
            entry["stage"] = "done"
            entry["processed_sig"] = current_sig
            entry["last_finished_at"] = now_iso()
            entry["skip_reason"] = "json already exists on ftp"
            logging.info(
                "Skipping audio because JSON already exists on FTP: %s",
                remote_audio_path,
            )
            return False

    if int(remote_file["size"]) < cfg.min_audio_bytes:
        reason = f"audio file smaller than {cfg.min_audio_bytes} bytes"
        if not remote_json_meta:
            try:
                remote_upload_json(
                    cfg,
                    remote_json_path,
                    build_skip_document(
                        remote_file,
                        "skipped_too_small",
                        reason,
                    ),
                )
            except Exception:
                logging.exception(
                    "Could not upload skip JSON for small audio: %s",
                    remote_audio_path,
                )
        entry["stage"] = "done"
        entry["processed_sig"] = current_sig
        entry["last_finished_at"] = now_iso()
        entry["skip_reason"] = reason
        logging.info(
            "Skipping small audio file: %s (%s bytes)",
            remote_audio_path,
            remote_file["size"],
        )
        return False

    return True


def scan_cycle(cfg: Config, client: OpenAI, state: Dict[str, Any]) -> None:
    instruction_text = load_instruction_text(cfg.instruction_json_path)
    try:
        all_files = remote_walk(cfg)
    except RemoteConnectionError as exc:
        logging.warning("Remote scan skipped: %s", exc)
        return

    remote_files_by_path = {item["path"]: item for item in all_files}
    audio_files = [
        item
        for item in all_files
        if posixpath.splitext(item["name"])[1].lower() in AUDIO_EXTENSIONS
    ]
    audio_files.sort(key=lambda item: item["path"])

    logging.info("Cycle started. Found %s audio file(s).", len(audio_files))
    for remote_file in audio_files:
        if should_process_file(remote_file, remote_files_by_path, state, cfg):
            save_state(cfg.state_path, state)
            process_remote_audio(cfg, client, instruction_text, state, remote_file)

    save_state(cfg.state_path, state)
    logging.info("Cycle finished.")


def main() -> None:
    setup_logging()
    cfg = Config.from_env()

    cfg.work_root.mkdir(parents=True, exist_ok=True)
    ensure_parent_dir(cfg.state_path)

    if cfg.ftp_protocol not in {"ftp", "sftp"}:
        raise ValueError("FTP_PROTOCOL must be either 'ftp' or 'sftp'")
    if cfg.ftp_protocol == "ftp" and cfg.ftp_port == 22:
        raise ValueError(
            "FTP_PROTOCOL=ftp with FTP_PORT=22 is likely a misconfiguration. "
            "Use FTP_PROTOCOL=sftp for port 22, or switch FTP_PORT to your FTP/FTPS port."
        )
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required but was not found in PATH")
    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is required but was not found in PATH")
    if not cfg.instruction_json_path.exists():
        raise FileNotFoundError(
            f"instructions file not found: {cfg.instruction_json_path}"
        )

    client = OpenAI(api_key=cfg.openai_api_key)
    state = load_state(cfg.state_path)

    logging.info(
        "Daemon started. Poll interval: %s sec. Protocol: %s. Remote root: %s",
        cfg.poll_interval_sec,
        cfg.ftp_protocol,
        cfg.ftp_remote_root,
    )
    while True:
        try:
            scan_cycle(cfg, client, state)
        except KeyboardInterrupt:
            raise
        except Exception:
            logging.exception("Cycle crashed")
        time.sleep(cfg.poll_interval_sec)


if __name__ == "__main__":
    main()
