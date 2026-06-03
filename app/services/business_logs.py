import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

from app.core.database import (
    database_configured,
    database_connection,
    database_driver_available,
    initialize_database,
    jsonb_value,
)
from app.core.timezone import now_iso as china_now_iso


BUSINESS_LOG_NAME = "business.log"
SENSITIVE_KEY_PARTS = (
    "cookie",
    "token",
    "password",
    "passwd",
    "secret",
    "authorization",
    "bearer",
    "base_url",
    "share_code",
    "access_code",
    "manifest_url",
    "share_url",
)
_DB_LOGS_READY = False
_DB_LOGS_LOCK = threading.Lock()
DATABASE_LOG_MAX_ATTEMPTS = 3
NOISY_INFO_EVENTS = {
    ("scrapling", "fetch_trace"),
    ("subscription", "metadata_refreshed"),
    ("subscription", "preview_snapshots_refreshed"),
    ("source_library", "preview_snapshots_refreshed"),
}


def _now_iso() -> str:
    return china_now_iso()


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    compact = lowered.replace("_", "").replace("-", "").replace(".", "")
    for part in SENSITIVE_KEY_PARTS:
        part_text = str(part or "").lower()
        part_compact = part_text.replace("_", "").replace("-", "").replace(".", "")
        if part_text in lowered or part_compact in compact:
            return True
    return False


def _safe_value(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return "***"
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(item_key): _safe_value(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 2000:
            return f"{value[:2000]}...<truncated>"
        return value
    return str(value)


def _safe_log_name(file_name: str = BUSINESS_LOG_NAME) -> str:
    name = Path(str(file_name or BUSINESS_LOG_NAME)).name
    if not name.endswith(".log"):
        return BUSINESS_LOG_NAME
    return name


def _database_logs_enabled() -> bool:
    return database_configured() and database_driver_available()


def _ensure_database_logs_ready() -> bool:
    global _DB_LOGS_READY
    if not _database_logs_enabled():
        return False
    if _DB_LOGS_READY:
        return True
    with _DB_LOGS_LOCK:
        if _DB_LOGS_READY:
            return True
        initialize_database()
        _DB_LOGS_READY = True
    return True


def _raw_hash(file_name: str, raw: str) -> str:
    return hashlib.sha1(f"{file_name}\0{raw}".encode("utf-8", errors="ignore")).hexdigest()


def _entry_for_db(entry: dict[str, Any], *, file_name: str, raw: str = "") -> dict[str, Any]:
    payload = {
        str(key): value
        for key, value in entry.items()
        if key not in {"time", "level", "category", "event", "message"}
    }
    raw_text = raw or json.dumps(entry, ensure_ascii=False)
    return {
        "file_name": _safe_log_name(file_name),
        "time_text": str(entry.get("time") or ""),
        "level": str(entry.get("level") or "info").lower(),
        "category": str(entry.get("category") or _safe_log_name(file_name).replace(".log", "")),
        "event": str(entry.get("event") or "event"),
        "message": str(entry.get("message") or ""),
        "payload": payload,
        "raw": raw_text,
        "raw_hash": _raw_hash(_safe_log_name(file_name), raw_text),
    }


def _should_persist_log_entry(entry: dict[str, Any]) -> bool:
    level = str(entry.get("level") or "info").lower()
    if level not in {"info", "debug"}:
        return True
    category = str(entry.get("category") or "").strip()
    event = str(entry.get("event") or "").strip()
    return (category, event) not in NOISY_INFO_EVENTS


def append_database_log_entry(file_name: str, entry: dict[str, Any], *, raw: str = "") -> bool:
    if not isinstance(entry, dict) or not _database_logs_enabled():
        return False
    payload = _entry_for_db(entry, file_name=file_name, raw=raw)
    for attempt in range(1, DATABASE_LOG_MAX_ATTEMPTS + 1):
        try:
            if not _ensure_database_logs_ready():
                return False
            with database_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO makerhub_logs (
                        file_name,
                        time_text,
                        level,
                        category,
                        event,
                        message,
                        payload,
                        raw,
                        raw_hash,
                        created_at
                    )
                    VALUES (
                        %(file_name)s,
                        %(time_text)s,
                        %(level)s,
                        %(category)s,
                        %(event)s,
                        %(message)s,
                        %(payload)s,
                        %(raw)s,
                        %(raw_hash)s,
                        now()
                    )
                    ON CONFLICT (file_name, raw_hash) DO NOTHING
                    """,
                    {
                        **payload,
                        "payload": jsonb_value(payload["payload"]),
                    },
                )
            return True
        except Exception:
            if attempt >= DATABASE_LOG_MAX_ATTEMPTS:
                return False
            time.sleep(0.05 * attempt)
    return False


def append_structured_log(
    file_name: str,
    event: str,
    *,
    level: str = "info",
    category: str = "",
    message: str = "",
    time_text: str = "",
    **payload: Any,
) -> None:
    safe_file_name = _safe_log_name(file_name)
    entry: dict[str, Any] = {
        "time": str(time_text or _now_iso()),
        "level": str(level or "info").lower(),
        "category": str(category or safe_file_name.replace(".log", "")).strip() or safe_file_name.replace(".log", ""),
        "event": str(event or "event").strip() or "event",
        "message": str(message or "").strip(),
        **{str(key): _safe_value(value, key=str(key)) for key, value in payload.items()},
    }
    line = json.dumps(entry, ensure_ascii=False)
    if _should_persist_log_entry(entry):
        append_database_log_entry(safe_file_name, entry, raw=line)


def append_business_log(
    category: str,
    event: str,
    message: str = "",
    *,
    level: str = "info",
    **fields: Any,
) -> None:
    entry = {
        "time": _now_iso(),
        "level": str(level or "info").lower(),
        "category": str(category or "system").strip() or "system",
        "event": str(event or "event").strip() or "event",
        "message": str(message or "").strip(),
        **{str(key): _safe_value(value, key=str(key)) for key, value in fields.items()},
    }
    try:
        line = json.dumps(entry, ensure_ascii=False)
        if not _should_persist_log_entry(entry):
            return
        append_database_log_entry(BUSINESS_LOG_NAME, entry, raw=line)
        print(
            f"[makerhub][{entry['level']}][{entry['category']}] {entry['event']} {entry['message']}".strip(),
            flush=True,
        )
    except Exception:
        return


def list_log_files() -> list[dict[str, Any]]:
    return list(_database_log_file_items().values())


def _database_log_file_items() -> dict[str, dict[str, Any]]:
    if not _database_logs_enabled():
        return {}
    try:
        if not _ensure_database_logs_ready():
            return {}
        with database_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    file_name,
                    count(*) AS count,
                    max(created_at) AS modified_at
                FROM makerhub_logs
                GROUP BY file_name
                """
            ).fetchall()
    except Exception:
        return {}

    items: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = _safe_log_name(str(row.get("file_name") or BUSINESS_LOG_NAME))
        modified_at = row.get("modified_at")
        items[name] = {
            "name": name,
            "size": 0,
            "modified_at": modified_at.isoformat(timespec="seconds") if hasattr(modified_at, "isoformat") else str(modified_at or ""),
            "exists": True,
            "primary": name == BUSINESS_LOG_NAME,
            "database": True,
            "count": int(row.get("count") or 0),
        }
    return items


def _parse_log_line(line: str, source: str) -> dict[str, Any]:
    raw = line.rstrip("\n")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {}

    if isinstance(parsed, dict) and parsed:
        payload = {
            str(key): value
            for key, value in parsed.items()
            if key not in {"time", "level", "category", "event", "message"}
        }
        return {
            "time": str(parsed.get("time") or ""),
            "level": str(parsed.get("level") or "info"),
            "category": str(parsed.get("category") or source.replace(".log", "")),
            "event": str(parsed.get("event") or "event"),
            "message": str(parsed.get("message") or ""),
            "payload": payload,
            "raw": raw,
        }

    return {
        "time": "",
        "level": "info",
        "category": source.replace(".log", ""),
        "event": "line",
        "message": raw,
        "payload": {},
        "raw": raw,
    }


def _log_entry_from_database_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "time": str(row.get("time_text") or ""),
        "level": str(row.get("level") or "info"),
        "category": str(row.get("category") or str(row.get("file_name") or "").replace(".log", "")),
        "event": str(row.get("event") or "event"),
        "message": str(row.get("message") or ""),
        "payload": payload,
        "raw": str(row.get("raw") or ""),
    }


