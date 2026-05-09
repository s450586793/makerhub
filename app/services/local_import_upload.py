from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from bs4 import BeautifulSoup
from fastapi import UploadFile

from app.core.settings import ARCHIVE_DIR, MAX_LOCAL_IMPORT_UPLOAD_BYTES, STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import from_timestamp as china_from_timestamp, now_iso as china_now_iso
from app.services.business_logs import append_business_log
from app.services.catalog import invalidate_archive_snapshot
from app.services.legacy_archiver import sanitize_filename
from app.services.task_state import TaskStateStore


LOCAL_IMPORT_UPLOAD_SUBDIR = "web_uploads"
LOCAL_IMPORT_STAGING_DIR = STATE_DIR / "import_uploads"
LOCAL_IMPORT_CHUNK_SIZE_BYTES = 1024 * 1024
LOCAL_IMPORT_STAGING_TTL_SECONDS = 24 * 60 * 60
LOCAL_IMPORT_HASH_CHUNK_SIZE_BYTES = 1024 * 1024
LOCAL_IMPORT_HASH_PAUSE_EVERY_BYTES = 8 * 1024 * 1024
LOCAL_IMPORT_HASH_PAUSE_SECONDS = 0.01
LOCAL_IMPORT_MAX_ZIP_DEPTH = 3
LOCAL_IMPORT_MAX_ARCHIVE_DEPTH = LOCAL_IMPORT_MAX_ZIP_DEPTH
LOCAL_IMPORT_MAX_EXTRACTED_BYTES = MAX_LOCAL_IMPORT_UPLOAD_BYTES * 4
ORGANIZER_LIBRARY_INDEX_CACHE_PATH = STATE_DIR / "organizer_library_index.json"

PACKAGE_ARCHIVE_SUFFIXES = {".zip", ".rar"}
MODEL_SUFFIXES = {".3mf", ".stl", ".step", ".stp", ".obj"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
DOCUMENT_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".xlsb",
    ".xlt",
    ".xltx",
    ".xltm",
    ".csv",
    ".tsv",
    ".ods",
}
ATTACHMENT_SUFFIXES = VIDEO_SUFFIXES | DOCUMENT_SUFFIXES
IGNORED_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
IGNORED_PATH_PARTS = {"__macosx"}


def _safe_3mf_filename(filename: str) -> str:
    name = _safe_filename(filename, fallback="model.3mf")
    if Path(name).suffix.lower() != ".3mf":
        raise ValueError(f"仅支持上传 .3mf 文件：{name}")
    stem = Path(name).stem.strip() or "model"
    return f"{stem}.3mf"


def _safe_filename(filename: str, *, fallback: str = "file") -> str:
    raw = Path(str(filename or "").strip()).name
    name = sanitize_filename(raw).strip()
    if not name:
        name = sanitize_filename(fallback).strip() or "file"
    return name


def _normalize_relative_path(raw_path: str, fallback_name: str) -> str:
    raw = str(raw_path or "").replace("\\", "/").strip()
    fallback = _safe_filename(fallback_name, fallback="file")
    if not raw:
        raw = fallback
    if PurePosixPath(raw).is_absolute():
        raise ValueError("上传文件路径不安全。")

    parts: list[str] = []
    for raw_part in raw.split("/"):
        part = raw_part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError("上传文件路径不安全。")
        safe = sanitize_filename(Path(part).name).strip()
        if safe:
            parts.append(safe)
    if not parts:
        parts = [fallback]
    return "/".join(parts)


def _is_ignored_relative_path(relative_path: str) -> bool:
    parts = [part.strip() for part in str(relative_path or "").replace("\\", "/").split("/") if part.strip()]
    if not parts:
        return True
    lower_parts = [part.lower() for part in parts]
    if any(part in IGNORED_PATH_PARTS for part in lower_parts[:-1]):
        return True
    name = lower_parts[-1]
    return name in IGNORED_FILE_NAMES or name.startswith("._")


