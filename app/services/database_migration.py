from __future__ import annotations

import json
import os
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
from app.core.settings import CONFIG_DIR, CONFIG_PATH, LOGS_DIR, STATE_DIR
from app.services.business_logs import append_business_log, append_database_log_entry, _parse_log_line
from app.services.source_library import SOURCE_LIBRARY_METADATA_PATH
from app.services.subscriptions import COOKIE_SOURCE_INVENTORY_PATH, COOKIE_SOURCE_SYNC_STATE_PATH
from app.services.task_state import (
    ARCHIVE_QUEUE_PATH,
    MISSING_3MF_PATH,
    MODEL_FLAGS_PATH,
    ORGANIZE_TASKS_PATH,
    REMOTE_REFRESH_STATE_PATH,
    SOURCE_REFRESH_QUEUE_PATH,
    SOURCE_REFRESH_RUNS_PATH,
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
    ("source_refresh_queue", SOURCE_REFRESH_QUEUE_PATH, {"version": 1, "active": [], "queued": [], "recent_failures": [], "updated_at": ""}),
    ("source_refresh_runs", SOURCE_REFRESH_RUNS_PATH, {"version": 1, "active_run": {}, "last_completed_run": {}, "updated_at": ""}),
    ("three_mf_limit_guard", THREE_MF_LIMIT_GUARD_PATH, {}),
    ("cookie_source_sync_state", COOKIE_SOURCE_SYNC_STATE_PATH, {}),
    ("cookie_source_inventory", COOKIE_SOURCE_INVENTORY_PATH, {"platforms": {}, "updated_at": ""}),
    ("source_library_metadata", SOURCE_LIBRARY_METADATA_PATH, {"items": {}, "updated_at": ""}),
    ("model_shares", STATE_DIR / "model_shares.json", {"items": []}),
    ("archive_repair_status", STATE_DIR / "archive_repair_status.json", {}),
    ("archive_profile_backfill_status", STATE_DIR / "archive_profile_backfill_status.json", {}),
    ("system_update", STATE_DIR / "system_update.json", {}),
    ("auth_sessions", STATE_DIR / "auth_sessions.json", {"items": []}),
    ("three_mf_daily_quota", STATE_DIR / "three_mf_daily_quota.json", {"items": {}}),
    ("archive_snapshot_marker", STATE_DIR / "archive_snapshot.marker", {}),
    ("local_preview_queue_marker", STATE_DIR / "local_preview_queue.marker", {}),
)
LEGACY_CONFIG_PATH = CONFIG_DIR.parent / "config.json"
LEGACY_STATE_DIR = Path(os.getenv("MAKERHUB_LEGACY_STATE_DIR", "/app/state"))
LEGACY_LOGS_DIR = Path(os.getenv("MAKERHUB_LEGACY_LOGS_DIR", "/app/logs"))
BAMBU_STUDIO_SECRET_STATE_KEY = "bambu_studio_download_secret"
BAMBU_STUDIO_SECRET_PATH = STATE_DIR / "bambu_studio_download_secret"
LOG_FILE_IMPORT_LIMIT = 200_000
RUNTIME_JSON_STATE_KEYS = frozenset(
    {
        "archive_queue",
        "missing_3mf",
        "organize_tasks",
        "subscriptions_state",
        "remote_refresh_state",
        "source_refresh_queue",
        "source_refresh_runs",
        "three_mf_limit_guard",
        "three_mf_daily_quota",
        "archive_repair_status",
        "archive_profile_backfill_status",
        "system_update",
    }
)
RUNTIME_RESTORE_MARKERS = frozenset({"_makerhub_restore", "makerhub_restore"})


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default)
    return payload if isinstance(payload, dict) else dict(default)


def _read_marker_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    payload = _read_json_file(path, default)
    if payload:
        return payload
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return dict(default)
    if not token:
        return dict(default)
    return {
        "token": token,
        "reason": "legacy_marker",
        "updated_at": "",
    }


def _config_migration_paths(path: Path) -> list[Path]:
    if path != CONFIG_PATH:
        return [path]
    paths = [path]
    try:
        duplicate = LEGACY_CONFIG_PATH.resolve() == path.resolve()
    except OSError:
        duplicate = LEGACY_CONFIG_PATH == path
    if not duplicate:
        paths.append(LEGACY_CONFIG_PATH)
    return paths


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        try:
            key = path.resolve().as_posix()
        except OSError:
            key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _state_migration_paths(path: Path) -> list[Path]:
    paths = [path]
    if path.name:
        paths.append(LEGACY_STATE_DIR / path.name)
    return _unique_paths(paths)


def _migration_paths_for_key(key: str, path: Path) -> list[Path]:
    if key == "app_config":
        return _config_migration_paths(path)
    return _state_migration_paths(path)


