from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.core.database import (
    DatabaseUnavailable,
    database_configured,
    database_connection,
    database_driver_available,
    initialize_database,
    jsonb_value,
)
from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import ARCHIVE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.state_events import publish_state_event


MODEL_INDEX_SCHEMA_VERSION = 2
BOOTSTRAP_METADATA_KEY = "archive_model_index_bootstrap"
PROFILE_BACKFILL_STATUS_KEY = "archive_profile_backfill_status"
LOCAL_SHORT_KEY_START = 100001
LOCAL_SHORT_KEY_PREFIX = "local"
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_LAST_WARNING_AT = 0.0
_DB_RETRY_AFTER = 0.0
_WARNING_INTERVAL_SECONDS = 300
_DATABASE_RETRY_SECONDS = 30
_STALE_MODEL_DIR_SAMPLE_LIMIT = 50
_SHORT_KEY_PATTERN = re.compile(r"^[a-z]+[0-9]+$")


def _warn_once(event: str, message: str, **fields: Any) -> None:
    global _LAST_WARNING_AT
    now_value = time.monotonic()
    if now_value - _LAST_WARNING_AT < _WARNING_INTERVAL_SECONDS:
        return
    _LAST_WARNING_AT = now_value
    append_business_log(
        "database",
        event,
        message,
        level="warning",
        **fields,
    )


def _database_temporarily_unavailable() -> bool:
    return time.monotonic() < _DB_RETRY_AFTER


def _mark_database_unavailable() -> None:
    global _DB_RETRY_AFTER
    _DB_RETRY_AFTER = time.monotonic() + _DATABASE_RETRY_SECONDS


def _request_archive_model_index_rebuild(stale_model_dirs: list[str]) -> bool:
    stale_count = len(stale_model_dirs)
    if stale_count <= 0:
        return False
    try:
        current = load_database_json_state(PROFILE_BACKFILL_STATUS_KEY, {})
        if isinstance(current, dict) and current.get("running"):
            return False
        sample = stale_model_dirs[:_STALE_MODEL_DIR_SAMPLE_LIMIT]
        payload: dict[str, Any] = {
            "running": True,
            "phase": "database_migration",
            "database_rebuild_requested": True,
            "force_database_rebuild": True,
            "database_only": True,
            "auto_database_migration": False,
            "started_at": china_now_iso(),
            "finished_at": "",
            "last_error": "",
            "last_result": {
                "database_index": {
                    "requested_by": "archive_model_index_stale_rows",
                    "stale_count": stale_count,
                    "stale_model_dirs": sample,
                    "stale_model_dirs_truncated": stale_count > len(sample),
                }
            },
        }
        save_database_json_state(PROFILE_BACKFILL_STATUS_KEY, payload)
        publish_state_event(
            PROFILE_BACKFILL_STATUS_KEY,
            "profile_backfill.changed",
            {
                "running": True,
                "phase": "database_migration",
                "database_only": True,
                "stale_count": stale_count,
            },
        )
        append_business_log(
            "database",
            "archive_model_index_rebuild_requested",
            "归档模型数据库索引已交给 worker 后台重建。",
            stale_count=stale_count,
            sample_model_dirs=sample,
        )
        return True
    except Exception as exc:
        _warn_once(
            "archive_model_index_rebuild_request_failed",
            "归档模型数据库索引后台重建请求写入失败。",
            error=str(exc),
            count=stale_count,
        )
        return False


def archive_model_index_configured() -> bool:
    return database_configured() and database_driver_available()


