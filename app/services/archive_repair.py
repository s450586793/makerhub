import json
import os
from pathlib import Path
from typing import Any, Optional

from app.core.settings import ARCHIVE_DIR, STATE_DIR, ensure_app_dirs
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.catalog import invalidate_archive_snapshot, invalidate_model_detail_cache
from app.services.remote_refresh import _build_missing_3mf_items
from app.services.task_state import TaskStateStore
from app.services.three_mf import resolve_model_instance_files


ARCHIVE_REPAIR_STATUS_PATH = STATE_DIR / "archive_repair_status.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return china_now_iso()


def _base_archive_repair_status() -> dict[str, Any]:
    return {
        "running": False,
        "started_at": "",
        "finished_at": "",
        "last_error": "",
        "run_id": "",
        "pid": 0,
        "last_result": {},
        "progress": {},
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def write_archive_repair_status(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    current = _base_archive_repair_status()
    if ARCHIVE_REPAIR_STATUS_PATH.exists():
        try:
            existing = _read_json(ARCHIVE_REPAIR_STATUS_PATH)
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
            "run_id": str(payload.get("run_id", current.get("run_id")) or ""),
            "pid": int(payload.get("pid", current.get("pid")) or 0),
            "last_result": payload.get("last_result", current.get("last_result")) if isinstance(payload.get("last_result", current.get("last_result")), dict) else {},
            "progress": payload.get("progress", current.get("progress")) if isinstance(payload.get("progress", current.get("progress")), dict) else {},
        }
    )
    _write_json(ARCHIVE_REPAIR_STATUS_PATH, current)
    return current


def read_archive_repair_status() -> dict[str, Any]:
    ensure_app_dirs()
    if not ARCHIVE_REPAIR_STATUS_PATH.exists():
        return _base_archive_repair_status()

    try:
        payload = _read_json(ARCHIVE_REPAIR_STATUS_PATH)
    except (OSError, json.JSONDecodeError):
        return _base_archive_repair_status()

    status = _base_archive_repair_status()
    if isinstance(payload, dict):
        status.update(
            {
                "running": bool(payload.get("running")),
                "started_at": str(payload.get("started_at") or ""),
                "finished_at": str(payload.get("finished_at") or ""),
                "last_error": str(payload.get("last_error") or ""),
                "run_id": str(payload.get("run_id") or ""),
                "pid": int(payload.get("pid") or 0),
                "last_result": payload.get("last_result") if isinstance(payload.get("last_result"), dict) else {},
                "progress": payload.get("progress") if isinstance(payload.get("progress"), dict) else {},
            }
        )

    if status.get("running") and status.get("pid") and not _pid_alive(int(status.get("pid") or 0)):
        status["running"] = False
        status["finished_at"] = status.get("finished_at") or _now_iso()
        if not status.get("last_error") and not status.get("last_result"):
            status["last_error"] = "修复进程已退出，状态未正常回写。"
        write_archive_repair_status(status)

    return status


