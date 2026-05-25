from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from app.core.database import (
    DatabaseUnavailable,
    database_configured,
    database_driver_available,
    load_json_state,
    save_json_state,
)

JSON_STATE_MAX_ATTEMPTS = 3


def require_database_json_state() -> None:
    if not database_configured():
        raise DatabaseUnavailable("Postgres 未配置，JSON 状态需要数据库。")
    if not database_driver_available():
        raise DatabaseUnavailable("Postgres 驱动未安装，JSON 状态需要数据库。")


def _with_database_json_state_attempts(operation: Callable[[], Any]) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, JSON_STATE_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except DatabaseUnavailable:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= JSON_STATE_MAX_ATTEMPTS:
                break
            time.sleep(0.05 * attempt)
    raise DatabaseUnavailable(f"Postgres JSON 状态操作失败，已尝试 {JSON_STATE_MAX_ATTEMPTS} 次。") from last_exc


def load_database_json_state(key: str, default: dict[str, Any]) -> dict[str, Any]:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    require_database_json_state()
    payload = _with_database_json_state_attempts(lambda: load_json_state(clean_key))
    return payload if isinstance(payload, dict) else dict(default)


def save_database_json_state(key: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    if not isinstance(payload, dict):
        raise ValueError("JSON 状态 payload 必须是对象。")
    require_database_json_state()
    _with_database_json_state_attempts(lambda: save_json_state(clean_key, payload))
    return payload


def load_database_json_state_version(key: str) -> str:
    payload = load_database_json_state(key, {})
    for field in ("token", "version", "updated_at", "updatedAt", "last_updated_at"):
        value = str(payload.get(field) or "").strip()
        if value:
            return value
    return ""


def database_json_state_signature(key: str, default: dict[str, Any] | None = None) -> tuple[str, str]:
    payload = load_database_json_state(key, default or {})
    version = ""
    for field in ("token", "version", "updated_at", "updatedAt", "last_updated_at"):
        version = str(payload.get(field) or "").strip()
        if version:
            break
    digest = hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return (version, digest)
