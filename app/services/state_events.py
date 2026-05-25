from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from typing import Any, Callable

from app.core import database
from app.core.database import (
    DATABASE_STATE_EVENT_CHANNEL,
    DatabaseUnavailable,
    append_state_event,
    latest_state_event_id,
    list_state_events_after,
)
from app.core.timezone import now_iso as china_now_iso


STATE_EVENT_FALLBACK_WAIT_SECONDS = 15.0
_SUBSCRIBER_LOCK = threading.Lock()
_SUBSCRIBER_CALLBACKS: set[Callable[[], None]] = set()
_LISTENER_LOCK = threading.Lock()
_LISTENER_THREAD: threading.Thread | None = None


STATE_EVENT_SCOPES = {
    "archive_queue",
    "missing_3mf",
    "organize_tasks",
    "subscriptions_state",
    "remote_refresh_state",
    "archive_profile_backfill_status",
    "system_update",
    "source_library",
    "models",
    "dashboard",
}


def register_state_event_callback(callback: Callable[[], None]) -> Callable[[], None]:
    with _SUBSCRIBER_LOCK:
        _SUBSCRIBER_CALLBACKS.add(callback)

    def _unregister() -> None:
        with _SUBSCRIBER_LOCK:
            _SUBSCRIBER_CALLBACKS.discard(callback)

    return _unregister


def wake_state_event_subscribers() -> None:
    with _SUBSCRIBER_LOCK:
        callbacks = list(_SUBSCRIBER_CALLBACKS)
    for callback in callbacks:
        try:
            callback()
        except Exception:
            continue


def _listen_for_database_notifications() -> None:
    while True:
        try:
            if not database.database_configured() or not database.database_driver_available() or database.psycopg is None:
                time.sleep(STATE_EVENT_FALLBACK_WAIT_SECONDS)
                continue
            connection = database.psycopg.connect(
                database.database_url(),
                autocommit=True,
                connect_timeout=5,
            )
            try:
                connection.execute(f"LISTEN {DATABASE_STATE_EVENT_CHANNEL}")
                while True:
                    received = False
                    for _notify in connection.notifies(
                        timeout=STATE_EVENT_FALLBACK_WAIT_SECONDS,
                        stop_after=1,
                    ):
                        received = True
                        wake_state_event_subscribers()
                    if not received:
                        wake_state_event_subscribers()
            finally:
                connection.close()
        except Exception:
            time.sleep(5.0)


def start_state_event_listener() -> None:
    global _LISTENER_THREAD
    with _LISTENER_LOCK:
        if _LISTENER_THREAD and _LISTENER_THREAD.is_alive():
            return
        _LISTENER_THREAD = threading.Thread(
            target=_listen_for_database_notifications,
            name="makerhub-state-events",
            daemon=True,
        )
        _LISTENER_THREAD.start()


class StateEventWaiter:
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()
        self._unregister = register_state_event_callback(self.wake)

    def wake(self) -> None:
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self, timeout: float = STATE_EVENT_FALLBACK_WAIT_SECONDS) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._event.clear()

    def close(self) -> None:
        self._unregister()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def normalize_state_event(row: dict[str, Any]) -> dict[str, Any]:
    created_at = row.get("created_at")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {
        "id": int(row.get("id") or 0),
        "type": str(row.get("type") or "state.changed"),
        "scope": str(row.get("scope") or ""),
        "payload": _json_safe(payload),
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else str(created_at or ""),
    }


def publish_state_event(scope: str, event_type: str = "state.changed", payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    clean_scope = str(scope or "").strip()
    if not clean_scope:
        return None
    event_payload = _json_safe(payload if isinstance(payload, dict) else {})
    event_payload.setdefault("scope", clean_scope)
    event_payload.setdefault("published_at", china_now_iso())
    try:
        event = normalize_state_event(append_state_event(event_type, clean_scope, event_payload))
        wake_state_event_subscribers()
        return event
    except (DatabaseUnavailable, ValueError, RuntimeError, OSError):
        return None
    except Exception:
        return None


def fetch_state_events_after(last_id: int = 0, *, limit: int = 100) -> list[dict[str, Any]]:
    try:
        rows = list_state_events_after(last_id, limit=limit)
    except (DatabaseUnavailable, ValueError, RuntimeError, OSError):
        return []
    except Exception:
        return []
    return [normalize_state_event(row) for row in rows]


def current_state_event_id() -> int:
    try:
        return latest_state_event_id()
    except (DatabaseUnavailable, ValueError, RuntimeError, OSError):
        return 0
    except Exception:
        return 0


def task_counts_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "running_count": int(state.get("running_count") or 0),
        "queued_count": int(state.get("queued_count") or 0),
        "failed_count": int(state.get("failed_count") or 0),
        "count": int(state.get("count") or 0),
    }