def repair_model_instance_files(meta_path: Path) -> dict[str, Any]:
    try:
        meta = _read_json(meta_path)
    except (OSError, json.JSONDecodeError):
        return {
            "ok": False,
            "model_dir": meta_path.parent.name,
            "repaired_instances": 0,
            "repaired_instances_strong": 0,
            "repaired_instances_weak": 0,
            "missing_count": 0,
            "message": "meta.json 读取失败",
        }

    if not isinstance(meta, dict):
        return {
            "ok": False,
            "model_dir": meta_path.parent.name,
            "repaired_instances": 0,
            "repaired_instances_strong": 0,
            "repaired_instances_weak": 0,
            "missing_count": 0,
            "message": "meta.json 结构无效",
        }

    model_root = meta_path.parent
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    if not instances:
        return {
            "ok": True,
            "model_dir": model_root.relative_to(ARCHIVE_DIR).as_posix(),
            "repaired_instances": 0,
            "repaired_instances_strong": 0,
            "repaired_instances_weak": 0,
            "missing_count": 0,
            "changed": False,
        }

    resolved = resolve_model_instance_files(meta, model_root)
    matches = resolved.get("matches") if isinstance(resolved, dict) else {}
    changed = False
    repaired_instances = 0
    repaired_instances_strong = 0
    repaired_instances_weak = 0

    for index, instance in enumerate(instances):
        if not isinstance(instance, dict):
            continue
        match = matches.get(index) if isinstance(matches, dict) else None
        if not isinstance(match, dict):
            continue
        path = match.get("path")
        if not isinstance(path, Path) or not path.exists():
            continue
        actual_name = path.name
        current_name = Path(str(instance.get("fileName") or "")).name
        if current_name == actual_name:
            continue
        instance["fileName"] = actual_name
        changed = True
        repaired_instances += 1
        if str(match.get("confidence") or "") == "weak":
            repaired_instances_weak += 1
        else:
            repaired_instances_strong += 1

    if changed:
        _write_json(meta_path, meta)

    model_id = str(meta.get("id") or "").strip()
    missing_items = _build_missing_3mf_items(meta_path, meta, resolved_files=resolved)
    result = {
        "ok": True,
        "model_dir": model_root.relative_to(ARCHIVE_DIR).as_posix(),
        "model_id": model_id,
        "repaired_instances": repaired_instances,
        "repaired_instances_strong": repaired_instances_strong,
        "repaired_instances_weak": repaired_instances_weak,
        "missing_count": len(missing_items),
        "changed": changed,
        "unmatched_instances": len(resolved.get("unmatched_instance_indexes") or []),
        "inventory_count": int(resolved.get("inventory_count") or 0),
        "missing_items": missing_items,
    }
    return result


def _meta_needs_3mf_repair(meta_path: Path, meta: dict[str, Any], flagged_model_ids: set[str]) -> bool:
    model_id = str(meta.get("id") or "").strip()
    if model_id and model_id in flagged_model_ids:
        return True

    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    if not instances:
        return False

    instances_dir = meta_path.parent / "instances"
    if not instances_dir.exists():
        return True

    for instance in instances:
        if not isinstance(instance, dict):
            continue
        file_name = Path(str(instance.get("fileName") or "")).name
        if not file_name:
            return True
        if not (instances_dir / file_name).exists():
            return True

    return False


