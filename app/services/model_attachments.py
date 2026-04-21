import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from app.core.settings import ARCHIVE_DIR
from app.core.timezone import now as china_now, now_iso as china_now_iso


MANUAL_ATTACHMENTS_SIDECAR = ".makerhub-manual-attachments.json"
MANUAL_ATTACHMENTS_RELATIVE_DIR = Path("file") / "manual"
ATTACHMENT_CATEGORY_LABELS = {
    "assembly": "组装图",
    "guide": "组装指南",
    "manual": "使用手册",
    "bom": "BOM 清单",
    "other": "附件文件",
}


def resolve_model_root(model_dir: str) -> Path:
    clean_value = str(model_dir or "").strip().strip("/")
    if not clean_value:
        raise ValueError("模型不存在。")

    archive_root = ARCHIVE_DIR.resolve()
    target = (ARCHIVE_DIR / clean_value).resolve()
    try:
        target.relative_to(archive_root)
    except ValueError as exc:
        raise ValueError("非法模型路径。") from exc

    if not target.exists() or not target.is_dir() or not (target / "meta.json").exists():
        raise ValueError("模型不存在。")
    return target


def _sidecar_path(model_root: Path) -> Path:
    return model_root / MANUAL_ATTACHMENTS_SIDECAR


def load_manual_attachments(model_root: Path) -> list[dict]:
    sidecar_path = _sidecar_path(model_root)
    if not sidecar_path.exists():
        return []

    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def save_manual_attachments(model_root: Path, items: list[dict]) -> None:
    sidecar_path = _sidecar_path(model_root)
    if not items:
        if sidecar_path.exists():
            sidecar_path.unlink()
        return

    sidecar_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_category(category: str) -> str:
    normalized = str(category or "").strip().lower()
    if normalized in ATTACHMENT_CATEGORY_LABELS:
        return normalized
    return "other"


def _sanitize_filename(filename: str) -> str:
    name = Path(str(filename or "").strip()).name
    if not name:
        return "attachment"

    suffix = Path(name).suffix.lower()
    stem = Path(name).stem.strip() or "attachment"
    safe_stem = re.sub(r"[^\w()\-\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE).strip("._")
    safe_stem = safe_stem or "attachment"
    safe_suffix = re.sub(r"[^.\w]+", "", suffix)[:16]
    return f"{safe_stem}{safe_suffix}"


def _build_storage_name(target_dir: Path, filename: str) -> str:
    stem = Path(filename).stem or "attachment"
    suffix = Path(filename).suffix.lower()
    timestamp = china_now().strftime("%Y%m%d-%H%M%S")
    token = uuid4().hex[:8]
    candidate = f"{timestamp}-{token}-{stem}{suffix}"
    while (target_dir / candidate).exists():
        token = uuid4().hex[:8]
        candidate = f"{timestamp}-{token}-{stem}{suffix}"
    return candidate


def create_manual_attachment(model_dir: str, upload: UploadFile, name: str, category: str) -> dict:
    model_root = resolve_model_root(model_dir)
    original_filename = _sanitize_filename(upload.filename or "")
    if not original_filename:
        raise ValueError("请选择要上传的附件。")

    target_dir = model_root / MANUAL_ATTACHMENTS_RELATIVE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    storage_name = _build_storage_name(target_dir, original_filename)
    target_path = target_dir / storage_name

    upload.file.seek(0)
    with target_path.open("wb") as output_file:
        shutil.copyfileobj(upload.file, output_file)

    size = target_path.stat().st_size if target_path.exists() else 0
    if size <= 0:
        target_path.unlink(missing_ok=True)
        raise ValueError("上传文件为空。")

    uploaded_at = china_now_iso()
    mime_type = str(upload.content_type or mimetypes.guess_type(original_filename)[0] or "application/octet-stream")
    display_name = str(name or "").strip() or original_filename
    relative_path = (MANUAL_ATTACHMENTS_RELATIVE_DIR / storage_name).as_posix()

    items = load_manual_attachments(model_root)
    entry = {
        "id": uuid4().hex,
        "source": "manual",
        "name": display_name,
        "fileName": original_filename,
        "localName": relative_path,
        "relPath": relative_path,
        "category": _normalize_category(category),
        "mimeType": mime_type,
        "size": size,
        "uploadedAt": uploaded_at,
    }
    items.append(entry)
    save_manual_attachments(model_root, items)
    return entry


def delete_manual_attachment(model_dir: str, attachment_id: str) -> dict[str, Any]:
    model_root = resolve_model_root(model_dir)
    target_id = str(attachment_id or "").strip()
    if not target_id:
        raise ValueError("附件不存在。")

    items = load_manual_attachments(model_root)
    kept: list[dict] = []
    removed: dict[str, Any] | None = None
    for item in items:
        if removed is None and str(item.get("id") or "").strip() == target_id:
            removed = item
            continue
        kept.append(item)

    if removed is None:
        raise ValueError("附件不存在。")

    save_manual_attachments(model_root, kept)

    candidate_refs = {
        str(removed.get("localName") or "").strip(),
        str(removed.get("relPath") or "").strip(),
    }
    for ref in candidate_refs:
        if not ref:
            continue
        candidate = (model_root / ref).resolve()
        try:
            candidate.relative_to(model_root.resolve())
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            candidate.unlink()

    manual_dir = model_root / MANUAL_ATTACHMENTS_RELATIVE_DIR
    if manual_dir.exists() and manual_dir.is_dir():
        try:
            manual_dir.rmdir()
        except OSError:
            pass

    return removed