def _read_database_log_entries(file_name: str, *, limit: int, query: str = "") -> list[dict[str, Any]]:
    if not _database_logs_enabled():
        return []
    safe_file_name = _safe_log_name(file_name)
    search = str(query or "").strip()
    try:
        if not _ensure_database_logs_ready():
            return []
        with database_connection() as connection:
            if search:
                rows = connection.execute(
                    """
                    SELECT file_name, time_text, level, category, event, message, payload, raw
                    FROM makerhub_logs
                    WHERE file_name = %s
                      AND (
                        raw ILIKE %s
                        OR message ILIKE %s
                        OR event ILIKE %s
                        OR category ILIKE %s
                      )
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (safe_file_name, f"%{search}%", f"%{search}%", f"%{search}%", f"%{search}%", int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT file_name, time_text, level, category, event, message, payload, raw
                    FROM makerhub_logs
                    WHERE file_name = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (safe_file_name, int(limit)),
                ).fetchall()
    except Exception:
        return []
    return [_log_entry_from_database_row(row) for row in rows or [] if isinstance(row, dict)]


def read_log_entries(file_name: str = BUSINESS_LOG_NAME, *, limit: int = 300, query: str = "") -> dict[str, Any]:
    safe_limit = min(max(int(limit or 300), 1), 2000)
    safe_file_name = _safe_log_name(file_name)
    database_files = _database_log_file_items()
    database_entries = _read_database_log_entries(safe_file_name, limit=safe_limit, query=query)
    return {
        "file": safe_file_name,
        "entries": database_entries,
        "count": len(database_entries),
        "limit": safe_limit,
        "query": query,
        "files": list(database_files.values()),
        "source": "database" if _database_logs_enabled() else "database_unavailable",
    }