def repair_archive_3mf_mappings(
    *,
    archive_root: Path = ARCHIVE_DIR,
    task_store: Optional[TaskStateStore] = None,
    status_updater=None,
) -> dict[str, Any]:
    store = task_store or TaskStateStore()
    flagged_model_ids = {
        str(item.get("model_id") or "").strip()
        for item in (store.load_missing_3mf().get("items") or [])
        if isinstance(item, dict) and str(item.get("model_id") or "").strip()
    }
    meta_paths: list[Path] = []
    skipped_models = 0
    for path in sorted(archive_root.rglob("meta.json")):
        if not path.is_file():
            continue
        try:
            meta = _read_json(path)
        except (OSError, json.JSONDecodeError):
            meta_paths.append(path)
            continue
        if not isinstance(meta, dict):
            meta_paths.append(path)
            continue
        if _meta_needs_3mf_repair(path, meta, flagged_model_ids):
            meta_paths.append(path)
        else:
            skipped_models += 1
    stats = {
        "total_models": len(meta_paths),
        "skipped_models": skipped_models,
        "scanned_models": 0,
        "repaired_models": 0,
        "repaired_instances": 0,
        "repaired_instances_strong": 0,
        "repaired_instances_weak": 0,
        "missing_after_repair": 0,
        "failed_models": 0,
        "changed_model_dirs": [],
    }
    pending_missing_updates: dict[str, list[dict]] = {}

    def _flush_missing_updates() -> None:
        if pending_missing_updates:
            store.replace_missing_3mf_for_models(dict(pending_missing_updates))
            pending_missing_updates.clear()

    def _emit_progress(current_model_dir: str = "") -> None:
        if not status_updater:
            return
        status_updater(
            {
                "progress": {
                    "total_models": stats["total_models"],
                    "scanned_models": stats["scanned_models"],
                    "repaired_models": stats["repaired_models"],
                    "repaired_instances": stats["repaired_instances"],
                    "failed_models": stats["failed_models"],
                    "skipped_models": stats["skipped_models"],
                    "current_model_dir": current_model_dir,
                }
            }
        )

    append_business_log("archive_repair", "repair_started", "开始扫描全库 3MF 映射。", archive_root=archive_root)
    _emit_progress()
    for meta_path in meta_paths:
        stats["scanned_models"] += 1
        current_model_dir = meta_path.parent.relative_to(ARCHIVE_DIR).as_posix()
        result = repair_model_instance_files(meta_path)
        if not result.get("ok"):
            stats["failed_models"] += 1
            append_business_log(
                "archive_repair",
                "model_failed",
                str(result.get("message") or "模型 3MF 映射修复失败。"),
                level="error",
                model_dir=result.get("model_dir") or meta_path.parent.name,
            )
            if stats["scanned_models"] % 10 == 0 or stats["scanned_models"] == stats["total_models"]:
                _emit_progress(current_model_dir)
            continue

        if result.get("changed"):
            stats["repaired_models"] += 1
            stats["changed_model_dirs"].append(result.get("model_dir"))
            invalidate_model_detail_cache(str(result.get("model_dir") or ""))

        stats["repaired_instances"] += int(result.get("repaired_instances") or 0)
        stats["repaired_instances_strong"] += int(result.get("repaired_instances_strong") or 0)
        stats["repaired_instances_weak"] += int(result.get("repaired_instances_weak") or 0)
        stats["missing_after_repair"] += int(result.get("missing_count") or 0)

        model_id = str(result.get("model_id") or "").strip()
        if model_id:
            pending_missing_updates[model_id] = list(result.get("missing_items") or [])

        if len(pending_missing_updates) >= 25:
            _flush_missing_updates()

        if stats["scanned_models"] % 10 == 0 or stats["scanned_models"] == stats["total_models"]:
            _emit_progress(current_model_dir)

    _flush_missing_updates()

    if stats["repaired_models"] or stats["repaired_instances"]:
        invalidate_archive_snapshot("archive_repair_completed")

    append_business_log(
        "archive_repair",
        "repair_finished",
        "全库 3MF 映射扫描完成。",
        scanned_models=stats["scanned_models"],
        repaired_models=stats["repaired_models"],
        repaired_instances=stats["repaired_instances"],
        repaired_instances_strong=stats["repaired_instances_strong"],
        repaired_instances_weak=stats["repaired_instances_weak"],
        missing_after_repair=stats["missing_after_repair"],
        failed_models=stats["failed_models"],
        skipped_models=stats["skipped_models"],
    )
    return stats


def run_archive_repair_job(run_id: str, started_at: str = "") -> None:
    effective_started_at = str(started_at or "").strip() or _now_iso()
    write_archive_repair_status(
        {
            "running": True,
            "started_at": effective_started_at,
            "finished_at": "",
            "last_error": "",
            "run_id": run_id,
            "pid": os.getpid(),
            "last_result": {},
            "progress": {},
        }
    )

    try:
        def _status_updater(payload: dict[str, Any]) -> None:
            write_archive_repair_status(
                {
                    "running": True,
                    "started_at": effective_started_at,
                    "finished_at": "",
                    "last_error": "",
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "last_result": {},
                    "progress": payload.get("progress") if isinstance(payload.get("progress"), dict) else {},
                }
            )

        result = repair_archive_3mf_mappings(
            task_store=TaskStateStore(),
            status_updater=_status_updater,
        )
    except Exception as exc:
        write_archive_repair_status(
            {
                "running": False,
                "started_at": effective_started_at,
                "finished_at": _now_iso(),
                "last_error": str(exc),
                "run_id": run_id,
                "pid": os.getpid(),
                "last_result": {},
                "progress": {},
            }
        )
        append_business_log(
            "archive_repair",
            "repair_failed",
            f"全库 3MF 映射修复失败：{exc}",
            level="error",
            run_id=run_id,
        )
        raise

    write_archive_repair_status(
        {
            "running": False,
            "started_at": effective_started_at,
            "finished_at": _now_iso(),
            "last_error": "",
            "run_id": run_id,
            "pid": os.getpid(),
            "last_result": result,
            "progress": {},
        }
    )