def ensure_archive_model_index_schema() -> bool:
    global _SCHEMA_READY
    if not archive_model_index_configured():
        return False
    if _database_temporarily_unavailable():
        return False
    if _SCHEMA_READY:
        return True
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return True
        try:
            initialize_database()
            with database_connection() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS archive_model_index (
                        model_dir TEXT PRIMARY KEY,
                        short_key TEXT NOT NULL DEFAULT '',
                        model_id TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        origin_url TEXT NOT NULL DEFAULT '',
                        author_name TEXT NOT NULL DEFAULT '',
                        author_url TEXT NOT NULL DEFAULT '',
                        cover_url TEXT NOT NULL DEFAULT '',
                        cover_remote_url TEXT NOT NULL DEFAULT '',
                        collect_ts BIGINT NOT NULL DEFAULT 0,
                        publish_ts BIGINT NOT NULL DEFAULT 0,
                        meta_path TEXT NOT NULL DEFAULT '',
                        meta_mtime_ns BIGINT NOT NULL DEFAULT 0,
                        meta_size BIGINT NOT NULL DEFAULT 0,
                        model_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                connection.execute(
                    "ALTER TABLE archive_model_index ADD COLUMN IF NOT EXISTS short_key TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_model_index_source_idx ON archive_model_index (source)"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS archive_model_index_short_key_idx ON archive_model_index (short_key) WHERE short_key <> ''"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_model_index_model_id_idx ON archive_model_index (model_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_model_index_origin_url_idx ON archive_model_index (origin_url)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_model_index_collect_ts_idx ON archive_model_index (collect_ts DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS archive_model_index_updated_at_idx ON archive_model_index (updated_at DESC)"
                )
        except Exception:
            _mark_database_unavailable()
            raise
        _SCHEMA_READY = True
    return True


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _meta_signature(meta_path: Path) -> tuple[int, int]:
    try:
        stat = meta_path.stat()
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _remote_short_key(source: str, model_id: str) -> str:
    clean_source = str(source or "").strip().lower()
    clean_model_id = re.sub(r"\D+", "", str(model_id or ""))
    if not clean_model_id:
        return ""
    if clean_source == "cn":
        return f"mwcn{clean_model_id}"
    if clean_source == "global":
        return f"mwg{clean_model_id}"
    return ""


def _local_short_key_number(value: str) -> int:
    raw = str(value or "").strip().lower()
    if not raw.startswith(LOCAL_SHORT_KEY_PREFIX):
        return 0
    suffix = raw[len(LOCAL_SHORT_KEY_PREFIX):]
    if not suffix.isdigit():
        return 0
    try:
        return int(suffix)
    except ValueError:
        return 0


def _next_local_short_key(connection: Any) -> str:
    rows = connection.execute(
        "SELECT short_key FROM archive_model_index WHERE short_key LIKE %s",
        (f"{LOCAL_SHORT_KEY_PREFIX}%",),
    ).fetchall()
    max_number = LOCAL_SHORT_KEY_START - 1
    for row in rows or []:
        value = row.get("short_key") if isinstance(row, dict) else ""
        max_number = max(max_number, _local_short_key_number(str(value or "")))
    return f"{LOCAL_SHORT_KEY_PREFIX}{max_number + 1}"


def _existing_short_key(connection: Any, model_dir: str) -> str:
    row = connection.execute(
        "SELECT short_key FROM archive_model_index WHERE model_dir = %s",
        (model_dir,),
    ).fetchone()
    if not isinstance(row, dict):
        return ""
    return str(row.get("short_key") or "").strip()


def _short_key_owner(connection: Any, short_key: str) -> str:
    clean_short_key = str(short_key or "").strip()
    if not clean_short_key:
        return ""
    row = connection.execute(
        "SELECT model_dir FROM archive_model_index WHERE short_key = %s",
        (clean_short_key,),
    ).fetchone()
    if not isinstance(row, dict):
        return ""
    return str(row.get("model_dir") or "").strip().strip("/")


def _assign_short_key(connection: Any, *, model_dir: str, model: dict[str, Any]) -> str:
    existing = _existing_short_key(connection, model_dir)
    if existing:
        return existing

    source = str(model.get("source") or "").strip().lower()
    remote_key = _remote_short_key(source, str(model.get("id") or ""))
    if remote_key:
        owner = _short_key_owner(connection, remote_key)
        return remote_key if not owner or owner == model_dir else ""
    if source == "local":
        return _next_local_short_key(connection)
    return ""


def _model_with_short_detail_path(model: dict[str, Any], short_key: str) -> dict[str, Any]:
    payload = dict(model)
    clean_short_key = str(short_key or "").strip()
    if clean_short_key:
        payload["short_key"] = clean_short_key
        payload["detail_path"] = f"/models/{clean_short_key}"
    else:
        payload.pop("short_key", None)
        payload["detail_path"] = ""
    return payload


def is_model_short_key(value: str) -> bool:
    clean_value = str(value or "").strip().strip("/").lower()
    if not clean_value:
        return False
    if clean_value.startswith("mwcn"):
        return clean_value[4:].isdigit()
    if clean_value.startswith("mwg"):
        return clean_value[3:].isdigit()
    if clean_value.startswith(LOCAL_SHORT_KEY_PREFIX):
        return clean_value[len(LOCAL_SHORT_KEY_PREFIX):].isdigit()
    return bool(_SHORT_KEY_PATTERN.fullmatch(clean_value))


def resolve_model_dir_from_short_key(short_key: str) -> str:
    clean_short_key = str(short_key or "").strip().strip("/").lower()
    if not is_model_short_key(clean_short_key):
        return ""
    if _database_temporarily_unavailable():
        return ""
    try:
        if not ensure_archive_model_index_schema():
            return ""
        with database_connection() as connection:
            row = connection.execute(
                "SELECT model_dir FROM archive_model_index WHERE short_key = %s",
                (clean_short_key,),
            ).fetchone()
        if not isinstance(row, dict):
            return ""
        return str(row.get("model_dir") or "").strip().strip("/")
    except Exception as exc:
        _warn_once(
            "archive_model_short_key_resolve_failed",
            "模型短链接解析失败。",
            error=str(exc),
            short_key=clean_short_key,
        )
        return ""


def upsert_archive_model_index(
    model_dir: str,
    *,
    model: dict[str, Any],
    meta_path: Path,
    meta: Optional[dict[str, Any]] = None,
) -> bool:
    clean_model_dir = str(model_dir or "").strip().strip("/")
    if not clean_model_dir or not isinstance(model, dict):
        return False
    if _database_temporarily_unavailable():
        return False
    try:
        if not ensure_archive_model_index_schema():
            return False
        meta_payload = meta if isinstance(meta, dict) else _read_json(meta_path)
        meta_mtime_ns, meta_size = _meta_signature(meta_path)
        author = model.get("author") if isinstance(model.get("author"), dict) else {}
        with database_connection() as connection:
            short_key = _assign_short_key(connection, model_dir=clean_model_dir, model=model)
            indexed_model = _model_with_short_detail_path(model, short_key)
            if short_key:
                model["short_key"] = short_key
                model["detail_path"] = indexed_model["detail_path"]
            else:
                model.pop("short_key", None)
                model["detail_path"] = ""
            connection.execute(
                """
                INSERT INTO archive_model_index (
                    model_dir,
                    short_key,
                    model_id,
                    title,
                    source,
                    origin_url,
                    author_name,
                    author_url,
                    cover_url,
                    cover_remote_url,
                    collect_ts,
                    publish_ts,
                    meta_path,
                    meta_mtime_ns,
                    meta_size,
                    model_json,
                    meta_json,
                    indexed_at,
                    updated_at
                )
                VALUES (
                    %(model_dir)s,
                    %(short_key)s,
                    %(model_id)s,
                    %(title)s,
                    %(source)s,
                    %(origin_url)s,
                    %(author_name)s,
                    %(author_url)s,
                    %(cover_url)s,
                    %(cover_remote_url)s,
                    %(collect_ts)s,
                    %(publish_ts)s,
                    %(meta_path)s,
                    %(meta_mtime_ns)s,
                    %(meta_size)s,
                    %(model_json)s,
                    %(meta_json)s,
                    now(),
                    now()
                )
                ON CONFLICT (model_dir) DO UPDATE SET
                    short_key = CASE
                        WHEN archive_model_index.short_key <> '' THEN archive_model_index.short_key
                        ELSE EXCLUDED.short_key
                    END,
                    model_id = EXCLUDED.model_id,
                    title = EXCLUDED.title,
                    source = EXCLUDED.source,
                    origin_url = EXCLUDED.origin_url,
                    author_name = EXCLUDED.author_name,
                    author_url = EXCLUDED.author_url,
                    cover_url = EXCLUDED.cover_url,
                    cover_remote_url = EXCLUDED.cover_remote_url,
                    collect_ts = EXCLUDED.collect_ts,
                    publish_ts = EXCLUDED.publish_ts,
                    meta_path = EXCLUDED.meta_path,
                    meta_mtime_ns = EXCLUDED.meta_mtime_ns,
                    meta_size = EXCLUDED.meta_size,
                    model_json = EXCLUDED.model_json,
                    meta_json = EXCLUDED.meta_json,
                    updated_at = now()
                """,
                {
                    "model_dir": clean_model_dir,
                    "short_key": short_key,
                    "model_id": str(indexed_model.get("id") or ""),
                    "title": str(indexed_model.get("title") or ""),
                    "source": str(indexed_model.get("source") or ""),
                    "origin_url": str(indexed_model.get("origin_url") or ""),
                    "author_name": str(author.get("name") or ""),
                    "author_url": str(author.get("url") or ""),
                    "cover_url": str(indexed_model.get("cover_url") or ""),
                    "cover_remote_url": str(indexed_model.get("cover_remote_url") or ""),
                    "collect_ts": int(indexed_model.get("collect_ts") or 0),
                    "publish_ts": int(indexed_model.get("publish_ts") or 0),
                    "meta_path": meta_path.as_posix(),
                    "meta_mtime_ns": meta_mtime_ns,
                    "meta_size": meta_size,
                    "model_json": jsonb_value(indexed_model),
                    "meta_json": jsonb_value(meta_payload),
                },
            )
        return True
    except Exception as exc:
        _warn_once(
            "archive_model_index_upsert_failed",
            "归档模型数据库索引写入失败，本次跳过索引写入；模型 meta.json 仍是归档主数据。",
            error=str(exc),
            model_dir=clean_model_dir,
        )
        return False


def delete_archive_model_index(model_dirs: list[str]) -> int:
    clean_dirs = [str(item or "").strip().strip("/") for item in model_dirs]
    clean_dirs = [item for item in clean_dirs if item]
    if not clean_dirs:
        return 0
    if _database_temporarily_unavailable():
        return 0
    try:
        if not ensure_archive_model_index_schema():
            return 0
        with database_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM archive_model_index WHERE model_dir = ANY(%s)",
                (clean_dirs,),
            )
            return int(cursor.rowcount or 0)
    except Exception as exc:
        _warn_once(
            "archive_model_index_delete_failed",
            "归档模型数据库索引删除失败。",
            error=str(exc),
            count=len(clean_dirs),
        )
        return 0


