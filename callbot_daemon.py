#!/usr/bin/env python3
import io
import hashlib
import json
import logging
import mimetypes
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
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import paramiko
import psycopg
import requests
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    PermissionDeniedError,
)
from psycopg import sql
from psycopg.rows import dict_row


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
REMOTE_BACKEND_FTP = "ftp"
REMOTE_BACKEND_YANDEX_DISK = "yandex_disk"
YANDEX_DISK_API_BASE_URL = "https://cloud-api.yandex.net/v1/disk"
YANDEX_DISK_LIST_PAGE_SIZE = 1000
TELEGRAM_TEXT_LIMIT = 3900
DEFAULT_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_OPENAI_TIMEOUT_SEC = 600.0
DEFAULT_OPENAI_CONNECT_TIMEOUT_SEC = 30.0
DEFAULT_OPENAI_ROUTE_PROBE_TIMEOUT_SEC = 15.0
DEFAULT_OPENAI_ROUTE_PROBE_CONNECT_TIMEOUT_SEC = 5.0
DEFAULT_OPENAI_REQUEST_ATTEMPTS = 2
DEFAULT_OPENAI_RETRY_DELAY_SEC = 2.0
DEFAULT_OPENAI_RETRY_BACKOFF = 2.0
DEFAULT_OPENAI_PROXY_FAILURE_COOLDOWN_SEC = 300.0
DEFAULT_ANALYSIS_REASONING_EFFORT = "low"
MIN_ANALYSIS_RETRY_MAX_OUTPUT_TOKENS = 2500
MAX_ANALYSIS_RETRY_MAX_OUTPUT_TOKENS = 6000
DB_TRANSCRIPTION_TABLE = "ai_cell_trans"
DB_ANALYSIS_TABLE = "ai_cell_analisys"
DB_AUDIO_BLOB_TABLE = "ai_cell_audio_blob"
T = TypeVar("T")


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


