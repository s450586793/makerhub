from __future__ import annotations

import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core.settings import ARCHIVE_DIR, STATE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.catalog import get_model_detail, invalidate_archive_snapshot, invalidate_model_detail_cache
from app.services.legacy_archiver import sanitize_filename
from app.services.local_organizer import ORGANIZER_LIBRARY_INDEX_CACHE_PATH
from app.services.task_state import TaskStateStore


LOCAL_REF_KEYS = {
    "relPath",
    "localName",
    "fileName",
    "path",
    "thumbnailLocal",
    "thumbnailFile",
    "avatarRelPath",
    "avatarLocal",
    "cover",
}


def _clean_model_dir(value: str) -> str:
    return str(value or "").strip().strip("/")


def _resolve_model_root(model_dir: str) -> Path:
    clean_value = _clean_model_dir(model_dir)
    if not clean_value:
        raise HTTPException(status_code=400, detail="模型目录不能为空。")

    archive_root = ARCHIVE_DIR.resolve()
    target = (ARCHIVE_DIR / clean_value).resolve()
    try:
        target.relative_to(archive_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="模型目录非法。") from exc

    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"模型目录不存在：{clean_value}")
    return target


def _read_meta(model_root: Path) -> dict[str, Any]:
    meta_path = model_root / "meta.json"
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"模型元数据读取失败：{model_root.name}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"模型元数据格式错误：{model_root.name}")
    return payload


def _write_meta(model_root: Path, payload: dict[str, Any]) -> None:
    meta_path = model_root / "meta.json"
    temp_path = meta_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(meta_path)


def _assert_local_model(model_dir: str) -> dict[str, Any]:
    detail = get_model_detail(model_dir, include_detail=False)
    if not detail:
        raise HTTPException(status_code=404, detail=f"模型不存在：{model_dir}")
    if str(detail.get("source") or "").strip().lower() != "local":
        raise HTTPException(status_code=400, detail="只能合并本地整理导入的模型。")
    return detail


