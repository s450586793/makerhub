from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from app.core.settings import ARCHIVE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services.legacy_archiver import sanitize_filename
from app.services.resource_limiter import resource_slot


MODEL_PREVIEW_SUFFIXES = {".3mf", ".obj", ".stl"}
PREVIEW_REL_DIR = "images"
PREVIEW_KIND = "generated_three_preview"
LEGACY_PREVIEW_KIND = "generated_stl_preview"
PREVIEW_VERSION = 2
PREVIEW_TERMINAL_STATUSES = {"failed", "skipped", "too_large", "unsupported"}
PREVIEW_IMAGE_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
PACKAGE_PREVIEW_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
PACKAGE_PREVIEW_LIMIT = 6
PACKAGE_PREVIEW_MAX_BYTES = 8 * 1024 * 1024
PACKAGE_INSTANCE_PREVIEW_VERSION = 1


def _clean_ref(value: Any) -> str:
    return str(value or "").split("#", 1)[0].split("?", 1)[0].strip().lstrip("/")


def _relative_file_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        clean = _clean_ref(value)
        if clean and not clean.startswith(("http://", "https://", "data:", "//")):
            refs.add(clean)
        return refs
    if isinstance(value, dict):
        for key in ("relPath", "localName", "fileName", "path", "thumbnailLocal"):
            raw = value.get(key)
            if isinstance(raw, str):
                refs.update(_relative_file_refs(raw))
    return refs


def _preview_basename(ref: str) -> str:
    return Path(_clean_ref(ref)).name.lower()


def is_generated_preview_item(value: Any) -> bool:
    if isinstance(value, dict):
        kind = str(value.get("kind") or "").strip().lower()
        if kind in {PREVIEW_KIND, LEGACY_PREVIEW_KIND}:
            return True
        if bool(value.get("generated")) and str(value.get("generator") or "").strip().lower() in {"three", "stl"}:
            return True
        if bool(value.get("generated")) and any(is_generated_preview_item(ref) for ref in _relative_file_refs(value)):
            return True
        return any(is_generated_preview_item(ref) for ref in _relative_file_refs(value))

    basename = _preview_basename(str(value or ""))
    return (
        basename.startswith("stl_preview_")
        or basename.startswith("three_preview_")
        or basename.startswith("model_preview_")
    )


def _iter_image_items(meta: dict[str, Any]) -> list[Any]:
    items: list[Any] = []
    for key in ("cover", "designImages", "summaryImages"):
        value = meta.get(key)
        if isinstance(value, list):
            items.extend(value)
        elif value:
            items.append(value)
    return items


def meta_has_user_images(meta: dict[str, Any]) -> bool:
    for item in _iter_image_items(meta):
        refs = _relative_file_refs(item)
        if not refs and isinstance(item, str) and item.strip().startswith(("http://", "https://", "data:")):
            return True
        if any(not is_generated_preview_item(ref) for ref in refs):
            return True
        if isinstance(item, dict) and not refs and not is_generated_preview_item(item):
            url = str(item.get("url") or item.get("originalUrl") or "").strip()
            if url:
                return True
    return False


def meta_uses_legacy_generated_preview(meta: dict[str, Any]) -> bool:
    for item in _iter_image_items(meta):
        if isinstance(item, dict) and str(item.get("kind") or "").strip().lower() == LEGACY_PREVIEW_KIND:
            return True
        if any(_preview_basename(ref).startswith("stl_preview_") for ref in _relative_file_refs(item)):
            return True
    return False


def _is_previewable_file_name(value: Any) -> bool:
    return Path(str(value or "")).suffix.lower() in MODEL_PREVIEW_SUFFIXES


def _instance_key(item: dict[str, Any], index: int) -> str:
    return str(item.get("id") or item.get("profileId") or index + 1)


def local_model_metadata_slot(model_root: Path):
    return resource_slot(f"local_model_metadata:{model_root.resolve().as_posix()}")


