import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.settings import LOGS_DIR


BUSINESS_LOG_NAME = "business.log"
BUSINESS_LOG_PATH = LOGS_DIR / BUSINESS_LOG_NAME
SENSITIVE_KEY_PARTS = ("cookie", "token", "password", "passwd", "secret", "authorization")
_LOG_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_sensitive_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


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
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _LOG_LOCK:
            with BUSINESS_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        print(
            f"[makerhub][{entry['level']}][{entry['category']}] {entry['event']} {entry['message']}".strip(),
            flush=True,
        )
    except Exception:
        return


def list_log_files() -> list[dict[str, Any]]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    paths = {BUSINESS_LOG_PATH}
    paths.update(path for path in LOGS_DIR.glob("*.log") if path.is_file())
    items: list[dict[str, Any]] = []
    for path in sorted(paths, key=lambda item: (item.name != BUSINESS_LOG_NAME, item.name)):
        try:
            stat = path.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
            size = stat.st_size
            exists = True
        except OSError:
            modified_at = ""
            size = 0
            exists = False
        items.append(
            {
                "name": path.name,
                "size": size,
                "modified_at": modified_at,
                "exists": exists,
                "primary": path.name == BUSINESS_LOG_NAME,
            }
        )
    return items


def _safe_log_path(file_name: str) -> Path:
    name = Path(str(file_name or BUSINESS_LOG_NAME)).name
    if not name.endswith(".log"):
        name = BUSINESS_LOG_NAME
    return LOGS_DIR / name


def _tail_lines(path: Path, limit: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    max_lines = max(int(limit or 1), 1)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque(handle, maxlen=max_lines))


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


def read_log_entries(file_name: str = BUSINESS_LOG_NAME, *, limit: int = 300, query: str = "") -> dict[str, Any]:
    safe_limit = min(max(int(limit or 300), 1), 2000)
    path = _safe_log_path(file_name)
    search = str(query or "").strip().lower()
    tail_limit = min(max(safe_limit * (8 if search else 1), safe_limit), 10000)
    raw_lines = _tail_lines(path, tail_limit)
    if search:
        raw_lines = [line for line in raw_lines if search in line.lower()]
    entries = [_parse_log_line(line, path.name) for line in reversed(raw_lines[-safe_limit:])]
    return {
        "file": path.name,
        "entries": entries,
        "count": len(entries),
        "limit": safe_limit,
        "query": query,
        "files": list_log_files(),
    }