def env_csv(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_storage_backend(backend: str = "") -> str:
    normalized = (backend or REMOTE_BACKEND_FTP).strip().lower()
    if normalized in {"yandex", "yandex_disk", "yadisk"}:
        return REMOTE_BACKEND_YANDEX_DISK
    return REMOTE_BACKEND_FTP


def normalize_remote_path(path: str) -> str:
    normalized = posixpath.normpath((path or "/").replace("\\", "/"))
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def normalize_remote_roots(paths: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for path in paths:
        normalized = normalize_remote_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result or ["/"]


def normalize_yandex_disk_path(path: str) -> str:
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return "disk:/"
    if raw.startswith("disk:"):
        raw = raw[5:]
    normalized = posixpath.normpath(raw if raw.startswith("/") else f"/{raw}")
    if normalized in {"", "."}:
        normalized = "/"
    return f"disk:{normalized}"


def normalize_yandex_disk_roots(paths: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for path in paths:
        normalized = normalize_yandex_disk_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result or ["disk:/"]


def model_supports_reasoning_effort(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("gpt-5") or normalized.startswith("o")


def default_analysis_reasoning_effort(model: str) -> str:
    if model_supports_reasoning_effort(model):
        return DEFAULT_ANALYSIS_REASONING_EFFORT
    return ""


def join_remote_path(base: str, name: str) -> str:
    base = normalize_remote_path(base)
    if base == "/":
        return normalize_remote_path(f"/{name}")
    return normalize_remote_path(posixpath.join(base.rstrip("/"), name))


def join_yandex_disk_path(base: str, name: str) -> str:
    base = normalize_yandex_disk_path(base)
    suffix = base[5:]
    if suffix == "/":
        return normalize_yandex_disk_path(f"/{name}")
    return normalize_yandex_disk_path(posixpath.join(suffix.rstrip("/"), name))


def normalize_source_path(backend: str, path: str) -> str:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return normalize_yandex_disk_path(path)
    return normalize_remote_path(path)


def normalize_source_roots(backend: str, paths: List[str]) -> List[str]:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return normalize_yandex_disk_roots(paths)
    return normalize_remote_roots(paths)


def join_source_path(backend: str, base: str, name: str) -> str:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return join_yandex_disk_path(base, name)
    return join_remote_path(base, name)


def replace_ext(remote_path: str, new_ext: str) -> str:
    base = posixpath.splitext(remote_path)[0]
    return f"{base}{new_ext}"


def source_path_is_within(backend: str, path: str, directory: str) -> bool:
    normalized_backend = normalize_storage_backend(backend)
    normalized_path = normalize_source_path(normalized_backend, path)
    normalized_directory = normalize_source_path(normalized_backend, directory)
    root_path = "disk:/" if normalized_backend == REMOTE_BACKEND_YANDEX_DISK else "/"
    if normalized_directory == root_path:
        return normalized_path.startswith(root_path)
    return normalized_path == normalized_directory or normalized_path.startswith(
        normalized_directory.rstrip("/") + "/"
    )


def source_remote_root(cfg: "Config", backend: str) -> str:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_remote_root
    return cfg.ftp_remote_root


def source_remote_roots(cfg: "Config", backend: str) -> List[str]:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_remote_roots
    return cfg.ftp_remote_roots


def source_archive_dir(cfg: "Config", backend: str) -> str:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_archive_dir
    return cfg.ftp_archive_dir


def source_archive_dir_explicit(cfg: "Config", backend: str) -> bool:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_archive_dir_explicit
    return cfg.ftp_archive_dir_explicit


def source_move_to_archive_after_success(cfg: "Config", backend: str) -> bool:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_move_to_archive_after_success
    return cfg.ftp_move_to_archive_after_success


def source_delete_after_success(cfg: "Config", backend: str) -> bool:
    if normalize_storage_backend(backend) == REMOTE_BACKEND_YANDEX_DISK:
        return cfg.yandex_disk_delete_after_success
    return cfg.ftp_delete_after_success


def resolve_remote_root_for_path(
    cfg: "Config",
    remote_path: str,
    backend: str = REMOTE_BACKEND_FTP,
) -> str:
    normalized_backend = normalize_storage_backend(backend)
    normalized_path = normalize_source_path(normalized_backend, remote_path)
    matching_roots = [
        root
        for root in source_remote_roots(cfg, normalized_backend)
        if source_path_is_within(normalized_backend, normalized_path, root)
    ]
    if not matching_roots:
        return normalize_source_path(normalized_backend, source_remote_root(cfg, normalized_backend))
    return max(matching_roots, key=len)


def resolve_archive_dir_for_path(
    cfg: "Config",
    remote_path: str,
    backend: str = REMOTE_BACKEND_FTP,
) -> str:
    normalized_backend = normalize_storage_backend(backend)
    if source_archive_dir_explicit(cfg, normalized_backend):
        return normalize_source_path(normalized_backend, source_archive_dir(cfg, normalized_backend))
    return join_source_path(
        normalized_backend,
        resolve_remote_root_for_path(cfg, remote_path, backend=normalized_backend),
        "archive",
    )


def iter_scan_archive_dirs(
    cfg: "Config",
    backend: str = REMOTE_BACKEND_FTP,
) -> List[str]:
    normalized_backend = normalize_storage_backend(backend)
    if source_archive_dir_explicit(cfg, normalized_backend):
        return [
            normalize_source_path(
                normalized_backend,
                source_archive_dir(cfg, normalized_backend),
            )
        ]
    return normalize_source_roots(
        normalized_backend,
        [
            join_source_path(normalized_backend, root, "archive")
            for root in source_remote_roots(cfg, normalized_backend)
        ],
    )


def should_skip_remote_scan_path(
    cfg: "Config",
    remote_path: str,
    backend: str = REMOTE_BACKEND_FTP,
) -> bool:
    normalized_backend = normalize_storage_backend(backend)
    if not source_move_to_archive_after_success(cfg, normalized_backend):
        return False
    remote_roots = {
        normalize_source_path(normalized_backend, root)
        for root in source_remote_roots(cfg, normalized_backend)
    }
    for archive_dir in iter_scan_archive_dirs(cfg, backend=normalized_backend):
        if archive_dir in remote_roots:
            continue
        if source_path_is_within(normalized_backend, remote_path, archive_dir):
            return True
    return False


def remote_lookup_key(backend: str, remote_path: str) -> str:
    normalized_backend = normalize_storage_backend(backend)
    normalized_path = normalize_source_path(normalized_backend, remote_path)
    if normalized_backend == REMOTE_BACKEND_FTP:
        return normalized_path
    return f"{normalized_backend}:{normalized_path}"


def remote_file_backend(remote_file: Dict[str, Any]) -> str:
    return normalize_storage_backend(str(remote_file.get("backend") or REMOTE_BACKEND_FTP))


def remote_file_lookup_key(remote_file: Dict[str, Any]) -> str:
    return remote_lookup_key(remote_file_backend(remote_file), str(remote_file["path"]))


def parse_ftp_modify(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:14], "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


def response_to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    raise TypeError(f"Unsupported response type: {type(obj)!r}")


def build_requests_proxies(proxy_url: str) -> Optional[Dict[str, str]]:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return None
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def build_openai_api_url(cfg: "Config", path: str) -> str:
    base_url = (cfg.openai_base_url or "https://api.openai.com/v1").strip().rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base_url}{suffix}"


def resolve_openai_proxy_url(
    openai_clients: "OpenAIClients",
    openai_client: OpenAI,
    cfg: "Config",
) -> str:
    if openai_clients.direct_fallback is not None and openai_client is openai_clients.direct_fallback:
        return ""
    return cfg.openai_proxy


def iter_exception_chain(exc: BaseException):
    current: Optional[BaseException] = exc
    seen = set()
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None


def build_openai_connectivity_hint(openai_proxy: str) -> str:
    if openai_proxy:
        return (
            "OpenAI traffic is routed through OPENAI_PROXY. Verify from the app VPS "
            "that the proxy itself works with curl. If curl returns "
            "`CONNECT tunnel failed` or times out, fix the proxy server, firewall, "
            "or allow-list first. The proxy must run on a separate VPS with an "
            "egress IP in a supported OpenAI region."
        )
    return (
        "Check outbound connectivity to api.openai.com, or route OpenAI traffic "
        "through OPENAI_PROXY / OPENAI_BASE_URL using a supported-region egress."
    )


def describe_processing_error(
    exc: BaseException,
    openai_proxy: str = "",
) -> Dict[str, Any]:
    message = str(exc).strip() or exc.__class__.__name__
    details: Dict[str, Any] = {
        "class": exc.__class__.__name__,
        "message": message,
        "provider": None,
        "status_code": None,
        "code": None,
        "type": None,
        "param": None,
        "hint": None,
        "retryable": True,
    }
    exception_chain = list(iter_exception_chain(exc))
    if not isinstance(exc, APIStatusError):
        if any(isinstance(item, requests.exceptions.ProxyError) for item in exception_chain):
            details["provider"] = "openai"
            details["code"] = "proxy_error"
            details["type"] = "connection_error"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            if details["message"] == exc.__class__.__name__:
                details["message"] = "OpenAI proxy rejected or could not tunnel the request"
            return details
        if any(
            isinstance(item, requests.exceptions.ConnectTimeout)
            for item in exception_chain
        ):
            details["provider"] = "openai"
            details["code"] = "connect_timeout"
            details["type"] = "timeout"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            details["message"] = (
                "Timed out while connecting to the OpenAI route"
                if openai_proxy
                else "Timed out while connecting to OpenAI"
            )
            return details
        if any(
            isinstance(item, (requests.exceptions.ReadTimeout, requests.exceptions.Timeout))
            for item in exception_chain
        ):
            details["provider"] = "openai"
            details["code"] = "timeout"
            details["type"] = "timeout"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            return details
        if any(
            isinstance(item, requests.exceptions.ConnectionError)
            for item in exception_chain
        ):
            details["provider"] = "openai"
            details["code"] = "connection_error"
            details["type"] = "connection_error"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            return details
        http_error = next(
            (
                item
                for item in exception_chain
                if isinstance(item, requests.exceptions.HTTPError)
            ),
            None,
        )
        if http_error is not None:
            details["provider"] = "openai"
            response = getattr(http_error, "response", None)
            if response is not None:
                details["status_code"] = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    error_payload = payload.get("error")
                    if isinstance(error_payload, dict):
                        provider_message = error_payload.get("message")
                        if provider_message:
                            details["message"] = str(provider_message).strip()
                        code = error_payload.get("code")
                        if code:
                            details["code"] = str(code).strip() or None
                        error_type = error_payload.get("type")
                        if error_type:
                            details["type"] = str(error_type).strip() or None
                if details["message"] == exc.__class__.__name__:
                    details["message"] = (
                        response.text.strip()
                        or f"OpenAI request failed with HTTP {response.status_code}"
                    )
            return details
        if any(isinstance(item, httpx.ProxyError) for item in exception_chain):
            details["provider"] = "openai"
            details["code"] = "proxy_error"
            details["type"] = "connection_error"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            if details["message"] == exc.__class__.__name__:
                details["message"] = "OpenAI proxy rejected or could not tunnel the request"
            return details
        if any(isinstance(item, httpx.ConnectTimeout) for item in exception_chain):
            details["provider"] = "openai"
            details["code"] = "connect_timeout"
            details["type"] = "timeout"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            details["message"] = (
                "Timed out while connecting to the OpenAI route"
                if openai_proxy
                else "Timed out while connecting to OpenAI"
            )
            return details
        if any(
            isinstance(item, (APITimeoutError, httpx.ReadTimeout, httpx.WriteTimeout))
            for item in exception_chain
        ):
            details["provider"] = "openai"
            details["code"] = "timeout"
            details["type"] = "timeout"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            return details
        if any(
            isinstance(item, (APIConnectionError, httpx.ConnectError))
            for item in exception_chain
        ):
            details["provider"] = "openai"
            details["code"] = "connection_error"
            details["type"] = "connection_error"
            details["hint"] = build_openai_connectivity_hint(openai_proxy)
            return details
        return details

    details["provider"] = "openai"
    details["status_code"] = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    param = getattr(exc, "param", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_payload = body.get("error")
        if isinstance(error_payload, dict):
            code = error_payload.get("code") or code
            param = error_payload.get("param") or param
            error_type = error_payload.get("type")
            if error_type:
                details["type"] = str(error_type).strip() or None
            provider_message = error_payload.get("message")
            if provider_message:
                details["message"] = str(provider_message).strip()

    if code:
        details["code"] = str(code).strip() or None
    if param:
        details["param"] = str(param).strip() or None

    if (
        isinstance(exc, PermissionDeniedError)
        and (details["code"] or "").lower() == "unsupported_country_region_territory"
    ):
        details["retryable"] = False
        details["hint"] = (
            "OpenAI rejected the request because this server's outbound IP is in an "
            "unsupported country, region, or territory. Move the daemon to a supported "
            "region, route OpenAI traffic through supported-region egress with "
            "OPENAI_PROXY, or set OPENAI_BASE_URL if you use a compatible gateway."
        )

    return details


def count_words(text: str) -> int:
    return len(re.findall(r"\w+", text or "", flags=re.UNICODE))


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_postgres_url(raw: str) -> str:
    normalized = (raw or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("postgresql+asyncpg://"):
        return "postgresql://" + normalized[len("postgresql+asyncpg://"):]
    if normalized.startswith("postgres+asyncpg://"):
        return "postgresql://" + normalized[len("postgres+asyncpg://"):]
    return normalized


def parse_postgres_url(raw: str) -> Dict[str, Any]:
    normalized = normalize_postgres_url(raw)
    if not normalized:
        return {}

    parsed = urlparse(normalized)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError(
            "POSTGRES_DATABASE_URL must start with postgresql://, postgres://, "
            "postgresql+asyncpg:// or postgres+asyncpg://"
        )

    query = parse_qs(parsed.query or "", keep_blank_values=True)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 5432,
        "dbname": unquote((parsed.path or "").lstrip("/")),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "sslmode": str((query.get("sslmode") or [""])[0] or "").strip(),
        "connect_timeout": safe_int((query.get("connect_timeout") or [""])[0], None),
    }


def guess_audio_mime_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


@dataclass
class Config:
    ftp_enabled: bool
    ftp_protocol: str
    ftp_host: str
    ftp_port: int
    ftp_user: str
    ftp_password: str
    ftp_encoding: str
    ftp_encoding_fallbacks: List[str]
    ftp_remote_root: str
    ftp_remote_roots: List[str]
    ftp_archive_dir: str
    ftp_archive_dir_explicit: bool
    ftp_delete_after_success: bool
    ftp_move_to_archive_after_success: bool
    ftp_use_tls: bool
    ftp_timeout_sec: int
    ftp_connect_attempts: int
    ftp_retry_delay_sec: float
    yandex_disk_enabled: bool
    yandex_disk_oauth_token: str
    yandex_disk_timeout_sec: int
    yandex_disk_remote_root: str
    yandex_disk_remote_roots: List[str]
    yandex_disk_archive_dir: str
    yandex_disk_archive_dir_explicit: bool
    yandex_disk_delete_after_success: bool
    yandex_disk_move_to_archive_after_success: bool
    openai_api_key: str
    openai_base_url: str
    openai_proxy: str
    openai_timeout_sec: float
    openai_connect_timeout_sec: float
    openai_route_probe_timeout_sec: float
    openai_route_probe_connect_timeout_sec: float
    openai_request_attempts: int
    openai_retry_delay_sec: float
    openai_retry_backoff: float
    openai_proxy_failure_cooldown_sec: float
    openai_proxy_direct_fallback: bool
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
    db_enabled: bool
    db_host: str
    db_port: int
    db_name: str
    db_admin_db: str
    db_user: str
    db_password: str
    db_sslmode: str
    db_connect_timeout_sec: int
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_message_thread_id: Optional[int]
    telegram_proxy: str
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
        ftp_host = (os.getenv("FTP_HOST") or "").strip()
        ftp_password = os.getenv("FTP_PASSWORD") or ""
        ftp_requested = bool(
            ftp_host
            or ftp_password
            or ftp_user
            or (os.getenv("FTP_REMOTE_ROOT") or "").strip()
            or env_csv("FTP_REMOTE_ROOTS")
        )
        ftp_enabled = bool(ftp_host and ftp_password and ftp_user)
        if ftp_requested and not ftp_enabled:
            raise ValueError(
                "FTP source is partially configured. Set FTP_HOST, FTP_USER/FTP_USERNAME, "
                "and FTP_PASSWORD, or clear the FTP_* variables."
            )
        if ftp_enabled:
            ftp_remote_roots = normalize_remote_roots(
                env_csv("FTP_REMOTE_ROOTS")
                or [
                    (
                        os.getenv("FTP_REMOTE_ROOT")
                        or os.getenv("FTP_REMOTE_DIR")
                        or "/"
                    ).strip()
                    or "/"
                ]
            )
            ftp_remote_root = ftp_remote_roots[0]
            ftp_archive_dir_raw = (os.getenv("FTP_ARCHIVE_DIR") or "").strip()
            ftp_archive_dir = normalize_remote_path(
                ftp_archive_dir_raw or join_remote_path(ftp_remote_root, "archive")
            )
        else:
            ftp_remote_roots = []
            ftp_remote_root = "/"
            ftp_archive_dir_raw = ""
            ftp_archive_dir = "/archive"

        yandex_disk_oauth_token = (
            os.getenv("YANDEX_DISK_OAUTH_TOKEN")
            or os.getenv("YANDEX_DISK_TOKEN")
            or ""
        ).strip()
        yandex_disk_roots_raw = env_csv("YANDEX_DISK_REMOTE_ROOTS")
        yandex_disk_root_single = (
            os.getenv("YANDEX_DISK_REMOTE_ROOT")
            or os.getenv("YANDEX_DISK_ROOT")
            or ""
        ).strip()
        yandex_disk_requested = bool(
            yandex_disk_oauth_token
            or yandex_disk_roots_raw
            or yandex_disk_root_single
            or (os.getenv("YANDEX_DISK_ARCHIVE_DIR") or "").strip()
        )
        yandex_disk_remote_roots = (
            normalize_yandex_disk_roots(
                yandex_disk_roots_raw
                or ([yandex_disk_root_single] if yandex_disk_root_single else [])
            )
            if (yandex_disk_roots_raw or yandex_disk_root_single)
            else []
        )
        yandex_disk_enabled = bool(
            yandex_disk_oauth_token and yandex_disk_remote_roots
        )
        if yandex_disk_requested and not yandex_disk_enabled:
            raise ValueError(
                "Yandex Disk source is partially configured. Set "
                "YANDEX_DISK_OAUTH_TOKEN and YANDEX_DISK_REMOTE_ROOT/ROOTS, "
                "or clear the YANDEX_DISK_* variables."
            )
        if yandex_disk_enabled:
            yandex_disk_remote_root = yandex_disk_remote_roots[0]
            yandex_disk_archive_dir_raw = (
                os.getenv("YANDEX_DISK_ARCHIVE_DIR") or ""
            ).strip()
            yandex_disk_archive_dir = normalize_yandex_disk_path(
                yandex_disk_archive_dir_raw
                or join_yandex_disk_path(yandex_disk_remote_root, "archive")
            )
        else:
            yandex_disk_remote_root = "disk:/"
            yandex_disk_archive_dir_raw = ""
            yandex_disk_archive_dir = "disk:/archive"
        transcribe_model = os.getenv(
            "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe-diarize"
        ).strip()
        analysis_model = os.getenv("OPENAI_ANALYSIS_MODEL", "gpt-5-mini").strip()
        analysis_reasoning_effort_raw = os.getenv(
            "OPENAI_ANALYSIS_REASONING_EFFORT", ""
        ).strip()
        db_url = (
            os.getenv("POSTGRES_DATABASE_URL")
            or os.getenv("DATABASE_URL")
            or ""
        ).strip()
        db_url_parts = parse_postgres_url(db_url) if db_url else {}
        db_host = (os.getenv("DB_HOST") or db_url_parts.get("host") or "").strip()
        db_name = (os.getenv("DB_NAME") or db_url_parts.get("dbname") or "").strip()
        db_user = (os.getenv("DB_USER") or db_url_parts.get("user") or "").strip()
        db_password = (
            os.getenv("DB_PASSWORD")
            if os.getenv("DB_PASSWORD") is not None
            else str(db_url_parts.get("password") or "")
        )
        db_requested = env_bool("DB_ENABLED", False) or bool(
            db_url
            or db_host
            or db_name
            or db_user
            or db_password
            or (os.getenv("DB_PORT") or "").strip()
            or (os.getenv("DB_SSLMODE") or "").strip()
            or (os.getenv("DB_ADMIN_DB") or "").strip()
        )
        if db_requested and not (db_host and db_name and db_user):
            raise ValueError(
                "PostgreSQL storage is partially configured. Set POSTGRES_DATABASE_URL "
                "or DB_HOST, DB_NAME, DB_USER and optionally DB_PASSWORD/DB_PORT/DB_SSLMODE, "
                "or disable DB_ENABLED."
            )
        ftp_port_default = "22" if ftp_protocol == "sftp" else "21"
        return cls(
            ftp_enabled=ftp_enabled,
            ftp_protocol=ftp_protocol,
            ftp_host=ftp_host,
            ftp_port=int(os.getenv("FTP_PORT", ftp_port_default)),
            ftp_user=ftp_user,
            ftp_password=ftp_password,
            ftp_encoding=os.getenv("FTP_ENCODING", "utf-8").strip() or "utf-8",
            ftp_encoding_fallbacks=env_csv(
                "FTP_ENCODING_FALLBACKS",
                "cp1251,cp866,latin-1",
            ),
            ftp_remote_root=ftp_remote_root,
            ftp_remote_roots=ftp_remote_roots,
            ftp_archive_dir=ftp_archive_dir,
            ftp_archive_dir_explicit=bool(ftp_archive_dir_raw),
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
            yandex_disk_enabled=yandex_disk_enabled,
            yandex_disk_oauth_token=yandex_disk_oauth_token,
            yandex_disk_timeout_sec=int(
                os.getenv("YANDEX_DISK_TIMEOUT_SEC", "120")
            ),
            yandex_disk_remote_root=yandex_disk_remote_root,
            yandex_disk_remote_roots=yandex_disk_remote_roots,
            yandex_disk_archive_dir=yandex_disk_archive_dir,
            yandex_disk_archive_dir_explicit=bool(yandex_disk_archive_dir_raw),
            yandex_disk_delete_after_success=env_bool(
                "YANDEX_DISK_DELETE_AFTER_SUCCESS",
                False,
            ),
            yandex_disk_move_to_archive_after_success=env_bool(
                "YANDEX_DISK_MOVE_TO_ARCHIVE_AFTER_SUCCESS",
                False,
            ),
            openai_api_key=os.environ["OPENAI_API_KEY"],
            openai_base_url=os.getenv("OPENAI_BASE_URL", "").strip(),
            openai_proxy=os.getenv("OPENAI_PROXY", "").strip(),
            openai_timeout_sec=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_TIMEOUT_SEC",
                        str(DEFAULT_OPENAI_TIMEOUT_SEC),
                    )
                ),
            ),
            openai_connect_timeout_sec=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_CONNECT_TIMEOUT_SEC",
                        str(DEFAULT_OPENAI_CONNECT_TIMEOUT_SEC),
                    )
                ),
            ),
            openai_route_probe_timeout_sec=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_ROUTE_PROBE_TIMEOUT_SEC",
                        str(DEFAULT_OPENAI_ROUTE_PROBE_TIMEOUT_SEC),
                    )
                ),
            ),
            openai_route_probe_connect_timeout_sec=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_ROUTE_PROBE_CONNECT_TIMEOUT_SEC",
                        str(DEFAULT_OPENAI_ROUTE_PROBE_CONNECT_TIMEOUT_SEC),
                    )
                ),
            ),
            openai_request_attempts=max(
                1,
                int(
                    os.getenv(
                        "OPENAI_REQUEST_ATTEMPTS",
                        str(DEFAULT_OPENAI_REQUEST_ATTEMPTS),
                    )
                ),
            ),
            openai_retry_delay_sec=max(
                0.0,
                float(
                    os.getenv(
                        "OPENAI_RETRY_DELAY_SEC",
                        str(DEFAULT_OPENAI_RETRY_DELAY_SEC),
                    )
                ),
            ),
            openai_retry_backoff=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_RETRY_BACKOFF",
                        str(DEFAULT_OPENAI_RETRY_BACKOFF),
                    )
                ),
            ),
            openai_proxy_failure_cooldown_sec=max(
                1.0,
                float(
                    os.getenv(
                        "OPENAI_PROXY_FAILURE_COOLDOWN_SEC",
                        str(DEFAULT_OPENAI_PROXY_FAILURE_COOLDOWN_SEC),
                    )
                ),
            ),
            openai_proxy_direct_fallback=env_bool(
                "OPENAI_PROXY_DIRECT_FALLBACK",
                False,
            ),
            transcribe_model=transcribe_model,
            transcribe_language=os.getenv("OPENAI_TRANSCRIBE_LANGUAGE", "ru").strip(),
            transcribe_chunking_strategy=os.getenv(
                "OPENAI_CHUNKING_STRATEGY", "auto"
            ).strip(),
            analysis_model=analysis_model,
            analysis_reasoning_effort=(
                analysis_reasoning_effort_raw
                or default_analysis_reasoning_effort(analysis_model)
            ),
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
            db_enabled=db_requested,
            db_host=db_host,
            db_port=max(
                1,
                int(os.getenv("DB_PORT", str(db_url_parts.get("port") or 5432))),
            ),
            db_name=db_name,
            db_admin_db=(os.getenv("DB_ADMIN_DB", "postgres").strip() or "postgres"),
            db_user=db_user,
            db_password=db_password,
            db_sslmode=(
                os.getenv("DB_SSLMODE")
                or str(db_url_parts.get("sslmode") or "")
                or "prefer"
            ).strip()
            or "prefer",
            db_connect_timeout_sec=max(
                1,
                int(
                    os.getenv(
                        "DB_CONNECT_TIMEOUT_SEC",
                        str(db_url_parts.get("connect_timeout") or 10),
                    )
                ),
            ),
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            telegram_message_thread_id=env_optional_int("TELEGRAM_MESSAGE_THREAD_ID"),
            telegram_proxy=os.getenv("TELEGRAM_PROXY", "").strip(),
            poll_interval_sec=int(os.getenv("POLL_INTERVAL_SEC", "60")),
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