def image_cover_priority(item: Any) -> int:
    if is_generated_preview_item(item):
        return 100
    if isinstance(item, dict):
        names = [item.get(key) for key in ("originalName", "fileName", "relPath", "localName", "path")]
    else:
        names = [item]
    name = " ".join(Path(str(value or "")).stem.lower() for value in names)
    if re.search(r"(?:^|[^a-z])(?:instructions?|dimensions?|drawings?|diagram|sizing)(?:$|[^a-z])", name) or any(
        word in name for word in ("尺寸", "说明", "步骤", "装配图", "工程图", "支撑")
    ):
        return 20
    if re.search(r"(?:^|[^a-z])(?:cover|main|complete|assembled|finished|render|preview)(?:$|[^a-z])", name) or any(
        word in name for word in ("成品", "整机", "全身", "渲染", "封面", "效果", "预览")
    ):
        return 0
    return 10


def has_manual_local_model_cover(meta: dict[str, Any]) -> bool:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    return bool(meta.get("cover")) and local_import.get("coverSelection") == "manual"


def promote_local_model_cover(meta: dict[str, Any]) -> bool:
    if has_manual_local_model_cover(meta):
        return False
    cover = meta.get("cover")
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    # 既有原图没有选择来源标记时，保留可能由用户设定的封面。
    if cover and not is_generated_preview_item(cover) and local_import.get("coverSelection") != "automatic":
        return False
    images = meta.get("designImages") if isinstance(meta.get("designImages"), list) else []
    candidates = [item for item in images if _first_relative_file_ref([item])]
    if not candidates:
        return False
    selected = min(candidates, key=image_cover_priority)
    selected_ref = _first_relative_file_ref([selected])
    if selected_ref == cover:
        return False
    meta["cover"] = selected_ref
    meta["designImages"] = [selected, *(item for item in images if item is not selected)]
    _local_import_meta(meta)["coverSelection"] = "automatic"
    return True


def _preview_instance_priority(item: dict[str, Any]) -> int:
    name = Path(str(item.get("fileName") or item.get("name") or "")).stem.lower()
    is_part = re.search(r"(?:^|[^a-z])(?:arm|leg|head|hand|bottom|base|section|part)(?:$|[^a-z])", name) or any(
        word in name for word in ("零件", "底壳", "底座", "手臂", "躯干")
    )
    if is_part:
        return 20
    if re.search(r"(?:^|[^a-z])(?:complete|assembled|full|whole)(?:$|[^a-z])", name) or any(
        word in name for word in ("整机", "完整", "整体", "合体", "组装完成")
    ):
        return 0
    return 10


def first_previewable_instance(meta: dict[str, Any], model_root: Path | None = None) -> dict[str, str]:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    ordered_instances = sorted(
        ((index, item) for index, item in enumerate(instances) if isinstance(item, dict)),
        key=lambda indexed: _preview_instance_priority(indexed[1]),
    )
    for index, item in ordered_instances:
        if not isinstance(item, dict):
            continue
        file_name = Path(str(item.get("fileName") or item.get("name") or "")).name
        if not file_name or not _is_previewable_file_name(file_name):
            continue
        if model_root is not None and not (model_root / "instances" / file_name).is_file():
            continue
        suffix = Path(file_name).suffix.lower().lstrip(".")
        return {
            "instance_key": _instance_key(item, index),
            "file_name": file_name,
            "file_kind": str(item.get("fileKind") or suffix.upper() or "文件"),
        }
    return {}


def _local_import_meta(meta: dict[str, Any]) -> dict[str, Any]:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    meta["localImport"] = local_import
    return local_import


def _ref_exists(model_root: Path, ref: str) -> bool:
    clean = _clean_ref(ref)
    if not clean:
        return False
    try:
        target = (model_root / clean).resolve()
        target.relative_to(model_root.resolve())
    except ValueError:
        return False
    return target.is_file()


def _has_current_three_preview(meta: dict[str, Any], model_root: Path) -> bool:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    if str(local_import.get("previewGenerator") or "").strip().lower() != "three":
        return False
    if int(local_import.get("previewVersion") or 0) < PREVIEW_VERSION:
        return False
    if str(local_import.get("previewStatus") or "").strip().lower() != "success":
        return False
    preview_file = _clean_ref(local_import.get("previewFile"))
    if preview_file and _ref_exists(model_root, preview_file):
        return True
    return any(
        is_generated_preview_item(item) and any(_ref_exists(model_root, ref) for ref in _relative_file_refs(item))
        for item in _iter_image_items(meta)
    )


