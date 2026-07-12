from __future__ import annotations

import os
import threading
import time
from typing import Any

from app.core.database import (
    database_autocommit_connection,
    database_connection,
    database_url,
    initialize_database,
)


DEFAULT_STATE_EVENT_RETENTION_DAYS = 14
DEFAULT_BUSINESS_LOG_RETENTION_DAYS = 90
DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_BATCHES = 10
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60
_ALLOWED_TABLES = {"makerhub_state_events", "makerhub_logs"}
_MAINTENANCE_LOCK = threading.Lock()
_LAST_MAINTENANCE_AT: float | None = None
_RETENTION_INDEX_LOCK = threading.Lock()
_RETENTION_INDEX_READY_KEY: tuple[int, str] | None = None


class PartialCleanupError(RuntimeError):
    def __init__(self, deleted: int):
        super().__init__("数据库分批清理未完整执行。")
        self.deleted = max(int(deleted or 0), 0)


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _retention_days(value: int | None, env_name: str, default: int) -> int:
    if value is None:
        return _env_int(env_name, default, minimum=0, maximum=3650)
    return max(0, min(int(value), 3650))


def _delete_expired_batch(table: str, *, retention_days: int, batch_size: int) -> int:
    if table not in _ALLOWED_TABLES:
        raise ValueError("不支持的数据库清理表。")
    initialize_database()
    if table == "makerhub_logs":
        _ensure_log_retention_index()
    with database_connection() as connection:
        cursor = connection.execute(
            f"""
            WITH expired AS (
                SELECT id
                FROM {table}
                WHERE created_at < now() - (%s * INTERVAL '1 day')
                ORDER BY created_at ASC, id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            DELETE FROM {table} AS target
            USING expired
            WHERE target.id = expired.id
            """,
            (retention_days, batch_size),
        )
        return max(int(cursor.rowcount or 0), 0)


def _cleanup_table(
    table: str,
    *,
    retention_days: int,
    batch_size: int,
    max_batches: int,
) -> int:
    if retention_days <= 0:
        return 0
    deleted = 0
    for _batch in range(max_batches):
        try:
            batch_deleted = _delete_expired_batch(
                table,
                retention_days=retention_days,
                batch_size=batch_size,
            )
        except Exception as exc:
            raise PartialCleanupError(deleted) from exc
        deleted += batch_deleted
        if batch_deleted < batch_size:
            break
    return deleted


def _ensure_log_retention_index() -> None:
    global _RETENTION_INDEX_READY_KEY
    key = (os.getpid(), database_url())
    if _RETENTION_INDEX_READY_KEY == key:
        return
    with _RETENTION_INDEX_LOCK:
        if _RETENTION_INDEX_READY_KEY == key:
            return
        with database_autocommit_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    to_regclass('public.makerhub_logs_created_idx') IS NOT NULL AS exists,
                    COALESCE(
                        (
                            SELECT indisvalid
                            FROM pg_index
                            WHERE indexrelid = to_regclass('public.makerhub_logs_created_idx')
                        ),
                        false
                    ) AS valid
                """
            ).fetchone()
            exists = bool((row or {}).get("exists")) if isinstance(row, dict) else False
            valid = bool((row or {}).get("valid")) if isinstance(row, dict) else False
            if exists and not valid:
                connection.execute("DROP INDEX CONCURRENTLY IF EXISTS makerhub_logs_created_idx")
                exists = False
            if not exists:
                connection.execute(
                    "CREATE INDEX CONCURRENTLY makerhub_logs_created_idx ON makerhub_logs (created_at, id)"
                )
        _RETENTION_INDEX_READY_KEY = key


def cleanup_expired_rows(
    *,
    event_days: int | None = None,
    log_days: int | None = None,
    batch_size: int | None = None,
    max_batches: int | None = None,
) -> dict[str, Any]:
    clean_event_days = _retention_days(
        event_days,
        "MAKERHUB_STATE_EVENT_RETENTION_DAYS",
        DEFAULT_STATE_EVENT_RETENTION_DAYS,
    )
    clean_log_days = _retention_days(
        log_days,
        "MAKERHUB_BUSINESS_LOG_RETENTION_DAYS",
        DEFAULT_BUSINESS_LOG_RETENTION_DAYS,
    )
    clean_batch_size = (
        _env_int(
            "MAKERHUB_DATABASE_MAINTENANCE_BATCH_SIZE",
            DEFAULT_BATCH_SIZE,
            minimum=100,
            maximum=1000,
        )
        if batch_size is None
        else max(1, min(int(batch_size), 1000))
    )
    clean_max_batches = (
        _env_int(
            "MAKERHUB_DATABASE_MAINTENANCE_MAX_BATCHES",
            DEFAULT_MAX_BATCHES,
            minimum=1,
            maximum=100,
        )
        if max_batches is None
        else max(1, min(int(max_batches), 100))
    )
    result: dict[str, Any] = {
        "events_deleted": 0,
        "logs_deleted": 0,
        "event_retention_days": clean_event_days,
        "log_retention_days": clean_log_days,
        "errors": {},
    }
    for result_key, error_key, table, days in (
        ("events_deleted", "events", "makerhub_state_events", clean_event_days),
        ("logs_deleted", "logs", "makerhub_logs", clean_log_days),
    ):
        try:
            result[result_key] = _cleanup_table(
                table,
                retention_days=days,
                batch_size=clean_batch_size,
                max_batches=clean_max_batches,
            )
        except PartialCleanupError as exc:
            result[result_key] = exc.deleted
            result["errors"][error_key] = "数据库清理失败。"
        except Exception:
            result["errors"][error_key] = "数据库清理失败。"

    if result["logs_deleted"]:
        try:
            from app.services.business_logs import invalidate_log_facet_cache

            invalidate_log_facet_cache()
        except Exception:
            pass
    return result


def run_database_maintenance_if_due(
    *,
    now: float | None = None,
    interval_seconds: int | None = None,
) -> dict[str, Any]:
    global _LAST_MAINTENANCE_AT
    current = float(time.monotonic() if now is None else now)
    interval = (
        _env_int(
            "MAKERHUB_DATABASE_MAINTENANCE_INTERVAL_SECONDS",
            DEFAULT_INTERVAL_SECONDS,
            minimum=60,
            maximum=7 * 24 * 60 * 60,
        )
        if interval_seconds is None
        else max(1, int(interval_seconds))
    )
    with _MAINTENANCE_LOCK:
        if _LAST_MAINTENANCE_AT is not None and current - _LAST_MAINTENANCE_AT < interval:
            return {"ran": False}
        _LAST_MAINTENANCE_AT = current
    return {"ran": True, **cleanup_expired_rows()}


def _reset_database_maintenance_for_tests() -> None:
    global _LAST_MAINTENANCE_AT, _RETENTION_INDEX_READY_KEY
    with _MAINTENANCE_LOCK:
        _LAST_MAINTENANCE_AT = None
    with _RETENTION_INDEX_LOCK:
        _RETENTION_INDEX_READY_KEY = None
