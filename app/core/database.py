import json
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Optional

DATABASE_SCHEMA_VERSION = 2
DATABASE_SCHEMA_METADATA_KEY = "database_schema_version"
DATABASE_STATE_EVENT_CHANNEL = "makerhub_state_events"
_DATABASE_INITIALIZED = False
_DATABASE_INITIALIZE_LOCK = threading.Lock()


try:  # pragma: no cover - exercised in deployed images with the dependency installed.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - keeps local/dev file-mode runs working.
    psycopg = None
    dict_row = None
    Jsonb = None


class DatabaseUnavailable(RuntimeError):
    pass


def database_url() -> str:
    return str(os.getenv("MAKERHUB_DATABASE_URL", "") or "").strip()


def database_configured() -> bool:
    return bool(database_url())


def database_driver_available() -> bool:
    return psycopg is not None


def jsonb_value(value: Any) -> Any:
    if Jsonb is None:
        return value
    return Jsonb(value)


def _connect_timeout() -> int:
    raw = str(os.getenv("MAKERHUB_DATABASE_CONNECT_TIMEOUT", "") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 5
    return max(1, min(value, 30))


@contextmanager
def database_connection() -> Iterator[Any]:
    url = database_url()
    if not url:
        raise DatabaseUnavailable("Postgres 未配置。")
    if psycopg is None or dict_row is None:
        raise DatabaseUnavailable("Postgres 驱动未安装。")

    connection = psycopg.connect(
        url,
        connect_timeout=_connect_timeout(),
        row_factory=dict_row,
    )
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize_database_schema() -> None:
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS makerhub_metadata (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS makerhub_json_state (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS makerhub_logs (
                id BIGSERIAL PRIMARY KEY,
                file_name TEXT NOT NULL DEFAULT 'business.log',
                time_text TEXT NOT NULL DEFAULT '',
                level TEXT NOT NULL DEFAULT 'info',
                category TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL DEFAULT 'event',
                message TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw TEXT NOT NULL DEFAULT '',
                raw_hash TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS makerhub_state_events (
                id BIGSERIAL PRIMARY KEY,
                type TEXT NOT NULL DEFAULT 'state.changed',
                scope TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS makerhub_state_events_created_idx ON makerhub_state_events (created_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS makerhub_state_events_scope_idx ON makerhub_state_events (scope)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS makerhub_logs_file_created_idx ON makerhub_logs (file_name, created_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS makerhub_logs_level_idx ON makerhub_logs (level)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS makerhub_logs_category_idx ON makerhub_logs (category)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS makerhub_logs_file_raw_hash_idx ON makerhub_logs (file_name, raw_hash)"
        )
        connection.execute(
            """
            INSERT INTO makerhub_metadata (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = now()
            """,
            (
                DATABASE_SCHEMA_METADATA_KEY,
                jsonb_value(
                    {
                        "version": DATABASE_SCHEMA_VERSION,
                        "name": "core",
                    }
                ),
            ),
        )


def initialize_database() -> bool:
    global _DATABASE_INITIALIZED
    if not database_configured():
        return False
    if _DATABASE_INITIALIZED:
        return True
    with _DATABASE_INITIALIZE_LOCK:
        if _DATABASE_INITIALIZED:
            return True
        _initialize_database_schema()
        _DATABASE_INITIALIZED = True
    return True


def _reset_database_initialization_for_tests() -> None:
    global _DATABASE_INITIALIZED
    with _DATABASE_INITIALIZE_LOCK:
        _DATABASE_INITIALIZED = False


def database_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "configured": database_configured(),
        "driver_available": database_driver_available(),
        "available": False,
        "schema_version": 0,
        "expected_schema_version": DATABASE_SCHEMA_VERSION,
    }
    if not status["configured"]:
        status["error"] = "Postgres 未配置。"
        return status
    if not status["driver_available"]:
        status["error"] = "Postgres 驱动未安装。"
        return status

    try:
        initialize_database()
        with database_connection() as connection:
            row = connection.execute(
                "SELECT value FROM makerhub_metadata WHERE key = %s",
                (DATABASE_SCHEMA_METADATA_KEY,),
            ).fetchone()
        value = row.get("value") if isinstance(row, dict) else {}
        if isinstance(value, dict):
            status["schema_version"] = int(value.get("version") or 0)
        status["available"] = status["schema_version"] >= DATABASE_SCHEMA_VERSION
    except Exception as exc:
        status["error"] = str(exc)
    return status


def load_json_state(key: str) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    initialize_database()
    with database_connection() as connection:
        row = connection.execute(
            "SELECT value FROM makerhub_json_state WHERE key = %s",
            (clean_key,),
        ).fetchone()
    if isinstance(row, dict):
        return row.get("value")
    return None


def load_json_state_array_summary(key: str, array_field: str, *, limit: int = 5) -> dict[str, Any]:
    clean_key = str(key or "").strip()
    clean_field = str(array_field or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    if not clean_field:
        raise ValueError("JSON 状态数组字段不能为空。")
    clean_limit = max(0, int(limit or 0))
    initialize_database()
    with database_connection() as connection:
        row = connection.execute(
            """
            WITH params AS (
                SELECT %s::text AS array_field, %s::int AS item_limit, %s::text AS state_key
            ),
            state AS (
                SELECT value -> params.array_field AS raw_items, params.item_limit
                FROM makerhub_json_state, params
                WHERE key = params.state_key
            ),
            counted AS (
                SELECT
                    raw_items,
                    item_limit,
                    CASE
                        WHEN jsonb_typeof(raw_items) = 'array' THEN jsonb_array_length(raw_items)
                        ELSE 0
                    END AS item_count
                FROM state
            )
            SELECT
                item_count AS count,
                COALESCE(
                    (
                        SELECT jsonb_agg(raw_items -> idx ORDER BY idx)
                        FROM generate_series(0, LEAST(item_limit, item_count) - 1) AS idx
                    ),
                    '[]'::jsonb
                ) AS items
            FROM counted
            """,
            (clean_field, clean_limit, clean_key),
        ).fetchone()
    if not isinstance(row, dict):
        return {"items": [], "count": 0}
    items = row.get("items")
    return {
        "items": items if isinstance(items, list) else [],
        "count": int(row.get("count") or 0),
    }


def save_json_state(key: str, value: Any) -> Any:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    initialize_database()
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO makerhub_json_state (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = now()
            """,
            (clean_key, jsonb_value(value)),
        )
    return value


def delete_json_state(key: str) -> bool:
    clean_key = str(key or "").strip()
    if not clean_key:
        return False
    initialize_database()
    with database_connection() as connection:
        connection.execute("DELETE FROM makerhub_json_state WHERE key = %s", (clean_key,))
    return True


def append_state_event(event_type: str, scope: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    clean_type = str(event_type or "").strip() or "state.changed"
    clean_scope = str(scope or "").strip()
    if not clean_scope:
        raise ValueError("状态事件 scope 不能为空。")
    initialize_database()
    event_payload = payload if isinstance(payload, dict) else {}
    with database_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO makerhub_state_events (type, scope, payload, created_at)
            VALUES (%s, %s, %s, now())
            RETURNING id, type, scope, payload, created_at
            """,
            (clean_type, clean_scope, jsonb_value(event_payload)),
        ).fetchone()
        event = dict(row or {})
        event_id = int(event.get("id") or 0)
        notify_payload = json.dumps(
            {
                "id": event_id,
                "type": clean_type,
                "scope": clean_scope,
            },
            ensure_ascii=False,
        )
        connection.execute("SELECT pg_notify(%s, %s)", (DATABASE_STATE_EVENT_CHANNEL, notify_payload))
    return event


def list_state_events_after(last_id: int = 0, *, limit: int = 100) -> list[dict[str, Any]]:
    try:
        clean_last_id = max(0, int(last_id or 0))
    except (TypeError, ValueError):
        clean_last_id = 0
    try:
        clean_limit = int(limit or 100)
    except (TypeError, ValueError):
        clean_limit = 100
    clean_limit = max(1, min(clean_limit, 500))
    initialize_database()
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, type, scope, payload, created_at
            FROM makerhub_state_events
            WHERE id > %s
            ORDER BY id ASC
            LIMIT %s
            """,
            (clean_last_id, clean_limit),
        ).fetchall()
    return [dict(row or {}) for row in (rows or [])]


def latest_state_event_id() -> int:
    initialize_database()
    with database_connection() as connection:
        row = connection.execute("SELECT COALESCE(MAX(id), 0) AS id FROM makerhub_state_events").fetchone()
    if isinstance(row, dict):
        return int(row.get("id") or 0)
    return 0