def _payload_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        return json.dumps(left, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return left == right


def _payload_has_user_data(payload: dict[str, Any], default: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if default and _payload_equivalent(payload, default):
        return False
    if not default:
        return True
    for value in payload.values():
        if isinstance(value, (list, dict)) and value:
            return True
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            return True
    return False


def _payload_requests_runtime_restore(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(bool(payload.get(marker)) for marker in RUNTIME_RESTORE_MARKERS)


def _strip_runtime_restore_markers(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    clean = dict(payload)
    for marker in RUNTIME_RESTORE_MARKERS:
        clean.pop(marker, None)
    return clean


def _read_json_migration_payload(key: str, path: Path, default: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    read_paths = _migration_paths_for_key(key, path)
    candidates: list[tuple[dict[str, Any], Path, bool]] = []
    for candidate in read_paths:
        exists = candidate.exists()
        payload = (
            _read_marker_file(candidate, default)
            if key in {"archive_snapshot_marker", "local_preview_queue_marker"}
            else _read_json_file(candidate, default)
        )
        candidates.append((payload, candidate, exists))
        if exists and _payload_has_user_data(payload, default):
            return payload, candidate
    for payload, candidate, exists in candidates:
        if exists and not _payload_equivalent(payload, default):
            return payload, candidate
    for payload, candidate, exists in candidates:
        if exists:
            return payload, candidate
    return dict(default), read_paths[0]


def _load_existing_json_state(key: str) -> Any:
    try:
        return load_json_state(key)
    except Exception:
        return None


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
            payload, source_path = _read_json_migration_payload(key, path, default)
            item["path"] = source_path.as_posix()
            if force and key in RUNTIME_JSON_STATE_KEYS:
                existing = _load_existing_json_state(key)
                existing_payload = existing if isinstance(existing, dict) else {}
                existing_has_data = _payload_has_user_data(existing_payload, default)
                if existing_has_data and not _payload_requests_runtime_restore(payload):
                    result["skipped"] += 1
                    item["status"] = "protected_runtime_state"
                    continue
                if _payload_requests_runtime_restore(payload):
                    payload = _strip_runtime_restore_markers(payload)
                    item["status"] = "restored_runtime_state"
            if not force:
                existing = _load_existing_json_state(key)
                if existing is not None:
                    existing_payload = existing if isinstance(existing, dict) else {}
                    source_has_data = _payload_has_user_data(payload, default)
                    existing_has_data = _payload_has_user_data(existing_payload, default)
                    if existing_has_data or not source_has_data:
                        result["skipped"] += 1
                        item["status"] = "exists"
                        item["path"] = source_path.as_posix()
                        continue
                    item["status"] = "backfilled"
            save_json_state(key, payload)
            result["updated"] += 1
            if item["status"] == "skipped":
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
        items=result["items"],
    )
    return result


def migrate_log_files_to_database(*, limit_per_file: int = LOG_FILE_IMPORT_LIMIT) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "available": bool(database_configured() and database_driver_available()),
        "processed_files": 0,
        "processed_lines": 0,
        "updated": 0,
        "failed": 0,
        "items": [],
    }
    if not result["available"]:
        result["reason"] = "Postgres 未配置或驱动未安装。"
        return result

    log_paths: list[Path] = []
    for logs_dir in _unique_paths([LOGS_DIR, LEGACY_LOGS_DIR]):
        try:
            log_paths.extend(path for path in logs_dir.glob("*.log") if path.is_file())
        except OSError:
            continue
    log_paths = sorted(_unique_paths(log_paths))

    for path in log_paths:
        item = {
            "file": path.name,
            "processed": 0,
            "updated": 0,
            "failed": 0,
        }
        try:
            result["processed_files"] += 1
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if line_number > max(int(limit_per_file or 0), 1):
                        break
                    raw = raw_line.rstrip("\n")
                    if not raw.strip():
                        continue
                    item["processed"] += 1
                    result["processed_lines"] += 1
                    parsed = _parse_log_line(raw, path.name)
                    entry = {
                        "time": parsed.get("time") or "",
                        "level": parsed.get("level") or "info",
                        "category": parsed.get("category") or path.stem,
                        "event": parsed.get("event") or "line",
                        "message": parsed.get("message") or "",
                    }
                    payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}
                    entry.update(payload)
                    if append_database_log_entry(path.name, entry, raw=raw):
                        item["updated"] += 1
                        result["updated"] += 1
        except Exception as exc:
            item["failed"] += 1
            result["failed"] += 1
            item["message"] = str(exc)
        finally:
            if len(result["items"]) < 50:
                result["items"].append(item)

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    append_business_log(
        "database",
        "log_files_migrated",
        "历史日志已迁移到 Postgres。",
        processed_files=result["processed_files"],
        processed_lines=result["processed_lines"],
        updated=result["updated"],
        failed=result["failed"],
        duration_seconds=result["duration_seconds"],
    )
    return result
