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
from app.services.business_logs import append_business_log
from app.services.catalog import _normalize_model, invalidate_archive_snapshot
from app.services.state_events import publish_state_event


ARCHIVE_MODEL_INDEX_REBUILD_STATUS_PATH = STATE_DIR / "archive_model_index_rebuild_status.json"
ARCHIVE_MODEL_INDEX_REBUILD_STATUS_KEY = "archive_model_index_rebuild_status"

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


@contextmanager
def _status_file_lock():
    lock_path = ARCHIVE_MODEL_INDEX_REBUILD_STATUS_PATH.with_name(
        f"{ARCHIVE_MODEL_INDEX_REBUILD_STATUS_PATH.name}.lock"
    )
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


def _base_archive_model_index_rebuild_status() -> dict[str, Any]:
    return {
        "running": False,
        "phase": "idle",
        "force": False,
        "auto": False,
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "last_result": {},
    }


def write_archive_model_index_rebuild_status(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    with _status_file_lock():
        current = _base_archive_model_index_rebuild_status()
        existing = load_database_json_state(ARCHIVE_MODEL_INDEX_REBUILD_STATUS_KEY, {})
        if isinstance(existing, dict):
            current.update(existing)

        last_result = payload.get("last_result", current.get("last_result"))
        current.update(
            {
                "running": bool(payload.get("running", current.get("running"))),
                "phase": str(payload.get("phase", current.get("phase")) or "idle"),
                "force": bool(payload.get("force", current.get("force"))),
                "auto": bool(payload.get("auto", current.get("auto"))),
                "started_at": str(payload.get("started_at", current.get("started_at")) or ""),
                "finished_at": str(payload.get("finished_at", current.get("finished_at")) or ""),
                "last_error": str(payload.get("last_error", current.get("last_error")) or ""),
                "last_result": last_result if isinstance(last_result, dict) else {},
            }
        )
        save_database_json_state(ARCHIVE_MODEL_INDEX_REBUILD_STATUS_KEY, current)
        publish_state_event(
            ARCHIVE_MODEL_INDEX_REBUILD_STATUS_KEY,
            "archive_model_index_rebuild.changed",
            {
                "running": bool(current.get("running")),
                "phase": current.get("phase") or "idle",
                "finished_at": current.get("finished_at") or "",
                "last_error": current.get("last_error") or "",
            },
        )
        return current


def read_archive_model_index_rebuild_status() -> dict[str, Any]:
    ensure_app_dirs()
    payload = load_database_json_state(ARCHIVE_MODEL_INDEX_REBUILD_STATUS_KEY, {})
    status = _base_archive_model_index_rebuild_status()
    if isinstance(payload, dict):
        status.update(
            {
                "running": bool(payload.get("running")),
                "phase": str(payload.get("phase") or "idle"),
                "force": bool(payload.get("force")),
                "auto": bool(payload.get("auto")),
                "started_at": str(payload.get("started_at") or ""),
                "finished_at": str(payload.get("finished_at") or ""),
                "last_error": str(payload.get("last_error") or ""),
                "last_result": payload.get("last_result") if isinstance(payload.get("last_result"), dict) else {},
            }
        )
    return status


def _count_archive_meta_files(archive_root: Path = ARCHIVE_DIR) -> int:
    return sum(1 for path in archive_root.rglob("meta.json") if path.is_file())


def _write_database_index_progress(result: dict[str, Any]) -> None:
    write_archive_model_index_rebuild_status(
        {
            "running": True,
            "phase": "database_index_rebuild",
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
            "reason": "数据库索引已完成。",
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
        "total": total,
        "processed": 0,
        "updated": 0,
        "failed": 0,
        "items": [],
    }
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


def should_auto_rebuild_database_index(archive_root: Path = ARCHIVE_DIR) -> bool:
    if not archive_model_index_configured():
        return False
    if archive_model_index_is_bootstrapped(archive_root=archive_root):
        return False

    # 历史 meta 文件损坏时，不能在每次 worker 重启后重复整库重建。
    try:
        status = read_archive_model_index_rebuild_status()
    except Exception:
        return True
    database_result = status.get("last_result", {}).get("database_index", {})
    try:
        failed_count = int(database_result.get("failed") or 0)
    except (AttributeError, TypeError, ValueError):
        failed_count = 0
    return not (str(status.get("phase") or "") == "completed" and failed_count > 0)


def request_archive_model_index_rebuild(
    *,
    force: bool = False,
    auto: bool = False,
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = write_archive_model_index_rebuild_status(
        {
            "running": True,
            "phase": "database_index_rebuild",
            "force": bool(force),
            "auto": bool(auto),
            "started_at": china_now_iso(),
            "finished_at": "",
            "last_error": "",
            "last_result": {
                "database_index": {
                    "requested_by": str(reason or "manual"),
                    **(details if isinstance(details, dict) else {}),
                }
            },
        }
    )
    append_business_log(
        "database",
        "archive_model_index_rebuild_queued",
        "归档模型数据库索引已提交后台重建。",
        force=bool(force),
        auto=bool(auto),
        reason=str(reason or ""),
    )
    return status


def run_archive_model_index_rebuild(force: bool = False, archive_root: Path = ARCHIVE_DIR) -> dict[str, Any]:
    current_status = read_archive_model_index_rebuild_status()
    started_at = str(current_status.get("started_at") or "") or china_now_iso()
    force_rebuild = bool(force or current_status.get("force"))
    auto = bool(current_status.get("auto"))
    write_archive_model_index_rebuild_status(
        {
            "running": True,
            "phase": "database_index_rebuild",
            "force": force_rebuild,
            "auto": auto,
            "started_at": started_at,
            "finished_at": "",
            "last_error": "",
        }
    )
    try:
        result = rebuild_archive_model_database_index(
            archive_root=archive_root,
            force=force_rebuild,
        )
        finished_at = china_now_iso()
        write_archive_model_index_rebuild_status(
            {
                "running": False,
                "phase": "completed",
                "force": False,
                "auto": False,
                "finished_at": finished_at,
                "last_error": "",
                "last_result": {"database_index": result},
            }
        )
        append_business_log(
            "database",
            "archive_model_index_worker_rebuild_completed",
            "归档模型数据库索引后台重建完成。",
            processed=int(result.get("processed") or 0),
            updated=int(result.get("updated") or 0),
            failed=int(result.get("failed") or 0),
            skipped=bool(result.get("skipped")),
        )
        return {
            "running": False,
            "phase": "completed",
            "started_at": started_at,
            "finished_at": finished_at,
            "last_error": "",
            "last_result": {"database_index": result},
            "message": "数据库索引重建完成。",
        }
    except Exception as exc:
        finished_at = china_now_iso()
        write_archive_model_index_rebuild_status(
            {
                "running": False,
                "phase": "failed",
                "force": False,
                "auto": False,
                "finished_at": finished_at,
                "last_error": str(exc),
            }
        )
        append_business_log(
            "database",
            "archive_model_index_rebuild_failed",
            str(exc),
            level="error",
        )
        raise
