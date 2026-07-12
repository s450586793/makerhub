import json
import os
import threading
from copy import deepcopy
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Iterator, Optional

DATABASE_SCHEMA_VERSION = 3
DATABASE_SCHEMA_METADATA_KEY = "database_schema_version"
DATABASE_STATE_EVENT_CHANNEL = "makerhub_state_events"
_DATABASE_INITIALIZED_KEY: tuple[int, str] | None = None
_DATABASE_INITIALIZE_LOCK = threading.Lock()
_DATABASE_POOL = None
_DATABASE_POOL_KEY: tuple[int, str] | None = None
_DATABASE_POOL_LOCK = threading.RLock()


try:  # pragma: no cover - exercised in deployed images with the dependency installed.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - keeps local/dev file-mode runs working.
    psycopg = None
    dict_row = None
    Jsonb = None

try:  # pragma: no cover - exercised in deployed images with the dependency installed.
    from psycopg_pool import ConnectionPool
except Exception:  # pragma: no cover - keeps local/dev file-mode runs working.
    ConnectionPool = None


class DatabaseUnavailable(RuntimeError):
    pass


class JsonStateConflict(RuntimeError):
    pass


def database_url() -> str:
    return str(os.getenv("MAKERHUB_DATABASE_URL", "") or "").strip()


def database_configured() -> bool:
    return bool(database_url())


def database_driver_available() -> bool:
    return psycopg is not None and ConnectionPool is not None


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


def _pool_max_size() -> int:
    raw = str(os.getenv("MAKERHUB_DATABASE_POOL_MAX", "") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 8
    return max(1, min(value, 32))


def _pool_timeout() -> int:
    raw = str(os.getenv("MAKERHUB_DATABASE_POOL_TIMEOUT", "") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 5
    return max(1, min(value, 30))


def _close_pool_instance(pool: Any) -> None:
    if pool is None:
        return
    try:
        pool.close()
    except Exception:
        pass


def close_database_pool() -> None:
    global _DATABASE_POOL, _DATABASE_POOL_KEY
    with _DATABASE_POOL_LOCK:
        pool = _DATABASE_POOL
        _DATABASE_POOL = None
        _DATABASE_POOL_KEY = None
    _close_pool_instance(pool)


def _reset_database_pool_for_tests() -> None:
    close_database_pool()


def _get_database_pool() -> Any:
    global _DATABASE_POOL, _DATABASE_POOL_KEY
    url = database_url()
    if not url:
        raise DatabaseUnavailable("Postgres 未配置。")
    if psycopg is None or dict_row is None or ConnectionPool is None:
        raise DatabaseUnavailable("Postgres 连接池驱动未安装。")

    key = (os.getpid(), url)
    with _DATABASE_POOL_LOCK:
        if _DATABASE_POOL is not None and _DATABASE_POOL_KEY == key:
            return _DATABASE_POOL
        previous = _DATABASE_POOL
        _DATABASE_POOL = None
        _DATABASE_POOL_KEY = None
        _close_pool_instance(previous)
        try:
            pool = ConnectionPool(
                conninfo=url,
                min_size=0,
                max_size=_pool_max_size(),
                timeout=_pool_timeout(),
                kwargs={
                    "connect_timeout": _connect_timeout(),
                    "row_factory": dict_row,
                },
                open=False,
            )
            pool.open(wait=False)
        except Exception:
            _close_pool_instance(locals().get("pool"))
            raise DatabaseUnavailable("Postgres 连接池初始化失败。") from None
        _DATABASE_POOL = pool
        _DATABASE_POOL_KEY = key
        return pool


@contextmanager
def database_connection() -> Iterator[Any]:
    pool = _get_database_pool()
    try:
        connection = pool.getconn()
    except Exception:
        raise DatabaseUnavailable("Postgres 连接池暂时不可用。") from None

    try:
        yield connection
        connection.commit()
    except BaseException:
        try:
            try:
                connection.rollback()
            except Exception:
                pass
        finally:
            pool.putconn(connection)
        raise
    else:
        pool.putconn(connection)


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
                revision BIGINT NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        connection.execute(
            "ALTER TABLE makerhub_json_state ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0"
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
            "CREATE INDEX IF NOT EXISTS makerhub_logs_created_idx ON makerhub_logs (created_at, id)"
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
    global _DATABASE_INITIALIZED_KEY
    if not database_configured():
        return False
    key = (os.getpid(), database_url())
    if _DATABASE_INITIALIZED_KEY == key:
        return True
    with _DATABASE_INITIALIZE_LOCK:
        if _DATABASE_INITIALIZED_KEY == key:
            return True
        _initialize_database_schema()
        _DATABASE_INITIALIZED_KEY = key
    return True


def _reset_database_initialization_for_tests() -> None:
    global _DATABASE_INITIALIZED_KEY
    with _DATABASE_INITIALIZE_LOCK:
        _DATABASE_INITIALIZED_KEY = None


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


def load_json_state_with_revision(key: str) -> tuple[Any, int]:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    initialize_database()
    with database_connection() as connection:
        row = connection.execute(
            "SELECT value, revision FROM makerhub_json_state WHERE key = %s",
            (clean_key,),
        ).fetchone()
    if not isinstance(row, dict):
        return None, 0
    return row.get("value"), int(row.get("revision") or 0)


def load_json_states(keys: Iterable[str]) -> dict[str, Any]:
    clean_keys = list(dict.fromkeys(str(key or "").strip() for key in keys))
    clean_keys = [key for key in clean_keys if key]
    if not clean_keys:
        return {}
    initialize_database()
    with database_connection() as connection:
        rows = connection.execute(
            "SELECT key, value FROM makerhub_json_state WHERE key = ANY(%s)",
            (clean_keys,),
        ).fetchall()
    values = {key: None for key in clean_keys}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        if key in values:
            values[key] = row.get("value")
    return values


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
                revision = makerhub_json_state.revision + 1,
                updated_at = now()
            """,
            (clean_key, jsonb_value(value)),
        )
    return value


def update_json_state(
    key: str,
    default: Any,
    mutator: Callable[[Any], Any],
    *,
    expected_revision: int | None = None,
) -> tuple[Any, int]:
    clean_key = str(key or "").strip()
    if not clean_key:
        raise ValueError("JSON 状态 key 不能为空。")
    if not callable(mutator):
        raise TypeError("JSON 状态 mutator 必须可调用。")
    initialize_database()
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO makerhub_json_state (key, value, revision, updated_at)
            VALUES (%s, %s, 0, now())
            ON CONFLICT (key) DO NOTHING
            """,
            (clean_key, jsonb_value(default)),
        )
        row = connection.execute(
            "SELECT value, revision FROM makerhub_json_state WHERE key = %s FOR UPDATE",
            (clean_key,),
        ).fetchone()
        current = row.get("value") if isinstance(row, dict) else deepcopy(default)
        revision = int((row or {}).get("revision") or 0) if isinstance(row, dict) else 0
        if expected_revision is not None and revision != int(expected_revision):
            raise JsonStateConflict(
                f"JSON 状态 {clean_key} 已更新，请重新加载后再保存。"
            )
        working = deepcopy(current)
        result = mutator(working)
        updated = working if result is None else result
        updated_row = connection.execute(
            """
            UPDATE makerhub_json_state
            SET value = %s, revision = revision + 1, updated_at = now()
            WHERE key = %s
            RETURNING revision
            """,
            (jsonb_value(updated), clean_key),
        ).fetchone()
        updated_revision = int((updated_row or {}).get("revision") or (revision + 1))
    return updated, updated_revision


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