@dataclass
class OpenAIClients:
    primary: OpenAI
    direct_fallback: Optional[OpenAI]
    proxy_enabled: bool
    proxy_failure_cooldown_sec: float
    proxy_unavailable_until: float = 0.0


def build_postgres_connect_kwargs(
    cfg: Config,
    database: Optional[str] = None,
    autocommit: bool = False,
) -> Dict[str, Any]:
    connect_kwargs: Dict[str, Any] = {
        "host": cfg.db_host,
        "port": cfg.db_port,
        "dbname": database or cfg.db_name,
        "user": cfg.db_user,
        "connect_timeout": cfg.db_connect_timeout_sec,
        "autocommit": autocommit,
        "row_factory": dict_row,
    }
    if cfg.db_password:
        connect_kwargs["password"] = cfg.db_password
    if cfg.db_sslmode:
        connect_kwargs["sslmode"] = cfg.db_sslmode
    return connect_kwargs


def ensure_postgres_database(cfg: Config) -> bool:
    if not cfg.db_enabled:
        return False

    try:
        with psycopg.connect(
            **build_postgres_connect_kwargs(
                cfg,
                database=cfg.db_admin_db,
                autocommit=True,
            )
        ) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (cfg.db_name,),
                )
                if cur.fetchone() is not None:
                    return False
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(cfg.db_name)
                    )
                )
                return True
    except psycopg.Error as exc:
        raise RuntimeError(
            "Could not ensure PostgreSQL database "
            f"'{cfg.db_name}' on {cfg.db_host}:{cfg.db_port}: {exc}"
        ) from exc