def mark_local_preview_not_needed(meta: dict[str, Any]) -> bool:
    local_import = _local_import_meta(meta)
    changed = False
    if bool(local_import.get("previewNeedsGeneration")):
        local_import["previewNeedsGeneration"] = False
        changed = True
    if str(local_import.get("previewStatus") or "").strip().lower() in {"pending", "running"}:
        local_import["previewStatus"] = "skipped"
        changed = True
    return changed


def _update_preview_pending_state(meta: dict[str, Any], *, model_root: Path | None = None) -> bool:
    if str(meta.get("source") or "").strip().lower() != "local":
        return False
    local_import = _local_import_meta(meta)

    if meta_has_user_images(meta) or has_manual_local_model_cover(meta):
        return mark_local_preview_not_needed(meta)

    candidate = first_previewable_instance(meta, model_root=model_root)
    if not candidate:
        if str(local_import.get("previewStatus") or "").strip().lower() != "unsupported":
            local_import.update(
                {
                    "previewGenerator": "three",
                    "previewVersion": PREVIEW_VERSION,
                    "previewStatus": "unsupported",
                    "previewNeedsGeneration": False,
                    "previewError": "没有找到可用的 3MF / STL / OBJ 模型文件。",
                }
            )
            return True
        return False

    if model_root is not None and _has_current_three_preview(meta, model_root):
        changed = False
        updates = {
            "previewGenerator": "three",
            "previewVersion": PREVIEW_VERSION,
            "previewStatus": "success",
            "previewNeedsGeneration": False,
            "previewSourceFileName": local_import.get("previewSourceFileName") or candidate.get("file_name") or "",
        }
        for key, value in updates.items():
            if local_import.get(key) != value:
                local_import[key] = value
                changed = True
        return changed

    current_status = str(local_import.get("previewStatus") or "").strip().lower()
    current_version = int(local_import.get("previewVersion") or 0)
    if current_status == "running" and current_version >= PREVIEW_VERSION:
        return False
    if current_status in PREVIEW_TERMINAL_STATUSES and current_version >= PREVIEW_VERSION:
        if bool(local_import.get("previewNeedsGeneration")):
            local_import["previewNeedsGeneration"] = False
            return True
        return False

    updates = {
        "previewGenerator": "three",
        "previewVersion": PREVIEW_VERSION,
        "previewStatus": "pending",
        "previewNeedsGeneration": True,
        "previewSourceFileName": candidate.get("file_name") or "",
    }
    changed = False
    for key, value in updates.items():
        if local_import.get(key) != value:
            local_import[key] = value
            changed = True
    return changed


def mark_local_preview_pending(meta: dict[str, Any], *, model_root: Path | None = None) -> bool:
    repaired = _repair_generated_preview_instance_refs(meta)
    if model_root is not None:
        repaired = repair_package_instance_preview_images(model_root, meta) or repaired
    return _update_preview_pending_state(meta, model_root=model_root) or repaired


def build_local_preview_state(meta: dict[str, Any], model_root: Path) -> dict[str, Any]:
    if str(meta.get("source") or "").strip().lower() != "local":
        return {}

    generated_refs_repaired = _repair_generated_preview_instance_refs(meta)
    package_previews_repaired = repair_package_instance_preview_images(model_root, meta)
    preview_state_changed = _update_preview_pending_state(meta, model_root=model_root)
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    status = str(local_import.get("previewStatus") or "").strip().lower()
    version = int(local_import.get("previewVersion") or 0)
    candidate = first_previewable_instance(meta, model_root=model_root)
    has_user_images = meta_has_user_images(meta)
    has_current_preview = _has_current_three_preview(meta, model_root)
    uses_legacy_preview = meta_uses_legacy_generated_preview(meta)
    terminal = status in PREVIEW_TERMINAL_STATUSES and version >= PREVIEW_VERSION
    needs_generation = bool(candidate) and not has_user_images and not has_manual_local_model_cover(meta) and not terminal and (uses_legacy_preview or not has_current_preview)

    if needs_generation and status not in {"pending", "running"}:
        status = "pending"
    elif has_current_preview:
        status = "success"
    elif not candidate:
        status = "unsupported"
    elif not status:
        status = "idle"

    return {
        "generator": "three",
        "version": PREVIEW_VERSION,
        "status": status,
        "needs_generation": needs_generation,
        "has_user_images": has_user_images,
        "has_generated_preview": has_current_preview,
        "uses_legacy_preview": uses_legacy_preview,
        "candidate": candidate,
        "message": str(local_import.get("previewError") or ""),
        "generated_at": str(local_import.get("previewGeneratedAt") or ""),
        "metadata_changed": generated_refs_repaired or package_previews_repaired or preview_state_changed,
    }