def _unique_destination(parent: Path, filename: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(filename, fallback="file")
    stem = Path(name).stem or "file"
    suffix = Path(name).suffix
    candidate = parent / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


def _unique_relative_destination(root: Path, relative_path: str) -> Path:
    clean = _normalize_relative_path(relative_path, Path(relative_path).name or "file")
    parts = clean.split("/")
    parent = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
    return _unique_destination(parent, parts[-1])


def _cleanup_stale_staging_dirs() -> None:
    if not LOCAL_IMPORT_STAGING_DIR.exists():
        return
    cutoff = time.time() - LOCAL_IMPORT_STAGING_TTL_SECONDS
    for path in LOCAL_IMPORT_STAGING_DIR.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _new_staging_dir() -> Path:
    _cleanup_stale_staging_dirs()
    token = f"{int(time.time() * 1000)}-{os.getpid()}-{threading.get_ident()}-{uuid4().hex[:8]}"
    staging_dir = LOCAL_IMPORT_STAGING_DIR / token
    staging_dir.mkdir(parents=True, exist_ok=False)
    return staging_dir


def _copy_upload_to_staging(upload: UploadFile, staging_path: Path) -> int:
    total_size = 0
    temp_path = staging_path.with_name(f".{staging_path.name}.{os.getpid()}.{threading.get_ident()}.uploading")
    upload.file.seek(0)
    try:
        with temp_path.open("wb") as output_file:
            while True:
                chunk = upload.file.read(LOCAL_IMPORT_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > MAX_LOCAL_IMPORT_UPLOAD_BYTES:
                    raise ValueError("上传文件过大。")
                output_file.write(chunk)
        if total_size <= 0:
            raise ValueError("上传文件为空。")
        temp_path.replace(staging_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        staging_path.unlink(missing_ok=True)
        raise
    return total_size


def _move_staged_file_to_target(staging_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    moving_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.moving")
    try:
        shutil.move(str(staging_path), str(moving_path))
        moving_path.replace(target_path)
    except Exception:
        moving_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        raise


def _upload_entries(files: list[UploadFile], paths: list[str] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    raw_paths = list(paths or [])
    for index, upload in enumerate(files or []):
        file_name = _safe_filename(upload.filename or f"upload-{index + 1}", fallback=f"upload-{index + 1}")
        raw_path = raw_paths[index] if index < len(raw_paths) and str(raw_paths[index] or "").strip() else file_name
        relative_path = _normalize_relative_path(raw_path, file_name)
        if _is_ignored_relative_path(relative_path):
            continue
        entries.append(
            {
                "upload": upload,
                "file_name": Path(relative_path).name,
                "relative_path": relative_path,
                "suffix": Path(relative_path).suffix.lower(),
                "is_folder_item": "/" in relative_path,
            }
        )
    return entries


def _is_legacy_3mf_batch(entries: list[dict[str, Any]]) -> bool:
    return bool(entries) and all(
        str(item.get("suffix") or "").lower() == ".3mf" and not item.get("is_folder_item")
        for item in entries
    )


def _has_direct_3mf_mix(entries: list[dict[str, Any]]) -> bool:
    if any(item.get("is_folder_item") for item in entries):
        return False
    has_3mf = any(str(item.get("suffix") or "").lower() == ".3mf" for item in entries)
    return has_3mf and any(str(item.get("suffix") or "").lower() != ".3mf" for item in entries)


def _package_title(entries: list[dict[str, Any]]) -> str:
    folder_roots = []
    for item in entries:
        rel = str(item.get("relative_path") or "")
        if "/" in rel:
            folder_roots.append(rel.split("/", 1)[0])
    if folder_roots and len(set(folder_roots)) == 1:
        return folder_roots[0]

    archive_entries = [item for item in entries if str(item.get("suffix") or "").lower() in PACKAGE_ARCHIVE_SUFFIXES]
    if len(archive_entries) == 1:
        return Path(str(archive_entries[0].get("file_name") or "本地模型")).stem

    model_entries = [item for item in entries if str(item.get("suffix") or "").lower() in MODEL_SUFFIXES]
    if len(model_entries) == 1:
        return Path(str(model_entries[0].get("file_name") or "本地模型")).stem
    return "本地模型导入"


def _model_root_string(model_root: Path) -> str:
    try:
        return model_root.resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        return model_root.name


def _prepare_model_root(library_root: Path, title: str) -> Path:
    stem = sanitize_filename(str(title or "").strip()) or "local_model"
    base_name = f"LOCAL_{stem}"
    for index in range(0, 1000):
        name = base_name if index == 0 else f"{base_name}_{index + 1}"
        candidate = library_root / name
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
    raise RuntimeError("无法为本地模型分配新的目标目录。")


def _stage_package_uploads(entries: list[dict[str, Any]], staging_dir: Path) -> list[dict[str, Any]]:
    upload_root = staging_dir / "uploads"
    staged: list[dict[str, Any]] = []
    for entry in entries:
        target_path = _unique_relative_destination(upload_root, str(entry.get("relative_path") or entry.get("file_name") or "file"))
        size = _copy_upload_to_staging(entry["upload"], target_path)
        staged.append(
            {
                "path": target_path,
                "file_name": target_path.name,
                "relative_path": str(entry.get("relative_path") or target_path.name),
                "size": size,
                "archive_depth": 0,
            }
        )
    return staged


def _copy_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target_path: Path) -> int:
    written = 0
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.extracting")
    try:
        with archive.open(info) as source, temp_path.open("wb") as output:
            while True:
                chunk = source.read(LOCAL_IMPORT_CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_LOCAL_IMPORT_UPLOAD_BYTES:
                    raise ValueError(f"ZIP 内文件过大：{Path(info.filename).name}")
                output.write(chunk)
        if written <= 0:
            temp_path.unlink(missing_ok=True)
            return 0
        temp_path.replace(target_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        raise
    return written


def _archive_type_label(suffix: str) -> str:
    return "RAR" if str(suffix or "").lower() == ".rar" else "ZIP"


def _archive_unreadable_reason(suffix: str) -> str:
    return f"{_archive_type_label(suffix)} 文件无法读取"


def _extract_zip_file(
    item: dict[str, Any],
    extraction_root: Path,
    extracted_total: list[int],
    skipped_archives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    zip_path = Path(item["path"])
    source_relative = str(item.get("relative_path") or zip_path.name)
    depth = int(item.get("archive_depth") or 0)
    zip_stem = sanitize_filename(Path(source_relative).stem) or "zip"
    extracted: list[dict[str, Any]] = []

    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        if depth <= 0:
            raise ValueError(f"{_archive_unreadable_reason('.zip')}：{Path(source_relative).name}") from exc
        skipped_archives.append(
            {
                "file_name": Path(source_relative).name,
                "relative_path": source_relative,
                "reason": _archive_unreadable_reason(".zip"),
            }
        )
        return []

    with archive:
        for info in sorted(archive.infolist(), key=lambda value: value.filename):
            if info.is_dir():
                continue
            member_relative = _normalize_relative_path(info.filename, Path(info.filename).name or "file")
            if _is_ignored_relative_path(member_relative):
                continue
            if info.file_size <= 0:
                continue
            if info.file_size > MAX_LOCAL_IMPORT_UPLOAD_BYTES:
                raise ValueError(f"ZIP 内文件过大：{Path(member_relative).name}")

            extracted_total[0] += int(info.file_size or 0)
            if extracted_total[0] > LOCAL_IMPORT_MAX_EXTRACTED_BYTES:
                raise ValueError("ZIP 解压后的文件总量过大。")

            target_path = _unique_relative_destination(extraction_root, f"{zip_stem}/{member_relative}")
            size = _copy_zip_member(archive, info, target_path)
            if size <= 0:
                continue
            extracted.append(
                {
                    "path": target_path,
                    "file_name": target_path.name,
                    "relative_path": f"{source_relative}!/{member_relative}",
                    "size": size,
                    "archive_depth": depth + 1,
                }
            )
    return extracted


def _extract_rar_with_bsdtar(rar_path: Path, destination: Path) -> None:
    executable = shutil.which("bsdtar")
    if not executable:
        raise RuntimeError("RAR 解包工具不可用")

    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [executable, "-xf", str(rar_path), "-C", str(destination)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = " ".join((result.stderr or result.stdout or "").split()).strip()
        raise RuntimeError(message or "RAR 解包失败")


def _copy_extracted_archive_file(source_path: Path, target_path: Path) -> int:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    size = source_path.stat().st_size
    temp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex[:8]}.copying")
    try:
        shutil.copy2(source_path, temp_path)
        temp_path.replace(target_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        target_path.unlink(missing_ok=True)
        raise
    return size


def _extract_rar_file(
    item: dict[str, Any],
    extraction_root: Path,
    extracted_total: list[int],
    skipped_archives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rar_path = Path(item["path"])
    source_relative = str(item.get("relative_path") or rar_path.name)
    depth = int(item.get("archive_depth") or 0)
    rar_stem = sanitize_filename(Path(source_relative).stem) or "rar"
    temp_dir = extraction_root.parent / f".{rar_stem}.{uuid4().hex[:8]}.rar_extracting"
    extracted: list[dict[str, Any]] = []

    try:
        _extract_rar_with_bsdtar(rar_path, temp_dir)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if depth <= 0:
            raise ValueError(f"{_archive_unreadable_reason('.rar')}：{Path(source_relative).name}") from exc
        skipped_archives.append(
            {
                "file_name": Path(source_relative).name,
                "relative_path": source_relative,
                "reason": _archive_unreadable_reason(".rar"),
            }
        )
        return []

    try:
        for source_path in sorted(temp_dir.rglob("*")):
            if source_path.is_symlink() or not source_path.is_file():
                continue
            try:
                raw_relative = source_path.relative_to(temp_dir).as_posix()
            except ValueError:
                continue
            member_relative = _normalize_relative_path(raw_relative, source_path.name or "file")
            if _is_ignored_relative_path(member_relative):
                continue

            size = int(source_path.stat().st_size)
            if size <= 0:
                continue
            if size > MAX_LOCAL_IMPORT_UPLOAD_BYTES:
                raise ValueError(f"RAR 内文件过大：{Path(member_relative).name}")
            extracted_total[0] += size
            if extracted_total[0] > LOCAL_IMPORT_MAX_EXTRACTED_BYTES:
                raise ValueError("RAR 解压后的文件总量过大。")

            target_path = _unique_relative_destination(extraction_root, f"{rar_stem}/{member_relative}")
            copied_size = _copy_extracted_archive_file(source_path, target_path)
            if copied_size <= 0:
                continue
            extracted.append(
                {
                    "path": target_path,
                    "file_name": target_path.name,
                    "relative_path": f"{source_relative}!/{member_relative}",
                    "size": copied_size,
                    "archive_depth": depth + 1,
                }
            )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    return extracted


def _extract_package_archive_file(
    item: dict[str, Any],
    extraction_root: Path,
    extracted_total: list[int],
    skipped_archives: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    suffix = Path(str(item.get("file_name") or "")).suffix.lower()
    if suffix == ".rar":
        return _extract_rar_file(item, extraction_root, extracted_total, skipped_archives)
    return _extract_zip_file(item, extraction_root, extracted_total, skipped_archives)


def _expand_package_archives(staged: list[dict[str, Any]], staging_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    extraction_root = staging_dir / "extracted"
    pending = list(staged)
    expanded: list[dict[str, Any]] = []
    extracted_total = [0]
    skipped_archives: list[dict[str, Any]] = []

    while pending:
        item = pending.pop(0)
        suffix = Path(str(item.get("file_name") or "")).suffix.lower()
        depth = int(item.get("archive_depth") or 0)
        if suffix in PACKAGE_ARCHIVE_SUFFIXES and depth < LOCAL_IMPORT_MAX_ARCHIVE_DEPTH:
            pending.extend(_extract_package_archive_file(item, extraction_root, extracted_total, skipped_archives))
            continue
        expanded.append(item)
    return expanded, skipped_archives


def _classify_package_files(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    classified = {
        "models": [],
        "images": [],
        "texts": [],
        "attachments": [],
    }
    for item in sorted(items, key=lambda value: str(value.get("relative_path") or value.get("file_name") or "").lower()):
        suffix = Path(str(item.get("file_name") or "")).suffix.lower()
        if suffix in MODEL_SUFFIXES:
            classified["models"].append(item)
        elif suffix in IMAGE_SUFFIXES:
            classified["images"].append(item)
        elif suffix in TEXT_SUFFIXES:
            classified["texts"].append(item)
        elif suffix in ATTACHMENT_SUFFIXES:
            classified["attachments"].append(item)
    return classified


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    bytes_since_pause = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(LOCAL_IMPORT_HASH_CHUNK_SIZE_BYTES), b""):
            if not chunk:
                continue
            digest.update(chunk)
            bytes_since_pause += len(chunk)
            if bytes_since_pause >= LOCAL_IMPORT_HASH_PAUSE_EVERY_BYTES:
                time.sleep(LOCAL_IMPORT_HASH_PAUSE_SECONDS)
                bytes_since_pause = 0
    return digest.hexdigest()


def _dedupe_model_files(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}

    for item in items:
        path = Path(item["path"])
        digest = _sha256_file(path)
        suffix = path.suffix.lower()
        key = f"{suffix}:{digest}"
        next_item = {**item, "sha256": digest}
        existing = seen.get(key)
        if existing:
            duplicates.append(
                {
                    "file_name": str(item.get("file_name") or path.name),
                    "relative_path": str(item.get("relative_path") or path.name),
                    "duplicate_of": str(existing.get("relative_path") or existing.get("file_name") or ""),
                    "sha256": digest,
                }
            )
            continue
        seen[key] = next_item
        kept.append(next_item)
    return kept, duplicates


def _description_text_from_item(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    path = Path(item["path"])
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")[:262_144]
    except OSError:
        return ""

    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    else:
        text = raw
    return " ".join(str(text or "").replace("\r", "\n").split()).strip()


def _pick_description_item(text_items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not text_items:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int, str]:
        rel = str(item.get("relative_path") or item.get("file_name") or "")
        stem = Path(rel).stem.lower()
        name_score = 0 if stem in {"readme", "说明", "描述", "description", "desc"} else 1
        depth = rel.count("/")
        return (name_score, depth, rel.lower())

    return sorted(text_items, key=score)[0]


def _summary_html_from_text(text: str) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    paragraphs = [part.strip() for part in clean.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [clean]
    return "".join(f"<p>{html.escape(part)}</p>" for part in paragraphs)


def _copy_item_to_dir(item: dict[str, Any], target_dir: Path) -> Path:
    target_path = _unique_destination(target_dir, str(item.get("file_name") or Path(item["path"]).name))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(item["path"]), target_path)
    return target_path


def _attachment_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "manual"
    if suffix in VIDEO_SUFFIXES:
        return "guide"
    return "other"


def _file_kind_label(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix.upper() if suffix else "文件"


def _build_package_meta(
    *,
    model_root: Path,
    title: str,
    package_source: str,
    model_files: list[dict[str, Any]],
    duplicate_files: list[dict[str, Any]],
    skipped_archives: list[dict[str, Any]],
    image_paths: list[str],
    attachments: list[dict[str, Any]],
    description_text: str,
) -> dict[str, Any]:
    now_iso = china_now_iso()
    publish_iso = now_iso
    first_model_path = Path(str(model_files[0].get("target_path") or "")) if model_files else None
    if first_model_path and first_model_path.exists():
        try:
            publish_iso = china_from_timestamp(first_model_path.stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            publish_iso = now_iso

    cover_path = image_paths[0] if image_paths else ""
    gallery_items = [{"relPath": item} for item in image_paths]
    summary_text = description_text or "从本地文件导入的模型。"
    instances = []
    for index, item in enumerate(model_files, start=1):
        target_path = Path(item["target_path"])
        source_relative = str(item.get("relative_path") or item.get("file_name") or target_path.name)
        file_label = _file_kind_label(target_path)
        instances.append(
            {
                "id": f"local-{index}",
                "profileId": "",
                "title": Path(str(item.get("file_name") or target_path.name)).stem or target_path.stem,
                "name": target_path.name,
                "machine": f"本地 {file_label}",
                "publishedAt": publish_iso,
                "publishTime": publish_iso,
                "summary": "",
                "thumbnailLocal": cover_path,
                "pictures": gallery_items,
                "fileName": target_path.name,
                "sourceFileName": Path(source_relative).name,
                "downloadCount": 0,
                "printCount": 0,
                "plateCount": 0,
                "fileKind": file_label,
                "localImport": {
                    "sourcePath": source_relative,
                    "originalFilename": Path(source_relative).name,
                    "organizedAt": now_iso,
                    "moveFiles": False,
                    "fileHash": str(item.get("sha256") or ""),
                    "configFingerprint": f"sha256:{item.get('sha256')}" if item.get("sha256") else "",
                    "packageSource": package_source,
                },
            }
        )

    return {
        "id": "",
        "title": title,
        "source": "local",
        "url": "",
        "baseName": model_root.name,
        "collectDate": now_iso,
        "publishedAt": publish_iso,
        "update_time": now_iso,
        "cover": cover_path,
        "designImages": gallery_items,
        "summaryImages": [],
        "summary": {
            "text": summary_text,
            "html": _summary_html_from_text(summary_text),
        },
        "author": {
            "name": "本地导入",
            "url": "",
        },
        "tags": ["本地导入"],
        "tagsOriginal": [],
        "stats": {
            "likes": 0,
            "favorites": 0,
            "downloads": 0,
            "prints": 0,
            "comments": 0,
            "views": 0,
        },
        "comments": [],
        "attachments": attachments,
        "instances": instances,
        "localImport": {
            "sourcePath": package_source,
            "originalFilename": Path(package_source).name if package_source else title,
            "organizedAt": now_iso,
            "moveFiles": False,
            "modelKey": "",
            "package": True,
            "modelFileCount": len(model_files),
            "duplicateFileCount": len(duplicate_files),
            "duplicateFiles": duplicate_files[:50],
            "skippedArchiveCount": len(skipped_archives),
            "skippedArchives": skipped_archives[:50],
        },
    }


def _remove_organizer_index_cache() -> None:
    try:
        ORGANIZER_LIBRARY_INDEX_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        return


def _update_last_import(
    *,
    task_store: TaskStateStore,
    uploaded: list[dict[str, Any]],
    source_dir: str,
    upload_dir: str,
) -> None:
    current_tasks = task_store.load_organize_tasks()
    task_store.save_organize_tasks(
        {
            **current_tasks,
            "last_import": {
                "uploaded_at": china_now_iso(),
                "uploaded_count": len(uploaded),
                "source_dir": source_dir,
                "upload_dir": upload_dir,
                "files": uploaded,
            },
        }
    )


def _package_source_summary(entries: list[dict[str, Any]]) -> str:
    package_source = ", ".join(
        str(item.get("relative_path") or item.get("file_name") or "")
        for item in entries[:3]
    )
    if len(entries) > 3:
        package_source = f"{package_source} 等 {len(entries)} 项"
    return package_source


def _package_task_id(staging_dir: Path) -> str:
    return hashlib.sha1(f"local-package:{staging_dir.resolve()}".encode("utf-8")).hexdigest()[:16]


def _upsert_package_progress(
    *,
    task_store: TaskStateStore,
    task_id: str,
    title: str,
    source_dir: str,
    library_root: Path,
    package_source: str,
    status: str,
    message: str,
    progress: int,
    model_root: Path | None = None,
) -> None:
    model_dir = _model_root_string(model_root) if model_root else ""
    target_path = (model_root / "meta.json").as_posix() if model_root else ""
    task_store.upsert_organize_task(
        {
            "id": task_id,
            "title": title,
            "file_name": title,
            "source_dir": source_dir,
            "target_dir": library_root.as_posix(),
            "source_path": package_source,
            "target_path": target_path,
            "model_dir": model_dir,
            "status": status,
            "message": message,
            "progress": max(0, min(100, int(progress or 0))),
            "updated_at": china_now_iso(),
            "move_files": False,
            "fingerprint": f"local-package:{task_id}",
        },
        limit=50,
    )


def _upload_legacy_3mf_files(
    *,
    entries: list[dict[str, Any]],
    store: JsonStore,
    task_store: TaskStateStore,
) -> dict[str, Any]:
    config = store.load()
    source_raw = str(config.organizer.source_dir or "").strip()
    if not source_raw:
        raise ValueError("请先在设置里配置本地整理扫描目录。")

    source_dir = Path(source_raw).expanduser()
    upload_dir = source_dir / LOCAL_IMPORT_UPLOAD_SUBDIR

    prepared: list[tuple[UploadFile, str]] = []
    for entry in entries:
        prepared.append((entry["upload"], _safe_3mf_filename(str(entry.get("file_name") or ""))))

    staging_dir = _new_staging_dir()
    staged: list[dict[str, Any]] = []
    uploaded: list[dict[str, Any]] = []
    try:
        for upload, filename in prepared:
            staging_path = _unique_destination(staging_dir, filename)
            size = _copy_upload_to_staging(upload, staging_path)
            staged.append(
                {
                    "file_name": filename,
                    "staging_path": staging_path.as_posix(),
                    "size": size,
                }
            )

        for item in staged:
            filename = str(item.get("file_name") or "model.3mf")
            target_path = _unique_destination(upload_dir, filename)
            _move_staged_file_to_target(Path(str(item.get("staging_path") or "")), target_path)
            uploaded.append(
                {
                    "file_name": filename,
                    "source_path": target_path.as_posix(),
                    "size": int(item.get("size") or 0),
                }
            )
    except Exception:
        for item in uploaded:
            Path(str(item.get("source_path") or "")).unlink(missing_ok=True)
        for item in staged:
            Path(str(item.get("staging_path") or "")).unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    append_business_log(
        "organizer",
        "local_import_uploaded",
        "本地 3MF 已从暂存区移动到本地整理扫描目录。",
        uploaded_count=len(uploaded),
        source_dir=source_dir.as_posix(),
        upload_dir=upload_dir.as_posix(),
        files=[item["file_name"] for item in uploaded],
    )

    _update_last_import(
        task_store=task_store,
        uploaded=uploaded,
        source_dir=source_dir.as_posix(),
        upload_dir=upload_dir.as_posix(),
    )

    return {
        "success": True,
        "mode": "3mf",
        "trigger_organizer": True,
        "message": f"已上传 {len(uploaded)} 个 3MF，等待本地整理处理。",
        "source_dir": source_dir.as_posix(),
        "upload_dir": upload_dir.as_posix(),
        "uploaded": uploaded,
    }


def _import_package_files(
    *,
    entries: list[dict[str, Any]],
    store: JsonStore,
    task_store: TaskStateStore,
) -> dict[str, Any]:
    config = store.load()
    target_raw = str(config.organizer.target_dir or "").strip()
    if not target_raw:
        raise ValueError("请先在设置里配置本地整理目标目录。")
    source_dir = Path(str(config.organizer.source_dir or "")).expanduser()
    library_root = Path(target_raw).expanduser()
    library_root.mkdir(parents=True, exist_ok=True)

    staging_dir = _new_staging_dir()
    task_id = _package_task_id(staging_dir)
    title = _package_title(entries)
    package_source = _package_source_summary(entries)
    source_dir_text = source_dir.as_posix() if str(source_dir) != "." else ""
    model_root: Path | None = None
    try:
        _upsert_package_progress(
            task_store=task_store,
            task_id=task_id,
            title=title,
            source_dir=source_dir_text,
            library_root=library_root,
            package_source=package_source,
            status="running",
            message="正在上传到暂存区。",
            progress=10,
        )
        staged = _stage_package_uploads(entries, staging_dir)
        _upsert_package_progress(
            task_store=task_store,
            task_id=task_id,
            title=title,
            source_dir=source_dir_text,
            library_root=library_root,
            package_source=package_source,
            status="running",
            message="正在解压并展开文件。",
            progress=30,
        )
        expanded, skipped_archives = _expand_package_archives(staged, staging_dir)
        _upsert_package_progress(
            task_store=task_store,
            task_id=task_id,
            title=title,
            source_dir=source_dir_text,
            library_root=library_root,
            package_source=package_source,
            status="running",
            message="正在按文件类型分类并去重。",
            progress=55,
        )
        classified = _classify_package_files(expanded)
        model_items, duplicate_items = _dedupe_model_files(classified["models"])
        if not model_items:
            raise ValueError("没有找到可导入的 STL / 3MF / STEP / OBJ 模型文件。")

        model_root = _prepare_model_root(library_root, title)
        _upsert_package_progress(
            task_store=task_store,
            task_id=task_id,
            title=title,
            source_dir=source_dir_text,
            library_root=library_root,
            package_source=package_source,
            status="running",
            message="正在写入模型目录。",
            progress=75,
            model_root=model_root,
        )
        instances_dir = model_root / "instances"
        images_dir = model_root / "images"
        attachments_dir = model_root / "attachments"
        instances_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        attachments_dir.mkdir(parents=True, exist_ok=True)

        copied_models: list[dict[str, Any]] = []
        for item in model_items:
            target_path = _copy_item_to_dir(item, instances_dir)
            copied_models.append({**item, "target_path": target_path.as_posix(), "file_name": target_path.name})

        image_paths: list[str] = []
        for item in classified["images"]:
            target_path = _copy_item_to_dir(item, images_dir)
            image_paths.append(f"images/{target_path.name}")

        description_item = _pick_description_item(classified["texts"])
        description_text = _description_text_from_item(description_item)
        text_attachments = [
            item
            for item in classified["texts"]
            if description_item is None or Path(item["path"]) != Path(description_item["path"])
        ]

        attachments: list[dict[str, Any]] = []
        attachment_items = [*classified["attachments"], *text_attachments]
        for item in attachment_items:
            target_path = _copy_item_to_dir(item, attachments_dir)
            rel_path = f"attachments/{target_path.name}"
            mime_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"
            attachments.append(
                {
                    "id": uuid4().hex,
                    "source": "local_import",
                    "name": Path(str(item.get("relative_path") or target_path.name)).name,
                    "fileName": target_path.name,
                    "localName": rel_path,
                    "relPath": rel_path,
                    "category": _attachment_category(target_path),
                    "mimeType": mime_type,
                    "size": int(item.get("size") or target_path.stat().st_size),
                    "uploadedAt": china_now_iso(),
                }
            )

        meta = _build_package_meta(
            model_root=model_root,
            title=title,
            package_source=package_source,
            model_files=copied_models,
            duplicate_files=duplicate_items,
            skipped_archives=skipped_archives,
            image_paths=image_paths,
            attachments=attachments,
            description_text=description_text,
        )
        meta_path = model_root / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        _upsert_package_progress(
            task_store=task_store,
            task_id=task_id,
            title=title,
            source_dir=source_dir_text,
            library_root=library_root,
            package_source=package_source,
            status="failed",
            message=str(exc) or "本地模型包导入失败。",
            progress=0,
            model_root=model_root,
        )
        if model_root and model_root.exists():
            shutil.rmtree(model_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    model_dir = _model_root_string(model_root)
    uploaded = [
        {
            "file_name": title,
            "source_path": package_source,
            "target_path": (model_root / "meta.json").as_posix(),
            "size": sum(int(item.get("size") or 0) for item in model_items),
            "status": "success",
            "message": "本地模型包已导入。",
            "model_dir": model_dir,
        }
    ]
    _upsert_package_progress(
        task_store=task_store,
        task_id=task_id,
        title=title,
        source_dir=source_dir_text,
        library_root=library_root,
        package_source=package_source,
        status="success",
        message="本地模型包已导入。",
        progress=100,
        model_root=model_root,
    )
    _update_last_import(
        task_store=task_store,
        uploaded=uploaded,
        source_dir=source_dir_text,
        upload_dir=model_root.as_posix(),
    )
    _remove_organizer_index_cache()
    invalidate_archive_snapshot("local_import_package")

    append_business_log(
        "organizer",
        "local_import_package_imported",
        "本地模型包已导入。",
        title=title,
        model_dir=model_dir,
        model_file_count=len(model_items),
        duplicate_file_count=len(duplicate_items),
        skipped_zip_count=len(skipped_archives),
        image_count=len(image_paths),
        attachment_count=len(attachments),
    )

    return {
        "success": True,
        "mode": "package",
        "trigger_organizer": False,
        "message": f"已导入本地模型包：{title}",
        "model_dir": model_dir,
        "model_file_count": len(model_items),
        "duplicate_file_count": len(duplicate_items),
        "skipped_zip_count": len(skipped_archives),
        "image_count": len(image_paths),
        "attachment_count": len(attachments),
        "uploaded": uploaded,
    }


def upload_local_import_files(
    *,
    files: list[UploadFile],
    paths: list[str] | None = None,
    store: JsonStore | None = None,
    task_store: TaskStateStore | None = None,
) -> dict[str, Any]:
    if not files:
        raise ValueError("请选择要导入的文件。")

    entries = _upload_entries(files, paths)
    if not entries:
        raise ValueError("请选择要导入的文件。")
    if _has_direct_3mf_mix(entries):
        raise ValueError("3MF 请单独导入；包含图片、说明、STL、zip 或 rar 时，请打包为 zip/rar 或选择文件夹。")

    store = store or JsonStore()
    task_store = task_store or TaskStateStore()
    if _is_legacy_3mf_batch(entries):
        return _upload_legacy_3mf_files(entries=entries, store=store, task_store=task_store)
    return _import_package_files(entries=entries, store=store, task_store=task_store)
