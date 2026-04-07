import hashlib
import hmac
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import psycopg
import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row


TELEGRAM_TEXT_LIMIT = 4096
DB_MISSED_CALLS_TABLE = "missed_calls"
DB_SET_UPDATED_AT_FUNCTION = "set_updated_at"
DB_MISSED_CALLS_UPDATED_TRIGGER = "trg_missed_calls_set_updated_at"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean-like value")


def env_csv(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_optional_int(name: str) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    return int(raw)


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_postgres_url(raw: str) -> str:
    normalized = (raw or "").strip()
    if not normalized:
        return ""
    if normalized.startswith("postgresql+asyncpg://"):
        return "postgresql://" + normalized[len("postgresql+asyncpg://") :]
    if normalized.startswith("postgres+asyncpg://"):
        return "postgresql://" + normalized[len("postgres+asyncpg://") :]
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


def build_requests_proxies(proxy_url: str) -> Optional[Dict[str, str]]:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return None
    return {
        "http": proxy_url,
        "https": proxy_url,
    }


@dataclass(frozen=True)
class MangoConfig:
    db_enabled: bool
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_sslmode: str
    db_connect_timeout_sec: int
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_message_thread_id: Optional[int]
    telegram_proxy: str
    mango_enabled: bool
    mango_http_host: str
    mango_http_port: int
    mango_webhook_path: str
    mango_api_key: str
    mango_api_salt: str
    mango_allowed_ips: List[str]
    mango_display_timezone: str
    mango_retry_enabled: bool
    mango_retry_interval_sec: int
    mango_retry_batch_size: int
    mango_log_payloads: bool
    log_level: str

    @classmethod
    def from_env(cls, env_path: Optional[Path] = None) -> "MangoConfig":
        load_dotenv(dotenv_path=env_path)

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
        )
        if not db_requested or not (db_host and db_name and db_user):
            raise ValueError(
                "MANGO webhook requires PostgreSQL. Set DB_ENABLED=1 and DB_* "
                "or POSTGRES_DATABASE_URL."
            )

        webhook_path = (
            os.getenv("MANGO_WEBHOOK_PATH", "/events/summary").strip()
            or "/events/summary"
        )
        if not webhook_path.startswith("/"):
            raise ValueError("MANGO_WEBHOOK_PATH must start with '/'")

        mango_api_key = (os.getenv("MANGO_API_KEY") or "").strip()
        mango_api_salt = (os.getenv("MANGO_API_SALT") or "").strip()
        if not mango_api_key:
            raise ValueError("Set MANGO_API_KEY for the webhook service.")
        if not mango_api_salt:
            raise ValueError("Set MANGO_API_SALT for the webhook service.")

        return cls(
            db_enabled=db_requested,
            db_host=db_host,
            db_port=max(
                1,
                int(os.getenv("DB_PORT", str(db_url_parts.get("port") or 5432))),
            ),
            db_name=db_name,
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
            telegram_proxy=(os.getenv("TELEGRAM_PROXY") or "").strip(),
            mango_enabled=env_bool("MANGO_ENABLED", True),
            mango_http_host=(os.getenv("MANGO_HTTP_HOST") or "127.0.0.1").strip()
            or "127.0.0.1",
            mango_http_port=max(1, int(os.getenv("MANGO_HTTP_PORT", "8081"))),
            mango_webhook_path=webhook_path.rstrip("/") or "/events/summary",
            mango_api_key=mango_api_key,
            mango_api_salt=mango_api_salt,
            mango_allowed_ips=env_csv("MANGO_ALLOWED_IPS", ""),
            mango_display_timezone=(
                os.getenv("MANGO_DISPLAY_TIMEZONE") or "Asia/Novosibirsk"
            ).strip()
            or "Asia/Novosibirsk",
            mango_retry_enabled=env_bool("MANGO_RETRY_ENABLED", True),
            mango_retry_interval_sec=max(
                1, int(os.getenv("MANGO_RETRY_INTERVAL_SEC", "60"))
            ),
            mango_retry_batch_size=max(
                1, int(os.getenv("MANGO_RETRY_BATCH_SIZE", "20"))
            ),
            mango_log_payloads=env_bool("MANGO_LOG_PAYLOADS", False),
            log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper() or "INFO",
        )