def _generated_preview_filename(source_file_name: str, mime_type: str) -> str:
    suffix = PREVIEW_IMAGE_MIME_TYPES.get(mime_type, ".png")
    stem = sanitize_filename(Path(str(source_file_name or "")).stem).strip() or "model"
    return f"three_preview_{stem}{suffix}"


def _target_preview_instance(
    meta: dict[str, Any],
    *,
    source_instance_key: str,
    source_file_name: str,
) -> dict[str, Any] | None:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    clean_key = str(source_instance_key or "").strip()
    clean_file_name = Path(str(source_file_name or "")).name

    for index, instance in enumerate(instances):
        if isinstance(instance, dict) and clean_key and _instance_key(instance, index) == clean_key:
            return instance
    for instance in instances:
        if not isinstance(instance, dict) or not clean_file_name:
            continue
        file_name = Path(str(instance.get("fileName") or instance.get("name") or "")).name
        if file_name == clean_file_name:
            return instance
    if clean_key or clean_file_name:
        return None
    return next((instance for instance in instances if isinstance(instance, dict)), None)


def _first_relative_file_ref(items: list[Any]) -> str:
    for item in items:
        if isinstance(item, str):
            clean = _clean_ref(item)
            if clean:
                return clean
            continue
        if not isinstance(item, dict):
            continue
        for key in ("relPath", "localName", "fileName", "path", "thumbnailLocal"):
            clean = _clean_ref(item.get(key))
            if clean:
                return clean
    return ""


def _generated_preview_targets_instance(
    value: Any,
    *,
    instance: dict[str, Any],
    index: int,
    local_import: dict[str, Any],
) -> bool:
    item = value if isinstance(value, dict) else {}
    source_key = str(item.get("sourceInstanceKey") or local_import.get("previewSourceInstanceKey") or "").strip()
    if source_key and _instance_key(instance, index) == source_key:
        return True

    source_file_name = Path(
        str(item.get("sourceFileName") or local_import.get("previewSourceFileName") or "")
    ).name
    if source_file_name:
        instance_file_name = Path(str(instance.get("fileName") or instance.get("name") or "")).name
        return instance_file_name == source_file_name
    return not source_key and index == 0


def _repair_generated_preview_instance_refs(meta: dict[str, Any]) -> bool:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    changed = False
    for index, instance in enumerate(instances):
        if not isinstance(instance, dict):
            continue
        pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
        kept_pictures = [
            picture
            for picture in pictures
            if not is_generated_preview_item(picture)
            or _generated_preview_targets_instance(
                picture,
                instance=instance,
                index=index,
                local_import=local_import,
            )
        ]
        if kept_pictures != pictures:
            instance["pictures"] = kept_pictures
            changed = True

        thumbnail = instance.get("thumbnailLocal")
        if is_generated_preview_item(thumbnail) and not _generated_preview_targets_instance(
            thumbnail,
            instance=instance,
            index=index,
            local_import=local_import,
        ):
            instance["thumbnailLocal"] = _first_relative_file_ref(kept_pictures)
            changed = True
    return changed


def _unlink_generated_preview_files(model_root: Path, meta: dict[str, Any]) -> None:
    refs: set[str] = set()
    for item in _iter_image_items(meta):
        if is_generated_preview_item(item):
            refs.update(_relative_file_refs(item))
    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        if is_generated_preview_item(instance.get("thumbnailLocal")):
            refs.update(_relative_file_refs(instance.get("thumbnailLocal")))
        for picture in instance.get("pictures") or []:
            if is_generated_preview_item(picture):
                refs.update(_relative_file_refs(picture))
    for ref in refs:
        clean = _clean_ref(ref)
        if not clean:
            continue
        try:
            target = (model_root / clean).resolve()
            target.relative_to(model_root.resolve())
        except ValueError:
            continue
        if target.is_file():
            target.unlink(missing_ok=True)


