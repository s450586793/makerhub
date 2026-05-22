from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from app.core.database import (
    database_configured,
    database_driver_available,
    load_json_state,
    save_json_state,
)
from app.core.settings import CONFIG_PATH, STATE_DIR
from app.services.business_logs import append_business_log
from app.services.source_library import SOURCE_LIBRARY_METADATA_PATH
from app.services.subscriptions import COOKIE_SOURCE_INVENTORY_PATH, COOKIE_SOURCE_SYNC_STATE_PATH
from app.services.task_state import (
    ARCHIVE_QUEUE_PATH,
    MISSING_3MF_PATH,
    MODEL_FLAGS_PATH,
    ORGANIZE_TASKS_PATH,
    REMOTE_REFRESH_STATE_PATH,
    SUBSCRIPTIONS_STATE_PATH,
    THREE_MF_LIMIT_GUARD_PATH,
)


JSON_STATE_FILE_MIGRATIONS: tuple[tuple[str, Path, dict[str, Any]], ...] = (
    ("app_config", CONFIG_PATH, {}),
    ("archive_queue", ARCHIVE_QUEUE_PATH, {"active": [], "queued": [], "recent_failures": []}),
    ("missing_3mf", MISSING_3MF_PATH, {"items": []}),
    ("organize_tasks", ORGANIZE_TASKS_PATH, {"items": []}),
    ("model_flags", MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []}),
    ("subscriptions_state", SUBSCRIPTIONS_STATE_PATH, {"items": []}),
    ("remote_refresh_state", REMOTE_REFRESH_STATE_PATH, {}),
    ("three_mf_limit_guard", THREE_MF_LIMIT_GUARD_PATH, {}),
    ("cookie_source_sync_state", COOKIE_SOURCE_SYNC_STATE_PATH, {}),
    ("cookie_source_inventory", COOKIE_SOURCE_INVENTORY_PATH, {"platforms": {}, "updated_at": ""}),
    ("source_library_metadata", SOURCE_LIBRARY_METADATA_PATH, {"items": {}, "updated_at": ""}),
    ("model_shares", STATE_DIR / "model_shares.json", {"items": []}),
)
BAMBU_STUDIO_SECRET_STATE_KEY = "bambu_studio_download_secret"
BAMBU_STUDIO_SECRET_PATH = STATE_DIR / "bambu_studio_download_secret"


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _json_state_exists(key: str) -> bool:
    try:
        value = load_json_state(key)
    except Exception:
        return False
    return value is not None


def migrate_json_files_to_database(*, force: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "available": bool(database_configured() and database_driver_available()),
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "items": [],
    }
    if not result["available"]:
        result["reason"] = "Postgres 未配置或驱动未安装。"
        return result

    for key, path, default in JSON_STATE_FILE_MIGRATIONS:
        item = {"key": key, "path": path.as_posix(), "status": "skipped"}
        try:
            result["processed"] += 1
            if not force and _json_state_exists(key):
                result["skipped"] += 1
                item["status"] = "exists"
                result["items"].append(item)
                continue
            payload = _read_json_file(path, default)
            save_json_state(key, payload)
            result["updated"] += 1
            item["status"] = "updated"
            item["count"] = len(payload.get("items") or []) if isinstance(payload.get("items"), list) else len(payload)
        except Exception as exc:
            result["failed"] += 1
            item["status"] = "failed"
            item["message"] = str(exc)
        finally:
            result["items"].append(item)

    secret_item = {"key": BAMBU_STUDIO_SECRET_STATE_KEY, "path": BAMBU_STUDIO_SECRET_PATH.as_posix(), "status": "skipped"}
    try:
        result["processed"] += 1
        if not force and _json_state_exists(BAMBU_STUDIO_SECRET_STATE_KEY):
            result["skipped"] += 1
            secret_item["status"] = "exists"
        else:
            try:
                secret = BAMBU_STUDIO_SECRET_PATH.read_text(encoding="utf-8").strip()
            except OSError:
                secret = ""
            if not secret:
                secret = secrets.token_urlsafe(32)
            save_json_state(BAMBU_STUDIO_SECRET_STATE_KEY, {"secret": secret})
            result["updated"] += 1
            secret_item["status"] = "updated"
    except Exception as exc:
        result["failed"] += 1
        secret_item["status"] = "failed"
        secret_item["message"] = str(exc)
    finally:
        result["items"].append(secret_item)

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    append_business_log(
        "database",
        "json_state_migrated",
        "结构化运行状态已迁移到 Postgres。",
        processed=result["processed"],
        updated=result["updated"],
        skipped=result["skipped"],
        failed=result["failed"],
        duration_seconds=result["duration_seconds"],
    )
    return result
