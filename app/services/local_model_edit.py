from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from app.core.settings import ARCHIVE_DIR, MAX_LOCAL_IMPORT_UPLOAD_BYTES, MAX_MANUAL_ATTACHMENT_BYTES
from app.core.timezone import now_iso as china_now_iso
from app.services.catalog import invalidate_archive_snapshot, invalidate_model_detail_cache


MODEL_SUFFIXES = {".3mf", ".stl", ".step", ".stp", ".obj"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def _summary_html_from_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    paragraphs = [part.strip() for part in clean.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [clean]
    return "".join(f"<p>{html.escape(part)}</p>" for part in paragraphs)


def _safe_filename(filename: str, fallback: str = "file") -> str:
    raw = Path(str(filename or "").strip()).name
    fallback_name = Path(fallback).name
    suffix = Path(raw).suffix.lower() or Path(fallback_name).suffix.lower()
    stem = Path(raw).stem.strip() or Path(fallback).stem or "file"
    safe_stem = re.sub(r"[^\w()\-\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE).strip("._")
    safe_stem = safe_stem or Path(fallback).stem or "file"
    safe_suffix = re.sub(r"[^.\w]+", "", suffix)[:16]
    return f"{safe_stem}{safe_suffix}"


def _resolve_local_model_root(model_dir: str) -> tuple[Path, dict[str, Any]]:
    clean_value = str(model_dir or "").strip().strip("/")
    if not clean_value:
        raise ValueError("模型不存在。")

    archive_root = ARCHIVE_DIR.resolve()
    model_root = (ARCHIVE_DIR / clean_value).resolve()
    try:
        model_root.relative_to(archive_root)
    except ValueError as exc:
        raise ValueError("非法模型路径。") from exc

    meta_path = model_root / "meta.json"
    if not model_root.exists() or not model_root.is_dir() or not meta_path.exists():
        raise ValueError("模型不存在。")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("模型元数据无法读取。") from exc

    source = str(meta.get("source") or "").strip().lower()
    try:
        relative_dir = model_root.relative_to(archive_root)
    except ValueError:
        relative_dir = Path(clean_value)
    if source != "local" and (not relative_dir.parts or relative_dir.parts[0] != "local"):
        raise ValueError("只有本地导入模型支持编辑。")
    return model_root, meta


def _write_meta(model_root: Path, meta: dict[str, Any]) -> None:
    meta["update_time"] = china_now_iso()
    (model_root / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    model_dir = model_root.relative_to(ARCHIVE_DIR.resolve()).as_posix()
    invalidate_model_detail_cache(model_dir)
    invalidate_archive_snapshot("local_model_edited")


def _unique_path(target_dir: Path, filename: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(filename)
    stem = Path(name).stem or "file"
    suffix = Path(name).suffix
    candidate = target_dir / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = target_dir / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _copy_upload(upload: UploadFile, target_path: Path, max_bytes: int = MAX_MANUAL_ATTACHMENT_BYTES) -> tuple[int, str]:
    total_size = 0
    digest = hashlib.sha256()
    upload.file.seek(0)
    try:
        with target_path.open("wb") as output_file:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > max_bytes:
                    raise ValueError("上传文件过大。")
                digest.update(chunk)
                output_file.write(chunk)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    if total_size <= 0:
        target_path.unlink(missing_ok=True)
        raise ValueError("上传文件为空。")
    return total_size, digest.hexdigest()


def _relative_file_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        clean = value.split("#", 1)[0].split("?", 1)[0].strip().lstrip("/")
        if clean and not clean.startswith(("http://", "https://", "data:", "//")):
            refs.add(clean)
        return refs

    if isinstance(value, dict):
        for key in ("relPath", "localName", "fileName", "path", "thumbnailLocal"):
            raw = value.get(key)
            if isinstance(raw, str):
                refs.update(_relative_file_refs(raw))
    return refs


def _is_ref_used(meta: dict[str, Any], ref: str) -> bool:
    clean_ref = str(ref or "").strip().lstrip("/")
    if not clean_ref:
        return False
    containers = [
        meta.get("cover"),
        meta.get("designImages"),
        meta.get("summaryImages"),
        meta.get("instances"),
        meta.get("attachments"),
        meta.get("images"),
    ]
    for container in containers:
        stack = [container]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if clean_ref in _relative_file_refs(item):
                    return True
                stack.extend(item.values())
            elif isinstance(item, str) and clean_ref in _relative_file_refs(item):
                return True
    return False


def _unlink_relative(model_root: Path, rel_path: str) -> None:
    clean_ref = str(rel_path or "").strip().lstrip("/")
    if not clean_ref:
        return
    target = (model_root / clean_ref).resolve()
    try:
        target.relative_to(model_root.resolve())
    except ValueError:
        return
    if target.exists() and target.is_file():
        target.unlink()


def update_local_model_description(model_dir: str, description: str) -> dict[str, Any]:
    model_root, meta = _resolve_local_model_root(model_dir)
    clean_text = str(description or "").strip()
    summary = meta.get("summary") if isinstance(meta.get("summary"), dict) else {}
    summary["text"] = clean_text
    summary["raw"] = clean_text
    summary["html"] = _summary_html_from_text(clean_text)
    meta["summary"] = summary
    _write_meta(model_root, meta)
    return {"description": clean_text}


def add_local_model_file(model_dir: str, upload: UploadFile, title: str = "") -> dict[str, Any]:
    model_root, meta = _resolve_local_model_root(model_dir)
    original_filename = _safe_filename(upload.filename or "", fallback="model.3mf")
    suffix = Path(original_filename).suffix.lower()
    if suffix not in MODEL_SUFFIXES:
        raise ValueError("只支持上传 3MF、STL、STEP、STP、OBJ 模型文件。")

    target_path = _unique_path(model_root / "instances", original_filename)
    size, digest = _copy_upload(upload, target_path, max_bytes=MAX_LOCAL_IMPORT_UPLOAD_BYTES)
    now_iso = china_now_iso()
    file_kind = suffix.lstrip(".").upper() or "文件"
    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    existing_ids = {str(item.get("id") or "") for item in instances if isinstance(item, dict)}
    instance_id = f"local-edit-{uuid4().hex[:8]}"
    while instance_id in existing_ids:
        instance_id = f"local-edit-{uuid4().hex[:8]}"
    image_items = meta.get("designImages") if isinstance(meta.get("designImages"), list) else []
    cover_ref = ""
    for item in image_items:
        refs = _relative_file_refs(item)
        if refs:
            cover_ref = sorted(refs)[0]
            break
    entry = {
        "id": instance_id,
        "profileId": "",
        "title": str(title or "").strip() or target_path.stem,
        "name": target_path.name,
        "machine": f"本地 {file_kind}",
        "publishedAt": now_iso,
        "publishTime": now_iso,
        "summary": "",
        "thumbnailLocal": cover_ref,
        "pictures": image_items,
        "fileName": target_path.name,
        "sourceFileName": original_filename,
        "downloadCount": 0,
        "printCount": 0,
        "plateCount": 0,
        "fileKind": file_kind,
        "localImport": {
            "sourcePath": original_filename,
            "originalFilename": original_filename,
            "organizedAt": now_iso,
            "moveFiles": False,
            "fileHash": digest,
            "configFingerprint": f"sha256:{digest}",
            "packageSource": str((meta.get("localImport") or {}).get("sourcePath") or ""),
            "size": size,
        },
    }
    instances.append(entry)
    meta["instances"] = instances
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    local_import["modelFileCount"] = len(instances)
    meta["localImport"] = local_import
    _write_meta(model_root, meta)
    return entry


def delete_local_model_file(model_dir: str, instance_key: str) -> dict[str, Any]:
    model_root, meta = _resolve_local_model_root(model_dir)
    target_key = str(instance_key or "").strip()
    if not target_key:
        raise ValueError("模型文件不存在。")

    instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
    kept: list[dict[str, Any]] = []
    removed: dict[str, Any] | None = None
    for index, item in enumerate(instances):
        if not isinstance(item, dict):
            continue
        current_key = str(item.get("id") or item.get("profileId") or index + 1)
        if removed is None and current_key == target_key:
            removed = item
            continue
        kept.append(item)
    if removed is None:
        raise ValueError("模型文件不存在。")
    if not kept:
        raise ValueError("至少保留一个模型文件。")

    file_name = Path(str(removed.get("fileName") or removed.get("name") or "")).name
    meta["instances"] = kept
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    local_import["modelFileCount"] = len(kept)
    meta["localImport"] = local_import
    _write_meta(model_root, meta)
    if file_name:
        _unlink_relative(model_root, f"instances/{file_name}")
    return removed


def add_local_model_image(model_dir: str, upload: UploadFile) -> dict[str, Any]:
    model_root, meta = _resolve_local_model_root(model_dir)
    original_filename = _safe_filename(upload.filename or "", fallback="image.jpg")
    suffix = Path(original_filename).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError("只支持上传 JPG、PNG、WEBP、GIF、BMP、AVIF 图片。")

    target_path = _unique_path(model_root / "images", original_filename)
    size, digest = _copy_upload(upload, target_path)
    rel_path = f"images/{target_path.name}"
    image_item = {
        "relPath": rel_path,
        "fileName": target_path.name,
        "originalName": original_filename,
        "mimeType": str(upload.content_type or mimetypes.guess_type(original_filename)[0] or "image/*"),
        "size": size,
        "sha256": digest,
        "uploadedAt": china_now_iso(),
    }
    images = meta.get("designImages") if isinstance(meta.get("designImages"), list) else []
    images.append(image_item)
    meta["designImages"] = images
    if not meta.get("cover"):
        meta["cover"] = rel_path
    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        pictures = instance.get("pictures") if isinstance(instance.get("pictures"), list) else []
        pictures.append({"relPath": rel_path})
        instance["pictures"] = pictures
        if not str(instance.get("thumbnailLocal") or "").strip():
            instance["thumbnailLocal"] = rel_path
    _write_meta(model_root, meta)
    return image_item


def delete_local_model_image(model_dir: str, rel_path: str) -> dict[str, Any]:
    model_root, meta = _resolve_local_model_root(model_dir)
    target_ref = str(rel_path or "").strip().lstrip("/")
    if not target_ref:
        raise ValueError("图片不存在。")

    removed: dict[str, Any] | None = None
    images = []
    for item in meta.get("designImages") or []:
        refs = _relative_file_refs(item)
        if removed is None and target_ref in refs:
            removed = item if isinstance(item, dict) else {"relPath": target_ref}
            continue
        images.append(item)
    if removed is None:
        raise ValueError("图片不存在。")

    meta["designImages"] = images
    if target_ref in _relative_file_refs(meta.get("cover")):
        meta["cover"] = ""
    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        instance["pictures"] = [
            item for item in (instance.get("pictures") if isinstance(instance.get("pictures"), list) else [])
            if target_ref not in _relative_file_refs(item)
        ]
        if target_ref in _relative_file_refs(instance.get("thumbnailLocal")):
            instance["thumbnailLocal"] = ""
    _write_meta(model_root, meta)
    if not _is_ref_used(meta, target_ref):
        _unlink_relative(model_root, target_ref)
    return removed