def apply_generated_preview_image(
    *,
    model_root: Path,
    meta: dict[str, Any],
    image_bytes: bytes,
    mime_type: str = "image/png",
    source_file_name: str = "",
    source_instance_key: str = "",
) -> dict[str, Any]:
    clean_mime = str(mime_type or "image/png").split(";", 1)[0].strip().lower() or "image/png"
    if clean_mime not in PREVIEW_IMAGE_MIME_TYPES:
        raise ValueError("只支持保存 PNG、JPG、WEBP 预览图。")
    if not image_bytes:
        raise ValueError("预览图为空。")
    if len(image_bytes) > 8 * 1024 * 1024:
        raise ValueError("预览图过大。")

    if has_manual_local_model_cover(meta) and is_generated_preview_item(meta.get("cover")):
        cover_ref = _first_relative_file_ref([meta.get("cover")])
        for item in meta.get("designImages") or []:
            if cover_ref in _relative_file_refs(item):
                return dict(item) if isinstance(item, dict) else {"relPath": cover_ref}
        return {"relPath": cover_ref}

    preserve_cover = has_manual_local_model_cover(meta) or meta_has_user_images(meta)
    _unlink_generated_preview_files(model_root, meta)
    images_dir = model_root / PREVIEW_REL_DIR
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / _generated_preview_filename(source_file_name, clean_mime)
    temp_path = target.with_name(f".{target.name}.saving")
    try:
        temp_path.write_bytes(image_bytes)
        temp_path.replace(target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    rel_path = f"{PREVIEW_REL_DIR}/{target.name}"
    now_iso = china_now_iso()
    image_item = {
        "relPath": rel_path,
        "fileName": target.name,
        "kind": PREVIEW_KIND,
        "generated": True,
        "generator": "three",
        "previewVersion": PREVIEW_VERSION,
        "sourceFileName": Path(str(source_file_name or "")).name,
        "sourceInstanceKey": str(source_instance_key or ""),
        "mimeType": clean_mime,
        "size": len(image_bytes),
        "generatedAt": now_iso,
    }

    existing_images = meta.get("designImages") if isinstance(meta.get("designImages"), list) else []
    user_images = [item for item in existing_images if not is_generated_preview_item(item)]
    if preserve_cover:
        meta["designImages"] = [*user_images, image_item]
        promote_local_model_cover(meta)
    else:
        meta["cover"] = rel_path
        meta["designImages"] = [image_item, *user_images]
        _local_import_meta(meta)["coverSelection"] = "automatic"

    target_instance = _target_preview_instance(
        meta,
        source_instance_key=source_instance_key,
        source_file_name=source_file_name,
    )
    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
        user_pictures = [item for item in pictures if not is_generated_preview_item(item)]
        instance["pictures"] = [image_item, *user_pictures] if instance is target_instance else user_pictures
        thumbnail = str(instance.get("thumbnailLocal") or "").strip()
        if instance is target_instance and (not thumbnail or is_generated_preview_item(thumbnail)):
            instance["thumbnailLocal"] = rel_path
        elif instance is not target_instance and is_generated_preview_item(thumbnail):
            instance["thumbnailLocal"] = _first_relative_file_ref(user_pictures)

    local_import = _local_import_meta(meta)
    local_import.update(
        {
            "previewGenerator": "three",
            "previewVersion": PREVIEW_VERSION,
            "previewStatus": "success",
            "previewNeedsGeneration": False,
            "previewGeneratedAt": now_iso,
            "previewFile": rel_path,
            "previewSourceFileName": Path(str(source_file_name or "")).name,
            "previewSourceInstanceKey": str(source_instance_key or ""),
            "previewError": "",
        }
    )
    meta["localImport"] = local_import
    return image_item


def record_generated_preview_failure(
    meta: dict[str, Any],
    *,
    message: str,
    status: str = "failed",
    source_file_name: str = "",
    source_instance_key: str = "",
) -> dict[str, Any]:
    clean_status = str(status or "failed").strip().lower()
    if clean_status not in PREVIEW_TERMINAL_STATUSES:
        clean_status = "failed"
    local_import = _local_import_meta(meta)
    local_import.update(
        {
            "previewGenerator": "three",
            "previewVersion": PREVIEW_VERSION,
            "previewStatus": clean_status,
            "previewNeedsGeneration": False,
            "previewFailedAt": china_now_iso(),
            "previewError": str(message or "Three.js 预览图生成失败。").strip()[:400],
            "previewSourceFileName": Path(str(source_file_name or "")).name,
            "previewSourceInstanceKey": str(source_instance_key or ""),
        }
    )
    meta["localImport"] = local_import
    return local_import


def ensure_package_preview_images(
    *,
    model_root: Path,
    model_files: list[dict[str, Any]],
    image_paths: list[str],
    title: str,
) -> list[str]:
    images_dir = model_root / PREVIEW_REL_DIR
    for item in model_files:
        target_path = Path(str(item.get("target_path") or ""))
        item["preview_paths"] = _extract_three_mf_preview_images(target_path, images_dir)
    return sorted(image_paths, key=image_cover_priority)


def repair_package_instance_preview_images(model_root: Path, meta: dict[str, Any]) -> bool:
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    if not bool(local_import.get("package")):
        return False
    if int(local_import.get("instancePreviewVersion") or 0) >= PACKAGE_INSTANCE_PREVIEW_VERSION:
        return False

    package_refs: set[str] = set()
    for item in _iter_image_items(meta):
        package_refs.update(_relative_file_refs(item))

    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        file_name = Path(str(instance.get("fileName") or instance.get("name") or "")).name
        if Path(file_name).suffix.lower() != ".3mf":
            continue

        current_refs = _relative_file_refs(instance.get("thumbnailLocal"))
        for picture in instance.get("pictures") or []:
            if not is_generated_preview_item(picture):
                current_refs.update(_relative_file_refs(picture))
        if current_refs and not current_refs.issubset(package_refs):
            continue

        preview_paths = _extract_three_mf_preview_images(model_root / "instances" / file_name, model_root / PREVIEW_REL_DIR)
        if not preview_paths:
            continue
        instance["thumbnailLocal"] = preview_paths[0]
        instance["pictures"] = [{"relPath": preview_path} for preview_path in preview_paths]

    local_import["instancePreviewVersion"] = PACKAGE_INSTANCE_PREVIEW_VERSION
    meta["localImport"] = local_import
    return True


def _package_preview_priority(info: zipfile.ZipInfo) -> tuple[int, str]:
    name = info.filename.lower()
    if "thumbnail" in name:
        score = 0
    elif "cover" in name or "preview" in name:
        score = 1
    elif "plate" in name:
        score = 2
    elif "metadata" in name:
        score = 3
    else:
        score = 100
    return (score, name)


def _unique_package_preview_target(images_dir: Path, source_path: Path, member_name: str) -> Path:
    source_stem = sanitize_filename(source_path.stem).strip() or "model"
    member_path = Path(member_name)
    member_stem = sanitize_filename(member_path.stem).strip() or "preview"
    suffix = member_path.suffix.lower() or ".png"
    target = images_dir / f"{source_stem}_{member_stem}{suffix}"
    index = 2
    while target.exists():
        target = images_dir / f"{source_stem}_{member_stem}_{index}{suffix}"
        index += 1
    return target


def _extract_three_mf_preview_images(source_path: Path, images_dir: Path) -> list[str]:
    if not source_path.is_file() or source_path.suffix.lower() != ".3mf":
        return []

    try:
        with zipfile.ZipFile(source_path) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and Path(info.filename).suffix.lower() in PACKAGE_PREVIEW_SUFFIXES
                and 0 < info.file_size <= PACKAGE_PREVIEW_MAX_BYTES
            ]
            members.sort(key=_package_preview_priority)
            preview_paths: list[str] = []
            for info in members[:PACKAGE_PREVIEW_LIMIT]:
                with archive.open(info) as handle:
                    data = handle.read(PACKAGE_PREVIEW_MAX_BYTES + 1)
                if not data or len(data) > PACKAGE_PREVIEW_MAX_BYTES:
                    continue
                images_dir.mkdir(parents=True, exist_ok=True)
                target = _unique_package_preview_target(images_dir, source_path, Path(info.filename).name)
                target.write_bytes(data)
                preview_paths.append(f"{PREVIEW_REL_DIR}/{target.name}")
            return preview_paths
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return []


def ensure_local_model_preview(model_root: Path) -> bool:
    meta_path = model_root / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(meta, dict):
        return False
    if not mark_local_preview_pending(meta, model_root=model_root):
        return False
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    try:
        model_dir = model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        model_dir = model_root.name
    from app.services.catalog import upsert_archive_snapshot_model

    upsert_archive_snapshot_model(model_dir, "local_preview_pending", broadcast=False)
    return True
