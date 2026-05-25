import os
from contextlib import contextmanager
from typing import Any, Iterator

DATABASE_SCHEMA_VERSION = 1
DATABASE_SCHEMA_METADATA_KEY = "database_schema_version"


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


def initialize_database() -> bool:
    if not database_configured():
        return False
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
    return True


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
