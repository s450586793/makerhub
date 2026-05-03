from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from app.core.settings import MAX_LOCAL_IMPORT_UPLOAD_BYTES, STATE_DIR
from app.core.store import JsonStore
from app.services.business_logs import append_business_log
from app.services.legacy_archiver import sanitize_filename


LOCAL_IMPORT_UPLOAD_SUBDIR = "web_uploads"
LOCAL_IMPORT_STAGING_DIR = STATE_DIR / "import_uploads"
LOCAL_IMPORT_CHUNK_SIZE_BYTES = 1024 * 1024
LOCAL_IMPORT_STAGING_TTL_SECONDS = 24 * 60 * 60


def _safe_3mf_filename(filename: str) -> str:
    name = sanitize_filename(Path(str(filename or "").strip()).name)
    if not name:
        raise ValueError("请选择要上传的 3MF 文件。")
    if Path(name).suffix.lower() != ".3mf":
        raise ValueError(f"仅支持上传 .3mf 文件：{name}")
    stem = Path(name).stem.strip() or "model"
    return f"{stem}.3mf"


def _unique_destination(parent: Path, filename: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem or "model"
    suffix = Path(filename).suffix or ".3mf"
    candidate = parent / f"{stem}{suffix}"
    index = 2
    while candidate.exists():
        candidate = parent / f"{stem}_{index}{suffix}"
        index += 1
    return candidate


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
                        raise ValueError("上传的 3MF 文件过大。")
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


def upload_local_import_files(*, files: list[UploadFile], store: JsonStore | None = None) -> dict[str, Any]:
    if not files:
        raise ValueError("请选择要导入的 3MF 文件。")

    store = store or JsonStore()
    config = store.load()
    source_raw = str(config.organizer.source_dir or "").strip()
    if not source_raw:
        raise ValueError("请先在设置里配置本地整理扫描目录。")

    source_dir = Path(source_raw).expanduser()
    upload_dir = source_dir / LOCAL_IMPORT_UPLOAD_SUBDIR

    prepared: list[tuple[UploadFile, str]] = []
    for upload in files:
        prepared.append((upload, _safe_3mf_filename(upload.filename or "")))

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

    return {
        "success": True,
        "message": f"已上传 {len(uploaded)} 个 3MF，等待本地整理处理。",
        "source_dir": source_dir.as_posix(),
        "upload_dir": upload_dir.as_posix(),
        "uploaded": uploaded,
    }
