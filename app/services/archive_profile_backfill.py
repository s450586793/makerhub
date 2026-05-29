import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import ARCHIVE_DIR, STATE_DIR, ensure_app_dirs
from app.core.timezone import now_iso as china_now_iso
from app.services.archive_model_index import (
    archive_model_index_configured,
    archive_model_index_is_bootstrapped,
    archive_model_index_status,
    clear_archive_model_index_bootstrap_marker,
    mark_archive_model_index_bootstrapped,
    truncate_archive_model_index,
    upsert_archive_model_index,
)
from app.services.archive_worker import ArchiveTaskManager
from app.services.business_logs import append_business_log
from app.services.catalog import _normalize_model, invalidate_archive_snapshot
from app.services.database_migration import migrate_json_files_to_database, migrate_log_files_to_database
from app.services.legacy_archiver import PROFILE_DETAIL_SCHEMA_VERSION
from app.services.state_events import publish_state_event


PROFILE_BACKFILL_STATUS_PATH = STATE_DIR / "archive_profile_backfill_status.json"
PROFILE_BACKFILL_STATUS_KEY = "archive_profile_backfill_status"

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


@contextmanager
def _status_file_lock():
    lock_path = PROFILE_BACKFILL_STATUS_PATH.with_name(f"{PROFILE_BACKFILL_STATUS_PATH.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _base_profile_backfill_status() -> dict[str, Any]:
    return {
        "running": False,
        "phase": "idle",
        "database_rebuild_requested": False,
        "force_database_rebuild": False,
        "database_only": False,
        "auto_database_migration": False,
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "last_result": {},
    }


def write_profile_backfill_status(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    with _status_file_lock():
        current = _base_profile_backfill_status()
        existing = load_database_json_state(PROFILE_BACKFILL_STATUS_KEY, {})
        if isinstance(existing, dict):
            current.update(existing)

        current.update(
            {
                "running": bool(payload.get("running", current.get("running"))),
                "phase": str(payload.get("phase", current.get("phase")) or "idle"),
                "database_rebuild_requested": bool(payload.get("database_rebuild_requested", current.get("database_rebuild_requested"))),
                "force_database_rebuild": bool(payload.get("force_database_rebuild", current.get("force_database_rebuild"))),
                "database_only": bool(payload.get("database_only", current.get("database_only"))),
                "auto_database_migration": bool(payload.get("auto_database_migration", current.get("auto_database_migration"))),
                "started_at": str(payload.get("started_at", current.get("started_at")) or ""),
                "finished_at": str(payload.get("finished_at", current.get("finished_at")) or ""),
                "last_error": str(payload.get("last_error", current.get("last_error")) or ""),
                "last_result": payload.get("last_result", current.get("last_result")) if isinstance(payload.get("last_result", current.get("last_result")), dict) else {},
            }
        )
        save_database_json_state(PROFILE_BACKFILL_STATUS_KEY, current)
        publish_state_event(
            PROFILE_BACKFILL_STATUS_KEY,
            "profile_backfill.changed",
            {
                "running": bool(current.get("running")),
                "phase": current.get("phase") or "idle",
                "finished_at": current.get("finished_at") or "",
                "last_error": current.get("last_error") or "",
                "database_only": bool(current.get("database_only")),
            },
        )
        return current


def read_profile_backfill_status() -> dict[str, Any]:
    ensure_app_dirs()
    payload = load_database_json_state(PROFILE_BACKFILL_STATUS_KEY, {})
    status = _base_profile_backfill_status()
    if isinstance(payload, dict):
        status.update(
            {
                "running": bool(payload.get("running")),
                "phase": str(payload.get("phase") or "idle"),
                "database_rebuild_requested": bool(payload.get("database_rebuild_requested")),
                "force_database_rebuild": bool(payload.get("force_database_rebuild")),
                "database_only": bool(payload.get("database_only")),
                "auto_database_migration": bool(payload.get("auto_database_migration")),
                "started_at": str(payload.get("started_at") or ""),
                "finished_at": str(payload.get("finished_at") or ""),
                "last_error": str(payload.get("last_error") or ""),
                "last_result": payload.get("last_result") if isinstance(payload.get("last_result"), dict) else {},
            }
        )
    return status


def _profile_detail_version(instance: dict[str, Any]) -> int:
    details = instance.get("profileDetails") if isinstance(instance.get("profileDetails"), dict) else {}
    for value in (instance.get("profileDetailVersion"), details.get("schemaVersion")):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _asset_has_value(value: Any, keys: tuple[str, ...] = ()) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict):
        return False
    return any(str(value.get(key) or "").strip() for key in keys)


def _instance_has_display_media(instance: dict[str, Any]) -> bool:
    pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
    for picture in pictures:
        if _asset_has_value(
            picture,
            ("url", "originalUrl", "imageUrl", "src", "relPath", "localName", "coverUrl", "previewImage"),
        ):
            return True

    plates = instance.get("plates") if isinstance(instance.get("plates"), list) else []
    for plate in plates:
        if not isinstance(plate, dict):
            continue
        if any(
            str(plate.get(key) or "").strip()
            for key in ("thumbnailUrl", "thumbnailRelPath", "thumbnailFile")
        ):
            return True

    return any(
        str(instance.get(key) or "").strip()
        for key in ("thumbnailUrl", "thumbnail", "thumbnailLocal", "cover", "previewImage")
    )


def _comment_reply_count(item: dict[str, Any]) -> int:
    for value in (
        item.get("replyCount"),
        item.get("reply_count"),
        item.get("subCommentCount"),
        item.get("childrenCount"),
    ):
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 0


def _comment_has_flattened_reply_signal(item: dict[str, Any]) -> bool:
    comment_id = str(item.get("id") or item.get("commentId") or "").strip()
    root_comment_id = str(item.get("rootCommentId") or item.get("root_comment_id") or "").strip()
    if root_comment_id and root_comment_id != comment_id:
        return True
    rating_id = str(item.get("ratingId") or item.get("rating_id") or "").strip()
    if rating_id and rating_id != comment_id:
        return True

    direct_fields = (
        "replyToName",
        "replyUserName",
        "replyNickName",
        "targetUserName",
        "parentAuthor",
        "parentUserName",
        "toUserName",
        "beRepliedUserName",
    )
    if any(str(item.get(field) or "").strip() for field in direct_fields):
        return True

    nested_fields = (
        "replyToUser",
        "replyUser",
        "targetUser",
        "beRepliedUser",
        "parentUser",
        "atUser",
    )
    for field in nested_fields:
        value = item.get(field)
        if not isinstance(value, dict):
            continue
        if any(str(value.get(key) or "").strip() for key in ("nickname", "nickName", "name", "username", "userName")):
            return True

    comment_type = str(item.get("commentType") or item.get("comment_type") or "").strip().lower()
    if comment_type and comment_type not in {"0", "root", "comment", "main"}:
        return True
    return False


def _iter_comment_tree(items: Any):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        yield item
        yield from _iter_comment_tree(item.get("replies"))


def _comment_tree_count(items: Any) -> int:
    return sum(1 for _ in _iter_comment_tree(items))


def _meta_needs_comment_reply_backfill(meta: dict[str, Any]) -> bool:
    comments = meta.get("comments") if isinstance(meta.get("comments"), list) else []
    try:
        expected_comment_count = int(meta.get("commentCount") or 0)
    except (TypeError, ValueError):
        expected_comment_count = 0
    if expected_comment_count > _comment_tree_count(comments):
        return True
    for item in comments:
        if isinstance(item, dict) and _comment_has_flattened_reply_signal(item):
            return True
    for item in _iter_comment_tree(comments):
        reply_count = _comment_reply_count(item)
        if reply_count <= 0:
            continue
        replies = item.get("replies") if isinstance(item.get("replies"), list) else []
        if len(replies) < reply_count:
            return True
    return False


def _meta_needs_profile_backfill(meta: dict[str, Any]) -> bool:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    if not instances:
        return _meta_needs_comment_reply_backfill(meta)
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        if _profile_detail_version(instance) < PROFILE_DETAIL_SCHEMA_VERSION:
            return True
        if not _instance_has_display_media(instance):
            return True
    return _meta_needs_comment_reply_backfill(meta)


def discover_profile_backfill_candidates(archive_root: Path = ARCHIVE_DIR) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for path in sorted(archive_root.rglob("meta.json")):
        if not path.is_file():
            continue
        try:
            meta = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(meta, dict) or not _meta_needs_profile_backfill(meta):
            continue
        url = str(meta.get("url") or "").strip()
        if not url:
            continue
        try:
            model_dir = path.parent.relative_to(archive_root).as_posix()
        except ValueError:
            model_dir = path.parent.name
        candidates.append(
            {
                "url": url,
                "model_dir": model_dir,
                "title": str(meta.get("title") or model_dir),
                "model_id": str(meta.get("id") or ""),
            }
        )
    return candidates


def _count_archive_meta_files(archive_root: Path = ARCHIVE_DIR) -> int:
    return sum(1 for path in archive_root.rglob("meta.json") if path.is_file())


def _write_database_index_progress(result: dict[str, Any]) -> None:
    write_profile_backfill_status(
        {
            "running": True,
            "phase": "database_migration",
            "last_error": "",
            "last_result": {"database_index": dict(result)},
        }
    )


def rebuild_archive_model_database_index(
    *,
    archive_root: Path = ARCHIVE_DIR,
    force: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    if not archive_model_index_configured():
        return {
            "available": False,
            "skipped": True,
            "reason": "Postgres 未配置或驱动未安装。",
            "total": 0,
            "processed": 0,
            "failed": 0,
            "items": [],
        }

    if not force and archive_model_index_is_bootstrapped(archive_root=archive_root):
        return {
            "available": True,
            "skipped": True,
            "forced": False,
            "json_state": {},
            "log_state": {},
            "reason": "数据库索引已迁移完成。",
            "total": _count_archive_meta_files(archive_root=archive_root),
            "processed": 0,
            "updated": 0,
            "failed": 0,
            "items": [],
            "status": archive_model_index_status(archive_root=archive_root),
        }

    total = _count_archive_meta_files(archive_root=archive_root)
    result: dict[str, Any] = {
        "available": True,
        "skipped": False,
        "forced": bool(force),
        "json_state": {},
        "log_state": {},
        "total": total,
        "processed": 0,
        "updated": 0,
        "failed": 0,
        "items": [],
    }
    _write_database_index_progress(result)

    result["json_state"] = migrate_json_files_to_database(force=force)
    _write_database_index_progress(result)
    result["log_state"] = migrate_log_files_to_database()
    _write_database_index_progress(result)

    if force:
        clear_archive_model_index_bootstrap_marker()
        truncate_archive_model_index()

    for meta_path in sorted(archive_root.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        try:
            model = _normalize_model(meta_path, include_detail=False)
            if not model:
                raise ValueError("meta.json 无法解析为模型。")
            model_dir = str(model.get("model_dir") or meta_path.parent.relative_to(archive_root).as_posix())
            ok = upsert_archive_model_index(model_dir, model=model, meta_path=meta_path)
            if not ok:
                raise RuntimeError("数据库写入失败。")
            result["updated"] += 1
        except Exception as exc:
            result["failed"] += 1
            if len(result["items"]) < 50:
                result["items"].append(
                    {
                        "model_dir": meta_path.parent.name,
                        "message": str(exc),
                    }
                )
        finally:
            result["processed"] += 1
            if result["processed"] == total or result["processed"] % 25 == 0:
                _write_database_index_progress(result)

    duration = time.monotonic() - started
    if result["failed"] == 0:
        mark_archive_model_index_bootstrapped(
            archive_root=archive_root,
            processed_count=result["processed"],
            failed_count=result["failed"],
            duration_seconds=duration,
            forced=force,
        )
    else:
        clear_archive_model_index_bootstrap_marker()
    result["duration_seconds"] = round(duration, 3)
    result["status"] = archive_model_index_status(archive_root=archive_root)
    invalidate_archive_snapshot("archive_model_index_rebuilt")
    append_business_log(
        "database",
        "archive_model_index_rebuilt",
        "归档模型数据库索引重建完成。",
        total=result["total"],
        processed=result["processed"],
        updated=result["updated"],
        failed=result["failed"],
        forced=bool(force),
        duration_seconds=result["duration_seconds"],
    )
    return result


def should_auto_run_database_migration(archive_root: Path = ARCHIVE_DIR) -> bool:
    if not archive_model_index_configured():
        return False
    return not archive_model_index_is_bootstrapped(archive_root=archive_root)


def queue_profile_backfill(
    archive_manager: ArchiveTaskManager,
    *,
    archive_root: Path = ARCHIVE_DIR,
    rebuild_database: bool = False,
    force_database_rebuild: bool = False,
    database_only: bool = False,
) -> dict[str, Any]:
    current_status = read_profile_backfill_status()
    started_at = str(current_status.get("started_at") or "") or china_now_iso()
    write_profile_backfill_status(
        {
            "running": True,
            "phase": "database_migration" if rebuild_database else "profile_scan",
            "started_at": started_at,
            "finished_at": "",
            "last_error": "",
            "database_only": bool(database_only),
            "last_result": {},
        }
    )
    result = {
        "database_index": {},
        "scanned_candidates": 0,
        "queued_count": 0,
        "already_queued_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "items": [],
    }
    try:
        if rebuild_database:
            result["database_index"] = rebuild_archive_model_database_index(
                archive_root=archive_root,
                force=force_database_rebuild,
            )
            if database_only:
                finished_at = china_now_iso()
                write_profile_backfill_status(
                    {
                        "running": False,
                        "phase": "completed",
                        "database_rebuild_requested": False,
                        "force_database_rebuild": False,
                        "database_only": False,
                        "auto_database_migration": False,
                        "finished_at": finished_at,
                        "last_error": "",
                        "last_result": result,
                    }
                )
                append_business_log(
                    "database",
                    "archive_model_index_worker_rebuild_completed",
                    "归档模型数据库索引后台重建完成。",
                    processed=int(result["database_index"].get("processed") or 0),
                    updated=int(result["database_index"].get("updated") or 0),
                    failed=int(result["database_index"].get("failed") or 0),
                    skipped=bool(result["database_index"].get("skipped")),
                )
                return {
                    "running": False,
                    "phase": "completed",
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "last_error": "",
                    "last_result": result,
                    "message": "数据库索引重建完成。",
                }
            write_profile_backfill_status(
                {
                    "running": True,
                    "phase": "profile_scan",
                    "database_only": False,
                    "last_result": result,
                }
            )

        candidates = discover_profile_backfill_candidates(archive_root=archive_root)
        result["scanned_candidates"] = len(candidates)
        for item in candidates:
            response = archive_manager.submit_profile_metadata_backfill(
                item.get("url") or "",
                model_dir=item.get("model_dir") or "",
                title=item.get("title") or "",
            )
            result_item = {
                **item,
                "accepted": bool(response.get("accepted")),
                "message": str(response.get("message") or ""),
                "task_id": str(response.get("task_id") or ""),
            }
            result["items"].append(result_item)
            if response.get("accepted"):
                result["queued_count"] += 1
            elif response.get("queued"):
                result["already_queued_count"] += 1
            else:
                result["failed_count"] += 1

        result["skipped_count"] = max(len(candidates) - result["queued_count"] - result["already_queued_count"] - result["failed_count"], 0)
        finished_at = china_now_iso()
        write_profile_backfill_status(
            {
                "running": False,
                "phase": "completed",
                "database_rebuild_requested": False,
                "force_database_rebuild": False,
                "database_only": False,
                "auto_database_migration": False,
                "finished_at": finished_at,
                "last_error": "",
                "last_result": result,
            }
        )
        append_business_log(
            "archive_backfill",
            "profile_backfill_queued",
            "现有库信息补全扫描完成。",
            scanned_candidates=result["scanned_candidates"],
            queued_count=result["queued_count"],
            already_queued_count=result["already_queued_count"],
            failed_count=result["failed_count"],
        )
        return {
            "running": False,
            "phase": "completed",
            "started_at": started_at,
            "finished_at": finished_at,
            "last_error": "",
            "last_result": result,
            "message": (
                f"扫描完成：发现 {result['scanned_candidates']} 个缺信息模型，"
                f"新增入队 {result['queued_count']} 个，已在队列 {result['already_queued_count']} 个。"
                "这些模型会继续在归档队列后台补全。"
            ),
        }
    except Exception as exc:
        finished_at = china_now_iso()
        write_profile_backfill_status(
            {
                "running": False,
                "phase": "failed",
                "database_rebuild_requested": False,
                "force_database_rebuild": False,
                "database_only": False,
                "auto_database_migration": False,
                "finished_at": finished_at,
                "last_error": str(exc),
                "last_result": result,
            }
        )
        append_business_log(
            "archive_backfill",
            "profile_backfill_failed",
            str(exc),
            level="error",
            queued_count=result["queued_count"],
        )
        raise