@dataclass
class DatabaseStore:
    cfg: Config

    @classmethod
    def from_config(cls, cfg: Config) -> "DatabaseStore":
        created = ensure_postgres_database(cfg)
        if created:
            logging.info(
                "Created PostgreSQL database '%s' on %s:%s.",
                cfg.db_name,
                cfg.db_host,
                cfg.db_port,
            )
        store = cls(cfg=cfg)
        store.initialize()
        return store

    def close(self) -> None:
        return None

    def _connect(self, autocommit: bool = False):
        return psycopg.connect(
            **build_postgres_connect_kwargs(
                self.cfg,
                autocommit=autocommit,
            )
        )

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {DB_TRANSCRIPTION_TABLE} (
                        id BIGSERIAL PRIMARY KEY,
                        created_datetime TIMESTAMPTZ NOT NULL,
                        updated_datetime TIMESTAMPTZ NOT NULL,
                        filename_mp3 TEXT NOT NULL,
                        source_path_audio TEXT,
                        storage_backend TEXT,
                        status TEXT,
                        transcription_json JSONB NOT NULL,
                        CONSTRAINT uq_{DB_TRANSCRIPTION_TABLE}_source
                            UNIQUE (storage_backend, source_path_audio)
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {DB_ANALYSIS_TABLE} (
                        id BIGSERIAL PRIMARY KEY,
                        created_datetime TIMESTAMPTZ NOT NULL,
                        updated_datetime TIMESTAMPTZ NOT NULL,
                        id_cell_trans BIGINT NOT NULL UNIQUE,
                        text_bot TEXT,
                        delivered_telegram BOOLEAN NOT NULL DEFAULT FALSE,
                        delivered_datetime TIMESTAMPTZ,
                        analysis_json JSONB,
                        CONSTRAINT fk_{DB_ANALYSIS_TABLE}_cell_trans
                            FOREIGN KEY (id_cell_trans)
                            REFERENCES {DB_TRANSCRIPTION_TABLE}(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {DB_AUDIO_BLOB_TABLE} (
                        id BIGSERIAL PRIMARY KEY,
                        created_datetime TIMESTAMPTZ NOT NULL,
                        updated_datetime TIMESTAMPTZ NOT NULL,
                        id_cell_trans BIGINT NOT NULL UNIQUE,
                        filename_mp3 TEXT NOT NULL,
                        mime_type TEXT,
                        size_bytes BIGINT NOT NULL,
                        sha256_hex TEXT,
                        audio_blob BYTEA NOT NULL,
                        CONSTRAINT fk_{DB_AUDIO_BLOB_TABLE}_cell_trans
                            FOREIGN KEY (id_cell_trans)
                            REFERENCES {DB_TRANSCRIPTION_TABLE}(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{DB_TRANSCRIPTION_TABLE}_filename_mp3
                    ON {DB_TRANSCRIPTION_TABLE}(filename_mp3)
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{DB_AUDIO_BLOB_TABLE}_filename_mp3
                    ON {DB_AUDIO_BLOB_TABLE}(filename_mp3)
                    """
                )

    def _save_transcription(
        self,
        cur: Any,
        transcript_doc: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        source = transcript_doc.get("source")
        if not isinstance(source, dict):
            source = {}
        created_datetime = (
            str(transcript_doc.get("generated_at") or "").strip() or now_iso()
        )
        updated_datetime = now_iso()
        filename_mp3 = str(source.get("file_name_audio") or "").strip() or "unknown.mp3"
        source_path_audio = str(
            source.get("source_path_audio") or source.get("ftp_path_audio") or ""
        ).strip()
        storage_backend = str(source.get("storage_backend") or "").strip()
        status = str(
            transcript_doc.get("status") or transcript_doc.get("stage") or ""
        ).strip()
        transcription_json = json.dumps(transcript_doc, ensure_ascii=False)

        if record_id:
            cur.execute(
                f"""
                UPDATE {DB_TRANSCRIPTION_TABLE}
                SET updated_datetime = %s,
                    filename_mp3 = %s,
                    source_path_audio = %s,
                    storage_backend = %s,
                    status = %s,
                    transcription_json = %s::jsonb
                WHERE id = %s
                RETURNING id
                """,
                (
                    updated_datetime,
                    filename_mp3,
                    source_path_audio or None,
                    storage_backend or None,
                    status or None,
                    transcription_json,
                    record_id,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                return int(row["id"])

        if source_path_audio and storage_backend:
            cur.execute(
                f"""
                INSERT INTO {DB_TRANSCRIPTION_TABLE} (
                    created_datetime,
                    updated_datetime,
                    filename_mp3,
                    source_path_audio,
                    storage_backend,
                    status,
                    transcription_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (storage_backend, source_path_audio)
                DO UPDATE SET
                    updated_datetime = EXCLUDED.updated_datetime,
                    filename_mp3 = EXCLUDED.filename_mp3,
                    status = EXCLUDED.status,
                    transcription_json = EXCLUDED.transcription_json
                RETURNING id
                """,
                (
                    created_datetime,
                    updated_datetime,
                    filename_mp3,
                    source_path_audio,
                    storage_backend,
                    status or None,
                    transcription_json,
                ),
            )
            return int(cur.fetchone()["id"])

        cur.execute(
            f"""
            INSERT INTO {DB_TRANSCRIPTION_TABLE} (
                created_datetime,
                updated_datetime,
                filename_mp3,
                source_path_audio,
                storage_backend,
                status,
                transcription_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                created_datetime,
                updated_datetime,
                filename_mp3,
                source_path_audio or None,
                storage_backend or None,
                status or None,
                transcription_json,
            ),
        )
        return int(cur.fetchone()["id"])

    def save_transcription(
        self,
        transcript_doc: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        with self._connect() as connection:
            with connection.cursor() as cur:
                return self._save_transcription(cur, transcript_doc, record_id)

    def _save_analysis(
        self,
        cur: Any,
        transcription_id: int,
        transcript_doc: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        analysis = transcript_doc.get("analysis")
        if not isinstance(analysis, dict):
            raise ValueError("analysis payload is missing or invalid")
        telegram = transcript_doc.get("telegram")
        if not isinstance(telegram, dict):
            telegram = {}

        created_datetime = str(analysis.get("generated_at") or "").strip() or now_iso()
        updated_datetime = now_iso()
        text_bot_raw = analysis.get("telegram_message")
        text_bot = (
            str(text_bot_raw).strip()
            if text_bot_raw is not None and str(text_bot_raw).strip()
            else None
        )
        delivered_telegram = telegram.get("sent") is True
        delivered_datetime = (
            str(telegram.get("updated_at") or "").strip() or updated_datetime
            if delivered_telegram
            else None
        )
        analysis_json = json.dumps(
            {
                "analysis": analysis,
                "telegram": telegram,
            },
            ensure_ascii=False,
        )

        if record_id:
            cur.execute(
                f"""
                UPDATE {DB_ANALYSIS_TABLE}
                SET updated_datetime = %s,
                    id_cell_trans = %s,
                    text_bot = %s,
                    delivered_telegram = %s,
                    delivered_datetime = %s,
                    analysis_json = %s::jsonb
                WHERE id = %s
                RETURNING id
                """,
                (
                    updated_datetime,
                    transcription_id,
                    text_bot,
                    delivered_telegram,
                    delivered_datetime,
                    analysis_json,
                    record_id,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                return int(row["id"])

        cur.execute(
            f"""
            INSERT INTO {DB_ANALYSIS_TABLE} (
                created_datetime,
                updated_datetime,
                id_cell_trans,
                text_bot,
                delivered_telegram,
                delivered_datetime,
                analysis_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id_cell_trans)
            DO UPDATE SET
                updated_datetime = EXCLUDED.updated_datetime,
                text_bot = EXCLUDED.text_bot,
                delivered_telegram = EXCLUDED.delivered_telegram,
                delivered_datetime = EXCLUDED.delivered_datetime,
                analysis_json = EXCLUDED.analysis_json
            RETURNING id
            """,
            (
                created_datetime,
                updated_datetime,
                transcription_id,
                text_bot,
                delivered_telegram,
                delivered_datetime,
                analysis_json,
            ),
        )
        return int(cur.fetchone()["id"])

    def save_analysis(
        self,
        transcription_id: int,
        transcript_doc: Dict[str, Any],
        record_id: Optional[int] = None,
    ) -> int:
        with self._connect() as connection:
            with connection.cursor() as cur:
                return self._save_analysis(
                    cur,
                    transcription_id,
                    transcript_doc,
                    record_id,
                )

    def _save_audio_blob(
        self,
        cur: Any,
        transcription_id: int,
        audio_path: Path,
        record_id: Optional[int] = None,
        filename_override: str = "",
    ) -> int:
        audio_bytes = audio_path.read_bytes()
        filename_mp3 = filename_override.strip() or audio_path.name
        mime_type = guess_audio_mime_type(filename_mp3)
        size_bytes = len(audio_bytes)
        sha256_hex = hashlib.sha256(audio_bytes).hexdigest()
        created_datetime = now_iso()
        updated_datetime = created_datetime

        if record_id:
            cur.execute(
                f"""
                UPDATE {DB_AUDIO_BLOB_TABLE}
                SET updated_datetime = %s,
                    id_cell_trans = %s,
                    filename_mp3 = %s,
                    mime_type = %s,
                    size_bytes = %s,
                    sha256_hex = %s,
                    audio_blob = %s
                WHERE id = %s
                RETURNING id
                """,
                (
                    updated_datetime,
                    transcription_id,
                    filename_mp3,
                    mime_type,
                    size_bytes,
                    sha256_hex,
                    audio_bytes,
                    record_id,
                ),
            )
            row = cur.fetchone()
            if row is not None:
                return int(row["id"])

        cur.execute(
            f"""
            INSERT INTO {DB_AUDIO_BLOB_TABLE} (
                created_datetime,
                updated_datetime,
                id_cell_trans,
                filename_mp3,
                mime_type,
                size_bytes,
                sha256_hex,
                audio_blob
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id_cell_trans)
            DO UPDATE SET
                updated_datetime = EXCLUDED.updated_datetime,
                filename_mp3 = EXCLUDED.filename_mp3,
                mime_type = EXCLUDED.mime_type,
                size_bytes = EXCLUDED.size_bytes,
                sha256_hex = EXCLUDED.sha256_hex,
                audio_blob = EXCLUDED.audio_blob
            RETURNING id
            """,
            (
                created_datetime,
                updated_datetime,
                transcription_id,
                filename_mp3,
                mime_type,
                size_bytes,
                sha256_hex,
                audio_bytes,
            ),
        )
        return int(cur.fetchone()["id"])

    def save_audio_blob(
        self,
        transcription_id: int,
        audio_path: Path,
        record_id: Optional[int] = None,
        filename_override: str = "",
    ) -> int:
        with self._connect() as connection:
            with connection.cursor() as cur:
                return self._save_audio_blob(
                    cur,
                    transcription_id,
                    audio_path,
                    record_id,
                    filename_override,
                )

    def get_audio_blob_id(self, transcription_id: int) -> Optional[int]:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"SELECT id FROM {DB_AUDIO_BLOB_TABLE} WHERE id_cell_trans = %s",
                    (transcription_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return int(row["id"])

    def sync_document(self, transcript_doc: Dict[str, Any]) -> Dict[str, Optional[int]]:
        db_info = ensure_db_storage_metadata(transcript_doc)
        with self._connect() as connection:
            with connection.cursor() as cur:
                transcription_id = self._save_transcription(
                    cur,
                    transcript_doc,
                    safe_int(db_info.get("transcription_id")),
                )
                db_info["transcription_id"] = transcription_id

                analysis = transcript_doc.get("analysis")
                if isinstance(analysis, dict):
                    analysis_id = self._save_analysis(
                        cur,
                        transcription_id,
                        transcript_doc,
                        safe_int(db_info.get("analysis_id")),
                    )
                    db_info["analysis_id"] = analysis_id
        return {
            "transcription_id": safe_int(db_info.get("transcription_id")),
            "analysis_id": safe_int(db_info.get("analysis_id")),
        }

    def sync_audio_blob(
        self,
        transcript_doc: Dict[str, Any],
        audio_path: Path,
    ) -> Dict[str, Optional[int]]:
        db_info = ensure_db_storage_metadata(transcript_doc)
        with self._connect() as connection:
            with connection.cursor() as cur:
                transcription_id = self._save_transcription(
                    cur,
                    transcript_doc,
                    safe_int(db_info.get("transcription_id")),
                )
                db_info["transcription_id"] = transcription_id
                source = transcript_doc.get("source")
                if not isinstance(source, dict):
                    source = {}
                audio_blob_id = self._save_audio_blob(
                    cur,
                    transcription_id,
                    audio_path,
                    safe_int(db_info.get("audio_blob_id")),
                    str(source.get("file_name_audio") or audio_path.name),
                )
                db_info["audio_blob_id"] = audio_blob_id
        return {
            "transcription_id": safe_int(db_info.get("transcription_id")),
            "audio_blob_id": safe_int(db_info.get("audio_blob_id")),
        }


def ensure_db_storage_metadata(transcript_doc: Dict[str, Any]) -> Dict[str, Any]:
    storage = transcript_doc.get("storage")
    if not isinstance(storage, dict):
        storage = {}
        transcript_doc["storage"] = storage
    db_info = storage.get("db")
    if not isinstance(db_info, dict):
        db_info = {}
        storage["db"] = db_info
    return db_info


def sync_transcript_doc_to_db(
    db_store: DatabaseStore,
    transcript_doc: Dict[str, Any],
) -> Dict[str, Optional[int]]:
    return db_store.sync_document(transcript_doc)


def sync_audio_blob_to_db(
    db_store: DatabaseStore,
    transcript_doc: Dict[str, Any],
    audio_path: Path,
) -> Dict[str, Optional[int]]:
    return db_store.sync_audio_blob(transcript_doc, audio_path)


def build_openai_timeout(cfg: Config) -> httpx.Timeout:
    return httpx.Timeout(
        cfg.openai_timeout_sec,
        connect=cfg.openai_connect_timeout_sec,
    )


def build_openai_route_probe_timeout(cfg: Config) -> httpx.Timeout:
    return httpx.Timeout(
        cfg.openai_route_probe_timeout_sec,
        connect=cfg.openai_route_probe_connect_timeout_sec,
    )


def build_openai_client_kwargs(
    cfg: Config,
    http_client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    client_kwargs: Dict[str, Any] = {
        "api_key": cfg.openai_api_key,
        "max_retries": 0,
    }
    if cfg.openai_base_url:
        client_kwargs["base_url"] = cfg.openai_base_url
    if http_client is not None:
        client_kwargs["http_client"] = http_client
    else:
        client_kwargs["timeout"] = build_openai_timeout(cfg)
    return client_kwargs


def build_openai_clients(cfg: Config) -> Tuple[OpenAIClients, List[httpx.Client]]:
    http_clients: List[httpx.Client] = []

    primary_http_client: Optional[httpx.Client] = None
    if cfg.openai_proxy:
        primary_http_client = build_openai_http_client(
            cfg,
            proxy_url=cfg.openai_proxy,
            trust_env=False,
        )
        http_clients.append(primary_http_client)
    primary = OpenAI(**build_openai_client_kwargs(cfg, primary_http_client))

    direct_fallback: Optional[OpenAI] = None
    if cfg.openai_proxy and cfg.openai_proxy_direct_fallback:
        direct_http_client = build_openai_http_client(cfg, trust_env=False)
        http_clients.append(direct_http_client)
        direct_fallback = OpenAI(
            **build_openai_client_kwargs(cfg, direct_http_client)
        )

    return (
        OpenAIClients(
            primary=primary,
            direct_fallback=direct_fallback,
            proxy_enabled=bool(cfg.openai_proxy),
            proxy_failure_cooldown_sec=cfg.openai_proxy_failure_cooldown_sec,
        ),
        http_clients,
    )


def is_retryable_openai_error(
    exc: BaseException,
    details: Dict[str, Any],
) -> bool:
    if details.get("provider") != "openai" or not details.get("retryable", True):
        return False
    status_code = details.get("status_code")
    if isinstance(status_code, int):
        return status_code in {408, 409, 429} or status_code >= 500
    if isinstance(exc, APIStatusError):
        return False
    return details.get("code") in {
        "proxy_error",
        "connect_timeout",
        "timeout",
        "connection_error",
    }


def is_openai_proxy_route_error(cfg: Config, details: Dict[str, Any]) -> bool:
    return bool(cfg.openai_proxy) and details.get("provider") == "openai" and details.get(
        "code"
    ) in {
        "proxy_error",
        "connect_timeout",
        "connection_error",
    }


def openai_retry_delay_sec(cfg: Config, attempt: int) -> float:
    return cfg.openai_retry_delay_sec * (cfg.openai_retry_backoff ** max(0, attempt - 1))


def openai_proxy_route_cooldown_remaining_sec(openai_clients: OpenAIClients) -> float:
    return max(0.0, openai_clients.proxy_unavailable_until - time.monotonic())


def openai_proxy_route_is_in_cooldown(openai_clients: OpenAIClients) -> bool:
    return openai_proxy_route_cooldown_remaining_sec(openai_clients) > 0.0


def pause_openai_proxy_route(
    openai_clients: OpenAIClients,
    error_details: Dict[str, Any],
) -> None:
    now = time.monotonic()
    if openai_clients.proxy_unavailable_until > now:
        return
    openai_clients.proxy_unavailable_until = max(
        openai_clients.proxy_unavailable_until,
        now + openai_clients.proxy_failure_cooldown_sec,
    )
    logging.warning(
        "OpenAI proxy route paused for %.0f sec after connectivity failure: %s",
        openai_proxy_route_cooldown_remaining_sec(openai_clients),
        error_details.get("message") or "OpenAI proxy route failure",
    )


def execute_openai_request(
    client: OpenAI,
    operation: str,
    cfg: Config,
    describe_proxy_errors_with: str,
    fn: Callable[[OpenAI], T],
) -> T:
    for attempt in range(1, cfg.openai_request_attempts + 1):
        try:
            return fn(client)
        except Exception as exc:
            error_details = describe_processing_error(
                exc,
                describe_proxy_errors_with,
            )
            if (
                not is_retryable_openai_error(exc, error_details)
                or attempt >= cfg.openai_request_attempts
            ):
                raise
            delay_sec = openai_retry_delay_sec(cfg, attempt)
            logging.warning(
                "OpenAI %s attempt %s/%s failed: %s. Retrying in %.1f sec.",
                operation,
                attempt,
                cfg.openai_request_attempts,
                error_details.get("message") or exc.__class__.__name__,
                delay_sec,
            )
            if delay_sec > 0:
                time.sleep(delay_sec)
    raise RuntimeError(f"OpenAI {operation} failed without raising an exception")


def run_openai_request(
    openai_clients: OpenAIClients,
    operation: str,
    cfg: Config,
    fn: Callable[[OpenAI], T],
) -> T:
    if openai_proxy_route_is_in_cooldown(openai_clients) and openai_clients.direct_fallback:
        return execute_openai_request(
            openai_clients.direct_fallback,
            operation,
            cfg,
            "",
            fn,
        )

    try:
        return execute_openai_request(
            openai_clients.primary,
            operation,
            cfg,
            cfg.openai_proxy,
            fn,
        )
    except Exception as exc:
        error_details = describe_processing_error(exc, cfg.openai_proxy)
        if (
            operation != "route probe"
            and is_openai_proxy_route_error(cfg, error_details)
            and openai_clients.proxy_enabled
        ):
            pause_openai_proxy_route(openai_clients, error_details)
            if openai_clients.direct_fallback:
                logging.warning(
                    "Falling back to direct OpenAI route for %s after proxy failure.",
                    operation,
                )
                return execute_openai_request(
                    openai_clients.direct_fallback,
                    operation,
                    cfg,
                    "",
                    fn,
                )
        raise


def verify_openai_route_before_processing(
    cfg: Config,
    openai_clients: OpenAIClients,
) -> bool:
    if not cfg.openai_proxy:
        return True
    if openai_proxy_route_is_in_cooldown(openai_clients):
        return openai_clients.direct_fallback is not None

    try:
        run_openai_request(
            openai_clients,
            "route probe",
            cfg,
            lambda openai_client: requests.get(
                build_openai_api_url(cfg, "/models"),
                headers={
                    "Authorization": f"Bearer {cfg.openai_api_key}",
                },
                timeout=(
                    cfg.openai_route_probe_connect_timeout_sec,
                    cfg.openai_route_probe_timeout_sec,
                ),
                proxies=build_requests_proxies(
                    resolve_openai_proxy_url(openai_clients, openai_client, cfg)
                ),
            ).raise_for_status(),
        )
        return True
    except Exception as exc:
        error_details = describe_processing_error(exc, cfg.openai_proxy)
        is_proxy_probe_failure = (
            bool(cfg.openai_proxy)
            and error_details.get("provider") == "openai"
            and error_details.get("code") in {
                "proxy_error",
                "connect_timeout",
                "connection_error",
                "timeout",
            }
        )
        if is_proxy_probe_failure and openai_clients.proxy_enabled:
            logging.warning(
                "OpenAI route probe failed before file processing: %s",
                error_details.get("message") or exc.__class__.__name__,
            )
            if error_details.get("hint"):
                logging.error("%s", error_details["hint"])
            if openai_clients.direct_fallback is not None:
                pause_openai_proxy_route(openai_clients, error_details)
                logging.warning(
                    "Continuing cycle with direct OpenAI fallback after route probe failure."
                )
            else:
                logging.warning(
                    "Continuing cycle despite route probe failure; actual OpenAI requests will retry during processing."
                )
            return True
        raise


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


def parse_json_text(raw: str, source_name: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        logging.exception("Could not parse JSON from %s", source_name)
        return None
    if not isinstance(obj, dict):
        logging.warning("JSON root is not an object in %s", source_name)
        return None
    return obj


class RemoteConnectionError(RuntimeError):
    pass


def is_retryable_remote_error(exc: Exception) -> bool:
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return isinstance(status_code, int) and (
            status_code in {408, 409, 429} or status_code >= 500
        )
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


def is_remote_permission_error(exc: Exception) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "errno", None) == 13:
        return True
    return "permission denied" in str(exc).lower()


def ftp_connect_once(cfg: Config, encoding: Optional[str] = None):
    ftp = ReusedSessionFTP_TLS() if cfg.ftp_use_tls else FTP()
    ftp.encoding = encoding or cfg.ftp_encoding
    ftp.connect(cfg.ftp_host, cfg.ftp_port, timeout=cfg.ftp_timeout_sec)
    ftp.login(cfg.ftp_user, cfg.ftp_password)
    if cfg.ftp_use_tls and isinstance(ftp, FTP_TLS):
        ftp.prot_p()
    return ftp


def ftp_connect(cfg: Config, encoding: Optional[str] = None):
    return connect_with_retry(
        cfg,
        "ftp",
        lambda: ftp_connect_once(cfg, encoding=encoding),
    )


def ftp_download_file(cfg: Config, remote_path: str, local_path: Path) -> None:
    ensure_parent_dir(local_path)
    with closing(ftp_connect(cfg)) as ftp, local_path.open("wb") as out:
        ftp.retrbinary(f"RETR {remote_path}", out.write)


def ftp_load_json(cfg: Config, remote_path: str) -> Optional[Dict[str, Any]]:
    buffer = io.BytesIO()
    try:
        with closing(ftp_connect(cfg)) as ftp:
            ftp.retrbinary(f"RETR {remote_path}", buffer.write)
    except error_perm as exc:
        if str(exc).startswith("550"):
            return None
        raise

    try:
        raw = buffer.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        logging.exception("Could not decode remote JSON as UTF-8: %s", remote_path)
        return None
    return parse_json_text(raw, remote_path)


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
    root_path = normalize_remote_path(root)

    def walk(path: str) -> None:
        try:
            entries = sftp.listdir_attr(path)
        except Exception as exc:
            if path != root_path and is_remote_permission_error(exc):
                logging.debug(
                    "Skipping unreadable SFTP directory during scan: %s (%s)",
                    path,
                    exc,
                )
                return
            raise

        for entry in entries:
            name = entry.filename
            if name in (".", ".."):
                continue
            full_path = join_remote_path(path, name)
            if should_skip_remote_scan_path(cfg, full_path):
                continue
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
                    "backend": REMOTE_BACKEND_FTP,
                    "name": name,
                    "path": full_path,
                    "size": int(entry.st_size),
                    "modify": modify,
                }
            )

    try:
        walk(root_path)
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


def sftp_load_json(cfg: Config, remote_path: str) -> Optional[Dict[str, Any]]:
    transport, sftp = sftp_connect(cfg)
    try:
        try:
            with sftp.open(remote_path, "r") as remote_file:
                raw = remote_file.read()
        except FileNotFoundError:
            return None
    finally:
        try:
            sftp.close()
        finally:
            transport.close()

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            logging.exception("Could not decode remote JSON as UTF-8: %s", remote_path)
            return None
    return parse_json_text(str(raw), remote_path)


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


def build_yandex_disk_headers(cfg: Config) -> Dict[str, str]:
    return {"Authorization": f"OAuth {cfg.yandex_disk_oauth_token}"}


def run_yandex_disk_request(
    cfg: Config,
    method: str,
    path_or_url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Any = None,
    headers: Optional[Dict[str, str]] = None,
    stream: bool = False,
    absolute_url: bool = False,
    allowed_status_codes: Optional[List[int]] = None,
) -> requests.Response:
    url = path_or_url if absolute_url else f"{YANDEX_DISK_API_BASE_URL}{path_or_url}"
    request_headers = build_yandex_disk_headers(cfg)
    if headers:
        request_headers.update(headers)
    allowed = set(allowed_status_codes or [])
    last_exc: Optional[Exception] = None

    for attempt in range(1, cfg.ftp_connect_attempts + 1):
        response: Optional[requests.Response] = None
        try:
            response = requests.request(
                method,
                url,
                headers=request_headers,
                params=params,
                data=data,
                timeout=cfg.yandex_disk_timeout_sec,
                stream=stream,
            )
            if response.status_code in allowed:
                return response
            response.raise_for_status()
            return response
        except KeyboardInterrupt:
            if response is not None:
                response.close()
            raise
        except Exception as exc:
            if response is not None:
                response.close()
            last_exc = exc
            if not is_retryable_remote_error(exc) or attempt >= cfg.ftp_connect_attempts:
                raise
            logging.warning(
                "Yandex Disk request attempt %s/%s failed: %s %s (%s). "
                "Retrying in %.1f sec.",
                attempt,
                cfg.ftp_connect_attempts,
                method.upper(),
                url,
                exc,
                cfg.ftp_retry_delay_sec,
            )
            time.sleep(cfg.ftp_retry_delay_sec)

    raise RemoteConnectionError(
        "Yandex Disk request failed without a terminal response"
    ) from last_exc


def yandex_disk_get_resource_href(
    cfg: Config,
    endpoint: str,
    remote_path: str,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    response = run_yandex_disk_request(
        cfg,
        "GET",
        endpoint,
        params={
            "path": normalize_yandex_disk_path(remote_path),
            **(extra_params or {}),
        },
        allowed_status_codes=[404],
    )
    try:
        if response.status_code == 404:
            return None
        data = response.json()
    finally:
        response.close()

    href = str(data.get("href") or "").strip() if isinstance(data, dict) else ""
    if not href:
        raise RuntimeError(
            f"Yandex Disk API did not return a transfer href for {remote_path}"
        )
    return href


def yandex_disk_ensure_dir(cfg: Config, directory: str) -> None:
    response = run_yandex_disk_request(
        cfg,
        "PUT",
        "/resources",
        params={"path": normalize_yandex_disk_path(directory)},
        allowed_status_codes=[201, 409],
    )
    response.close()


def yandex_disk_walk(cfg: Config, root: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []

    def walk(path: str) -> None:
        normalized_path = normalize_yandex_disk_path(path)
        offset = 0
        while True:
            response = run_yandex_disk_request(
                cfg,
                "GET",
                "/resources",
                params={
                    "path": normalized_path,
                    "limit": YANDEX_DISK_LIST_PAGE_SIZE,
                    "offset": offset,
                },
            )
            try:
                data = response.json()
            finally:
                response.close()

            embedded = data.get("_embedded") if isinstance(data, dict) else {}
            if not isinstance(embedded, dict):
                embedded = {}
            items = embedded.get("items") or []
            if not isinstance(items, list):
                items = []

            for item in items:
                if not isinstance(item, dict):
                    continue
                full_path = normalize_yandex_disk_path(
                    str(
                        item.get("path")
                        or join_yandex_disk_path(
                            normalized_path,
                            str(item.get("name") or ""),
                        )
                    )
                )
                if should_skip_remote_scan_path(
                    cfg,
                    full_path,
                    backend=REMOTE_BACKEND_YANDEX_DISK,
                ):
                    continue
                item_type = str(item.get("type") or "").strip().lower()
                if item_type == "dir":
                    walk(full_path)
                    continue
                if item_type != "file":
                    continue
                result.append(
                    {
                        "backend": REMOTE_BACKEND_YANDEX_DISK,
                        "name": str(item.get("name") or posixpath.basename(full_path)),
                        "path": full_path,
                        "size": int(item.get("size") or 0),
                        "modify": str(item.get("modified") or "").strip() or None,
                    }
                )

            total = int(embedded.get("total") or len(items))
            if not items:
                break
            offset += len(items)
            if offset >= total:
                break

    walk(root)
    return result


def yandex_disk_download_file(cfg: Config, remote_path: str, local_path: Path) -> None:
    href = yandex_disk_get_resource_href(cfg, "/resources/download", remote_path)
    if not href:
        raise FileNotFoundError(remote_path)
    ensure_parent_dir(local_path)
    response = run_yandex_disk_request(
        cfg,
        "GET",
        href,
        stream=True,
        absolute_url=True,
    )
    try:
        with local_path.open("wb") as out:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    out.write(chunk)
    finally:
        response.close()


def yandex_disk_load_json(cfg: Config, remote_path: str) -> Optional[Dict[str, Any]]:
    href = yandex_disk_get_resource_href(cfg, "/resources/download", remote_path)
    if not href:
        return None
    response = run_yandex_disk_request(
        cfg,
        "GET",
        href,
        absolute_url=True,
        stream=True,
    )
    try:
        raw = response.content
    finally:
        response.close()

    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        logging.exception("Could not decode remote JSON as UTF-8: %s", remote_path)
        return None
    return parse_json_text(decoded, remote_path)


def yandex_disk_upload_json(cfg: Config, remote_path: str, payload: Dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    normalized_path = normalize_yandex_disk_path(remote_path)
    yandex_disk_ensure_dir(
        cfg,
        normalize_yandex_disk_path(posixpath.dirname(normalized_path) or "disk:/"),
    )
    href = yandex_disk_get_resource_href(
        cfg,
        "/resources/upload",
        normalized_path,
        extra_params={"overwrite": "true"},
    )
    if not href:
        raise RuntimeError(f"Could not get Yandex Disk upload URL for {remote_path}")
    response = run_yandex_disk_request(
        cfg,
        "PUT",
        href,
        data=content,
        headers={"Content-Type": "application/json; charset=utf-8"},
        absolute_url=True,
        allowed_status_codes=[201, 202],
    )
    response.close()


def yandex_disk_move(cfg: Config, source_path: str, destination_path: str) -> int:
    response = run_yandex_disk_request(
        cfg,
        "POST",
        "/resources/move",
        params={
            "from": normalize_yandex_disk_path(source_path),
            "path": normalize_yandex_disk_path(destination_path),
            "overwrite": "false",
        },
        allowed_status_codes=[201, 202, 409],
    )
    try:
        return response.status_code
    finally:
        response.close()


def yandex_disk_delete(cfg: Config, remote_path: str) -> None:
    response = run_yandex_disk_request(
        cfg,
        "DELETE",
        "/resources",
        params={
            "path": normalize_yandex_disk_path(remote_path),
            "permanently": "true",
        },
        allowed_status_codes=[202, 204],
    )
    response.close()


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


def ftp_walk_mlsd(ftp, root: str, cfg: Config) -> List[Dict[str, Any]]:
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
            if should_skip_remote_scan_path(cfg, full_path):
                continue
            item_type = facts.get("type")
            if item_type == "dir":
                walk(full_path)
                continue
            if item_type != "file":
                continue
            result.append(
                {
                    "backend": REMOTE_BACKEND_FTP,
                    "name": name,
                    "path": full_path,
                    "size": int(facts.get("size") or 0),
                    "modify": facts.get("modify"),
                }
            )

    walk(root)
    return result


def ftp_walk_nlst(ftp, root: str, cfg: Config) -> List[Dict[str, Any]]:
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
            if should_skip_remote_scan_path(cfg, full_path):
                continue
            if is_remote_dir(ftp, full_path):
                walk(full_path)
                continue
            result.append(
                {
                    "backend": REMOTE_BACKEND_FTP,
                    "name": name,
                    "path": full_path,
                    "size": safe_ftp_size(ftp, full_path),
                    "modify": safe_ftp_modify(ftp, full_path),
                }
            )

    walk(root)
    return result


def ftp_walk(ftp, root: str, cfg: Config) -> List[Dict[str, Any]]:
    try:
        return ftp_walk_mlsd(ftp, root, cfg)
    except all_errors as exc:
        if not is_mlsd_unsupported(exc):
            raise
        logging.warning(
            "FTP server does not support MLSD, falling back to NLST scan: %s", exc
        )
        return ftp_walk_nlst(ftp, root, cfg)


def iter_ftp_encodings(cfg: Config) -> List[str]:
    seen = set()
    result: List[str] = []
    for encoding in [cfg.ftp_encoding, *cfg.ftp_encoding_fallbacks]:
        normalized = (encoding or "").strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def dedupe_remote_files(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_paths = set()
    result: List[Dict[str, Any]] = []
    for item in sorted(files, key=remote_file_lookup_key):
        lookup_key = remote_file_lookup_key(item)
        if lookup_key in seen_paths:
            continue
        seen_paths.add(lookup_key)
        result.append(item)
    return result


def remote_walk(cfg: Config) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    ftp_scan_succeeded = False
    yandex_scan_succeeded = False
    source_errors: List[Exception] = []

    if cfg.ftp_enabled:
        try:
            if cfg.ftp_protocol == "sftp":
                for root in cfg.ftp_remote_roots:
                    files.extend(sftp_walk(cfg, root))
                ftp_scan_succeeded = True
            else:
                decode_errors: List[UnicodeDecodeError] = []
                for encoding in iter_ftp_encodings(cfg):
                    try:
                        with closing(ftp_connect(cfg, encoding=encoding)) as ftp:
                            for root in cfg.ftp_remote_roots:
                                files.extend(ftp_walk(ftp, root, cfg))
                        ftp_scan_succeeded = True
                        if encoding.lower() != cfg.ftp_encoding.lower():
                            logging.warning(
                                "FTP listing is not valid %s; switched runtime encoding to %s. "
                                "Set FTP_ENCODING=%s to make it permanent.",
                                cfg.ftp_encoding,
                                encoding,
                                encoding,
                            )
                            cfg.ftp_encoding = encoding
                        break
                    except UnicodeDecodeError as exc:
                        decode_errors.append(exc)
                        logging.warning(
                            "FTP listing decode failed with %s: %s",
                            encoding,
                            exc,
                        )

                if decode_errors and not ftp_scan_succeeded:
                    raise decode_errors[-1]
        except Exception as exc:
            source_errors.append(exc)
            logging.warning("FTP/SFTP scan skipped: %s", exc)

    if cfg.yandex_disk_enabled:
        try:
            for root in cfg.yandex_disk_remote_roots:
                files.extend(yandex_disk_walk(cfg, root))
            yandex_scan_succeeded = True
        except Exception as exc:
            source_errors.append(exc)
            logging.warning("Yandex Disk scan skipped: %s", exc)

    if files:
        return dedupe_remote_files(files)
    if ftp_scan_succeeded or yandex_scan_succeeded:
        return []
    if source_errors:
        raise source_errors[0]
    return []


def remote_download_file(
    cfg: Config,
    remote_path: str,
    local_path: Path,
    backend: str = REMOTE_BACKEND_FTP,
) -> None:
    normalized_backend = normalize_storage_backend(backend)
    if normalized_backend == REMOTE_BACKEND_YANDEX_DISK:
        yandex_disk_download_file(cfg, remote_path, local_path)
        return
    if cfg.ftp_protocol == "sftp":
        sftp_download_file(cfg, remote_path, local_path)
        return
    ftp_download_file(cfg, remote_path, local_path)


def remote_upload_json(
    cfg: Config,
    remote_path: str,
    payload: Dict[str, Any],
    backend: str = REMOTE_BACKEND_FTP,
) -> None:
    normalized_backend = normalize_storage_backend(backend)
    if normalized_backend == REMOTE_BACKEND_YANDEX_DISK:
        yandex_disk_upload_json(cfg, remote_path, payload)
        return
    if cfg.ftp_protocol == "sftp":
        sftp_upload_json(cfg, remote_path, payload)
        return
    ftp_upload_json(cfg, remote_path, payload)


def persist_processing_document(
    cfg: Config,
    remote_path: str,
    transcript_doc: Dict[str, Any],
    backend: str = REMOTE_BACKEND_FTP,
    db_store: Optional[DatabaseStore] = None,
) -> None:
    if db_store is not None:
        sync_transcript_doc_to_db(db_store, transcript_doc)
    remote_upload_json(
        cfg,
        remote_path,
        transcript_doc,
        backend=backend,
    )


def ensure_audio_blob_persisted(
    cfg: Config,
    remote_file: Dict[str, Any],
    transcript_doc: Optional[Dict[str, Any]],
    db_store: Optional[DatabaseStore],
    backend: str = REMOTE_BACKEND_FTP,
) -> None:
    if db_store is None or not isinstance(transcript_doc, dict):
        return

    db_info = ensure_db_storage_metadata(transcript_doc)
    transcription_id = safe_int(db_info.get("transcription_id"))
    audio_blob_id = safe_int(db_info.get("audio_blob_id"))
    if transcription_id is not None and audio_blob_id is not None:
        return

    if transcription_id is None:
        sync_transcript_doc_to_db(db_store, transcript_doc)
        transcription_id = safe_int(db_info.get("transcription_id"))
        audio_blob_id = safe_int(db_info.get("audio_blob_id"))
        if transcription_id is None:
            return
        if audio_blob_id is not None:
            return

    existing_audio_blob_id = db_store.get_audio_blob_id(transcription_id)
    if existing_audio_blob_id is not None:
        db_info["audio_blob_id"] = existing_audio_blob_id
        return

    with tempfile.TemporaryDirectory(dir=cfg.work_root) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        local_audio = tmp_dir / remote_file["name"]
        logging.info(
            "Downloading audio for PostgreSQL blob sync: %s",
            remote_file["path"],
        )
        remote_download_file(
            cfg,
            remote_file["path"],
            local_audio,
            backend=backend,
        )
        sync_audio_blob_to_db(db_store, transcript_doc, local_audio)


def remote_load_json(
    cfg: Config,
    remote_path: str,
    backend: str = REMOTE_BACKEND_FTP,
) -> Optional[Dict[str, Any]]:
    normalized_backend = normalize_storage_backend(backend)
    if normalized_backend == REMOTE_BACKEND_YANDEX_DISK:
        return yandex_disk_load_json(cfg, remote_path)
    if cfg.ftp_protocol == "sftp":
        return sftp_load_json(cfg, remote_path)
    return ftp_load_json(cfg, remote_path)


def remote_archive_or_delete(
    cfg: Config,
    remote_path: str,
    backend: str = REMOTE_BACKEND_FTP,
) -> None:
    normalized_backend = normalize_storage_backend(backend)
    if source_move_to_archive_after_success(cfg, normalized_backend):
        archive_dir = resolve_archive_dir_for_path(
            cfg,
            remote_path,
            backend=normalized_backend,
        )
        destination_name = posixpath.basename(remote_path)
        destination_path = join_source_path(
            normalized_backend,
            archive_dir,
            destination_name,
        )
        if normalized_backend == REMOTE_BACKEND_YANDEX_DISK:
            yandex_disk_ensure_dir(cfg, archive_dir)
            status_code = yandex_disk_move(cfg, remote_path, destination_path)
            if status_code == 409:
                stem, suffix = posixpath.splitext(destination_name)
                destination_path = join_source_path(
                    normalized_backend,
                    archive_dir,
                    f"{stem}__{int(time.time())}{suffix}",
                )
                yandex_disk_move(cfg, remote_path, destination_path)
            return

        if cfg.ftp_protocol == "sftp":
            transport, sftp = sftp_connect(cfg)
            try:
                ensure_sftp_dir(sftp, archive_dir)
                try:
                    sftp.rename(remote_path, destination_path)
                except OSError:
                    stem, suffix = posixpath.splitext(destination_name)
                    destination_path = join_remote_path(
                        archive_dir,
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
            ensure_ftp_dir(ftp, archive_dir)
            try:
                ftp.rename(remote_path, destination_path)
            except all_errors:
                stem, suffix = posixpath.splitext(destination_name)
                destination_path = join_remote_path(
                    archive_dir,
                    f"{stem}__{int(time.time())}{suffix}",
                )
                ftp.rename(remote_path, destination_path)
        return

    if not source_delete_after_success(cfg, normalized_backend):
        return

    if normalized_backend == REMOTE_BACKEND_YANDEX_DISK:
        yandex_disk_delete(cfg, remote_path)
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


def build_audio_part_descriptors(parts: List[Path]) -> List[Dict[str, Any]]:
    descriptors: List[Dict[str, Any]] = []
    start_offset_sec = 0.0
    parts_total = len(parts)
    for index, part in enumerate(parts, start=1):
        duration_sec = ffprobe_duration_seconds(part)
        descriptors.append(
            {
                "part_index": index,
                "parts_total": parts_total,
                "part_name": part.name,
                "part_path": str(part),
                "size_bytes": part.stat().st_size,
                "planned_duration_sec": duration_sec,
                "planned_duration_min": (
                    round(duration_sec / 60, 3) if duration_sec is not None else None
                ),
                "start_offset_sec": round(start_offset_sec, 3),
            }
        )
        if duration_sec is not None:
            start_offset_sec += duration_sec
    return descriptors


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
        "counterparty_name": None,
        "call_suffix": None,
    }
    parts = stem.split("__")
    if len(parts) < 4:
        return result

    result["file_date"] = parts[0].strip() or None
    result["file_time"] = parts[1].strip() or None

    third = parts[2].strip()
    fourth = parts[3].strip()

    if re.fullmatch(r"\d{10,15}", third):
        result["file_phone"] = third
        match = re.match(r"^(.*?)(?:_(\d+))?$", fourth)
        if match:
            result["manager_name"] = (
                (match.group(1) or "").replace("_", " ").strip() or None
            )
            result["call_suffix"] = match.group(2)
        else:
            result["manager_name"] = fourth.replace("_", " ").strip() or None
        return result

    result["manager_name"] = third.replace("_", " ").strip() or None

    phone_match = re.match(r"^(\d{10,15})(?:_(\d+))?$", fourth)
    if phone_match:
        result["file_phone"] = phone_match.group(1)
        result["call_suffix"] = phone_match.group(2)
        return result

    name_match = re.match(r"^(.*?)(?:_(\d+))?$", fourth)
    if name_match:
        result["counterparty_name"] = (
            (name_match.group(1) or "").replace("_", " ").strip() or None
        )
        result["call_suffix"] = name_match.group(2)
    else:
        result["counterparty_name"] = fourth.replace("_", " ").strip() or None
    return result


def document_matches_remote_audio(
    transcript_doc: Optional[Dict[str, Any]],
    remote_audio_path: str,
    remote_backend: str = REMOTE_BACKEND_FTP,
) -> bool:
    if not isinstance(transcript_doc, dict):
        return False
    source = transcript_doc.get("source")
    if not isinstance(source, dict):
        return False
    source_backend = normalize_storage_backend(
        str(source.get("storage_backend") or REMOTE_BACKEND_FTP)
    )
    source_path = str(
        source.get("source_path_audio")
        or source.get("ftp_path_audio")
        or ""
    ).strip()
    return (
        source_backend == normalize_storage_backend(remote_backend)
        and source_path == remote_audio_path
    )


def extract_saved_transcription_doc(
    transcript_doc: Optional[Dict[str, Any]],
    remote_audio_path: str,
    remote_backend: str = REMOTE_BACKEND_FTP,
) -> Optional[Dict[str, Any]]:
    if not document_matches_remote_audio(
        transcript_doc,
        remote_audio_path,
        remote_backend=remote_backend,
    ):
        return None

    stage = str((transcript_doc or {}).get("stage") or "").strip().lower()
    if stage not in {"transcribed", "analyzed", "error"}:
        return None

    transcription = (transcript_doc or {}).get("transcription")
    if not isinstance(transcription, dict):
        return None
    if transcription.get("parts_failed") not in {None, 0}:
        return None
    parts = transcription.get("parts")
    if isinstance(parts, list) and any(
        str(item.get("status") or "ok") != "ok"
        for item in parts
        if isinstance(item, dict)
    ):
        return None
    parts_planned = transcription.get("parts_planned")
    parts_completed = transcription.get("parts_completed")
    if (
        stage == "error"
        and isinstance(parts_planned, int)
        and isinstance(parts_completed, int)
        and parts_completed < parts_planned
    ):
        return None
    if not any(
        transcription.get(key)
        for key in ("dialogue_text", "full_text", "segments", "parts")
    ):
        return None
    return transcript_doc


def classify_saved_remote_json(
    transcript_doc: Optional[Dict[str, Any]],
    remote_audio_path: str,
    remote_backend: str = REMOTE_BACKEND_FTP,
) -> str:
    if not document_matches_remote_audio(
        transcript_doc,
        remote_audio_path,
        remote_backend=remote_backend,
    ):
        return "unknown"

    stage = str((transcript_doc or {}).get("stage") or "").strip().lower()
    status = str((transcript_doc or {}).get("status") or "").strip().lower()

    if status == "skipped_too_small":
        return "completed"
    if stage == "done":
        return "completed"
    if extract_saved_telegram_message(
        transcript_doc,
        remote_audio_path,
        remote_backend=remote_backend,
    ):
        return "retry_telegram"
    if extract_saved_transcription_doc(
        transcript_doc,
        remote_audio_path,
        remote_backend=remote_backend,
    ):
        return "resume_from_transcription"
    if stage in {"processing", "transcribed", "analyzed", "error"}:
        return "retry"
    return "unknown"


def merge_usage_values(existing: Any, new: Any) -> Any:
    if isinstance(new, dict):
        merged: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        for key, value in new.items():
            merged[key] = merge_usage_values(merged.get(key), value)
        return merged

    if isinstance(new, (int, float)) and not isinstance(new, bool):
        current = (
            existing
            if isinstance(existing, (int, float)) and not isinstance(existing, bool)
            else 0
        )
        total = current + new
        if isinstance(total, float):
            return round(total, 6)
        return total

    if (existing is None or existing == "") and isinstance(new, str) and new.strip():
        return new
    return existing


def aggregate_usage_records(usage_items: List[Any]) -> Dict[str, Any]:
    aggregated: Dict[str, Any] = {}
    for usage in usage_items:
        if not isinstance(usage, dict):
            continue
        aggregated = merge_usage_values(aggregated, usage)
    return aggregated


def build_skip_document(
    remote_file: Dict[str, Any],
    status: str,
    reason: str,
) -> Dict[str, Any]:
    remote_backend = remote_file_backend(remote_file)
    return {
        "generated_at": now_iso(),
        "stage": "done",
        "status": status,
        "error": reason,
        "source": {
            "storage_backend": remote_backend,
            "source_path_audio": remote_file["path"],
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


def build_error_document(
    remote_file: Dict[str, Any],
    error_details: Dict[str, Any],
) -> Dict[str, Any]:
    remote_backend = remote_file_backend(remote_file)
    return {
        "generated_at": now_iso(),
        "stage": "error",
        "status": "error",
        "error": error_details,
        "source": {
            "storage_backend": remote_backend,
            "source_path_audio": remote_file["path"],
            "ftp_path_audio": remote_file["path"],
            "file_name_audio": remote_file["name"],
            "file_size_bytes": remote_file["size"],
            "ftp_modify": remote_file.get("modify"),
            "file_metadata": parse_filename_metadata(remote_file["name"]),
        },
        "transcription": None,
        "analysis": None,
        "telegram": {
            "sent": False,
            "reason": "processing failed",
            "updated_at": now_iso(),
        },
    }


def transcribe_part(client: OpenAIClients, audio_path: Path, cfg: Config) -> Dict[str, Any]:
    def create_transcription_via_requests(openai_client: OpenAI) -> Dict[str, Any]:
        proxy_url = resolve_openai_proxy_url(client, openai_client, cfg)
        with audio_path.open("rb") as audio_file:
            response = requests.post(
                build_openai_api_url(cfg, "/audio/transcriptions"),
                headers={
                    "Authorization": f"Bearer {cfg.openai_api_key}",
                },
                data={
                    "model": cfg.transcribe_model,
                    "language": cfg.transcribe_language,
                    "response_format": "diarized_json",
                    "chunking_strategy": cfg.transcribe_chunking_strategy,
                },
                files={
                    "file": (audio_path.name, audio_file, "audio/mpeg"),
                },
                timeout=(
                    cfg.openai_connect_timeout_sec,
                    cfg.openai_timeout_sec,
                ),
                proxies=build_requests_proxies(proxy_url),
            )
            response.raise_for_status()
            return response.json()

    transcription = run_openai_request(
        client,
        f"transcription for {audio_path.name}",
        cfg,
        create_transcription_via_requests,
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
        "raw_response": data,
    }


def build_transcript_document(
    remote_file: Dict[str, Any],
    part_results: List[Dict[str, Any]],
    cfg: Config,
    planned_parts_count: Optional[int] = None,
    split_applied: Optional[bool] = None,
) -> Dict[str, Any]:
    metadata = parse_filename_metadata(remote_file["name"])
    remote_backend = remote_file_backend(remote_file)
    full_parts: List[str] = []
    all_segments: List[Dict[str, Any]] = []
    total_duration_sec = 0.0
    first_api_sent_at = ""
    last_api_finished_at = ""
    api_elapsed_sec_total = 0.0

    successful_parts = [
        item for item in part_results if str(item.get("status") or "ok") == "ok"
    ]
    usage_items = [item.get("usage") for item in successful_parts]
    total_usage = aggregate_usage_records(usage_items)
    derived_planned_parts_count = max(
        planned_parts_count or 0,
        len(part_results),
        max(
            [
                int(item.get("parts_total") or 0)
                for item in part_results
                if isinstance(item.get("parts_total"), int)
                or str(item.get("parts_total") or "").isdigit()
            ]
            or [0]
        ),
    )
    parts_completed = len(successful_parts)
    parts_failed = len(part_results) - parts_completed

    for item in part_results:
        api_sent_at = str(item.get("api_sent_at_utc") or "").strip()
        api_finished_at = str(item.get("api_finished_at_utc") or "").strip()
        if api_sent_at and not first_api_sent_at:
            first_api_sent_at = api_sent_at
        if api_finished_at:
            last_api_finished_at = api_finished_at
        api_elapsed_sec_total += safe_float(item.get("api_elapsed_sec"), 0.0) or 0.0

        if str(item.get("status") or "ok") != "ok":
            continue

        full_text = str(item.get("full_text") or "").strip()
        offset_sec = safe_float(item.get("start_offset_sec"), None)
        if offset_sec is None:
            offset_sec = total_duration_sec
        shifted_segments = shift_segments(item.get("segments") or [], offset_sec)
        all_segments.extend(shifted_segments)
        if full_text:
            full_parts.append(full_text)

        part_duration_sec = safe_float(item.get("duration_sec"), None)
        if part_duration_sec is not None:
            total_duration_sec = max(total_duration_sec, offset_sec + part_duration_sec)

    full_text_joined = "\n\n".join(full_parts).strip()
    dialogue_text_joined = build_dialogue_from_segments(all_segments) or full_text_joined
    duration_sec_total = round(total_duration_sec, 3) if total_duration_sec else None
    duration_min_total = (
        round(duration_sec_total / 60, 3) if duration_sec_total is not None else None
    )

    return {
        "generated_at": now_iso(),
        "stage": "transcribed",
        "status": "transcribed",
        "source": {
            "storage_backend": remote_backend,
            "source_path_audio": remote_file["path"],
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
            "split_applied": (
                bool(split_applied)
                if split_applied is not None
                else derived_planned_parts_count > 1
            ),
            "parts_planned": derived_planned_parts_count,
            "parts_completed": parts_completed,
            "parts_failed": parts_failed,
            "api_started_at": first_api_sent_at or None,
            "api_finished_at": last_api_finished_at or None,
            "api_elapsed_sec_total": round(api_elapsed_sec_total, 3),
            "full_text": full_text_joined,
            "dialogue_text": dialogue_text_joined,
            "segments": all_segments,
            "usage": total_usage,
            "parts": part_results,
        },
        "analysis": None,
        "telegram": None,
    }


def extract_response_string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "value", "refusal"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
            if isinstance(nested, dict):
                text = extract_response_string(nested)
                if text:
                    return text
    return ""


def inspect_response_output(response: Any) -> Dict[str, Any]:
    data = response_to_dict(response)
    output_text = getattr(response, "output_text", None)
    collected: List[str] = []
    if isinstance(output_text, str) and output_text.strip():
        collected.append(output_text.strip())

    refusals: List[str] = []
    output_types: List[str] = []
    message_statuses: List[str] = []
    phases: List[str] = []
    for item in data.get("output") or []:
        item_type = str(item.get("type") or "").strip() or "<unknown>"
        output_types.append(item_type)
        if item_type != "message":
            continue

        item_status = str(item.get("status") or "").strip()
        if item_status:
            message_statuses.append(item_status)
        item_phase = str(item.get("phase") or "").strip()
        if item_phase:
            phases.append(item_phase)

        for content in item.get("content") or []:
            content_type = str(content.get("type") or "").strip()
            if content_type == "output_text":
                piece = extract_response_string(content.get("text"))
                if piece:
                    collected.append(piece)
            elif content_type == "refusal":
                refusal = extract_response_string(content.get("refusal"))
                if refusal:
                    refusals.append(refusal)

    incomplete_reason = ""
    incomplete_details = data.get("incomplete_details")
    if isinstance(incomplete_details, dict):
        incomplete_reason = str(incomplete_details.get("reason") or "").strip()

    unique_text = list(dict.fromkeys(piece for piece in collected if piece))
    unique_refusals = list(dict.fromkeys(piece for piece in refusals if piece))
    return {
        "text": "\n".join(unique_text).strip(),
        "refusal": "\n".join(unique_refusals).strip(),
        "response_id": str(data.get("id") or "").strip(),
        "status": str(data.get("status") or "").strip(),
        "incomplete_reason": incomplete_reason,
        "output_types": output_types,
        "message_statuses": message_statuses,
        "phases": phases,
        "usage": data.get("usage") or {},
    }


def format_analysis_response_issue(response_info: Dict[str, Any]) -> str:
    response_id = response_info.get("response_id") or "unknown"
    status = response_info.get("status") or "unknown"
    incomplete_reason = response_info.get("incomplete_reason") or "none"
    output_types = ", ".join(response_info.get("output_types") or []) or "none"
    message_statuses = (
        ", ".join(response_info.get("message_statuses") or []) or "none"
    )
    refusal = (response_info.get("refusal") or "").strip()

    if refusal:
        return (
            "OpenAI analysis returned a refusal "
            f"(response_id={response_id}, status={status}): {refusal}"
        )
    if incomplete_reason == "content_filter":
        return (
            "OpenAI analysis was blocked by the content filter "
            f"(response_id={response_id}, status={status})"
        )
    if incomplete_reason == "max_output_tokens":
        return (
            "OpenAI analysis produced no assistant text before hitting "
            "max_output_tokens "
            f"(response_id={response_id}, status={status}, "
            f"output_types={output_types}, message_statuses={message_statuses}). "
            "Increase OPENAI_ANALYSIS_MAX_OUTPUT_TOKENS or lower "
            "OPENAI_ANALYSIS_REASONING_EFFORT."
        )
    return (
        "OpenAI analysis produced no assistant text "
        f"(response_id={response_id}, status={status}, "
        f"incomplete_reason={incomplete_reason}, output_types={output_types}, "
        f"message_statuses={message_statuses})"
    )


def build_analysis_request(
    instruction_text: str,
    transcript_doc: Dict[str, Any],
    cfg: Config,
    reasoning_effort: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
) -> Dict[str, Any]:
    selected_reasoning_effort = (
        cfg.analysis_reasoning_effort
        if reasoning_effort is None
        else (reasoning_effort or "").strip()
    )
    selected_max_output_tokens = max_output_tokens or cfg.analysis_max_output_tokens

    analysis_payload = {
        "task": "РЎС„РѕСЂРјРёСЂСѓР№ С‚РѕР»СЊРєРѕ РіРѕС‚РѕРІРѕРµ РёС‚РѕРіРѕРІРѕРµ СЃРѕРѕР±С‰РµРЅРёРµ РґР»СЏ Telegram-РіСЂСѓРїРїС‹ РїРѕ СЌС‚РѕР№ Р·Р°РїРёСЃРё СЂР°Р·РіРѕРІРѕСЂР°.",
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
            + "\n\nР”РѕРїРѕР»РЅРёС‚РµР»СЊРЅРѕРµ С‚СЂРµР±РѕРІР°РЅРёРµ: РІРµСЂРЅРё С‚РѕР»СЊРєРѕ РіРѕС‚РѕРІС‹Р№ С‚РµРєСЃС‚ РґР»СЏ РїСѓР±Р»РёРєР°С†РёРё РІ Telegram-РіСЂСѓРїРїСѓ. "
            + "Р‘РµР· РІРІРѕРґРЅС‹С… СЃР»РѕРІ, Р±РµР· РїРѕСЏСЃРЅРµРЅРёР№ РІРЅРµ СЃР°РјРѕРіРѕ СЃРѕРѕР±С‰РµРЅРёСЏ, Р±РµР· markdown-РєРѕРґР°."
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
        "text": {"format": {"type": "text"}},
        "max_output_tokens": selected_max_output_tokens,
        "store": cfg.analysis_store,
    }
    if selected_reasoning_effort:
        request["reasoning"] = {"effort": selected_reasoning_effort}
    return request


def build_analysis_retry_settings(
    cfg: Config,
    response_info: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if response_info.get("text") or response_info.get("refusal"):
        return None

    incomplete_reason = (response_info.get("incomplete_reason") or "").strip()
    if incomplete_reason and incomplete_reason != "max_output_tokens":
        return None

    current_effort = (cfg.analysis_reasoning_effort or "").strip().lower()
    retry_effort = current_effort
    if model_supports_reasoning_effort(cfg.analysis_model) and current_effort not in {
        "none",
        "minimal",
        "low",
    }:
        retry_effort = DEFAULT_ANALYSIS_REASONING_EFFORT

    retry_max_output_tokens = min(
        max(
            cfg.analysis_max_output_tokens * 2,
            MIN_ANALYSIS_RETRY_MAX_OUTPUT_TOKENS,
        ),
        MAX_ANALYSIS_RETRY_MAX_OUTPUT_TOKENS,
    )
    if (
        retry_effort == current_effort
        and retry_max_output_tokens == cfg.analysis_max_output_tokens
    ):
        return None

    return {
        "reasoning_effort": retry_effort,
        "max_output_tokens": retry_max_output_tokens,
    }


def _legacy_analyze_transcript(
    client: OpenAIClients,
    instruction_text: str,
    transcript_doc: Dict[str, Any],
    cfg: Config,
) -> str:
    return analyze_transcript(client, instruction_text, transcript_doc, cfg)

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

    response = run_openai_request(
        client,
        "analysis request",
        cfg,
        lambda openai_client: openai_client.responses.create(**request),
    )
    text = extract_response_text(response)
    if not text:
        raise RuntimeError("OpenAI analysis returned an empty message")
    return text


def analyze_transcript(
    client: OpenAIClients,
    instruction_text: str,
    transcript_doc: Dict[str, Any],
    cfg: Config,
) -> str:
    request = build_analysis_request(instruction_text, transcript_doc, cfg)

    def create_analysis_response(openai_client: OpenAI, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        response = requests.post(
            build_openai_api_url(cfg, "/responses"),
            headers={
                "Authorization": f"Bearer {cfg.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
            timeout=(
                cfg.openai_connect_timeout_sec,
                cfg.openai_timeout_sec,
            ),
            proxies=build_requests_proxies(
                resolve_openai_proxy_url(client, openai_client, cfg)
            ),
        )
        response.raise_for_status()
        return response.json()

    response = run_openai_request(
        client,
        "analysis request",
        cfg,
        lambda openai_client: create_analysis_response(openai_client, request),
    )
    response_info = inspect_response_output(response)
    if response_info["text"]:
        return str(response_info["text"])

    retry_settings = build_analysis_retry_settings(cfg, response_info)
    if retry_settings is not None:
        logging.warning(
            "Analysis response had no assistant text "
            "(response_id=%s, status=%s, incomplete_reason=%s, output_types=%s). "
            "Retrying with reasoning effort=%s and max_output_tokens=%s.",
            response_info.get("response_id") or "-",
            response_info.get("status") or "-",
            response_info.get("incomplete_reason") or "-",
            ",".join(response_info.get("output_types") or []) or "-",
            retry_settings["reasoning_effort"] or "<unchanged>",
            retry_settings["max_output_tokens"],
        )
        retry_request = build_analysis_request(
            instruction_text,
            transcript_doc,
            cfg,
            reasoning_effort=retry_settings["reasoning_effort"],
            max_output_tokens=retry_settings["max_output_tokens"],
        )
        response = run_openai_request(
            client,
            "analysis retry",
            cfg,
            lambda openai_client: create_analysis_response(openai_client, retry_request),
        )
        response_info = inspect_response_output(response)
        if response_info["text"]:
            return str(response_info["text"])

    raise RuntimeError(format_analysis_response_issue(response_info))


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


def describe_telegram_failure(
    response: requests.Response,
    piece_index: int,
    pieces_total: int,
    payload: Dict[str, Any],
) -> str:
    response_text = (response.text or "").strip()
    description = ""
    parameters: Any = None
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        description = str(data.get("description") or "").strip()
        parameters = data.get("parameters")

    parts = [
        f"Telegram sendMessage failed with HTTP {response.status_code}",
        f"piece {piece_index}/{pieces_total}",
        f"text_length={len(str(payload.get('text') or ''))}",
    ]
    if payload.get("message_thread_id") is not None:
        parts.append(f"thread_id={payload['message_thread_id']}")
    message = " (" + ", ".join(parts[1:]) + ")"
    message = parts[0] + message
    if description:
        message += f": {description}"
    elif response.reason:
        message += f": {response.reason}"
    if parameters:
        message += f" | parameters={json.dumps(parameters, ensure_ascii=False)}"
    if not description and response_text:
        message += f" | body={response_text[:500]}"
    return message


def extract_saved_telegram_message(
    transcript_doc: Optional[Dict[str, Any]],
    remote_audio_path: str,
    remote_backend: str = REMOTE_BACKEND_FTP,
) -> str:
    if not document_matches_remote_audio(
        transcript_doc,
        remote_audio_path,
        remote_backend=remote_backend,
    ):
        return ""

    telegram = transcript_doc.get("telegram")
    if isinstance(telegram, dict) and telegram.get("sent") is True:
        return ""

    analysis = transcript_doc.get("analysis")
    if not isinstance(analysis, dict):
        return ""
    message = analysis.get("telegram_message")
    if not isinstance(message, str):
        return ""
    return message.strip()


def send_telegram_message(cfg: Config, text: str) -> List[Dict[str, Any]]:
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    pieces = split_text_for_telegram(text)
    results: List[Dict[str, Any]] = []
    proxies = build_requests_proxies(cfg.telegram_proxy)

    for index, piece in enumerate(pieces, start=1):
        payload: Dict[str, Any] = {
            "chat_id": cfg.telegram_chat_id,
            "text": piece if len(pieces) == 1 else f"[{index}/{len(pieces)}]\n{piece}",
            "disable_web_page_preview": True,
        }
        if cfg.telegram_message_thread_id is not None:
            payload["message_thread_id"] = cfg.telegram_message_thread_id
        response = requests.post(url, json=payload, timeout=60, proxies=proxies)
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            raise RuntimeError(
                describe_telegram_failure(response, index, len(pieces), payload)
            )
        if not isinstance(data, dict):
            raise RuntimeError(
                "Telegram sendMessage returned a non-JSON response "
                f"(piece {index}/{len(pieces)})"
            )
        if not data.get("ok"):
            raise RuntimeError(
                describe_telegram_failure(response, index, len(pieces), payload)
            )
        results.append(data)

    return results


def build_openai_http_client(
    cfg: Config,
    proxy_url: str = "",
    trust_env: bool = True,
) -> httpx.Client:
    client_kwargs: Dict[str, Any] = {
        "timeout": build_openai_timeout(cfg),
        "trust_env": trust_env,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
    return httpx.Client(**client_kwargs)


def process_remote_audio(
    cfg: Config,
    client: OpenAIClients,
    instruction_text: str,
    state: Dict[str, Any],
    remote_file: Dict[str, Any],
    db_store: Optional[DatabaseStore] = None,
) -> None:
    remote_backend = remote_file_backend(remote_file)
    remote_audio_path = remote_file["path"]
    remote_json_path = replace_ext(remote_audio_path, ".json")
    entry = state["files"].setdefault(remote_file_lookup_key(remote_file), {})
    entry["stage"] = "processing"
    entry["last_started_at"] = now_iso()
    entry["last_error"] = None
    entry["last_error_code"] = None
    entry["last_error_hint"] = None
    entry["skip_reason"] = None
    save_state(cfg.state_path, state)

    transcript_doc: Optional[Dict[str, Any]] = None
    try:
        transcript_doc = remote_load_json(cfg, remote_json_path, backend=remote_backend)
        reusable_telegram_message = extract_saved_telegram_message(
            transcript_doc,
            remote_audio_path,
            remote_backend=remote_backend,
        )
        if reusable_telegram_message:
            ensure_audio_blob_persisted(
                cfg,
                remote_file,
                transcript_doc,
                db_store,
                backend=remote_backend,
            )
            logging.info(
                "Reusing saved analysis and retrying Telegram only: %s",
                remote_audio_path,
            )
            logging.info("Sending Telegram message: %s", remote_audio_path)
            telegram_results = send_telegram_message(cfg, reusable_telegram_message)
            transcript_doc["telegram"] = {
                "sent": True,
                "parts_sent": len(telegram_results),
                "message_ids": [
                    item.get("result", {}).get("message_id") for item in telegram_results
                ],
                "updated_at": now_iso(),
            }
            transcript_doc["stage"] = "done"
            transcript_doc["status"] = "ok"
            persist_processing_document(
                cfg,
                remote_json_path,
                transcript_doc,
                backend=remote_backend,
                db_store=db_store,
            )

            entry["stage"] = "done"
            entry["processed_sig"] = entry.get("last_sig")
            entry["last_finished_at"] = now_iso()
            save_state(cfg.state_path, state)
            try:
                remote_archive_or_delete(
                    cfg,
                    remote_audio_path,
                    backend=remote_backend,
                )
            except Exception:
                logging.exception(
                    "Could not archive/delete remote audio after Telegram retry success: %s",
                    remote_audio_path,
                )
            logging.info("Done after Telegram retry: %s", remote_audio_path)
            return

        reusable_transcript_doc = extract_saved_transcription_doc(
            transcript_doc,
            remote_audio_path,
            remote_backend=remote_backend,
        )
        if reusable_transcript_doc is not None:
            transcript_doc = reusable_transcript_doc
            ensure_audio_blob_persisted(
                cfg,
                remote_file,
                transcript_doc,
                db_store,
                backend=remote_backend,
            )
            logging.info(
                "Reusing saved transcription and resuming downstream steps: %s",
                remote_audio_path,
            )
        else:
            with tempfile.TemporaryDirectory(dir=cfg.work_root) as tmp_dir_name:
                tmp_dir = Path(tmp_dir_name)
                local_audio = tmp_dir / remote_file["name"]

                logging.info("Downloading audio: %s", remote_audio_path)
                remote_download_file(
                    cfg,
                    remote_audio_path,
                    local_audio,
                    backend=remote_backend,
                )

                logging.info("Normalizing audio: %s", local_audio.name)
                audio_parts = prepare_audio_parts(local_audio, tmp_dir, cfg)
                part_descriptors = build_audio_part_descriptors(audio_parts)

                logging.info(
                    "Transcribing %s part(s) with %s: %s",
                    len(audio_parts),
                    cfg.transcribe_model,
                    local_audio.name,
                )

                part_results: List[Dict[str, Any]] = []
                for part, descriptor in zip(audio_parts, part_descriptors):
                    part_started_at = now_iso()
                    part_started_perf = time.perf_counter()
                    try:
                        part_data = transcribe_part(client, part, cfg)
                    except Exception as exc:
                        part_finished_at = now_iso()
                        part_results.append(
                            {
                                **descriptor,
                                "file_name": descriptor["part_name"],
                                "status": "error",
                                "error": str(exc).strip() or exc.__class__.__name__,
                                "api_sent_at_utc": part_started_at,
                                "api_finished_at_utc": part_finished_at,
                                "api_elapsed_sec": round(
                                    time.perf_counter() - part_started_perf,
                                    3,
                                ),
                                "duration_sec": descriptor.get("planned_duration_sec"),
                                "full_text": "",
                                "dialogue_text": "",
                                "segments": [],
                                "usage": {},
                            }
                        )
                        transcript_doc = build_transcript_document(
                            remote_file,
                            part_results,
                            cfg,
                            planned_parts_count=len(part_descriptors),
                            split_applied=len(part_descriptors) > 1,
                        )
                        if db_store is not None:
                            sync_audio_blob_to_db(db_store, transcript_doc, local_audio)
                        raise

                    part_finished_at = now_iso()
                    part_results.append(
                        {
                            **descriptor,
                            **part_data,
                            "status": "ok",
                            "error": None,
                            "api_sent_at_utc": part_started_at,
                            "api_finished_at_utc": part_finished_at,
                            "api_elapsed_sec": round(
                                time.perf_counter() - part_started_perf,
                                3,
                            ),
                        }
                    )

                transcript_doc = build_transcript_document(
                    remote_file,
                    part_results,
                    cfg,
                    planned_parts_count=len(part_descriptors),
                    split_applied=len(part_descriptors) > 1,
                )
                if db_store is not None:
                    sync_audio_blob_to_db(db_store, transcript_doc, local_audio)
            persist_processing_document(
                cfg,
                remote_json_path,
                transcript_doc,
                backend=remote_backend,
                db_store=db_store,
            )

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
            transcript_doc["status"] = "ok"
            persist_processing_document(
                cfg,
                remote_json_path,
                transcript_doc,
                backend=remote_backend,
                db_store=db_store,
            )

            entry["stage"] = "done"
            entry["processed_sig"] = entry.get("last_sig")
            entry["last_finished_at"] = now_iso()
            entry["skip_reason"] = "; ".join(skip_reasons)
            save_state(cfg.state_path, state)
            try:
                remote_archive_or_delete(
                    cfg,
                    remote_audio_path,
                    backend=remote_backend,
                )
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
        transcript_doc["status"] = "analyzed"
        persist_processing_document(
            cfg,
            remote_json_path,
            transcript_doc,
            backend=remote_backend,
            db_store=db_store,
        )

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
        transcript_doc["status"] = "ok"
        persist_processing_document(
            cfg,
            remote_json_path,
            transcript_doc,
            backend=remote_backend,
            db_store=db_store,
        )

        entry["stage"] = "done"
        entry["processed_sig"] = entry.get("last_sig")
        entry["last_finished_at"] = now_iso()
        save_state(cfg.state_path, state)
        try:
            remote_archive_or_delete(
                cfg,
                remote_audio_path,
                backend=remote_backend,
            )
        except Exception:
            logging.exception(
                "Could not archive/delete remote audio after success: %s",
                remote_audio_path,
            )
        logging.info("Done: %s", remote_audio_path)
    except Exception as exc:
        error_details = describe_processing_error(
            exc,
            ""
            if openai_proxy_route_is_in_cooldown(client) and client.direct_fallback
            else cfg.openai_proxy,
        )
        logging.exception("Processing failed for %s", remote_audio_path)
        if error_details.get("hint"):
            logging.error("%s", error_details["hint"])
        entry["stage"] = "error"
        entry["last_error"] = error_details["message"]
        entry["last_error_code"] = error_details.get("code")
        entry["last_error_hint"] = error_details.get("hint")
        entry["last_failed_at"] = now_iso()
        save_state(cfg.state_path, state)
        error_doc = transcript_doc or build_error_document(remote_file, error_details)
        error_doc["stage"] = "error"
        error_doc["status"] = "error"
        error_doc["error"] = error_details
        if error_doc.get("telegram") is None:
            error_doc["telegram"] = {
                "sent": False,
                "reason": "processing failed",
                "updated_at": now_iso(),
            }
        try:
            persist_processing_document(
                cfg,
                remote_json_path,
                error_doc,
                backend=remote_backend,
                db_store=db_store,
            )
        except Exception:
            logging.exception("Could not upload error JSON for %s", remote_audio_path)


def should_process_file(
    remote_file: Dict[str, Any],
    remote_files_by_path: Dict[str, Dict[str, Any]],
    state: Dict[str, Any],
    cfg: Config,
    db_store: Optional[DatabaseStore] = None,
) -> bool:
    remote_backend = remote_file_backend(remote_file)
    remote_audio_path = remote_file["path"]
    remote_json_path = replace_ext(remote_audio_path, ".json")
    remote_json_meta = remote_files_by_path.get(
        remote_lookup_key(remote_backend, remote_json_path)
    )
    entry = state["files"].setdefault(remote_file_lookup_key(remote_file), {})

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
        entry["skip_reason"] = entry.get("skip_reason") or "already processed"
        return False

    if stable_polls < cfg.min_stable_polls:
        return False

    if remote_json_meta and not entry.get("stage"):
        audio_modify = parse_ftp_modify(remote_file.get("modify"))
        json_modify = parse_ftp_modify(remote_json_meta.get("modify"))
        if json_modify is None or audio_modify is None or json_modify >= audio_modify:
            saved_remote_doc: Optional[Dict[str, Any]] = None
            try:
                saved_remote_doc = remote_load_json(
                    cfg,
                    remote_json_path,
                    backend=remote_backend,
                )
            except Exception:
                logging.exception(
                    "Could not inspect existing remote JSON while deciding retry/skip: %s",
                    remote_json_path,
                )

            existing_json_state = classify_saved_remote_json(
                saved_remote_doc,
                remote_audio_path,
                remote_backend=remote_backend,
            )
            if existing_json_state == "completed":
                entry["stage"] = "done"
                entry["processed_sig"] = current_sig
                entry["last_finished_at"] = now_iso()
                entry["skip_reason"] = "remote json already completed"
                logging.info(
                    "Skipping audio because remote JSON already represents a completed result: %s",
                    remote_audio_path,
                )
                return False

            if existing_json_state in {
                "retry_telegram",
                "resume_from_transcription",
                "retry",
            }:
                logging.info(
                    "Existing remote JSON is incomplete or retryable; processing again: %s",
                    remote_audio_path,
                )

    if int(remote_file["size"]) < cfg.min_audio_bytes:
        reason = f"audio file smaller than {cfg.min_audio_bytes} bytes"
        if not remote_json_meta:
            try:
                persist_processing_document(
                    cfg,
                    remote_json_path,
                    build_skip_document(
                        remote_file,
                        "skipped_too_small",
                        reason,
                    ),
                    backend=remote_backend,
                    db_store=db_store,
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


def scan_cycle(
    cfg: Config,
    client: OpenAIClients,
    state: Dict[str, Any],
    db_store: Optional[DatabaseStore] = None,
) -> None:
    instruction_text = load_instruction_text(cfg.instruction_json_path)
    try:
        all_files = remote_walk(cfg)
    except RemoteConnectionError as exc:
        logging.warning("Remote scan skipped: %s", exc)
        return

    remote_files_by_path = {remote_file_lookup_key(item): item for item in all_files}
    audio_files = [
        item
        for item in all_files
        if posixpath.splitext(item["name"])[1].lower() in AUDIO_EXTENSIONS
    ]
    audio_files.sort(key=lambda item: item["path"])

    logging.info("Cycle started. Found %s audio file(s).", len(audio_files))
    processable_files = [
        remote_file
        for remote_file in audio_files
        if should_process_file(
            remote_file,
            remote_files_by_path,
            state,
            cfg,
            db_store=db_store,
        )
    ]
    save_state(cfg.state_path, state)

    if not processable_files:
        logging.info("Cycle finished. No audio files required processing.")
        return

    logging.info(
        "Queued %s audio file(s) for processing this cycle.",
        len(processable_files),
    )
    if not verify_openai_route_before_processing(cfg, client):
        logging.warning(
            "Skipping cycle before remote downloads because the OpenAI route is unavailable."
        )
        return
    if (
        openai_proxy_route_is_in_cooldown(client)
        and client.direct_fallback is None
    ):
        logging.warning(
            "Skipping cycle because OpenAI proxy route is paused for %.0f more sec.",
            openai_proxy_route_cooldown_remaining_sec(client),
        )
        return
    for remote_file in processable_files:
        if (
            openai_proxy_route_is_in_cooldown(client)
            and client.direct_fallback is None
        ):
            logging.warning(
                "Stopping cycle early because OpenAI proxy route is paused for %.0f more sec.",
                openai_proxy_route_cooldown_remaining_sec(client),
            )
            break
        save_state(cfg.state_path, state)
        process_remote_audio(
            cfg,
            client,
            instruction_text,
            state,
            remote_file,
            db_store=db_store,
        )

    save_state(cfg.state_path, state)
    logging.info("Cycle finished.")


def main() -> None:
    setup_logging()
    cfg = Config.from_env()
    db_store: Optional[DatabaseStore] = None
    openai_http_clients: List[httpx.Client] = []

    cfg.work_root.mkdir(parents=True, exist_ok=True)
    ensure_parent_dir(cfg.state_path)
    if cfg.db_enabled:
        db_store = DatabaseStore.from_config(cfg)
        logging.info(
            "PostgreSQL storage is enabled: %s@%s:%s/%s",
            cfg.db_user,
            cfg.db_host,
            cfg.db_port,
            cfg.db_name,
        )
    else:
        logging.warning("PostgreSQL storage is disabled. Set DB_ENABLED=1 and DB_* vars.")

    if not cfg.ftp_enabled and not cfg.yandex_disk_enabled:
        raise ValueError(
            "Configure at least one remote source: FTP_* or YANDEX_DISK_*."
        )
    if cfg.ftp_enabled:
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

    if cfg.openai_base_url:
        logging.info("Using custom OpenAI base URL: %s", cfg.openai_base_url)
    if cfg.openai_proxy:
        logging.info(
            "Using dedicated OpenAI proxy from OPENAI_PROXY "
            "(connect timeout: %.1f sec, overall timeout: %.1f sec).",
            cfg.openai_connect_timeout_sec,
            cfg.openai_timeout_sec,
        )
        logging.info(
            "OpenAI proxy failures will pause the proxy route for %.0f sec.",
            cfg.openai_proxy_failure_cooldown_sec,
        )
    logging.info(
        "OpenAI request policy: %s attempt(s), retry delay %.1f sec, backoff %.1f.",
        cfg.openai_request_attempts,
        cfg.openai_retry_delay_sec,
        cfg.openai_retry_backoff,
    )
    logging.info(
        "OpenAI route probe timeout: connect %.1f sec, overall %.1f sec.",
        cfg.openai_route_probe_connect_timeout_sec,
        cfg.openai_route_probe_timeout_sec,
    )
    if cfg.openai_proxy_direct_fallback:
        logging.warning(
            "OPENAI_PROXY_DIRECT_FALLBACK is enabled. If the proxy route fails, "
            "the daemon will temporarily send OpenAI traffic directly from this VPS."
        )
    if cfg.telegram_proxy:
        logging.info("Using dedicated Telegram proxy from TELEGRAM_PROXY.")
    if cfg.ftp_enabled:
        logging.info(
            "FTP/SFTP source is enabled. Protocol: %s. Remote roots: %s",
            cfg.ftp_protocol,
            ", ".join(cfg.ftp_remote_roots),
        )
    if cfg.yandex_disk_enabled:
        logging.info(
            "Yandex Disk source is enabled. Remote roots: %s",
            ", ".join(cfg.yandex_disk_remote_roots),
        )
    try:
        client, openai_http_clients = build_openai_clients(cfg)
        state = load_state(cfg.state_path)

        logging.info(
            "Daemon started. Poll interval: %s sec. Sources: ftp=%s, yandex_disk=%s",
            cfg.poll_interval_sec,
            cfg.ftp_enabled,
            cfg.yandex_disk_enabled,
        )
        while True:
            try:
                scan_cycle(cfg, client, state, db_store=db_store)
            except KeyboardInterrupt:
                raise
            except Exception:
                logging.exception("Cycle crashed")
            time.sleep(cfg.poll_interval_sec)
    finally:
        if db_store is not None:
            db_store.close()
        for http_client in openai_http_clients:
            http_client.close()


if __name__ == "__main__":
    main()