def truncate_archive_model_index() -> bool:
    if _database_temporarily_unavailable():
        return False
    try:
        if not ensure_archive_model_index_schema():
            return False
        with database_connection() as connection:
            connection.execute("TRUNCATE TABLE archive_model_index")
        return True
    except Exception as exc:
        _warn_once(
            "archive_model_index_truncate_failed",
            "归档模型数据库索引清空失败。",
            error=str(exc),
        )
        return False


def _metadata_value(key: str) -> dict[str, Any]:
    if not ensure_archive_model_index_schema():
        return {}
    with database_connection() as connection:
        row = connection.execute(
            "SELECT value FROM makerhub_metadata WHERE key = %s",
            (key,),
        ).fetchone()
    value = row.get("value") if isinstance(row, dict) else None
    return value if isinstance(value, dict) else {}


def _set_metadata_value(key: str, value: dict[str, Any]) -> bool:
    if not ensure_archive_model_index_schema():
        return False
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO makerhub_metadata (key, value, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = now()
            """,
            (key, jsonb_value(value)),
        )
    return True


def archive_model_index_bootstrap_marker() -> dict[str, Any]:
    try:
        return _metadata_value(BOOTSTRAP_METADATA_KEY)
    except Exception:
        return {}


def archive_model_index_is_bootstrapped(archive_root: Path = ARCHIVE_DIR) -> bool:
    marker = archive_model_index_bootstrap_marker()
    if not marker.get("completed"):
        return False
    if int(marker.get("schema_version") or 0) != MODEL_INDEX_SCHEMA_VERSION:
        return False
    marker_root = str(marker.get("archive_root") or "")
    return marker_root == archive_root.resolve().as_posix()


def mark_archive_model_index_bootstrapped(
    *,
    archive_root: Path = ARCHIVE_DIR,
    processed_count: int = 0,
    failed_count: int = 0,
    duration_seconds: float = 0.0,
    forced: bool = False,
) -> bool:
    marker = {
        "completed": True,
        "schema_version": MODEL_INDEX_SCHEMA_VERSION,
        "archive_root": archive_root.resolve().as_posix(),
        "processed_count": int(processed_count or 0),
        "failed_count": int(failed_count or 0),
        "duration_seconds": round(float(duration_seconds or 0.0), 3),
        "forced": bool(forced),
        "completed_at": china_now_iso(),
    }
    try:
        return _set_metadata_value(BOOTSTRAP_METADATA_KEY, marker)
    except Exception as exc:
        _warn_once(
            "archive_model_index_marker_failed",
            "归档模型数据库索引完成标记写入失败。",
            error=str(exc),
        )
        return False


def clear_archive_model_index_bootstrap_marker() -> bool:
    try:
        return _set_metadata_value(
            BOOTSTRAP_METADATA_KEY,
            {
                "completed": False,
                "schema_version": MODEL_INDEX_SCHEMA_VERSION,
                "archive_root": ARCHIVE_DIR.resolve().as_posix(),
                "cleared_at": china_now_iso(),
            },
        )
    except Exception:
        return False


def load_archive_model_index(archive_root: Path = ARCHIVE_DIR) -> Optional[list[dict[str, Any]]]:
    if not archive_model_index_configured():
        return None
    try:
        if not ensure_archive_model_index_schema():
            return None
        if not archive_model_index_is_bootstrapped(archive_root=archive_root):
            return None
        archive_root_path = archive_root.resolve()
        stale_model_dirs: list[str] = []
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT model_dir, model_json, meta_mtime_ns, meta_size FROM archive_model_index ORDER BY model_dir"
            ).fetchall()
    except Exception as exc:
        if not isinstance(exc, DatabaseUnavailable):
            _warn_once(
                "archive_model_index_load_failed",
                "归档模型数据库索引读取失败，将从模型 meta.json 重建当前快照。",
                error=str(exc),
            )
        return None

    models: list[dict[str, Any]] = []
    for row in rows or []:
        model_dir = str(row.get("model_dir") or "").strip().strip("/") if isinstance(row, dict) else ""
        if model_dir:
            meta_path = archive_root_path / model_dir / "meta.json"
            current_mtime_ns, current_size = _meta_signature(meta_path)
            try:
                stored_mtime_ns = int(row.get("meta_mtime_ns") or 0)
                stored_size = int(row.get("meta_size") or 0)
            except (TypeError, ValueError):
                stored_mtime_ns = 0
                stored_size = 0
            if current_mtime_ns != stored_mtime_ns or current_size != stored_size:
                stale_model_dirs.append(model_dir)
        payload = row.get("model_json") if isinstance(row, dict) else None
        if isinstance(payload, dict):
            models.append(dict(payload))
    if stale_model_dirs:
        _request_archive_model_index_rebuild(stale_model_dirs)
        _warn_once(
            "archive_model_index_stale_rows_detected",
            "发现归档模型数据库索引与 meta.json 不一致，已提交 worker 后台重建；本次继续使用数据库快照。",
            count=len(stale_model_dirs),
        )
    return models


def load_archive_model_index_unchecked(archive_root: Path = ARCHIVE_DIR) -> Optional[list[dict[str, Any]]]:
    if not archive_model_index_configured():
        return None
    try:
        if not ensure_archive_model_index_schema():
            return None
        if not archive_model_index_is_bootstrapped(archive_root=archive_root):
            return None
        with database_connection() as connection:
            rows = connection.execute(
                "SELECT model_json FROM archive_model_index ORDER BY model_dir"
            ).fetchall()
    except Exception as exc:
        if not isinstance(exc, DatabaseUnavailable):
            _warn_once(
                "archive_model_index_unchecked_load_failed",
                "归档模型数据库索引快照读取失败。",
                error=str(exc),
            )
        return None

    models: list[dict[str, Any]] = []
    for row in rows or []:
        payload = row.get("model_json") if isinstance(row, dict) else None
        if isinstance(payload, dict):
            models.append(dict(payload))
    return models


def archive_model_index_row_count() -> int:
    try:
        if not ensure_archive_model_index_schema():
            return 0
        with database_connection() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM archive_model_index").fetchone()
        return int((row or {}).get("count") or 0)
    except Exception:
        return 0


def archive_model_index_status(archive_root: Path = ARCHIVE_DIR) -> dict[str, Any]:
    configured = database_configured()
    driver_available = database_driver_available()
    status = {
        "configured": configured,
        "driver_available": driver_available,
        "available": False,
        "bootstrapped": False,
        "schema_version": MODEL_INDEX_SCHEMA_VERSION,
        "row_count": 0,
        "marker": {},
    }
    if not configured or not driver_available:
        return status
    try:
        ensure_archive_model_index_schema()
        marker = archive_model_index_bootstrap_marker()
        status.update(
            {
                "available": True,
                "bootstrapped": archive_model_index_is_bootstrapped(archive_root=archive_root),
                "row_count": archive_model_index_row_count(),
                "marker": marker,
            }
        )
    except Exception as exc:
        status["error"] = str(exc)
    return status