def build_postgres_connect_kwargs(
    cfg: MangoConfig,
    autocommit: bool = False,
) -> Dict[str, Any]:
    connect_kwargs: Dict[str, Any] = {
        "host": cfg.db_host,
        "port": cfg.db_port,
        "dbname": cfg.db_name,
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


class MangoStore:
    def __init__(self, cfg: MangoConfig) -> None:
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg: MangoConfig) -> "MangoStore":
        store = cls(cfg)
        store.initialize()
        return store

    def close(self) -> None:
        return None

    def _connect(self, autocommit: bool = False):
        return psycopg.connect(
            **build_postgres_connect_kwargs(self.cfg, autocommit=autocommit)
        )

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE OR REPLACE FUNCTION {DB_SET_UPDATED_AT_FUNCTION}()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        NEW.updated_at = NOW();
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {DB_MISSED_CALLS_TABLE} (
                        id BIGSERIAL PRIMARY KEY,
                        entry_id TEXT NOT NULL UNIQUE,
                        from_number TEXT,
                        to_extension TEXT,
                        to_number TEXT,
                        line_number TEXT,
                        create_time TIMESTAMPTZ,
                        end_time TIMESTAMPTZ,
                        call_direction INTEGER,
                        entry_result INTEGER,
                        disconnect_reason TEXT,
                        telegram_message_id BIGINT,
                        telegram_sent BOOLEAN NOT NULL DEFAULT FALSE,
                        telegram_sent_at TIMESTAMPTZ,
                        telegram_error TEXT,
                        telegram_retry_count INTEGER NOT NULL DEFAULT 0,
                        last_telegram_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        raw_payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    f"""
                    DROP TRIGGER IF EXISTS {DB_MISSED_CALLS_UPDATED_TRIGGER}
                    ON {DB_MISSED_CALLS_TABLE}
                    """
                )
                cur.execute(
                    f"""
                    CREATE TRIGGER {DB_MISSED_CALLS_UPDATED_TRIGGER}
                    BEFORE UPDATE ON {DB_MISSED_CALLS_TABLE}
                    FOR EACH ROW
                    EXECUTE FUNCTION {DB_SET_UPDATED_AT_FUNCTION}()
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{DB_MISSED_CALLS_TABLE}_create_time
                    ON {DB_MISSED_CALLS_TABLE}(create_time)
                    """
                )
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{DB_MISSED_CALLS_TABLE}_pending_retry
                    ON {DB_MISSED_CALLS_TABLE}(telegram_sent, created_at)
                    """
                )

    def insert_missed_call(self, record: Dict[str, Any]) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {DB_MISSED_CALLS_TABLE} (
                        entry_id,
                        from_number,
                        to_extension,
                        to_number,
                        line_number,
                        create_time,
                        end_time,
                        call_direction,
                        entry_result,
                        disconnect_reason,
                        raw_payload
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s::jsonb
                    )
                    ON CONFLICT (entry_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        record["entry_id"],
                        record.get("from_number"),
                        record.get("to_extension"),
                        record.get("to_number"),
                        record.get("line_number"),
                        record.get("create_time"),
                        record.get("end_time"),
                        record.get("call_direction"),
                        record.get("entry_result"),
                        record.get("disconnect_reason"),
                        json.dumps(record.get("raw_payload") or {}, ensure_ascii=False),
                    ),
                )
                row = cur.fetchone()
        return row is not None

    def mark_telegram_sent(
        self,
        entry_id: str,
        telegram_message_id: Optional[int],
    ) -> None:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {DB_MISSED_CALLS_TABLE}
                    SET telegram_sent = TRUE,
                        telegram_message_id = %s,
                        telegram_sent_at = NOW(),
                        telegram_error = NULL,
                        last_telegram_attempt_at = NOW()
                    WHERE entry_id = %s
                    """,
                    (telegram_message_id, entry_id),
                )

    def mark_telegram_failed(self, entry_id: str, error_text: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE {DB_MISSED_CALLS_TABLE}
                    SET telegram_sent = FALSE,
                        telegram_error = %s,
                        telegram_retry_count = telegram_retry_count + 1,
                        last_telegram_attempt_at = NOW()
                    WHERE entry_id = %s
                    """,
                    (safe_str(error_text), entry_id),
                )

    def list_pending_retries(
        self,
        retry_interval_sec: int,
        batch_size: int,
    ) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        entry_id,
                        from_number,
                        to_extension,
                        to_number,
                        line_number,
                        create_time,
                        end_time,
                        call_direction,
                        entry_result,
                        disconnect_reason,
                        raw_payload,
                        telegram_retry_count,
                        telegram_error,
                        last_telegram_attempt_at
                    FROM {DB_MISSED_CALLS_TABLE}
                    WHERE telegram_sent = FALSE
                      AND last_telegram_attempt_at <= NOW() - (%s * INTERVAL '1 second')
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (retry_interval_sec, batch_size),
                )
                rows = cur.fetchall() or []
        return [dict(row) for row in rows]

    def get_missed_call(self, entry_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM {DB_MISSED_CALLS_TABLE}
                    WHERE entry_id = %s
                    """,
                    (entry_id,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return dict(row)


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
        description = safe_str(data.get("description"))
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


def send_telegram_message(cfg: MangoConfig, text: str) -> List[Dict[str, Any]]:
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


def extract_telegram_message_id(results: List[Dict[str, Any]]) -> Optional[int]:
    if not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    result = first.get("result")
    if not isinstance(result, dict):
        return None
    return safe_int(result.get("message_id"))


def build_mango_sign(api_key: str, json_str: str, api_salt: str) -> str:
    payload = f"{api_key}{json_str}{api_salt}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_mango_sign(api_key: str, json_str: str, api_salt: str, sign: str) -> bool:
    expected = build_mango_sign(api_key, json_str, api_salt)
    return bool(sign) and hmac.compare_digest(expected, sign.strip().lower())


def parse_mango_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    raw = safe_str(value)
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def format_display_datetime(value: Any, tz_name: str) -> str:
    parsed = parse_mango_datetime(value)
    if parsed is None:
        return "не указано"
    tz = ZoneInfo(tz_name)
    if parsed.tzinfo is None:
        localized = parsed.replace(tzinfo=tz)
    else:
        localized = parsed.astimezone(tz)
    return localized.strftime("%d.%m.%Y %H:%M:%S")


def normalize_to_target(to_value: Any) -> Dict[str, Any]:
    if isinstance(to_value, dict):
        return to_value
    if isinstance(to_value, list):
        for item in to_value:
            if isinstance(item, dict):
                return item
    return {}


def build_called_party_display(
    to_extension: str,
    to_number: str,
    line_number: str,
) -> str:
    if to_extension and to_number:
        return f"{to_extension} / {to_number}"
    if to_number:
        return to_number
    if to_extension:
        return to_extension
    if line_number:
        return line_number
    return "не указан"


def normalize_missed_call_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    entry_id = safe_str(payload.get("entry_id") or payload.get("entryId"))
    if not entry_id:
        return None

    from_block = payload.get("from")
    if not isinstance(from_block, dict):
        from_block = {}
    to_block = normalize_to_target(payload.get("to"))

    return {
        "entry_id": entry_id,
        "from_number": safe_str(from_block.get("number")),
        "to_extension": safe_str(to_block.get("extension")),
        "to_number": safe_str(to_block.get("number")),
        "line_number": safe_str(payload.get("line_number")),
        "create_time": parse_mango_datetime(payload.get("create_time")),
        "end_time": parse_mango_datetime(payload.get("end_time")),
        "call_direction": safe_int(payload.get("call_direction")),
        "entry_result": safe_int(payload.get("entry_result")),
        "disconnect_reason": safe_str(payload.get("disconnect_reason")),
        "raw_payload": payload,
    }


def is_missed_inbound(record: Dict[str, Any]) -> bool:
    return (
        safe_int(record.get("call_direction")) == 1
        and safe_int(record.get("entry_result")) == 0
    )


def build_missed_call_message(cfg: MangoConfig, record: Dict[str, Any]) -> str:
    return "\n".join(
        [
            "‼️‼️‼️ ПРОПУЩЕННЫЙ ЗВОНОК",
            f"Дата и время: {format_display_datetime(record.get('create_time'), cfg.mango_display_timezone)}",
            f"Входящий (пропущенный) номер: {safe_str(record.get('from_number')) or 'не указан'}",
            "Вызываемый абонент: "
            + build_called_party_display(
                safe_str(record.get("to_extension")),
                safe_str(record.get("to_number")),
                safe_str(record.get("line_number")),
            ),
        ]
    )


@dataclass(frozen=True)
class ProcessResult:
    status: str
    entry_id: str = ""
    telegram_message_id: Optional[int] = None
    error: str = ""


def process_summary_event(
    cfg: MangoConfig,
    store: Any,
    payload: Dict[str, Any],
    telegram_sender: Callable[[MangoConfig, str], List[Dict[str, Any]]] = send_telegram_message,
) -> ProcessResult:
    record = normalize_missed_call_payload(payload)
    if record is None:
        logging.warning("MANGO payload is incomplete: entry_id is missing.")
        return ProcessResult(status="invalid_payload")

    if not is_missed_inbound(record):
        return ProcessResult(status="skipped", entry_id=record["entry_id"])

    inserted = store.insert_missed_call(record)
    if not inserted:
        return ProcessResult(status="duplicate", entry_id=record["entry_id"])

    message_text = build_missed_call_message(cfg, record)
    try:
        results = telegram_sender(cfg, message_text)
        message_id = extract_telegram_message_id(results)
        store.mark_telegram_sent(record["entry_id"], message_id)
        return ProcessResult(
            status="sent",
            entry_id=record["entry_id"],
            telegram_message_id=message_id,
        )
    except Exception as exc:
        logging.exception(
            "Could not send Telegram notification for MANGO missed call entry_id=%s",
            record["entry_id"],
        )
        store.mark_telegram_failed(record["entry_id"], str(exc))
        return ProcessResult(
            status="telegram_failed",
            entry_id=record["entry_id"],
            error=str(exc),
        )


def retry_pending_notifications(
    cfg: MangoConfig,
    store: Any,
    telegram_sender: Callable[[MangoConfig, str], List[Dict[str, Any]]] = send_telegram_message,
) -> int:
    rows = store.list_pending_retries(
        retry_interval_sec=cfg.mango_retry_interval_sec,
        batch_size=cfg.mango_retry_batch_size,
    )
    sent_count = 0
    for row in rows:
        message_text = build_missed_call_message(cfg, row)
        try:
            results = telegram_sender(cfg, message_text)
            message_id = extract_telegram_message_id(results)
            store.mark_telegram_sent(safe_str(row.get("entry_id")), message_id)
            sent_count += 1
        except Exception as exc:
            logging.exception(
                "Retry failed for MANGO missed call entry_id=%s",
                safe_str(row.get("entry_id")),
            )
            store.mark_telegram_failed(safe_str(row.get("entry_id")), str(exc))
    return sent_count


def get_client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = safe_str(handler.headers.get("X-Forwarded-For"))
    if forwarded:
        return safe_str(forwarded.split(",")[0])
    return safe_str(handler.client_address[0])


def is_allowed_ip(client_ip: str, allowed_ips: List[str]) -> bool:
    if not allowed_ips:
        return True
    return client_ip in allowed_ips


def first_non_empty(*values: Any) -> str:
    for value in values:
        if isinstance(value, list):
            for item in value:
                if safe_str(item):
                    return safe_str(item)
            continue
        if safe_str(value):
            return safe_str(value)
    return ""


def extract_json_payload_and_sign(
    path: str,
    headers: Any,
    body: bytes,
) -> Tuple[str, str]:
    parsed = urlparse(path)
    query = parse_qs(parsed.query or "", keep_blank_values=True)
    content_type = safe_str(headers.get("Content-Type")).lower()
    body_text = body.decode("utf-8")

    def header_value(name: str) -> str:
        return safe_str(headers.get(name))

    if (
        "application/x-www-form-urlencoded" in content_type
        or ("=" in body_text and not body_text.lstrip().startswith(("{", "[")))
    ):
        form = parse_qs(body_text, keep_blank_values=True)
        json_str = first_non_empty(
            form.get("json"),
            form.get("payload"),
            form.get("data"),
        )
        sign = first_non_empty(
            form.get("sign"),
            form.get("signature"),
            query.get("sign"),
            query.get("signature"),
            header_value("Sign"),
            header_value("X-Sign"),
            header_value("X-MANGO-Sign"),
        )
        return json_str, sign

    sign = first_non_empty(
        query.get("sign"),
        query.get("signature"),
        header_value("Sign"),
        header_value("X-Sign"),
        header_value("X-MANGO-Sign"),
    )
    return body_text, sign


class MangoWebhookServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: Tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        cfg: MangoConfig,
        store: Any,
        telegram_sender: Callable[[MangoConfig, str], List[Dict[str, Any]]],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.telegram_sender = telegram_sender
        super().__init__(server_address, request_handler_class)


class MangoWebhookRequestHandler(BaseHTTPRequestHandler):
    server: MangoWebhookServer

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("MANGO webhook %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._respond_json(200, {"status": "ok"})
            return
        self._respond_json(404, {"status": "not_found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != self.server.cfg.mango_webhook_path:
            self._respond_json(404, {"status": "not_found"})
            return

        client_ip = get_client_ip(self)
        if not is_allowed_ip(client_ip, self.server.cfg.mango_allowed_ips):
            logging.warning("MANGO IP is not allowed: %s", client_ip)
            self._respond_json(200, {"status": "ignored", "reason": "ip_not_allowed"})
            return

        content_length = max(0, safe_int(self.headers.get("Content-Length"), 0) or 0)
        raw_body = self.rfile.read(content_length) if content_length else b""

        try:
            json_str, sign = extract_json_payload_and_sign(
                self.path,
                self.headers,
                raw_body,
            )
        except UnicodeDecodeError:
            logging.warning("MANGO body is not valid UTF-8.")
            self._respond_json(200, {"status": "ignored", "reason": "invalid_encoding"})
            return

        if not json_str.strip():
            logging.warning("MANGO body does not contain JSON payload.")
            self._respond_json(200, {"status": "ignored", "reason": "missing_json"})
            return
        if not verify_mango_sign(
            self.server.cfg.mango_api_key,
            json_str,
            self.server.cfg.mango_api_salt,
            sign,
        ):
            logging.warning("MANGO sign verification failed for IP %s", client_ip)
            self._respond_json(200, {"status": "ignored", "reason": "invalid_sign"})
            return

        try:
            payload = json.loads(json_str)
        except json.JSONDecodeError:
            logging.warning("MANGO payload is not valid JSON after sign verification.")
            self._respond_json(200, {"status": "ignored", "reason": "invalid_json"})
            return

        if self.server.cfg.mango_log_payloads:
            logging.info("MANGO payload: %s", json.dumps(payload, ensure_ascii=False))

        try:
            result = process_summary_event(
                self.server.cfg,
                self.server.store,
                payload,
                telegram_sender=self.server.telegram_sender,
            )
        except Exception:
            logging.exception("MANGO summary processing crashed.")
            self._respond_json(500, {"status": "error"})
            return

        self._respond_json(
            200,
            {
                "status": result.status,
                "entry_id": result.entry_id,
                "telegram_message_id": result.telegram_message_id,
            },
        )

    def _respond_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MangoRetryWorker(threading.Thread):
    def __init__(
        self,
        cfg: MangoConfig,
        store: Any,
        telegram_sender: Callable[[MangoConfig, str], List[Dict[str, Any]]] = send_telegram_message,
    ) -> None:
        super().__init__(name="mango-retry-worker", daemon=True)
        self.cfg = cfg
        self.store = store
        self.telegram_sender = telegram_sender
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                sent_count = retry_pending_notifications(
                    self.cfg,
                    self.store,
                    telegram_sender=self.telegram_sender,
                )
                if sent_count:
                    logging.info(
                        "MANGO retry worker delivered %s pending notification(s).",
                        sent_count,
                    )
            except Exception:
                logging.exception("MANGO retry worker crashed.")
            self._stop_event.wait(self.cfg.mango_retry_interval_sec)


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def main() -> None:
    cfg = MangoConfig.from_env()
    configure_logging(cfg.log_level)
    if not cfg.mango_enabled:
        logging.info("MANGO webhook service is disabled by MANGO_ENABLED=0.")
        return

    store = MangoStore.from_config(cfg)
    server = MangoWebhookServer(
        (cfg.mango_http_host, cfg.mango_http_port),
        MangoWebhookRequestHandler,
        cfg,
        store,
        send_telegram_message,
    )
    retry_worker: Optional[MangoRetryWorker] = None
    if cfg.mango_retry_enabled:
        retry_worker = MangoRetryWorker(cfg, store)
        retry_worker.start()

    logging.info(
        "MANGO webhook server started on %s:%s%s",
        cfg.mango_http_host,
        cfg.mango_http_port,
        cfg.mango_webhook_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping MANGO webhook server.")
    finally:
        server.shutdown()
        server.server_close()
        if retry_worker is not None:
            retry_worker.stop()
            retry_worker.join(timeout=5)
        store.close()


if __name__ == "__main__":
    main()
