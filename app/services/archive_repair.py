import json
from pathlib import Path
from typing import Any, Optional

from app.core.settings import ARCHIVE_DIR
from app.services.business_logs import append_business_log
from app.services.catalog import invalidate_archive_snapshot, invalidate_model_detail_cache
from app.services.remote_refresh import _build_missing_3mf_items
from app.services.task_state import TaskStateStore
from app.services.three_mf import resolve_model_instance_files


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
    missing_items = _build_missing_3mf_items(meta_path, meta)
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


def repair_archive_3mf_mappings(
    *,
    archive_root: Path = ARCHIVE_DIR,
    task_store: Optional[TaskStateStore] = None,
) -> dict[str, Any]:
    store = task_store or TaskStateStore()
    stats = {
        "scanned_models": 0,
        "repaired_models": 0,
        "repaired_instances": 0,
        "repaired_instances_strong": 0,
        "repaired_instances_weak": 0,
        "missing_after_repair": 0,
        "failed_models": 0,
        "changed_model_dirs": [],
    }

    append_business_log("archive_repair", "repair_started", "开始扫描全库 3MF 映射。", archive_root=archive_root)
    for meta_path in sorted(archive_root.rglob("meta.json")):
        if not meta_path.is_file():
            continue
        stats["scanned_models"] += 1
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
            store.replace_missing_3mf_for_model(model_id, result.get("missing_items") or [])

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
    )
    return stats
