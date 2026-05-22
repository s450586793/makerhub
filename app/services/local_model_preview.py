from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.legacy_archiver import sanitize_filename


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


def first_previewable_instance(meta: dict[str, Any], model_root: Path | None = None) -> dict[str, str]:
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    for index, item in enumerate(instances):
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


def _update_preview_pending_state(meta: dict[str, Any], *, model_root: Path | None = None) -> bool:
    if str(meta.get("source") or "").strip().lower() != "local":
        return False
    local_import = _local_import_meta(meta)

    if meta_has_user_images(meta):
        changed = False
        if bool(local_import.get("previewNeedsGeneration")):
            local_import["previewNeedsGeneration"] = False
            changed = True
        if str(local_import.get("previewStatus") or "").strip().lower() in {"pending", "running"}:
            local_import["previewStatus"] = "skipped"
            changed = True
        return changed

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
            "previewSourceFileName": candidate.get("file_name") or "",
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
    return _update_preview_pending_state(meta, model_root=model_root)


def build_local_preview_state(meta: dict[str, Any], model_root: Path) -> dict[str, Any]:
    if str(meta.get("source") or "").strip().lower() != "local":
        return {}

    _update_preview_pending_state(meta, model_root=model_root)
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    status = str(local_import.get("previewStatus") or "").strip().lower()
    version = int(local_import.get("previewVersion") or 0)
    candidate = first_previewable_instance(meta, model_root=model_root)
    has_user_images = meta_has_user_images(meta)
    has_current_preview = _has_current_three_preview(meta, model_root)
    uses_legacy_preview = meta_uses_legacy_generated_preview(meta)
    terminal = status in PREVIEW_TERMINAL_STATUSES and version >= PREVIEW_VERSION
    needs_generation = bool(candidate) and not has_user_images and not terminal and (uses_legacy_preview or not has_current_preview)

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
    }


def _generated_preview_filename(source_file_name: str, mime_type: str) -> str:
    suffix = PREVIEW_IMAGE_MIME_TYPES.get(mime_type, ".png")
    stem = sanitize_filename(Path(str(source_file_name or "")).stem).strip() or "model"
    return f"three_preview_{stem}{suffix}"


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
    meta["cover"] = rel_path
    meta["designImages"] = [image_item, *user_images]

    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
        user_pictures = [item for item in pictures if not is_generated_preview_item(item)]
        instance["pictures"] = [image_item, *user_pictures]
        thumbnail = str(instance.get("thumbnailLocal") or "").strip()
        if not thumbnail or is_generated_preview_item(thumbnail):
            instance["thumbnailLocal"] = rel_path

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
    return image_paths


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
    return True
