import os
from contextlib import contextmanager
from typing import Any, Iterator


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
    return True


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