def _unique_destination(parent: Path, raw_name: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    name = sanitize_filename(Path(str(raw_name or "")).name) or "asset"
    suffix = Path(name).suffix
    stem = Path(name).stem or "asset"
    candidate = parent / name
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _copy_tree_files(source_root: Path, target_root: Path, child_dir: str, path_map: dict[str, str]) -> int:
    source_dir = source_root / child_dir
    if not source_dir.exists() or not source_dir.is_dir():
        return 0

    copied_count = 0
    for source_file in sorted(source_dir.rglob("*")):
        if not source_file.is_file():
            continue
        relative = source_file.relative_to(source_root).as_posix()
        relative_parent = source_file.relative_to(source_root).parent
        target_parent = target_root / relative_parent
        target_file = _unique_destination(target_parent, source_file.name)
        shutil.copy2(source_file, target_file)
        target_relative = target_file.relative_to(target_root).as_posix()
        path_map[relative] = target_relative
        path_map[source_file.name] = target_file.name
        if child_dir == "instances":
            path_map[f"instances/{source_file.name}"] = target_relative
        elif child_dir == "images":
            path_map[f"images/{source_file.name}"] = target_relative
        elif child_dir == "attachments":
            path_map[f"attachments/{source_file.name}"] = target_relative
        copied_count += 1
    return copied_count


def _rewrite_local_ref(value: Any, path_map: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw or raw.startswith(("http://", "https://", "data:", "//")):
        return value
    clean = raw.strip("/")
    if clean in path_map:
        return path_map[clean]
    name = Path(clean).name
    if name in path_map:
        return path_map[name]
    return value


def _rewrite_local_refs(value: Any, path_map: dict[str, str], *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            item_key: _rewrite_local_refs(item_value, path_map, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_local_refs(item, path_map, key=key) for item in value]
    if key in LOCAL_REF_KEYS:
        return _rewrite_local_ref(value, path_map)
    return value


def _next_local_instance_id(instances: list[dict[str, Any]]) -> str:
    max_value = 0
    for item in instances:
        raw = str(item.get("id") or "")
        if raw.isdigit():
            max_value = max(max_value, int(raw))
            continue
        if raw.startswith("local-"):
            suffix = raw.split("local-", 1)[-1]
            if suffix.isdigit():
                max_value = max(max_value, int(suffix))
    return f"local-{max_value + 1}"


def _append_unique_items(target_meta: dict[str, Any], source_meta: dict[str, Any], key: str, path_map: dict[str, str]) -> int:
    source_items = source_meta.get(key) if isinstance(source_meta.get(key), list) else []
    if not source_items:
        return 0

    target_items = target_meta.get(key)
    if not isinstance(target_items, list):
        target_items = []
        target_meta[key] = target_items

    seen = {
        json.dumps(item, ensure_ascii=False, sort_keys=True)
        for item in target_items
        if isinstance(item, (dict, list, str))
    }
    added = 0
    for item in source_items:
        rewritten = _rewrite_local_refs(copy.deepcopy(item), path_map)
        identity = json.dumps(rewritten, ensure_ascii=False, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        target_items.append(rewritten)
        added += 1
    return added


def _inherit_tags(target_meta: dict[str, Any], source_meta: dict[str, Any]) -> None:
    for key in ("tags", "tagsOriginal"):
        target_items = [str(item) for item in target_meta.get(key) or [] if str(item).strip()]
        seen = set(target_items)
        for item in source_meta.get(key) or []:
            text = str(item).strip()
            if text and text not in seen:
                target_items.append(text)
                seen.add(text)
        if target_items:
            target_meta[key] = target_items


def _pick_cover_from_source(source_meta: dict[str, Any], path_map: dict[str, str]) -> str:
    cover = _rewrite_local_ref(source_meta.get("cover"), path_map)
    if isinstance(cover, str) and cover.strip():
        return cover.strip()

    instances = source_meta.get("instances") if isinstance(source_meta.get("instances"), list) else []
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        thumbnail = _rewrite_local_ref(instance.get("thumbnailLocal"), path_map)
        if isinstance(thumbnail, str) and thumbnail.strip():
            return thumbnail.strip()
        pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
        for picture in pictures:
            if not isinstance(picture, dict):
                continue
            rel_path = _rewrite_local_ref(picture.get("relPath"), path_map)
            if isinstance(rel_path, str) and rel_path.strip():
                return rel_path.strip()

    design_images = source_meta.get("designImages") if isinstance(source_meta.get("designImages"), list) else []
    for image in design_images:
        if not isinstance(image, dict):
            continue
        rel_path = _rewrite_local_ref(image.get("relPath") or image.get("localName"), path_map)
        if isinstance(rel_path, str) and rel_path.strip():
            return rel_path.strip()
    return ""


def _merge_model_flags(task_store: TaskStateStore, target_model_dir: str, source_model_dirs: list[str]) -> dict:
    flags = task_store.load_model_flags()
    source_set = set(source_model_dirs)
    for flag_name in ("favorites", "printed"):
        values = [str(item).strip().strip("/") for item in flags.get(flag_name) or [] if str(item).strip()]
        should_inherit = target_model_dir in values or any(item in values for item in source_set)
        values = [item for item in values if item not in source_set and item != target_model_dir]
        if should_inherit:
            values.append(target_model_dir)
        flags[flag_name] = values

    deleted_values = [str(item).strip().strip("/") for item in flags.get("deleted") or [] if str(item).strip()]
    flags["deleted"] = [item for item in deleted_values if item not in source_set]
    return task_store.save_model_flags(flags)


def _rewrite_organize_task_model_dirs(task_store: TaskStateStore, target_model_dir: str, source_model_dirs: list[str]) -> dict:
    source_set = set(source_model_dirs)
    payload = task_store.load_organize_tasks()
    items = []
    changed = False
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        next_item = dict(item)
        if str(next_item.get("model_dir") or "").strip().strip("/") in source_set:
            next_item["model_dir"] = target_model_dir
            changed = True
        items.append(next_item)
    if not changed:
        return payload
    return task_store.save_organize_tasks({**payload, "items": items})


def _remove_organizer_index_cache() -> None:
    try:
        ORGANIZER_LIBRARY_INDEX_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        return


def _unique_backup_root(target_root: Path) -> Path:
    stem = f"{int(time.time() * 1000)}_{sanitize_filename(target_root.name) or 'merge'}"
    candidate = STATE_DIR / "model_merge_backups" / stem
    index = 2
    while candidate.exists():
        candidate = STATE_DIR / "model_merge_backups" / f"{stem}_{index}"
        index += 1
    return candidate


def merge_local_models(
    *,
    target_model_dir: str,
    source_model_dirs: list[str],
    title: str = "",
    cover_from_model_dir: str = "",
    task_store: TaskStateStore | None = None,
) -> dict[str, Any]:
    task_store = task_store or TaskStateStore()
    clean_target = _clean_model_dir(target_model_dir)
    clean_sources = []
    for item in source_model_dirs or []:
        clean_item = _clean_model_dir(item)
        if clean_item and clean_item != clean_target and clean_item not in clean_sources:
            clean_sources.append(clean_item)

    if not clean_target or len(clean_sources) < 1:
        raise HTTPException(status_code=400, detail="至少需要选择一个主模型和一个待合并模型。")

    all_model_dirs = [clean_target, *clean_sources]
    if len(all_model_dirs) < 2:
        raise HTTPException(status_code=400, detail="至少需要选择两个模型。")

    target_detail = _assert_local_model(clean_target)
    for source_dir in clean_sources:
        _assert_local_model(source_dir)

    target_root = _resolve_model_root(clean_target)
    target_meta = _read_meta(target_root)
    target_instances = target_meta.get("instances")
    if not isinstance(target_instances, list):
        target_instances = []
        target_meta["instances"] = target_instances

    now_iso = china_now_iso()
    backup_root = _unique_backup_root(target_root)
    merged_sources: list[dict[str, Any]] = []
    copied_file_count = 0
    appended_instance_count = 0
    selected_cover = ""
    requested_cover_model_dir = _clean_model_dir(cover_from_model_dir) or clean_target

    for source_dir in clean_sources:
        source_root = _resolve_model_root(source_dir)
        source_meta = _read_meta(source_root)
        source_title = str(source_meta.get("title") or source_root.name)
        backup_target = backup_root / source_dir
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_root), str(backup_target))

        path_map: dict[str, str] = {}
        for child_dir in ("instances", "images", "attachments"):
            copied_file_count += _copy_tree_files(backup_target, target_root, child_dir, path_map)

        source_instances = source_meta.get("instances") if isinstance(source_meta.get("instances"), list) else []
        for source_instance in source_instances:
            if not isinstance(source_instance, dict):
                continue
            rewritten_instance = _rewrite_local_refs(copy.deepcopy(source_instance), path_map)
            old_file_name = str(source_instance.get("fileName") or "")
            new_file_name = _rewrite_local_ref(old_file_name, path_map)
            if isinstance(new_file_name, str) and new_file_name:
                rewritten_instance["fileName"] = Path(new_file_name).name
                if str(rewritten_instance.get("name") or "") in {"", old_file_name}:
                    rewritten_instance["name"] = Path(new_file_name).name
            rewritten_instance["id"] = _next_local_instance_id(target_instances)
            local_import = rewritten_instance.get("localImport") if isinstance(rewritten_instance.get("localImport"), dict) else {}
            rewritten_instance["localImport"] = {
                **local_import,
                "mergedFromModelDir": source_dir,
                "mergedFromTitle": source_title,
                "mergedAt": now_iso,
            }
            target_instances.append(rewritten_instance)
            appended_instance_count += 1

        for image_key in ("designImages", "summaryImages", "attachments"):
            _append_unique_items(target_meta, source_meta, image_key, path_map)
        _inherit_tags(target_meta, source_meta)

        if requested_cover_model_dir == source_dir:
            selected_cover = _pick_cover_from_source(source_meta, path_map)

        merged_sources.append(
            {
                "model_dir": source_dir,
                "title": source_title,
                "backup_path": backup_target.as_posix(),
                "instance_count": len(source_instances),
            }
        )

    clean_title = str(title or "").strip()
    if clean_title:
        target_meta["title"] = clean_title
    if requested_cover_model_dir == clean_target:
        selected_cover = str(target_meta.get("cover") or "")
    if selected_cover:
        target_meta["cover"] = selected_cover

    target_meta["source"] = "local"
    target_meta["update_time"] = now_iso
    local_import = target_meta.get("localImport") if isinstance(target_meta.get("localImport"), dict) else {}
    existing_merged = local_import.get("mergedModels") if isinstance(local_import.get("mergedModels"), list) else []
    local_import["mergedModels"] = [
        *existing_merged,
        *[
            {
                "modelDir": item["model_dir"],
                "title": item["title"],
                "backupPath": item["backup_path"],
                "mergedAt": now_iso,
            }
            for item in merged_sources
        ],
    ]
    local_import["lastMergedAt"] = now_iso
    target_meta["localImport"] = local_import

    _write_meta(target_root, target_meta)
    flags = _merge_model_flags(task_store, clean_target, clean_sources)
    _rewrite_organize_task_model_dirs(task_store, clean_target, clean_sources)
    _remove_organizer_index_cache()
    invalidate_archive_snapshot("local_model_merge")
    for model_dir in all_model_dirs:
        invalidate_model_detail_cache(model_dir)

    append_business_log(
        "organizer",
        "local_models_merged",
        "本地整理模型已合并。",
        model_dir=clean_target,
        source_model_dirs=clean_sources,
        merged_count=len(clean_sources),
        instance_count=appended_instance_count,
    )

    return {
        "success": True,
        "message": f"已合并 {len(clean_sources)} 个本地模型到「{target_meta.get('title') or target_detail.get('title') or clean_target}」。",
        "target_model_dir": clean_target,
        "source_model_dirs": clean_sources,
        "merged": merged_sources,
        "copied_file_count": copied_file_count,
        "appended_instance_count": appended_instance_count,
        "flags": flags,
    }
