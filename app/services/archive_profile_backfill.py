import json
from pathlib import Path
from typing import Any

from app.core.settings import ARCHIVE_DIR, STATE_DIR, ensure_app_dirs
from app.core.timezone import now_iso as china_now_iso
from app.services.archive_worker import ArchiveTaskManager
from app.services.business_logs import append_business_log
from app.services.legacy_archiver import PROFILE_DETAIL_SCHEMA_VERSION


PROFILE_BACKFILL_STATUS_PATH = STATE_DIR / "archive_profile_backfill_status.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _base_profile_backfill_status() -> dict[str, Any]:
    return {
        "running": False,
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "last_result": {},
    }


def write_profile_backfill_status(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    current = _base_profile_backfill_status()
    if PROFILE_BACKFILL_STATUS_PATH.exists():
        try:
            existing = _read_json(PROFILE_BACKFILL_STATUS_PATH)
            if isinstance(existing, dict):
                current.update(existing)
        except (OSError, json.JSONDecodeError):
            pass

    current.update(
        {
            "running": bool(payload.get("running", current.get("running"))),
            "started_at": str(payload.get("started_at", current.get("started_at")) or ""),
            "finished_at": str(payload.get("finished_at", current.get("finished_at")) or ""),
            "last_error": str(payload.get("last_error", current.get("last_error")) or ""),
            "last_result": payload.get("last_result", current.get("last_result")) if isinstance(payload.get("last_result", current.get("last_result")), dict) else {},
        }
    )
    _write_json(PROFILE_BACKFILL_STATUS_PATH, current)
    return current


def read_profile_backfill_status() -> dict[str, Any]:
    ensure_app_dirs()
    if not PROFILE_BACKFILL_STATUS_PATH.exists():
        return _base_profile_backfill_status()
    try:
        payload = _read_json(PROFILE_BACKFILL_STATUS_PATH)
    except (OSError, json.JSONDecodeError):
        return _base_profile_backfill_status()
    status = _base_profile_backfill_status()
    if isinstance(payload, dict):
        status.update(
            {
                "running": bool(payload.get("running")),
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


def _meta_needs_profile_backfill(meta: dict[str, Any]) -> bool:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    if not instances:
        return False
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        if _profile_detail_version(instance) < PROFILE_DETAIL_SCHEMA_VERSION:
            return True
        if not _instance_has_display_media(instance):
            return True
    return False


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


def queue_profile_backfill(
    archive_manager: ArchiveTaskManager,
    *,
    archive_root: Path = ARCHIVE_DIR,
) -> dict[str, Any]:
    started_at = china_now_iso()
    write_profile_backfill_status(
        {
            "running": True,
            "started_at": started_at,
            "finished_at": "",
            "last_error": "",
            "last_result": {},
        }
    )
    candidates = discover_profile_backfill_candidates(archive_root=archive_root)
    result = {
        "scanned_candidates": len(candidates),
        "queued_count": 0,
        "already_queued_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "items": [],
    }
    try:
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
