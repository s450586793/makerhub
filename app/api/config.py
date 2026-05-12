from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
import uuid
import zipfile
from datetime import timedelta
from io import BytesIO
from multiprocessing import Process
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import requests
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.store import JsonStore
from app.core.security import hash_api_token
from app.core.settings import APP_VERSION, ARCHIVE_DIR, BACKGROUND_TASKS_ENABLED, STATE_DIR
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.schemas.models import (
    AdvancedRuntimeConfig,
    ArchiveRequest,
    Missing3mfCancelRequest,
    CookiePair,
    CookieTestRequest,
    LocalModelDescriptionUpdateRequest,
    LocalModelFileDeleteRequest,
    LocalModelImageCoverRequest,
    LocalModelImageDeleteRequest,
    LocalModelMetadataUpdateRequest,
    LocalModelMergeRequest,
    Missing3mfRetryRequest,
    MobileImportConfig,
    MobileImportTokenResetRequest,
    ModelDeleteRequest,
    ModelFlagUpdateRequest,
    NotificationConfig,
    ProxyConfig,
    RemoteRefreshConfig,
    ShareCreateRequest,
    ShareDeleteExpiredRequest,
    ShareOptions,
    ShareReceiveRequest,
    SharingConfig,
    SubscriptionCreateRequest,
    SubscriptionSettingsUpdate,
    SubscriptionUpdateRequest,
    SystemUpdateRequest,
    ThemeSettingsUpdate,
    ThreeMfDownloadLimitsConfig,
    UserSettingsUpdate,
)
from app.schemas.models import OrganizeTask
from app.services.catalog import (
    build_dashboard_payload,
    build_models_payload,
    build_tasks_payload,
    get_model_comments_page,
    get_model_detail,
    invalidate_archive_snapshot,
    invalidate_model_detail_cache,
)
from app.services.crawler import LegacyCrawlerBridge
from app.services.business_logs import append_business_log, read_log_entries
from app.services.cookie_utils import sanitize_cookie_header
from app.services.local_organizer import LocalOrganizerService
from app.services.local_import_upload import upload_local_import_files
from app.services.local_model_edit import (
    add_local_model_file,
    add_local_model_image,
    delete_local_model_file,
    delete_local_model_image,
    set_local_model_cover_image,
    update_local_model_description,
    update_local_model_metadata,
)
from app.services.local_model_merge import merge_local_models
from app.services.model_attachments import create_manual_attachment, delete_manual_attachment
from app.services.remote_refresh import RemoteRefreshManager
from app.services.request_threads import run_task_api, run_ui_io, run_web_io
from app.services.auth import AuthManager
from app.services.archive_repair import (
    read_archive_repair_status,
    run_archive_repair_job,
    write_archive_repair_status,
)
from app.services.archive_profile_backfill import (
    read_profile_backfill_status,
    write_profile_backfill_status,
)
from app.services.batch_discovery import extract_model_id, normalize_source_url
from app.services.subscriptions import SubscriptionManager
from app.services.source_library import (
    build_source_group_models_payload,
    build_source_library_payload,
    build_state_group_models_payload,
)
from app.services.source_health import probe_cookie_auth_status
from app.services.task_state import TaskStateStore, compact_remote_refresh_state
from app.services.archive_worker import BATCH_TASK_MODES, detect_archive_mode
from app.services.self_update import get_update_status, request_system_update


router = APIRouter(prefix="/api")
store = JsonStore()
crawler = LegacyCrawlerBridge()
auth_manager = AuthManager(store=store)
task_state_store = TaskStateStore()
subscription_manager = SubscriptionManager(
    archive_manager=crawler.manager,
    store=store,
    task_store=task_state_store,
)
local_organizer = LocalOrganizerService(
    store=store,
    task_store=task_state_store,
)
remote_refresh_manager = RemoteRefreshManager(
    store=store,
    task_store=task_state_store,
    archive_manager=crawler.manager,
)
archive_repair_process: Process | None = None
archive_repair_start_lock = asyncio.Lock()
profile_backfill_start_lock = asyncio.Lock()
github_version_refresh_task: asyncio.Task | None = None
github_version_refresh_lock = asyncio.Lock()
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/s450586793/makerhub/main/VERSION"
GITHUB_VERSION_CDN_URL = "https://cdn.jsdelivr.net/gh/s450586793/makerhub@main/VERSION"
GITHUB_VERSION_API_URL = "https://api.github.com/repos/s450586793/makerhub/contents/VERSION?ref=main"
GITHUB_README_URL = "https://raw.githubusercontent.com/s450586793/makerhub/main/README.md"
GITHUB_README_CDN_URL = "https://cdn.jsdelivr.net/gh/s450586793/makerhub@main/README.md"
GITHUB_README_API_URL = "https://api.github.com/repos/s450586793/makerhub/contents/README.md?ref=main"
GITHUB_VERSION_CACHE_TTL_SECONDS = 300
GITHUB_VERSION_FAILURE_TTL_SECONDS = 60
GITHUB_CHANGELOG_CACHE_TTL_SECONDS = 300
GITHUB_CHANGELOG_FAILURE_TTL_SECONDS = 60
github_version_cache = {
    "version": "",
    "checked_at": 0.0,
    "checked_at_iso": "",
    "error": "",
    "source": "",
    "last_success_at": "",
}
github_changelog_refresh_task: asyncio.Task | None = None
github_changelog_refresh_lock = asyncio.Lock()
github_changelog_cache = {
    "items": [],
    "checked_at": 0.0,
    "checked_at_iso": "",
    "error": "",
    "source": "",
    "last_success_at": "",
}

SHARES_STATE_PATH = STATE_DIR / "model_shares.json"
SHARE_CODE_PREFIX = "MHSHARE1."
SHARE_CODE_COMPACT_PREFIX = "MHS1."
SHARE_CODE_OBSCURED_PREFIX = "MH3."
SHARE_CODE_TINY_PREFIX = "MH2|"
SHARE_CODE_TINY_SCHEMES = {"h": "http", "s": "https"}
SHARE_CODE_TINY_SCHEME_CODES = {"http": "h", "https": "s"}
SHARE_CODE_TINY_SEPARATOR = "\x1f"
MODEL_FILE_SUFFIX_ALIASES = {
    "3mf": {"3mf"},
    "stl": {"stl"},
    "step": {"step", "stp"},
    "obj": {"obj"},
}
ATTACHMENT_FILE_SUFFIX_ALIASES = {
    "pdf": {"pdf"},
    "excel": {"xls", "xlsx", "xlsm", "xlsb", "xlt", "xltx", "xltm", "csv", "tsv", "ods"},
}


def _read_shares_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not SHARES_STATE_PATH.exists():
        return {"items": []}
    try:
        payload = json.loads(SHARES_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"items": []}
    if not isinstance(payload, dict):
        return {"items": []}
    items = payload.get("items")
    if not isinstance(items, list):
        payload["items"] = []
    return payload


def _write_shares_state(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = SHARES_STATE_PATH.with_name(f"{SHARES_STATE_PATH.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(SHARES_STATE_PATH)


def _share_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _share_access_code_hash(code: str) -> str:
    return hashlib.sha256(f"makerhub-share-access:{code}".encode("utf-8")).hexdigest()


def _base64url_encode_json(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _base64url_decode_value(value: str):
    text = str(value or "").strip()
    padding = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode((text + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("分享码格式无效。") from exc
    return payload


def _base64url_decode_json(value: str) -> dict:
    payload = _base64url_decode_value(value)
    if not isinstance(payload, dict):
        raise ValueError("分享码格式无效。")
    return payload


def _new_share_id() -> str:
    return secrets.token_urlsafe(6)


def _new_share_token() -> str:
    return secrets.token_urlsafe(12)


def _new_share_access_code() -> str:
    return secrets.token_urlsafe(12)


def _ensure_share_access_code(record: dict) -> tuple[str, bool]:
    access_code = str(record.get("access_code") or "").strip()
    generated = False
    if not access_code:
        access_code = _new_share_access_code()
        record["access_code"] = access_code
        generated = True
    access_hash = str(record.get("access_code_hash") or "").strip()
    expected_hash = _share_access_code_hash(access_code)
    if not hmac.compare_digest(access_hash, expected_hash):
        record["access_code_hash"] = expected_hash
        generated = True
    return access_code, generated


def _encode_obscured_share_payload(*, scheme_code: str, base_ref: str, access_code: str) -> str:
    payload = SHARE_CODE_TINY_SEPARATOR.join([scheme_code, base_ref, access_code]).encode("utf-8")
    return base64.b85encode(payload).decode("ascii")


def _decode_obscured_share_payload(value: str) -> tuple[str, str, str]:
    try:
        raw = base64.b85decode(str(value or "").encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("分享码格式无效。") from exc
    parts = raw.split(SHARE_CODE_TINY_SEPARATOR)
    if len(parts) != 3:
        raise ValueError("分享码格式无效。")
    return parts[0], parts[1], parts[2]


def _encode_share_code(*, base_url: str, share_id: str = "", token: str = "", access_code: str = "") -> str:
    normalized_base_url = _normalize_public_base_url(base_url)
    clean_access_code = str(access_code or "").strip()
    if not clean_access_code:
        raise ValueError("分享记录缺少访问码。")
    parsed = urlparse(normalized_base_url)
    scheme_code = SHARE_CODE_TINY_SCHEME_CODES.get(parsed.scheme)
    if not scheme_code or not parsed.netloc:
        raise ValueError("公开访问地址必须以 http:// 或 https:// 开头。")
    base_ref = f"{parsed.netloc}{parsed.path.rstrip('/')}"
    if parsed.params:
        base_ref = f"{base_ref};{parsed.params}"
    if parsed.query:
        base_ref = f"{base_ref}?{parsed.query}"
    payload = _encode_obscured_share_payload(
        scheme_code=scheme_code,
        base_ref=base_ref,
        access_code=clean_access_code,
    )
    return f"{SHARE_CODE_OBSCURED_PREFIX}{payload}"


def _decode_share_code(value: str) -> dict:
    text = str(value or "").strip()
    if text.startswith(SHARE_CODE_OBSCURED_PREFIX):
        scheme_code, base_ref, access_code = _decode_obscured_share_payload(text[len(SHARE_CODE_OBSCURED_PREFIX):])
        scheme = SHARE_CODE_TINY_SCHEMES.get(scheme_code)
        if not scheme or not base_ref:
            raise ValueError("分享码格式无效。")
        base_url = _normalize_public_base_url(f"{scheme}://{base_ref}")
        share_id = ""
        token = ""
        access_code = str(access_code or "").strip()
    elif text.startswith(SHARE_CODE_TINY_PREFIX):
        parts = text.split("|", 4)
        if len(parts) != 5:
            raise ValueError("分享码格式无效。")
        scheme = SHARE_CODE_TINY_SCHEMES.get(parts[1])
        base_ref = unquote(parts[2]).strip()
        share_id = str(parts[3] or "").strip()
        token = str(parts[4] or "").strip()
        if not scheme or not base_ref:
            raise ValueError("分享码格式无效。")
        base_url = _normalize_public_base_url(f"{scheme}://{base_ref}")
        access_code = ""
    elif text.startswith(SHARE_CODE_COMPACT_PREFIX):
        payload = _base64url_decode_value(text[len(SHARE_CODE_COMPACT_PREFIX):])
        if not isinstance(payload, list) or len(payload) < 3:
            raise ValueError("分享码格式无效。")
        base_url = _normalize_public_base_url(str(payload[0] or ""))
        share_id = str(payload[1] or "").strip()
        token = str(payload[2] or "").strip()
        access_code = ""
    elif text.startswith(SHARE_CODE_PREFIX):
        payload = _base64url_decode_json(text[len(SHARE_CODE_PREFIX):])
        base_url = _normalize_public_base_url(str(payload.get("base_url") or ""))
        share_id = str(payload.get("share_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        access_code = ""
    else:
        payload = _base64url_decode_json(text)
        base_url = _normalize_public_base_url(str(payload.get("base_url") or ""))
        share_id = str(payload.get("share_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        access_code = ""
    if not base_url or (not access_code and (not share_id or not token)):
        raise ValueError("分享码缺少必要信息。")
    return {"base_url": base_url, "share_id": share_id, "token": token, "access_code": access_code}


def _share_receive_remote_error(action: str) -> ValueError:
    return ValueError(f"{action}失败：无法连接分享端，请确认分享码仍有效且分享端公开访问地址可用。")


def _normalize_share_options(options: ShareOptions | SharingConfig | dict | None) -> dict:
    if options is None:
        raw: dict = {}
    elif isinstance(options, (ShareOptions, SharingConfig)):
        raw = options.model_dump()
    elif isinstance(options, dict):
        raw = dict(options)
    else:
        raw = {}

    try:
        expires_days = int(raw.get("expires_days") or raw.get("default_expires_days") or 7)
    except (TypeError, ValueError):
        expires_days = 7
    expires_days = min(max(expires_days, 1), 90)
    model_types = _normalize_allowed_labels(
        raw.get("model_file_types"),
        aliases=MODEL_FILE_SUFFIX_ALIASES,
        default_labels=["3mf", "stl", "step", "obj"],
    )
    attachment_types = _normalize_allowed_labels(
        raw.get("attachment_file_types"),
        aliases=ATTACHMENT_FILE_SUFFIX_ALIASES,
        default_labels=["pdf", "excel"],
    )
    return {
        "expires_days": expires_days,
        "include_images": bool(raw.get("include_images", True)),
        "include_model_files": bool(raw.get("include_model_files", True)),
        "model_file_types": model_types,
        "include_attachments": bool(raw.get("include_attachments", True)),
        "attachment_file_types": attachment_types,
        "include_comments": bool(raw.get("include_comments", True)),
    }


def _normalize_allowed_labels(value: object, *, aliases: dict[str, set[str]], default_labels: list[str]) -> list[str]:
    labels: list[str] = []
    raw_items = value if isinstance(value, list) else default_labels
    for item in raw_items:
        label = str(item or "").strip().lower().lstrip(".")
        if label in aliases and label not in labels:
            labels.append(label)
    return labels or list(default_labels)


def _allowed_suffixes(labels: list[str], aliases: dict[str, set[str]]) -> set[str]:
    suffixes: set[str] = set()
    for label in labels:
        suffixes.update(aliases.get(str(label or "").lower(), set()))
    return suffixes


def _clean_relative_path(value: str) -> str:
    text = str(value or "").strip().split("#", 1)[0].split("?", 1)[0].strip().lstrip("/")
    if not text or text.startswith(("http://", "https://", "data:", "//")):
        return ""
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _relative_refs_from_value(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        ref = _clean_relative_path(value)
        if ref:
            refs.add(ref)
        return refs
    if isinstance(value, dict):
        for key in (
            "relPath",
            "localName",
            "fileName",
            "path",
            "thumbnailLocal",
            "thumbnailFile",
            "thumbnailRelPath",
            "avatarLocal",
            "avatarRelPath",
            "avatar",
        ):
            if key in value:
                refs.update(_relative_refs_from_value(value.get(key)))
    return refs


def _resolve_model_file(model_root: Path, rel_path: str) -> Path | None:
    clean_ref = _clean_relative_path(rel_path)
    if not clean_ref:
        return None
    model_root_resolved = model_root.resolve()
    candidates = [(model_root / clean_ref).resolve()]
    if "/" not in clean_ref:
        candidates.extend(
            [
                (model_root / "instances" / clean_ref).resolve(),
                (model_root / "images" / clean_ref).resolve(),
                (model_root / "attachments" / clean_ref).resolve(),
            ]
        )
    for candidate in candidates:
        try:
            candidate.relative_to(model_root_resolved)
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _add_share_file(
    *,
    files: list[dict],
    path_to_id: dict[str, str],
    model_root: Path,
    rel_path: str,
    role: str,
    source_model_dir: str,
) -> str:
    path = _resolve_model_file(model_root, rel_path)
    if path is None:
        return ""
    path_key = path.resolve().as_posix()
    if path_key in path_to_id:
        return path_to_id[path_key]
    file_id = f"f{len(files) + 1:04d}"
    rel_to_model = path.resolve().relative_to(model_root.resolve()).as_posix()
    stat = path.stat()
    files.append(
        {
            "id": file_id,
            "role": role,
            "source_model_dir": source_model_dir,
            "rel_path": rel_to_model,
            "name": path.name,
            "size": int(stat.st_size),
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
    )
    path_to_id[path_key] = file_id
    return file_id


def _share_file_counts(files: list[dict], source_model_dir: str = "") -> dict:
    counts = {"total": 0, "model": 0, "image": 0, "attachment": 0, "other": 0}
    clean_model_dir = str(source_model_dir or "").strip().strip("/")
    for file_item in files or []:
        if not isinstance(file_item, dict):
            continue
        if clean_model_dir and str(file_item.get("source_model_dir") or "").strip().strip("/") != clean_model_dir:
            continue
        role = str(file_item.get("role") or "").strip().lower()
        if role not in {"model", "image", "attachment"}:
            role = "other"
        counts["total"] += 1
        counts[role] += 1
    return counts


def _rewrite_file_refs(
    value: object,
    *,
    model_root: Path,
    source_model_dir: str,
    files: list[dict],
    path_to_id: dict[str, str],
    allowed_suffixes: set[str] | None = None,
    role: str,
) -> object:
    if isinstance(value, list):
        return [
            _rewrite_file_refs(
                item,
                model_root=model_root,
                source_model_dir=source_model_dir,
                files=files,
                path_to_id=path_to_id,
                allowed_suffixes=allowed_suffixes,
                role=role,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    item = copy.deepcopy(value)
    for key, raw_value in list(item.items()):
        if not isinstance(raw_value, str):
            continue
        if key == "fileName" and role != "attachment":
            continue
        for rel_ref in _relative_refs_from_value({key: raw_value}):
            suffix = Path(rel_ref).suffix.lower().lstrip(".")
            if allowed_suffixes is not None and suffix not in allowed_suffixes:
                continue
            file_id = _add_share_file(
                files=files,
                path_to_id=path_to_id,
                model_root=model_root,
                rel_path=rel_ref,
                role=role,
                source_model_dir=source_model_dir,
            )
            if file_id:
                item[f"{key}ShareFileId"] = file_id
    return item


def _filter_attachment_item(item: dict, allowed_suffixes: set[str]) -> bool:
    ref_name = str(item.get("localName") or item.get("relPath") or item.get("fileName") or item.get("name") or "")
    suffix = Path(ref_name).suffix.lower().lstrip(".")
    return suffix in allowed_suffixes or (suffix and suffix in allowed_suffixes)


def _build_share_model_entry(
    *,
    model_dir: str,
    options: dict,
    files: list[dict],
    path_to_id: dict[str, str],
) -> dict:
    clean_model_dir = str(model_dir or "").strip().strip("/")
    if not clean_model_dir:
        raise ValueError("模型目录不能为空。")
    model_root = (ARCHIVE_DIR / clean_model_dir).resolve()
    try:
        model_root.relative_to(ARCHIVE_DIR.resolve())
    except ValueError as exc:
        raise ValueError("非法模型路径。") from exc
    meta_path = model_root / "meta.json"
    if not meta_path.exists():
        raise ValueError(f"模型不存在：{clean_model_dir}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"模型元数据无法读取：{clean_model_dir}") from exc

    shared_meta = copy.deepcopy(meta)
    model_file_suffixes = _allowed_suffixes(options["model_file_types"], MODEL_FILE_SUFFIX_ALIASES)
    attachment_suffixes = _allowed_suffixes(options["attachment_file_types"], ATTACHMENT_FILE_SUFFIX_ALIASES)

    if options["include_images"]:
        for key in ("cover",):
            refs = _relative_refs_from_value(shared_meta.get(key))
            for rel_ref in refs:
                _add_share_file(
                    files=files,
                    path_to_id=path_to_id,
                    model_root=model_root,
                    rel_path=rel_ref,
                    role="image",
                    source_model_dir=clean_model_dir,
                )
        for key in ("designImages", "summaryImages"):
            shared_meta[key] = _rewrite_file_refs(
                shared_meta.get(key) if isinstance(shared_meta.get(key), list) else [],
                model_root=model_root,
                source_model_dir=clean_model_dir,
                files=files,
                path_to_id=path_to_id,
                role="image",
            )
        author = shared_meta.get("author") if isinstance(shared_meta.get("author"), dict) else {}
        shared_meta["author"] = _rewrite_file_refs(
            author,
            model_root=model_root,
            source_model_dir=clean_model_dir,
            files=files,
            path_to_id=path_to_id,
            role="image",
        )
    else:
        shared_meta["cover"] = ""
        shared_meta["coverUrl"] = ""
        shared_meta["designImages"] = []
        shared_meta["summaryImages"] = []
        if isinstance(shared_meta.get("author"), dict):
            for key in ("avatarLocal", "avatarRelPath", "avatar", "avatarUrl"):
                shared_meta["author"][key] = ""

    instances = shared_meta.get("instances") if isinstance(shared_meta.get("instances"), list) else []
    next_instances = []
    if options["include_model_files"]:
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            next_instance = copy.deepcopy(instance)
            file_name = Path(str(next_instance.get("fileName") or next_instance.get("name") or "")).name
            suffix = Path(file_name).suffix.lower().lstrip(".")
            if not file_name or suffix not in model_file_suffixes:
                continue
            file_id = _add_share_file(
                files=files,
                path_to_id=path_to_id,
                model_root=model_root,
                rel_path=f"instances/{file_name}",
                role="model",
                source_model_dir=clean_model_dir,
            )
            if not file_id:
                continue
            next_instance["shareFileId"] = file_id
            if options["include_images"]:
                next_instance["pictures"] = _rewrite_file_refs(
                    next_instance.get("pictures") if isinstance(next_instance.get("pictures"), list) else [],
                    model_root=model_root,
                    source_model_dir=clean_model_dir,
                    files=files,
                    path_to_id=path_to_id,
                    role="image",
                )
                next_instance = _rewrite_file_refs(
                    next_instance,
                    model_root=model_root,
                    source_model_dir=clean_model_dir,
                    files=files,
                    path_to_id=path_to_id,
                    role="image",
                )
            else:
                next_instance["pictures"] = []
                next_instance["thumbnailLocal"] = ""
                next_instance["thumbnailUrl"] = ""
            next_instances.append(next_instance)
    shared_meta["instances"] = next_instances

    if options["include_attachments"]:
        attachments = shared_meta.get("attachments") if isinstance(shared_meta.get("attachments"), list) else []
        next_attachments = []
        for attachment in attachments:
            if not isinstance(attachment, dict) or not _filter_attachment_item(attachment, attachment_suffixes):
                continue
            next_attachment = _rewrite_file_refs(
                attachment,
                model_root=model_root,
                source_model_dir=clean_model_dir,
                files=files,
                path_to_id=path_to_id,
                allowed_suffixes=attachment_suffixes,
                role="attachment",
            )
            next_attachments.append(next_attachment)
        shared_meta["attachments"] = next_attachments
    else:
        shared_meta["attachments"] = []

    if not options["include_comments"]:
        shared_meta["comments"] = []
        stats = shared_meta.get("stats") if isinstance(shared_meta.get("stats"), dict) else {}
        stats["comments"] = 0
        shared_meta["stats"] = stats

    local_import = shared_meta.get("localImport") if isinstance(shared_meta.get("localImport"), dict) else {}
    shared_meta["sharedImportSource"] = {
        "sourceModelDir": clean_model_dir,
        "sourceModelId": str(shared_meta.get("id") or local_import.get("designModelId") or ""),
        "sourceUrl": str(shared_meta.get("url") or ""),
        "title": str(shared_meta.get("title") or clean_model_dir),
    }
    return {
        "model_dir": clean_model_dir,
        "title": str(shared_meta.get("title") or clean_model_dir),
        "id": str(shared_meta.get("id") or local_import.get("designModelId") or ""),
        "origin_url": str(shared_meta.get("url") or ""),
        "source": str(shared_meta.get("source") or ""),
        "meta": shared_meta,
        "file_ids": [item["id"] for item in files if item.get("source_model_dir") == clean_model_dir],
    }


def _find_share_record(share_id: str) -> dict | None:
    clean_id = str(share_id or "").strip()
    if not clean_id:
        return None
    for item in _read_shares_state().get("items") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == clean_id:
            return item
    return None


def _find_share_record_by_access_code(access_code: str) -> dict | None:
    access_hash = _share_access_code_hash(str(access_code or "").strip())
    if not access_hash:
        return None
    for item in _read_shares_state().get("items") or []:
        if not isinstance(item, dict):
            continue
        if hmac.compare_digest(str(item.get("access_code_hash") or ""), access_hash):
            return item
        legacy_code = str(item.get("access_code") or "").strip()
        if legacy_code and hmac.compare_digest(legacy_code, str(access_code or "").strip()):
            return item
    return None


def _validate_share_record(share_id: str, token: str) -> dict:
    record = _find_share_record(share_id)
    if not record:
        raise ValueError("分享不存在或已被清除。")
    if not hmac.compare_digest(str(record.get("token_hash") or ""), _share_token_hash(token)):
        raise ValueError("分享 token 无效。")
    expires_at = parse_datetime(record.get("expires_at"))
    if expires_at is not None and expires_at < china_now():
        raise ValueError("分享已过期。")
    return record


def _validate_share_record_by_access_code(access_code: str) -> dict:
    clean_code = str(access_code or "").strip()
    if not clean_code:
        raise ValueError("分享访问码无效。")
    record = _find_share_record_by_access_code(clean_code)
    if not record:
        raise ValueError("分享不存在或已被清除。")
    expires_at = parse_datetime(record.get("expires_at"))
    if expires_at is not None and expires_at < china_now():
        raise ValueError("分享已过期。")
    return record


def _manifest_from_record(record: dict, token: str = "", access_code: str = "") -> dict:
    public_files = []
    access_query = f"access={quote(str(access_code or ''), safe='')}" if str(access_code or "").strip() else f"token={token}"
    for file_item in record.get("files") or []:
        if not isinstance(file_item, dict):
            continue
        public_files.append(
            {
                "id": str(file_item.get("id") or ""),
                "role": str(file_item.get("role") or ""),
                "source_model_dir": str(file_item.get("source_model_dir") or ""),
                "rel_path": str(file_item.get("rel_path") or ""),
                "name": str(file_item.get("name") or ""),
                "size": int(file_item.get("size") or 0),
                "mime_type": str(file_item.get("mime_type") or "application/octet-stream"),
                "url": f"/api/public/shares/{record.get('id')}/files/{file_item.get('id')}?{access_query}",
            }
        )
    return {
        "share_id": str(record.get("id") or ""),
        "created_at": str(record.get("created_at") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        "options": record.get("options") if isinstance(record.get("options"), dict) else {},
        "models": record.get("models") if isinstance(record.get("models"), list) else [],
        "file_counts": _share_file_counts(public_files),
        "files": public_files,
    }


def _share_record_summary(record: dict, *, base_url: str = "") -> dict:
    share_id = str(record.get("id") or "")
    files = record.get("files") if isinstance(record.get("files"), list) else []
    models = record.get("models") if isinstance(record.get("models"), list) else []
    expires_at_dt = parse_datetime(record.get("expires_at"))
    expired = expires_at_dt is not None and expires_at_dt < china_now()
    share_code = ""
    token = str(record.get("token") or "").strip()
    access_code = str(record.get("access_code") or "").strip()
    if share_id and base_url and access_code:
        share_code = _encode_share_code(base_url=base_url, access_code=access_code)
    return {
        "id": share_id,
        "created_at": str(record.get("created_at") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        "expired": expired,
        "options": record.get("options") if isinstance(record.get("options"), dict) else {},
        "model_count": len(models),
        "file_count": len(files),
        "file_counts": _share_file_counts(files),
        "share_code": share_code,
        "code_available": bool(share_code),
        "models": [
            {
                "title": str(item.get("title") or (item.get("meta") or {}).get("title") or ""),
                "id": str(item.get("id") or ""),
                "model_dir": str(item.get("model_dir") or ""),
                "origin_url": str(item.get("origin_url") or ""),
                "file_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("total", 0),
                "model_file_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("model", 0),
                "image_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("image", 0),
                "attachment_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("attachment", 0),
            }
            for item in models
            if isinstance(item, dict)
        ],
    }


def _active_share_model_conflicts(state: dict, model_dirs: list[str], now_dt) -> list[dict]:
    requested_dirs = {str(item or "").strip().strip("/") for item in model_dirs if str(item or "").strip().strip("/")}
    if not requested_dirs:
        return []
    conflicts: list[dict] = []
    for item in state.get("items") or []:
        if not isinstance(item, dict):
            continue
        expires_at = parse_datetime(item.get("expires_at"))
        if expires_at is None or expires_at < now_dt:
            continue
        matched_models = []
        for model in item.get("models") or []:
            if not isinstance(model, dict):
                continue
            model_dir = str(model.get("model_dir") or "").strip().strip("/")
            if model_dir not in requested_dirs:
                continue
            matched_models.append(
                {
                    "model_dir": model_dir,
                    "title": str(model.get("title") or (model.get("meta") or {}).get("title") or model_dir),
                }
            )
        if matched_models:
            conflicts.append(
                {
                    "share_id": str(item.get("id") or ""),
                    "created_at": str(item.get("created_at") or ""),
                    "expires_at": str(item.get("expires_at") or ""),
                    "code_available": bool(str(item.get("token") or "").strip()),
                    "models": matched_models,
                }
            )
    return conflicts


def _active_share_conflict_message(conflicts: list[dict]) -> str:
    names = []
    seen = set()
    for conflict in conflicts:
        for model in conflict.get("models") or []:
            title = str(model.get("title") or model.get("model_dir") or "模型").strip()
            if title and title not in seen:
                names.append(title)
                seen.add(title)
    if not names:
        names = ["所选模型"]
    preview_names = "、".join(names[:3]) + (f" 等 {len(names)} 个模型" if len(names) > 3 else "")
    if any(conflict.get("code_available") for conflict in conflicts):
        action = "请到设置 -> 模型分享 -> 已分享列表复制分享码，或撤销后重新生成。"
    else:
        action = "旧分享记录无法重新复制分享码，请到设置 -> 模型分享 -> 已分享列表撤销后再重新生成。"
    return f"{preview_names} 已经在分享有效期内，不能重复生成分享码。{action}"


def _fetch_share_manifest(share_code: str) -> tuple[dict, dict]:
    decoded = _decode_share_code(share_code)
    if decoded.get("access_code"):
        manifest_url = urljoin(
            f"{decoded['base_url']}/",
            f"api/public/share-access/{quote(decoded['access_code'], safe='')}/manifest",
        )
    else:
        manifest_url = urljoin(
            f"{decoded['base_url']}/",
            f"api/public/shares/{decoded['share_id']}/manifest?token={decoded['token']}",
        )
    try:
        response = requests.get(manifest_url, timeout=(8, 30), headers={"Accept": "application/json"})
    except requests.RequestException:
        raise _share_receive_remote_error("读取分享") from None
    if response.status_code != 200:
        raise ValueError(f"读取分享失败：HTTP {response.status_code}")
    try:
        manifest = response.json()
    except ValueError as exc:
        raise ValueError("分享 manifest 不是有效 JSON。") from exc
    if not isinstance(manifest, dict):
        raise ValueError("分享 manifest 格式无效。")
    return decoded, manifest


def _normalize_duplicate_hash(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if ":" in text else f"sha256:{text}"


def _collect_meta_duplicate_keys(meta: dict) -> dict[str, set[str]]:
    keys = {"model_ids": set(), "urls": set(), "shared_sources": set(), "file_hashes": set()}
    model_id = str(meta.get("id") or "").strip()
    local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
    if not model_id:
        model_id = str(local_import.get("designModelId") or "").strip()
    if model_id:
        keys["model_ids"].add(model_id)
    origin_url = str(meta.get("url") or "").strip()
    if origin_url:
        keys["urls"].add(origin_url)
        normalized_origin_url = normalize_source_url(origin_url)
        if normalized_origin_url:
            keys["urls"].add(normalized_origin_url)
        origin_model_id = extract_model_id(origin_url)
        if origin_model_id:
            keys["model_ids"].add(origin_model_id)
    shared_source = meta.get("sharedImportSource") if isinstance(meta.get("sharedImportSource"), dict) else {}
    source_model_id = str(shared_source.get("sourceModelId") or "").strip()
    source_url = str(shared_source.get("sourceUrl") or "").strip()
    source_dir = str(shared_source.get("sourceModelDir") or "").strip()
    if source_model_id:
        keys["model_ids"].add(source_model_id)
        keys["shared_sources"].add(f"model:{source_model_id}")
    if source_url:
        keys["urls"].add(source_url)
        normalized_source_url = normalize_source_url(source_url)
        if normalized_source_url:
            keys["urls"].add(normalized_source_url)
            keys["shared_sources"].add(f"url:{normalized_source_url}")
        source_url_model_id = extract_model_id(source_url)
        if source_url_model_id:
            keys["model_ids"].add(source_url_model_id)
            keys["shared_sources"].add(f"model:{source_url_model_id}")
        keys["shared_sources"].add(f"url:{source_url}")
    if source_dir:
        keys["shared_sources"].add(f"dir:{source_dir}")
    for hash_value in (
        local_import.get("configFingerprint"),
        local_import.get("fileHash"),
    ):
        normalized = _normalize_duplicate_hash(hash_value)
        if normalized:
            keys["file_hashes"].add(normalized)
    for instance in meta.get("instances") or []:
        if not isinstance(instance, dict):
            continue
        profile_id = str(instance.get("profileId") or instance.get("profile_id") or "").strip()
        if profile_id:
            keys["file_hashes"].add(f"design_profile:{profile_id}")
        inst_import = instance.get("localImport") if isinstance(instance.get("localImport"), dict) else {}
        for hash_value in (
            inst_import.get("configFingerprint"),
            inst_import.get("fileHash"),
        ):
            normalized = _normalize_duplicate_hash(hash_value)
            if normalized:
                keys["file_hashes"].add(normalized)
    return keys


def _build_library_duplicate_index() -> dict[str, dict[str, dict]]:
    index = {"model_ids": {}, "urls": {}, "shared_sources": {}, "file_hashes": {}}
    archive_root = ARCHIVE_DIR.resolve()

    def add_key(bucket: str, key: str, payload: dict) -> None:
        clean_key = str(key or "").strip()
        if clean_key:
            index[bucket].setdefault(clean_key, payload)

    for meta_path in sorted(ARCHIVE_DIR.rglob("meta.json")):
        try:
            model_root = meta_path.parent.resolve()
            model_dir = model_root.relative_to(archive_root).as_posix()
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        title = str(meta.get("title") or model_dir)
        source = str(meta.get("source") or "")
        payload = {"model_dir": model_dir, "title": title, "source": source}
        keys = _collect_meta_duplicate_keys(meta)
        for bucket, values in keys.items():
            for key in values:
                add_key(bucket, key, payload)

    subscription_state = task_state_store.load_subscriptions_state()
    for state_item in subscription_state.get("items") or []:
        if not isinstance(state_item, dict):
            continue
        subscription_id = str(state_item.get("id") or "").strip()
        payload = {
            "model_dir": "",
            "title": "订阅跟踪模型",
            "source": "subscription",
            "subscription_id": subscription_id,
        }
        for source_item in (state_item.get("current_items") or []) + (state_item.get("tracked_items") or []):
            if not isinstance(source_item, dict):
                continue
            model_id = str(source_item.get("model_id") or "").strip()
            source_url = normalize_source_url(str(source_item.get("url") or ""))
            task_key = str(source_item.get("task_key") or "").strip()
            if not model_id and task_key.startswith("model:"):
                model_id = task_key.split(":", 1)[1].strip()
            if not model_id and source_url:
                model_id = extract_model_id(source_url)
            add_key("model_ids", model_id, payload)
            add_key("shared_sources", f"model:{model_id}" if model_id else "", payload)
            add_key("urls", source_url, payload)
            add_key("shared_sources", f"url:{source_url}" if source_url else "", payload)
            add_key("urls", task_key if task_key.startswith(("http://", "https://", "/")) else "", payload)
    return index


def _find_manifest_duplicates(manifest: dict) -> list[dict]:
    duplicate_index = _build_library_duplicate_index()
    duplicates_by_match: dict[str, dict] = {}
    for model in manifest.get("models") or []:
        if not isinstance(model, dict):
            continue
        meta = model.get("meta") if isinstance(model.get("meta"), dict) else {}
        model_title = str(model.get("title") or meta.get("title") or "")
        model_id = str(model.get("id") or meta.get("id") or "")
        keys = _collect_meta_duplicate_keys(meta)
        for bucket, values in keys.items():
            for key in values:
                match = duplicate_index.get(bucket, {}).get(key)
                if not match:
                    continue
                duplicate_key = "|".join(
                    [
                        model_id,
                        model_title,
                        str(match.get("model_dir") or ""),
                        str(match.get("subscription_id") or ""),
                        str(match.get("title") or ""),
                    ]
                )
                existing = duplicates_by_match.get(duplicate_key)
                if existing:
                    existing.setdefault("reasons", []).append(bucket)
                    continue
                duplicates_by_match[duplicate_key] = (
                    {
                        "share_title": model_title,
                        "reason": bucket,
                        "reasons": [bucket],
                        "key": key,
                        "existing_model_dir": match.get("model_dir") or "",
                        "existing_title": match.get("title") or match.get("model_dir") or "",
                        "existing_source": match.get("source") or "",
                    }
                )
    return list(duplicates_by_match.values())


def _safe_share_filename(filename: str, fallback: str = "file") -> str:
    raw = Path(str(filename or "").strip()).name
    suffix = Path(raw).suffix.lower() or Path(fallback).suffix.lower()
    stem = Path(raw).stem.strip() or Path(fallback).stem or "file"
    safe_stem = re.sub(r"[^\w()\-\u4e00-\u9fff]+", "_", stem, flags=re.UNICODE).strip("._") or "file"
    safe_suffix = re.sub(r"[^.\w]+", "", suffix)[:16]
    return f"{safe_stem}{safe_suffix}"


def _safe_share_relative_path(rel_path: str, fallback_name: str = "file") -> Path:
    clean_ref = _clean_relative_path(rel_path)
    if not clean_ref:
        return Path(_safe_share_filename(fallback_name))
    parts = []
    raw_parts = list(Path(clean_ref).parts)
    for index, part in enumerate(raw_parts):
        if index == len(raw_parts) - 1:
            parts.append(_safe_share_filename(part, fallback=fallback_name))
        else:
            safe_part = re.sub(r"[^\w()\-\u4e00-\u9fff]+", "_", str(part or ""), flags=re.UNICODE).strip("._")
            parts.append(safe_part or "files")
    return Path(*parts)


def _unique_share_destination(path: Path) -> Path:
    candidate = path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        index += 1
    return candidate


def _share_file_lookup(manifest: dict) -> dict[str, dict]:
    return {
        str(item.get("id") or ""): item
        for item in manifest.get("files") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }


def _download_share_file(base_url: str, file_item: dict, target_path: Path) -> tuple[int, str]:
    file_url = str(file_item.get("url") or "")
    if not file_url:
        raise ValueError("分享文件缺少下载地址。")
    url = urljoin(f"{base_url}/", file_url.lstrip("/"))
    response = None
    try:
        response = requests.get(url, timeout=(8, 120), stream=True)
    except requests.RequestException:
        raise _share_receive_remote_error("下载分享文件") from None
    if response.status_code != 200:
        raise ValueError(f"下载分享文件失败：HTTP {response.status_code}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total_size = 0
    temp_path = target_path.with_name(f"{target_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total_size += len(chunk)
                digest.update(chunk)
                handle.write(chunk)
        temp_path.replace(target_path)
    except requests.RequestException:
        raise _share_receive_remote_error("下载分享文件") from None
    finally:
        if response is not None:
            response.close()
        temp_path.unlink(missing_ok=True)
    return total_size, digest.hexdigest()


def _replace_share_file_refs(value: object, file_id_to_rel: dict[str, str]) -> object:
    if isinstance(value, list):
        return [_replace_share_file_refs(item, file_id_to_rel) for item in value]
    if not isinstance(value, dict):
        return value
    item = copy.deepcopy(value)
    local_ref_keys = {
        "relPath",
        "localName",
        "path",
        "thumbnailLocal",
        "thumbnailFile",
        "thumbnailRelPath",
        "avatarLocal",
        "avatarRelPath",
        "avatar",
    }
    for key, raw_value in list(item.items()):
        if key not in local_ref_keys or not isinstance(raw_value, str):
            continue
        explicit_file_id = str(item.get(f"{key}ShareFileId") or "")
        if explicit_file_id and explicit_file_id in file_id_to_rel:
            continue
        clean_ref = _clean_relative_path(raw_value)
        if not clean_ref:
            continue
        for file_id, rel_path in file_id_to_rel.items():
            if Path(rel_path).name == Path(clean_ref).name:
                item[key] = rel_path
                break
    for key in list(item.keys()):
        if not key.endswith("ShareFileId"):
            continue
        original_key = key[: -len("ShareFileId")]
        file_id = str(item.get(key) or "")
        rel_path = file_id_to_rel.get(file_id)
        if rel_path:
            item[original_key] = rel_path
        item.pop(key, None)
    for key, raw in list(item.items()):
        if isinstance(raw, (dict, list)):
            item[key] = _replace_share_file_refs(raw, file_id_to_rel)
    return item


def _import_share_manifest(*, decoded: dict, manifest: dict) -> dict:
    duplicates = _find_manifest_duplicates(manifest)
    if duplicates:
        return {
            "success": False,
            "imported": [],
            "duplicates": duplicates,
            "message": "本地已存在分享中的模型，已停止导入。",
        }

    file_lookup = _share_file_lookup(manifest)
    imported: list[dict] = []
    now_iso = china_now_iso()
    for model_index, model in enumerate(manifest.get("models") or [], start=1):
        if not isinstance(model, dict):
            continue
        meta = copy.deepcopy(model.get("meta") if isinstance(model.get("meta"), dict) else {})
        title = str(meta.get("title") or model.get("title") or f"分享模型 {model_index}").strip()
        base_dir_name = _safe_share_filename(title, fallback=f"shared-{model_index}")
        model_root = _unique_share_destination(ARCHIVE_DIR / "local" / "shared" / base_dir_name)
        model_root.mkdir(parents=True, exist_ok=False)
        (model_root / "instances").mkdir(parents=True, exist_ok=True)
        (model_root / "images").mkdir(parents=True, exist_ok=True)
        (model_root / "attachments").mkdir(parents=True, exist_ok=True)
        file_id_to_rel: dict[str, str] = {}
        for file_id in model.get("file_ids") or []:
            file_item = file_lookup.get(str(file_id or ""))
            if not file_item:
                continue
            target_rel = _safe_share_relative_path(
                str(file_item.get("rel_path") or ""),
                fallback_name=str(file_item.get("name") or file_id),
            )
            target = _unique_share_destination(model_root / target_rel)
            size, digest = _download_share_file(decoded["base_url"], file_item, target)
            rel_path = target.relative_to(model_root).as_posix()
            file_id_to_rel[str(file_id)] = rel_path
            file_item["downloaded_size"] = size
            file_item["sha256"] = digest
        meta = _replace_share_file_refs(meta, file_id_to_rel)
        if isinstance(meta.get("cover"), str):
            clean_cover = _clean_relative_path(meta.get("cover"))
            if clean_cover and not (model_root / clean_cover).exists():
                for rel_path in file_id_to_rel.values():
                    if Path(rel_path).name == Path(clean_cover).name:
                        meta["cover"] = rel_path
                        break
        instances = meta.get("instances") if isinstance(meta.get("instances"), list) else []
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            share_file_id = str(instance.pop("shareFileId", "") or "")
            rel_path = file_id_to_rel.get(share_file_id)
            if rel_path:
                instance["fileName"] = Path(rel_path).name
                local_import = instance.get("localImport") if isinstance(instance.get("localImport"), dict) else {}
                local_import.update(
                    {
                        "sourcePath": rel_path,
                        "originalFilename": Path(rel_path).name,
                        "organizedAt": now_iso,
                        "moveFiles": False,
                        "shareId": manifest.get("share_id") or "",
                        "shareSourceBaseUrl": decoded["base_url"],
                    }
                )
                instance["localImport"] = local_import
        meta["source"] = "local"
        meta["url"] = ""
        meta["collectDate"] = now_iso
        meta["update_time"] = now_iso
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        if "分享导入" not in tags:
            tags.append("分享导入")
        meta["tags"] = tags
        shared_source = meta.get("sharedImportSource") if isinstance(meta.get("sharedImportSource"), dict) else {}
        shared_source.update(
            {
                "shareId": manifest.get("share_id") or "",
                "shareSourceBaseUrl": decoded["base_url"],
                "importedAt": now_iso,
            }
        )
        meta["sharedImportSource"] = shared_source
        local_import = meta.get("localImport") if isinstance(meta.get("localImport"), dict) else {}
        local_import.update(
            {
                "sourcePath": f"makerhub-share:{manifest.get('share_id') or ''}",
                "originalFilename": title,
                "organizedAt": now_iso,
                "moveFiles": False,
                "shareId": manifest.get("share_id") or "",
                "shareSourceBaseUrl": decoded["base_url"],
                "designModelId": str(shared_source.get("sourceModelId") or meta.get("id") or ""),
                "modelFileCount": len(instances),
            }
        )
        meta["localImport"] = local_import
        (model_root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        model_dir = model_root.relative_to(ARCHIVE_DIR.resolve()).as_posix()
        invalidate_model_detail_cache(model_dir)
        imported.append({"model_dir": model_dir, "title": title})
    if imported:
        invalidate_archive_snapshot("share_import")
    return {
        "success": bool(imported),
        "imported": imported,
        "duplicates": [],
        "message": f"已导入 {len(imported)} 个分享模型。" if imported else "分享中没有可导入模型。",
    }

MW_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
}
MAKERHUB_PUBLIC_PING_PATH = "/api/public/makerhub/ping"
MW_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
PROXY_TEST_TARGETS = (
    ("MakerWorld CN", "https://makerworld.com.cn/"),
    ("MakerWorld Global", "https://makerworld.com/"),
)
def _task_identity(item: dict) -> str:
    return str(item.get("id") or item.get("url") or item.get("title") or "")


def _now_iso() -> str:
    return china_now_iso()




def _make_github_version_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": "makerhub-version-check",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Accept": "text/plain, application/vnd.github+json;q=0.9, application/json;q=0.8, */*;q=0.1",
        }
    )
    return session


def _extract_github_text_from_response(response: requests.Response, source_kind: str) -> str:
    if source_kind in {"raw", "cdn"}:
        return str(response.text or "")

    payload = response.json()
    content = str(payload.get("content") or "").strip()
    encoding = str(payload.get("encoding") or "").strip().lower()
    if not content:
        raise ValueError("GitHub API 未返回文件内容")
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", errors="ignore")
    return content


def _extract_version_from_response(response: requests.Response, source_kind: str) -> str:
    return _extract_github_text_from_response(response, source_kind).strip()


def _parse_github_changelog(markdown: str, *, limit: int = 4) -> list[dict]:
    section_text = str(markdown or "")
    marker = "## 更新记录"
    marker_index = section_text.find(marker)
    if marker_index < 0:
        return []

    section_text = section_text[marker_index + len(marker):]
    next_section_match = re.search(r"^##\s+", section_text, flags=re.MULTILINE)
    if next_section_match:
        section_text = section_text[:next_section_match.start()]

    entries: list[dict] = []
    current_entry: dict | None = None
    version_pattern = re.compile(r"`v?([^`]+)`")

    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("### "):
            if current_entry and (current_entry.get("version") or current_entry.get("items")):
                entries.append(current_entry)
                if len(entries) >= limit:
                    break
            current_entry = {
                "date": line[4:].strip(),
                "version": "",
                "items": [],
            }
            continue

        if not current_entry or not line.startswith("- "):
            continue

        item_text = line[2:].strip()
        if not current_entry.get("version") and "版本号升级到" in item_text:
            version_match = version_pattern.search(item_text)
            if version_match:
                current_entry["version"] = str(version_match.group(1) or "").strip()
        current_entry["items"].append(item_text)

    if current_entry and len(entries) < limit and (current_entry.get("version") or current_entry.get("items")):
        entries.append(current_entry)

    return entries[:limit]


def _read_latest_github_version(proxy_config: ProxyConfig | None = None) -> dict[str, str]:
    proxies = _build_proxy_mapping(proxy_config) if proxy_config and bool(proxy_config.enabled) else {}
    targets = [
        ("raw", GITHUB_VERSION_URL),
        ("cdn", GITHUB_VERSION_CDN_URL),
        ("api", GITHUB_VERSION_API_URL),
    ]
    errors: list[str] = []
    session = _make_github_version_session()
    try:
        for source_kind, url in targets:
            headers = dict(session.headers)
            if source_kind == "api":
                headers["Accept"] = "application/vnd.github+json"
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    headers=headers,
                    proxies=proxies or None,
                    timeout=(6, 15),
                    allow_redirects=True,
                )
                response.raise_for_status()
                version = _extract_version_from_response(response, source_kind)
                if not version:
                    raise ValueError("VERSION 为空")
                return {
                    "version": version,
                    "source": source_kind,
                    "elapsed_ms": str(round((time.perf_counter() - started) * 1000, 1)),
                    "used_proxy": "true" if bool(proxies) else "false",
                }
            except Exception as exc:
                errors.append(f"{source_kind}:{_safe_error_message(exc)}")
    finally:
        session.close()

    raise RuntimeError(" | ".join(errors) or "GitHub 版本读取失败")


def _read_latest_github_changelog(proxy_config: ProxyConfig | None = None) -> dict:
    proxies = _build_proxy_mapping(proxy_config) if proxy_config and bool(proxy_config.enabled) else {}
    targets = [
        ("raw", GITHUB_README_URL),
        ("cdn", GITHUB_README_CDN_URL),
        ("api", GITHUB_README_API_URL),
    ]
    errors: list[str] = []
    session = _make_github_version_session()
    try:
        for source_kind, url in targets:
            headers = dict(session.headers)
            if source_kind == "api":
                headers["Accept"] = "application/vnd.github+json"
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    headers=headers,
                    proxies=proxies or None,
                    timeout=(6, 15),
                    allow_redirects=True,
                )
                response.raise_for_status()
                markdown = _extract_github_text_from_response(response, source_kind)
                items = _parse_github_changelog(markdown, limit=4)
                if not items:
                    raise ValueError("README 中未解析到更新记录")
                return {
                    "items": items,
                    "source": source_kind,
                    "elapsed_ms": str(round((time.perf_counter() - started) * 1000, 1)),
                    "used_proxy": "true" if bool(proxies) else "false",
                }
            except Exception as exc:
                errors.append(f"{source_kind}:{_safe_error_message(exc)}")
    finally:
        session.close()

    raise RuntimeError(" | ".join(errors) or "GitHub 更新日志读取失败")


async def _get_github_version_status(force: bool = False, proxy_config: ProxyConfig | None = None) -> dict:
    def _cache_payload() -> dict:
        return {
            "github_latest_version": str(github_version_cache.get("version") or ""),
            "github_version_checked_at": str(github_version_cache.get("checked_at_iso") or ""),
            "github_version_error": str(github_version_cache.get("error") or ""),
            "github_version_source": str(github_version_cache.get("source") or ""),
            "github_update_available": bool(
                str(github_version_cache.get("version") or "").strip()
                and str(github_version_cache.get("version") or "").strip() != APP_VERSION
            ),
        }

    async def _refresh_cache() -> None:
        now_inner = time.time()
        checked_at_iso_inner = _now_iso()
        try:
            result = await asyncio.to_thread(_read_latest_github_version, proxy_config)
            version = str(result.get("version") or "").strip()
            if not version:
                raise ValueError("GitHub VERSION 为空")
            github_version_cache.update(
                {
                    "version": version,
                    "checked_at": now_inner,
                    "checked_at_iso": checked_at_iso_inner,
                    "error": "",
                    "source": str(result.get("source") or ""),
                    "last_success_at": checked_at_iso_inner,
                }
            )
        except Exception as exc:
            github_version_cache.update(
                {
                    "checked_at": now_inner,
                    "checked_at_iso": checked_at_iso_inner,
                    "error": _safe_error_message(exc),
                }
            )

    async def _schedule_background_refresh() -> None:
        global github_version_refresh_task
        if github_version_refresh_task and not github_version_refresh_task.done():
            return
        async with github_version_refresh_lock:
            if github_version_refresh_task and not github_version_refresh_task.done():
                return
            github_version_refresh_task = asyncio.create_task(_refresh_cache())

    now = time.time()
    checked_at = float(github_version_cache.get("checked_at") or 0)
    cache_error = str(github_version_cache.get("error") or "")
    cache_ttl = GITHUB_VERSION_FAILURE_TTL_SECONDS if cache_error else GITHUB_VERSION_CACHE_TTL_SECONDS
    if not force and checked_at and now - checked_at < cache_ttl:
        return _cache_payload()

    if not force:
        await _schedule_background_refresh()
        return _cache_payload()

    await _refresh_cache()
    return _cache_payload()


async def _get_github_changelog_status(force: bool = False, proxy_config: ProxyConfig | None = None) -> dict:
    def _cache_payload() -> dict:
        return {
            "github_changelog": list(github_changelog_cache.get("items") or []),
            "github_changelog_checked_at": str(github_changelog_cache.get("checked_at_iso") or ""),
            "github_changelog_error": str(github_changelog_cache.get("error") or ""),
            "github_changelog_source": str(github_changelog_cache.get("source") or ""),
        }

    async def _refresh_cache() -> None:
        now_inner = time.time()
        checked_at_iso_inner = _now_iso()
        try:
            result = await asyncio.to_thread(_read_latest_github_changelog, proxy_config)
            items = list(result.get("items") or [])
            if not items:
                raise ValueError("GitHub 更新日志为空")
            github_changelog_cache.update(
                {
                    "items": items,
                    "checked_at": now_inner,
                    "checked_at_iso": checked_at_iso_inner,
                    "error": "",
                    "source": str(result.get("source") or ""),
                    "last_success_at": checked_at_iso_inner,
                }
            )
        except Exception as exc:
            github_changelog_cache.update(
                {
                    "checked_at": now_inner,
                    "checked_at_iso": checked_at_iso_inner,
                    "error": _safe_error_message(exc),
                }
            )

    async def _schedule_background_refresh() -> None:
        global github_changelog_refresh_task
        if github_changelog_refresh_task and not github_changelog_refresh_task.done():
            return
        async with github_changelog_refresh_lock:
            if github_changelog_refresh_task and not github_changelog_refresh_task.done():
                return
            github_changelog_refresh_task = asyncio.create_task(_refresh_cache())

    now = time.time()
    checked_at = float(github_changelog_cache.get("checked_at") or 0)
    cache_error = str(github_changelog_cache.get("error") or "")
    cache_ttl = GITHUB_CHANGELOG_FAILURE_TTL_SECONDS if cache_error else GITHUB_CHANGELOG_CACHE_TTL_SECONDS
    if not force and checked_at and now - checked_at < cache_ttl:
        return _cache_payload()

    if not force:
        await _schedule_background_refresh()
        return _cache_payload()

    await _refresh_cache()
    return _cache_payload()


def _with_version_status(payload: dict, version_status: dict) -> dict:
    return {
        **payload,
        "github_latest_version": str(version_status.get("github_latest_version") or ""),
        "github_version_checked_at": str(version_status.get("github_version_checked_at") or ""),
        "github_version_error": str(version_status.get("github_version_error") or ""),
        "github_version_source": str(version_status.get("github_version_source") or ""),
        "github_update_available": bool(version_status.get("github_update_available")),
    }


def _with_changelog_status(payload: dict, changelog_status: dict) -> dict:
    return {
        **payload,
        "github_changelog": list(changelog_status.get("github_changelog") or []),
        "github_changelog_checked_at": str(changelog_status.get("github_changelog_checked_at") or ""),
        "github_changelog_error": str(changelog_status.get("github_changelog_error") or ""),
        "github_changelog_source": str(changelog_status.get("github_changelog_source") or ""),
    }


def _build_proxy_mapping(config: ProxyConfig) -> dict[str, str]:
    http_proxy = str(config.http_proxy or "").strip()
    https_proxy = str(config.https_proxy or "").strip()
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    elif http_proxy:
        proxies["https"] = http_proxy
    return proxies


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text[:400]


def _normalize_public_base_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("公开访问地址必须以 http:// 或 https:// 开头。")
    return text.rstrip("/")


def _public_ping_url(base_url: str) -> str:
    normalized = _normalize_public_base_url(base_url)
    return urljoin(f"{normalized}/", MAKERHUB_PUBLIC_PING_PATH.lstrip("/"))


def _run_public_base_url_test(base_url: str) -> dict:
    normalized = _normalize_public_base_url(base_url)
    if not normalized:
        raise ValueError("请先填写公开访问地址。")

    ping_url = _public_ping_url(normalized)
    session = _make_test_session()
    started = time.perf_counter()
    try:
        response = session.get(
            ping_url,
            timeout=(6, 12),
            allow_redirects=True,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        content_type = str(response.headers.get("Content-Type") or "")
        if response.status_code != 200:
            raise ValueError(f"公开检测接口返回 HTTP {response.status_code}。")
        if "application/json" not in content_type.lower():
            raise ValueError("公开检测接口没有返回 JSON，可能反代到了错误页面。")
        payload = response.json()
        if not bool(payload.get("makerhub")):
            raise ValueError("公开检测接口不是 MakerHub 响应。")
        version = str(payload.get("app_version") or "")
        return {
            "ok": True,
            "message": f"公开访问地址可用，检测到 MakerHub v{version or 'unknown'}。",
            "base_url": normalized,
            "ping_url": ping_url,
            "final_url": str(response.url or ping_url),
            "status_code": int(response.status_code),
            "elapsed_ms": elapsed_ms,
            "app_version": version,
        }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"公开访问地址不可用：{_safe_error_message(exc)}") from exc
    finally:
        session.close()


def _make_test_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            **MW_BROWSER_HEADERS,
            "User-Agent": MW_BROWSER_USER_AGENT,
        }
    )
    return session


def _run_proxy_test(config: ProxyConfig) -> dict:
    if not bool(config.enabled):
        raise ValueError("请先启用 HTTP 代理。")

    proxies = _build_proxy_mapping(config)
    if not proxies:
        raise ValueError("请先填写 HTTP Proxy 或 HTTPS Proxy。")

    results: list[dict] = []
    session = _make_test_session()
    try:
        for name, url in PROXY_TEST_TARGETS:
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    proxies=proxies,
                    timeout=(6, 12),
                    allow_redirects=True,
                    stream=True,
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                final_url = str(response.url or url)
                history_codes = [int(item.status_code) for item in response.history]
                results.append(
                    {
                        "target": name,
                        "url": url,
                        "ok": True,
                        "status_code": int(response.status_code),
                        "elapsed_ms": elapsed_ms,
                        "final_url": final_url,
                        "history": history_codes,
                    }
                )
                response.close()
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                results.append(
                    {
                        "target": name,
                        "url": url,
                        "ok": False,
                        "elapsed_ms": elapsed_ms,
                        "error": _safe_error_message(exc),
                    }
                )
    finally:
        session.close()

    success_count = sum(1 for item in results if item.get("ok"))
    ok = success_count > 0
    if success_count == len(results):
        message = "HTTP 代理测试成功，国内站和国际站都可达。"
    elif success_count > 0:
        message = f"HTTP 代理部分成功，{success_count}/{len(results)} 个目标可达。"
    else:
        message = "HTTP 代理测试失败，两个目标都无法通过当前代理访问。"
    return {
        "ok": ok,
        "message": message,
        "results": results,
        "success_count": success_count,
        "target_count": len(results),
    }


def _run_cookie_test(payload: CookieTestRequest) -> dict:
    raw_cookie = sanitize_cookie_header(payload.cookie)
    if not raw_cookie:
        raise ValueError("请先填写 Cookie。")

    return probe_cookie_auth_status(
        payload.platform,
        raw_cookie,
        payload.proxy,
        include_limit_guard=False,
        use_cache=False,
    )


def _archive_event_snapshot() -> dict:
    queue = task_state_store.load_archive_queue()
    organize_tasks = task_state_store.load_organize_tasks()

    active = {}
    for item in queue.get("active") or []:
        identity = _task_identity(item)
        if not identity:
            continue
        active[identity] = {
            "mode": str(item.get("mode") or ""),
            "url": str(item.get("url") or ""),
            "title": str(item.get("title") or ""),
        }

    recent_failures = {_task_identity(item) for item in queue.get("recent_failures") or [] if _task_identity(item)}
    organize_success = {
        str(item.get("id") or item.get("fingerprint") or item.get("source_path") or "")
        for item in organize_tasks.get("items") or []
        if str(item.get("status") or "").lower() == "success"
        and str(item.get("id") or item.get("fingerprint") or item.get("source_path") or "")
    }

    return {
        "active": active,
        "recent_failures": recent_failures,
        "running_count": int(queue.get("running_count") or 0),
        "queued_count": int(queue.get("queued_count") or 0),
        "failed_count": int(queue.get("failed_count") or 0),
        "organize_success": organize_success,
    }


def _public_config_payload(config) -> dict:
    return {
        "app_version": APP_VERSION,
        "cookies": [item.model_dump() for item in config.cookies],
        "proxy": config.proxy.model_dump(),
        "notifications": config.notifications.model_dump(),
        "sharing": config.sharing.model_dump(),
        "mobile_import": {
            "enabled": bool(config.mobile_import.enabled),
            "token_prefix": config.mobile_import.token_prefix,
            "created_at": config.mobile_import.created_at,
            "last_used_at": config.mobile_import.last_used_at,
        },
        "user": {
            "username": config.user.username,
            "display_name": config.user.display_name,
            "password_hint": config.user.password_hint,
            "theme_preference": config.user.theme_preference,
            "password_updated_at": config.user.password_updated_at,
        },
        "api_tokens": [item.model_dump() for item in auth_manager.list_api_tokens()],
        "subscriptions": [item.model_dump() for item in config.subscriptions],
        "subscription_settings": config.subscription_settings.model_dump(),
        "missing_3mf": [item.model_dump() for item in config.missing_3mf],
        "organizer": config.organizer.model_dump(),
        "remote_refresh": config.remote_refresh.model_dump(),
        "three_mf_limits": config.three_mf_limits.model_dump(),
        "advanced": config.advanced.model_dump(),
        "remote_refresh_state": compact_remote_refresh_state(
            task_state_store.load_remote_refresh_state(),
            include_current=False,
        ),
        "paths": config.paths.model_dump(),
    }


def _session_payload(identity: dict, config=None) -> dict:
    if not identity:
        return {
            "authenticated": False,
            "kind": "",
            "username": "",
            "display_name": "",
        }

    return {
        "authenticated": True,
        "kind": identity.get("kind") or "",
        "username": config.user.username if config else "",
        "display_name": config.user.display_name if config else "",
    }


def _require_session_auth(request: Request) -> None:
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") != "session":
        raise HTTPException(status_code=403, detail="此操作需要登录会话。")


def _extract_bearer_token(request: Request) -> str:
    auth_header = str(request.headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    custom = str(request.headers.get("X-Mobile-Import-Token") or request.headers.get("X-API-Token") or request.headers.get("X-Token") or "").strip()
    if custom:
        return custom
    return str(request.query_params.get("token") or "").strip()


def _validate_mobile_import_token(raw_token: str) -> None:
    token_hash = hash_api_token(raw_token)
    config = store.load()
    mobile_import = config.mobile_import
    if (
        not mobile_import.enabled
        or not mobile_import.token_hash
        or not raw_token
        or not hmac.compare_digest(str(mobile_import.token_hash), token_hash)
    ):
        raise HTTPException(status_code=401, detail="移动端导入 Token 无效。")


def _generate_mobile_import_token() -> str:
    return f"mhi_{secrets.token_urlsafe(24)}"


def _require_mobile_import_token(request: Request) -> None:
    raw_token = _extract_bearer_token(request)
    _validate_mobile_import_token(raw_token)
    _mark_mobile_import_used()


def _mark_mobile_import_used() -> None:
    config = store.load()
    mobile_import = config.mobile_import
    mobile_import.last_used_at = china_now_iso()
    config.mobile_import = mobile_import
    store.save(config)


async def _run_mobile_import_upload(files: list[UploadFile], paths: list[str]) -> dict:
    result = await run_task_api(
        upload_local_import_files,
        files=files,
        paths=paths,
        store=store,
        task_store=task_state_store,
    )
    if BACKGROUND_TASKS_ENABLED and result.get("trigger_organizer", True):
        try:
            await run_task_api(local_organizer.run_once)
            result["triggered"] = True
        except Exception as exc:
            result["triggered"] = False
            result["trigger_error"] = str(exc)
            append_business_log("organizer", "mobile_import_trigger_failed", str(exc), level="warning")
    else:
        result["triggered"] = False
    append_business_log(
        "organizer",
        "mobile_import_uploaded",
        "移动端文件已上传。",
        uploaded_count=len(result.get("uploaded") or []),
        mode=result.get("mode") or "",
    )
    return {
        **result,
        "message": result.get("message") or "已上传",
    }


def _run_mobile_import_background(files: list[UploadFile], paths: list[str]) -> None:
    try:
        result = upload_local_import_files(
            files=files,
            paths=paths,
            store=store,
            task_store=task_state_store,
        )
        if BACKGROUND_TASKS_ENABLED and result.get("trigger_organizer", True):
            try:
                local_organizer.run_once()
            except Exception as exc:
                append_business_log("organizer", "mobile_import_trigger_failed", str(exc), level="warning")
        append_business_log(
            "organizer",
            "mobile_import_uploaded",
            "移动端文件已上传。",
            uploaded_count=len(result.get("uploaded") or []),
            mode=result.get("mode") or "",
        )
    except Exception as exc:
        append_business_log("organizer", "mobile_import_upload_failed", str(exc), level="error")


def _infer_mobile_upload_suffix(raw_body: bytes) -> str:
    head = raw_body[:512]
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(BytesIO(raw_body)) as archive:
                names = [str(item.filename or "").lower() for item in archive.infolist()]
            if any(name.endswith(".model") or name.startswith("3d/") for name in names):
                return ".3mf"
        except Exception:
            pass
        return ".zip"
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if head[:5].lower() == b"solid":
        return ".stl"
    if len(raw_body) >= 84:
        try:
            triangle_count = int.from_bytes(raw_body[80:84], "little")
            expected_size = 84 + triangle_count * 50
            if triangle_count > 0 and expected_size == len(raw_body):
                return ".stl"
        except Exception:
            pass
    return ""


def _mobile_raw_upload_file(raw_body: bytes, filename: str) -> tuple[UploadFile, str]:
    clean_name = Path(str(filename or "").strip()).name or "wechat-upload"
    if not Path(clean_name).suffix:
        clean_name = f"{clean_name}{_infer_mobile_upload_suffix(raw_body)}"
    upload = UploadFile(file=BytesIO(raw_body), filename=clean_name)
    return upload, clean_name


@router.get("/bootstrap")
async def get_bootstrap(request: Request):
    identity = getattr(request.state, "auth_identity", None) or {}
    config = await run_ui_io(store.load)
    payload = {
        "app_version": APP_VERSION,
        "session": _session_payload(identity, config=config if identity else None),
        "theme_preference": config.user.theme_preference if identity else "",
    }
    return _with_version_status(payload, await _get_github_version_status(proxy_config=config.proxy))


@router.get("/public/makerhub/ping")
async def public_makerhub_ping():
    return {
        "makerhub": True,
        "app_version": APP_VERSION,
    }


@router.get("/public/shares/{share_id}/manifest")
async def public_share_manifest(share_id: str, token: str = Query("")):
    try:
        record = await run_ui_io(_validate_share_record, share_id, token)
        return await run_ui_io(_manifest_from_record, record, token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/public/share-access/{access_code}/manifest")
async def public_share_access_manifest(access_code: str):
    try:
        record = await run_ui_io(_validate_share_record_by_access_code, access_code)
        return await run_ui_io(_manifest_from_record, record, "", access_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/public/shares/{share_id}/files/{file_id}")
async def public_share_file(share_id: str, file_id: str, token: str = Query(""), access: str = Query("")):
    try:
        if str(access or "").strip():
            record = await run_ui_io(_validate_share_record_by_access_code, access)
        else:
            record = await run_ui_io(_validate_share_record, share_id, token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if str(record.get("id") or "") != str(share_id or ""):
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    file_item = next(
        (
            item for item in record.get("files") or []
            if isinstance(item, dict) and str(item.get("id") or "") == str(file_id or "")
        ),
        None,
    )
    if not file_item:
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    source_model_dir = str(file_item.get("source_model_dir") or "").strip().strip("/")
    rel_path = str(file_item.get("rel_path") or "").strip().lstrip("/")
    target = (ARCHIVE_DIR / source_model_dir / rel_path).resolve()
    try:
        target.relative_to((ARCHIVE_DIR / source_model_dir).resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="分享文件路径无效。") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="分享文件不存在。")
    return FileResponse(
        target,
        media_type=str(file_item.get("mime_type") or "application/octet-stream"),
        filename=str(file_item.get("name") or target.name),
    )


@router.get("/config")
async def get_config():
    config = await run_ui_io(store.load)
    payload = await run_ui_io(_public_config_payload, config)
    return _with_version_status(payload, await _get_github_version_status(proxy_config=config.proxy))


@router.get("/system/update")
async def get_system_update(force: bool = Query(False)):
    config = await run_ui_io(store.load)
    payload = await run_ui_io(get_update_status)
    payload = _with_version_status(payload, await _get_github_version_status(force=force, proxy_config=config.proxy))
    return _with_changelog_status(payload, await _get_github_changelog_status(force=force, proxy_config=config.proxy))


@router.get("/system/version")
async def get_system_version(force: bool = Query(False)):
    config = await run_ui_io(store.load)
    payload = {"app_version": APP_VERSION}
    return _with_version_status(payload, await _get_github_version_status(force=force, proxy_config=config.proxy))


@router.post("/system/update")
async def start_system_update(payload: SystemUpdateRequest, request: Request):
    _require_session_auth(request)
    identity = getattr(request.state, "auth_identity", None) or {}
    requested_by = str(identity.get("username") or "").strip()
    config = store.load()
    try:
        response = request_system_update(
            requested_by=requested_by,
            target_version=str(payload.target_version or ""),
            force=bool(payload.force),
        )
        response = _with_version_status(response, await _get_github_version_status(proxy_config=config.proxy))
        return _with_changelog_status(response, await _get_github_changelog_status(proxy_config=config.proxy))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/config/cookies")
async def save_cookies(payload: list[CookiePair], request: Request):
    _require_session_auth(request)
    config = store.load()
    config.cookies = [
        CookiePair(platform=item.platform, cookie=sanitize_cookie_header(item.cookie))
        for item in payload
    ]
    append_business_log(
        "settings",
        "cookies_saved",
        "Cookie 配置已保存。",
        count=len(payload),
        platforms=[item.platform for item in payload],
    )
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/proxy")
async def save_proxy(payload: ProxyConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.proxy = payload
    append_business_log(
        "settings",
        "proxy_saved",
        "HTTP 代理配置已保存。",
        enabled=payload.enabled,
        has_http_proxy=bool(payload.http_proxy),
        has_https_proxy=bool(payload.https_proxy),
    )
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/proxy/test")
async def test_proxy(payload: ProxyConfig, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(_run_proxy_test, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "settings",
        "proxy_tested",
        result.get("message") or "HTTP 代理测试已完成。",
        ok=bool(result.get("ok")),
        enabled=payload.enabled,
        has_http_proxy=bool(payload.http_proxy),
        has_https_proxy=bool(payload.https_proxy),
        success_count=int(result.get("success_count") or 0),
        target_count=int(result.get("target_count") or 0),
        results=result.get("results") or [],
    )
    return result


@router.post("/config/cookies/test")
async def test_cookie(payload: CookieTestRequest, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(_run_cookie_test, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "settings",
        "cookie_tested",
        result.get("message") or "Cookie 测试已完成。",
        ok=bool(result.get("ok")),
        platform=payload.platform,
        used_proxy=bool(result.get("used_proxy")),
        success_count=int(result.get("success_count") or 0),
        target_count=int(result.get("target_count") or 0),
        results=result.get("results") or [],
    )
    return result


@router.post("/config/notifications")
async def save_notifications(payload: NotificationConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.notifications = payload
    append_business_log("settings", "notifications_saved", "通知配置已保存。", enabled=payload.enabled)
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/sharing")
async def save_sharing(payload: SharingConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    try:
        normalized_url = _normalize_public_base_url(payload.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    options = _normalize_share_options(payload)
    config.sharing = SharingConfig(
        public_base_url=normalized_url,
        default_expires_days=options["expires_days"],
        include_images=options["include_images"],
        include_model_files=options["include_model_files"],
        model_file_types=options["model_file_types"],
        include_attachments=options["include_attachments"],
        attachment_file_types=options["attachment_file_types"],
        include_comments=options["include_comments"],
    )
    append_business_log(
        "settings",
        "sharing_saved",
        "模型分享配置已保存。",
        public_base_url=normalized_url,
    )
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/sharing/test")
async def test_sharing(payload: SharingConfig, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(_run_public_base_url_test, payload.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "settings",
        "sharing_public_url_tested",
        result.get("message") or "模型分享公开访问地址检测完成。",
        ok=bool(result.get("ok")),
        public_base_url=result.get("base_url") or payload.public_base_url,
        ping_url=result.get("ping_url") or "",
        status_code=int(result.get("status_code") or 0),
        elapsed_ms=result.get("elapsed_ms"),
    )
    return result


@router.get("/config/sharing/check")
async def check_sharing_public_url(request: Request):
    _require_session_auth(request)
    config = await run_ui_io(store.load)
    try:
        return await run_task_api(_run_public_base_url_test, config.sharing.public_base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sharing/create")
async def create_model_share(payload: ShareCreateRequest, request: Request):
    _require_session_auth(request)

    def _create_payload() -> dict:
        config = store.load()
        base_url = _normalize_public_base_url(config.sharing.public_base_url)
        if not base_url:
            raise ValueError("请先到设置 -> 模型分享 配置公开访问地址。")
        _run_public_base_url_test(base_url)
        options = _normalize_share_options(payload.options)
        model_dirs = []
        seen_dirs = set()
        for item in payload.model_dirs or []:
            clean_item = str(item or "").strip().strip("/")
            if clean_item and clean_item not in seen_dirs:
                model_dirs.append(clean_item)
                seen_dirs.add(clean_item)
        if not model_dirs:
            raise ValueError("请选择要分享的模型。")
        state = _read_shares_state()
        share_id = _new_share_id()
        token = _new_share_token()
        now_dt = china_now()
        state["items"] = [
            item for item in state.get("items") or []
            if isinstance(item, dict) and parse_datetime(item.get("expires_at")) not in (None,)
            and parse_datetime(item.get("expires_at")) >= now_dt
        ]
        conflicts = _active_share_model_conflicts(state, model_dirs, now_dt)
        if conflicts:
            conflict_message = _active_share_conflict_message(conflicts)
            append_business_log(
                "sharing",
                "share_duplicate_blocked",
                conflict_message,
                level="warning",
                model_dirs=model_dirs,
                conflict_count=len(conflicts),
                share_ids=[item.get("share_id") or "" for item in conflicts],
            )
            raise ValueError(conflict_message)
        expires_at = now_dt + timedelta(days=options["expires_days"])
        files: list[dict] = []
        path_to_id: dict[str, str] = {}
        models = [
            _build_share_model_entry(
                model_dir=model_dir,
                options=options,
                files=files,
                path_to_id=path_to_id,
            )
            for model_dir in model_dirs
        ]
        record = {
            "id": share_id,
            "token": token,
            "token_hash": _share_token_hash(token),
            "created_at": now_dt.isoformat(timespec="seconds"),
            "expires_at": expires_at.isoformat(timespec="seconds"),
            "options": options,
            "models": models,
            "files": files,
        }
        access_code, _ = _ensure_share_access_code(record)
        state["items"].append(record)
        _write_shares_state(state)
        share_code = _encode_share_code(base_url=base_url, access_code=access_code)
        return {
            "success": True,
            "share_id": share_id,
            "share_code": share_code,
            "expires_at": record["expires_at"],
            "model_count": len(models),
            "file_count": len(files),
            "file_counts": _share_file_counts(files),
            "message": f"已生成 {len(models)} 个模型的分享码。",
        }

    try:
        result = await run_task_api(_create_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_created",
        result.get("message") or "模型分享码已生成。",
        share_id=result.get("share_id") or "",
        model_count=int(result.get("model_count") or 0),
        file_count=int(result.get("file_count") or 0),
    )
    return result


@router.get("/sharing/shares")
async def list_model_shares(request: Request):
    _require_session_auth(request)

    def _list_payload() -> dict:
        config = store.load()
        base_url = _normalize_public_base_url(config.sharing.public_base_url)
        state = _read_shares_state()
        items = [
            _share_record_summary(item, base_url=base_url)
            for item in state.get("items") or []
            if isinstance(item, dict)
        ]
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {
            "success": True,
            "items": items,
            "count": len(items),
        }

    return await run_ui_io(_list_payload)


@router.post("/sharing/shares/{share_id}/code")
async def ensure_model_share_code(share_id: str, request: Request):
    _require_session_auth(request)

    def _code_payload() -> dict:
        config = store.load()
        base_url = _normalize_public_base_url(config.sharing.public_base_url)
        if not base_url:
            raise ValueError("请先到设置 -> 模型分享 配置公开访问地址。")
        clean_id = str(share_id or "").strip()
        state = _read_shares_state()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        target = next((item for item in items if str(item.get("id") or "") == clean_id), None)
        if not target:
            raise ValueError("分享记录不存在。")
        expires_at = parse_datetime(target.get("expires_at"))
        if expires_at is not None and expires_at < china_now():
            raise ValueError("分享已过期，请清理或重新分享。")
        token = str(target.get("token") or "").strip()
        generated = False
        if not token:
            token = _new_share_token()
            target["token"] = token
            target["token_hash"] = _share_token_hash(token)
            generated = True
        access_code, access_generated = _ensure_share_access_code(target)
        generated = generated or access_generated
        if generated:
            _write_shares_state({"items": items})
        share_code = _encode_share_code(base_url=base_url, access_code=access_code)
        return {
            "success": True,
            "share_id": clean_id,
            "share_code": share_code,
            "generated": generated,
            "message": "分享码已生成。" if generated else "分享码已读取。",
        }

    try:
        result = await run_ui_io(_code_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_code_generated" if result.get("generated") else "share_code_accessed",
        result.get("message") or "分享码已处理。",
        share_id=result.get("share_id") or "",
    )
    return result


@router.delete("/sharing/shares/{share_id}")
async def revoke_model_share(share_id: str, request: Request):
    _require_session_auth(request)

    def _delete_payload() -> dict:
        clean_id = str(share_id or "").strip()
        state = _read_shares_state()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        next_items = [item for item in items if str(item.get("id") or "") != clean_id]
        if len(next_items) == len(items):
            raise ValueError("分享记录不存在。")
        state["items"] = next_items
        _write_shares_state(state)
        return {
            "success": True,
            "message": "分享已撤销。",
            "items": [_share_record_summary(item) for item in next_items],
            "count": len(next_items),
        }

    try:
        result = await run_ui_io(_delete_payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    append_business_log("sharing", "share_revoked", "模型分享已撤销。", share_id=share_id)
    return result


@router.post("/sharing/shares/cleanup")
async def cleanup_model_shares(payload: ShareDeleteExpiredRequest, request: Request):
    _require_session_auth(request)

    def _cleanup_payload() -> dict:
        if not payload.include_expired:
            raise ValueError("请选择要清理的分享记录。")
        state = _read_shares_state()
        now_dt = china_now()
        items = [item for item in state.get("items") or [] if isinstance(item, dict)]
        next_items = []
        removed_count = 0
        for item in items:
            expires_at = parse_datetime(item.get("expires_at"))
            if expires_at is not None and expires_at < now_dt:
                removed_count += 1
                continue
            next_items.append(item)
        state["items"] = next_items
        _write_shares_state(state)
        return {
            "success": True,
            "message": f"已清理 {removed_count} 条过期分享。",
            "removed_count": removed_count,
            "items": [_share_record_summary(item) for item in next_items],
            "count": len(next_items),
        }

    try:
        result = await run_ui_io(_cleanup_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_expired_cleaned",
        result.get("message") or "过期分享已清理。",
        removed_count=int(result.get("removed_count") or 0),
    )
    return result


@router.post("/sharing/receive/preview")
async def preview_model_share(payload: ShareReceiveRequest, request: Request):
    _require_session_auth(request)

    def _preview_payload() -> dict:
        decoded, manifest = _fetch_share_manifest(payload.share_code)
        duplicates = _find_manifest_duplicates(manifest)
        models = manifest.get("models") if isinstance(manifest.get("models"), list) else []
        files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
        file_counts = _share_file_counts(files)
        return {
            "success": True,
            "can_import": not duplicates,
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "manifest": {
                "share_id": str(manifest.get("share_id") or decoded.get("share_id") or ""),
                "created_at": str(manifest.get("created_at") or ""),
                "expires_at": str(manifest.get("expires_at") or ""),
                "model_count": len(models),
                "file_count": len(files),
                "file_counts": file_counts,
                "models": [
                    {
                        "title": str(item.get("title") or (item.get("meta") or {}).get("title") or ""),
                        "id": str(item.get("id") or ""),
                        "origin_url": str(item.get("origin_url") or ""),
                        "file_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("total", 0),
                        "model_file_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("model", 0),
                        "image_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("image", 0),
                        "attachment_count": _share_file_counts(files, str(item.get("model_dir") or "")).get("attachment", 0),
                    }
                    for item in models
                    if isinstance(item, dict)
                ],
            },
            "message": "分享可导入。" if not duplicates else "本地已存在分享中的模型，不能重复导入。",
        }

    try:
        return await run_task_api(_preview_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sharing/receive/import")
async def import_model_share(payload: ShareReceiveRequest, request: Request):
    _require_session_auth(request)

    def _import_payload() -> dict:
        decoded, manifest = _fetch_share_manifest(payload.share_code)
        return _import_share_manifest(decoded=decoded, manifest=manifest)

    try:
        result = await run_task_api(_import_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    append_business_log(
        "sharing",
        "share_imported" if result.get("success") else "share_import_skipped_duplicate",
        result.get("message") or "分享导入已处理。",
        imported_count=len(result.get("imported") or []),
        duplicate_count=len(result.get("duplicates") or []),
    )
    return result


@router.post("/config/user")
async def save_user(payload: UserSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.username = payload.username.strip() or "admin"
    config.user.display_name = payload.display_name.strip() or "Admin"
    config.user.password_hint = payload.password_hint.strip()
    append_business_log("settings", "user_saved", "用户信息已保存。", username=config.user.username)
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/theme")
async def save_theme(payload: ThemeSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.theme_preference = payload.theme_preference
    append_business_log("settings", "theme_saved", "主题设置已保存。", theme_preference=payload.theme_preference)
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/organizer")
async def save_organizer(payload: OrganizeTask, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.organizer = payload
    append_business_log(
        "settings",
        "organizer_saved",
        "本地整理配置已保存。",
        source_dir=payload.source_dir,
        target_dir=payload.target_dir,
        move_files=payload.move_files,
    )
    saved = store.save(config)
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/mobile-import/token")
async def reset_mobile_import_token(payload: MobileImportTokenResetRequest, request: Request):
    _require_session_auth(request)
    config = store.load()
    raw_token = _generate_mobile_import_token()
    config.mobile_import = MobileImportConfig(
        enabled=bool(payload.enabled),
        token_prefix=raw_token[:12],
        token_hash=hash_api_token(raw_token),
        created_at=china_now_iso(),
        last_used_at="",
    )
    saved = store.save(config)
    append_business_log(
        "settings",
        "mobile_import_token_reset",
        "移动端导入 Token 已生成。",
        enabled=config.mobile_import.enabled,
        token_prefix=config.mobile_import.token_prefix,
    )
    return {
        **_with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy)),
        "token": raw_token,
        "message": "移动端导入 Token 已生成。",
    }


@router.post("/config/mobile-import/disable")
async def disable_mobile_import(request: Request):
    _require_session_auth(request)
    config = store.load()
    config.mobile_import.enabled = False
    saved = store.save(config)
    append_business_log("settings", "mobile_import_disabled", "移动端导入 Token 已停用。")
    return _with_version_status(_public_config_payload(saved), await _get_github_version_status(proxy_config=saved.proxy))


@router.post("/config/remote-refresh")
async def save_remote_refresh(payload: RemoteRefreshConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.remote_refresh = payload
    store.save(config)
    state = remote_refresh_manager.notify_config_updated()
    append_business_log(
        "settings",
        "remote_refresh_saved",
        "源端刷新设置已保存。",
        enabled=payload.enabled,
        cron=payload.cron,
        next_run_at=state.get("next_run_at"),
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.post("/config/three-mf-limits")
async def save_three_mf_limits(payload: ThreeMfDownloadLimitsConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.three_mf_limits = payload
    store.save(config)
    append_business_log(
        "settings",
        "three_mf_limits_saved",
        "每日 3MF 下载上限已保存。",
        cn_daily_limit=payload.cn_daily_limit,
        global_daily_limit=payload.global_daily_limit,
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.post("/config/advanced")
async def save_advanced_runtime(payload: AdvancedRuntimeConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.advanced = payload
    store.save(config)
    append_business_log(
        "settings",
        "advanced_runtime_saved",
        "高级运行参数已保存。",
        remote_refresh_model_workers=payload.remote_refresh_model_workers,
        makerworld_request_limit=payload.makerworld_request_limit,
        comment_asset_download_limit=payload.comment_asset_download_limit,
        three_mf_download_limit=payload.three_mf_download_limit,
        disk_io_limit=payload.disk_io_limit,
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.post("/config/subscriptions")
async def save_subscription_settings(payload: SubscriptionSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.subscription_settings = payload
    store.save(config)
    append_business_log(
        "settings",
        "subscription_settings_saved",
        "订阅设置已保存。",
        default_cron=payload.default_cron,
        default_enabled=payload.default_enabled,
        default_initialize_from_source=payload.default_initialize_from_source,
        card_sort=payload.card_sort,
        hide_disabled_from_cards=payload.hide_disabled_from_cards,
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status(proxy_config=config.proxy))


@router.get("/dashboard")
async def get_dashboard_data():
    return await run_web_io(lambda: build_dashboard_payload(store.load()))


@router.get("/models")
async def get_models_data(
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
):
    return await run_web_io(
        build_models_payload,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/source-library")
async def get_source_library_data(
    q: str = Query("", description="搜索来源卡标题"),
):
    return await run_web_io(
        build_source_library_payload,
        q=q,
        store=store,
        task_store=task_state_store,
    )


@router.get("/source-library/sources/{source_type}/{source_key}")
async def get_source_group_models(
    source_type: str,
    source_key: str,
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
):
    payload = await run_web_io(
        build_source_group_models_payload,
        source_type=source_type,
        source_key=source_key,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=page,
        page_size=page_size,
        store=store,
        task_store=task_state_store,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="来源不存在。")
    return payload


@router.get("/source-library/states/{state_key}")
async def get_state_group_models(
    state_key: str,
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
):
    payload = await run_web_io(
        build_state_group_models_payload,
        state_key=state_key,
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=page,
        page_size=page_size,
        store=store,
        task_store=task_state_store,
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="状态卡不存在。")
    return payload


@router.get("/models/{model_dir:path}/comments")
async def get_model_detail_comments(
    model_dir: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    payload = await run_web_io(get_model_comments_page, model_dir, offset=offset, limit=limit)
    if payload is None:
        raise HTTPException(status_code=404, detail="模型不存在。")
    return payload


@router.post("/models/{model_dir:path}/source-backfill")
async def backfill_model_source_metadata(model_dir: str, request: Request):
    _require_session_auth(request)

    def _submit_backfill() -> dict:
        detail = get_model_detail(model_dir, include_detail=True)
        if detail is None:
            raise ValueError("模型不存在。")

        source = str(detail.get("source") or "").strip().lower()
        origin_url = str(detail.get("origin_url") or "").strip()
        if source not in {"cn", "global"} or not origin_url:
            raise ValueError("本地模型或缺少源端链接，无法补全源端信息。")

        response = crawler.manager.submit_profile_metadata_backfill(
            origin_url,
            model_dir=str(detail.get("model_dir") or model_dir),
            title=str(detail.get("title") or model_dir),
        )
        append_business_log(
            "model",
            "source_backfill_requested",
            response.get("message") or "源端信息补全已提交。",
            accepted=bool(response.get("accepted")),
            queued=bool(response.get("queued")),
            model_dir=str(detail.get("model_dir") or model_dir),
            url=origin_url,
            task_id=response.get("task_id"),
        )
        return {
            **response,
            "success": bool(response.get("accepted") or response.get("queued")),
            "model_dir": str(detail.get("model_dir") or model_dir),
        }

    try:
        return await run_task_api(_submit_backfill)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if "不存在" in message else 400
        raise HTTPException(status_code=status_code, detail=message) from exc


@router.get("/models/{model_dir:path}")
async def get_model_detail_data(model_dir: str):
    detail = await run_web_io(get_model_detail, model_dir)
    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")
    return detail


@router.post("/models/{model_dir:path}/attachments")
async def upload_model_attachment(
    model_dir: str,
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(""),
    category: str = Form("assembly"),
):
    _require_session_auth(request)
    try:
        def _upload_and_load_detail():
            attachment_item = create_manual_attachment(model_dir, file, name=name, category=category)
            return attachment_item, get_model_detail(model_dir)

        attachment, detail = await run_task_api(_upload_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "attachment_uploaded",
        "模型附件已上传。",
        model_dir=model_dir,
        attachment_id=attachment.get("id"),
        attachment_name=attachment.get("name"),
        category=category,
    )
    return {
        "success": True,
        "attachment": attachment,
        "detail": detail,
        "message": "附件已上传到当前模型目录。",
    }


@router.delete("/models/{model_dir:path}/attachments/{attachment_id}")
async def remove_model_attachment(model_dir: str, attachment_id: str, request: Request):
    _require_session_auth(request)
    try:
        def _remove_and_load_detail():
            removed_item = delete_manual_attachment(model_dir, attachment_id)
            return removed_item, get_model_detail(model_dir)

        removed, detail = await run_task_api(_remove_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "attachment_deleted",
        "模型附件已删除。",
        model_dir=model_dir,
        attachment_id=attachment_id,
        removed=removed,
    )
    return {
        "success": True,
        "removed": removed,
        "detail": detail,
        "message": "附件已删除。",
    }


@router.patch("/models/{model_dir:path}/local/description")
async def update_local_model_description_data(
    model_dir: str,
    payload: LocalModelDescriptionUpdateRequest,
    request: Request,
):
    _require_session_auth(request)
    try:
        def _update_and_load_detail():
            result = update_local_model_description(model_dir, payload.description)
            return result, get_model_detail(model_dir)

        result, detail = await run_task_api(_update_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_description_updated",
        "本地模型描述已更新。",
        model_dir=model_dir,
    )
    return {
        "success": True,
        "result": result,
        "detail": detail,
        "message": "描述已更新。",
    }


@router.patch("/models/{model_dir:path}/local/metadata")
async def update_local_model_metadata_data(
    model_dir: str,
    payload: LocalModelMetadataUpdateRequest,
    request: Request,
):
    _require_session_auth(request)
    try:
        def _update_and_load_detail():
            result = update_local_model_metadata(
                model_dir,
                title=payload.title,
                description=payload.description,
            )
            return result, get_model_detail(model_dir)

        result, detail = await run_task_api(_update_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_metadata_updated",
        "本地模型信息已更新。",
        model_dir=model_dir,
        title=result.get("title"),
    )
    return {
        "success": True,
        "result": result,
        "detail": detail,
        "message": "模型信息已更新。",
    }


@router.post("/models/{model_dir:path}/local/files")
async def upload_local_model_files(
    model_dir: str,
    request: Request,
    files: list[UploadFile] = File(...),
):
    _require_session_auth(request)
    uploaded = []
    try:
        def _upload_and_load_detail():
            items = [add_local_model_file(model_dir, file) for file in files]
            return items, get_model_detail(model_dir)

        uploaded, detail = await run_task_api(_upload_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        for file in files:
            await file.close()

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_files_uploaded",
        "本地模型文件已上传。",
        model_dir=model_dir,
        count=len(uploaded),
    )
    return {
        "success": True,
        "items": uploaded,
        "detail": detail,
        "message": f"已添加 {len(uploaded)} 个模型文件。",
    }


@router.delete("/models/{model_dir:path}/local/files")
async def remove_local_model_file(
    model_dir: str,
    payload: LocalModelFileDeleteRequest,
    request: Request,
):
    _require_session_auth(request)
    try:
        def _remove_and_load_detail():
            removed_item = delete_local_model_file(model_dir, payload.instance_key)
            return removed_item, get_model_detail(model_dir)

        removed, detail = await run_task_api(_remove_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_file_deleted",
        "本地模型文件已删除。",
        model_dir=model_dir,
        instance_key=payload.instance_key,
    )
    return {
        "success": True,
        "removed": removed,
        "detail": detail,
        "message": "模型文件已删除。",
    }


@router.post("/models/{model_dir:path}/local/images")
async def upload_local_model_images(
    model_dir: str,
    request: Request,
    files: list[UploadFile] = File(...),
):
    _require_session_auth(request)
    uploaded = []
    try:
        def _upload_and_load_detail():
            items = [add_local_model_image(model_dir, file) for file in files]
            return items, get_model_detail(model_dir)

        uploaded, detail = await run_task_api(_upload_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        for file in files:
            await file.close()

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_images_uploaded",
        "本地模型图片已上传。",
        model_dir=model_dir,
        count=len(uploaded),
    )
    return {
        "success": True,
        "items": uploaded,
        "detail": detail,
        "message": f"已添加 {len(uploaded)} 张图片。",
    }


@router.delete("/models/{model_dir:path}/local/images")
async def remove_local_model_image(
    model_dir: str,
    payload: LocalModelImageDeleteRequest,
    request: Request,
):
    _require_session_auth(request)
    try:
        def _remove_and_load_detail():
            removed_item = delete_local_model_image(model_dir, payload.rel_path)
            return removed_item, get_model_detail(model_dir)

        removed, detail = await run_task_api(_remove_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_image_deleted",
        "本地模型图片已删除。",
        model_dir=model_dir,
        rel_path=payload.rel_path,
    )
    return {
        "success": True,
        "removed": removed,
        "detail": detail,
        "message": "图片已删除。",
    }


@router.patch("/models/{model_dir:path}/local/images/cover")
async def update_local_model_cover_image(
    model_dir: str,
    payload: LocalModelImageCoverRequest,
    request: Request,
):
    _require_session_auth(request)
    try:
        def _update_and_load_detail():
            updated_item = set_local_model_cover_image(model_dir, payload.rel_path)
            return updated_item, get_model_detail(model_dir)

        updated, detail = await run_task_api(_update_and_load_detail)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")

    append_business_log(
        "model",
        "local_model_cover_updated",
        "本地模型封面已更新。",
        model_dir=model_dir,
        rel_path=payload.rel_path,
    )
    return {
        "success": True,
        "item": updated,
        "detail": detail,
        "message": "封面图已更新。",
    }


@router.post("/models/delete")
async def delete_models(payload: ModelDeleteRequest, request: Request):
    _require_session_auth(request)

    def _delete_models_payload() -> dict:
        marked: list[dict] = []
        skipped: list[dict] = []

        for raw_value in payload.model_dirs:
            clean_model_dir = str(raw_value or "").strip().strip("/")
            if not clean_model_dir:
                continue

            detail = get_model_detail(clean_model_dir, include_detail=False)
            if not detail:
                skipped.append({"model_dir": clean_model_dir, "reason": "模型不存在"})
                continue
            if detail.get("local_flags", {}).get("deleted"):
                skipped.append({"model_dir": clean_model_dir, "reason": "已在 MakerHub 端删除"})
                continue

            task_state_store.update_model_flag(clean_model_dir, "deleted", True)
            model_id = str(detail.get("id") or "").strip()
            origin_url = str(detail.get("origin_url") or "").strip()
            if model_id:
                task_state_store.remove_missing_3mf_for_model(model_id)
            task_state_store.remove_recent_failures_for_model(model_id, url=origin_url)
            invalidate_model_detail_cache(clean_model_dir)
            marked.append(
                {
                    "model_dir": clean_model_dir,
                    "id": model_id,
                    "title": str(detail.get("title") or clean_model_dir),
                }
            )

        if marked:
            invalidate_archive_snapshot("models_soft_deleted")

        result = {
            "success": bool(marked),
            "soft_deleted": marked,
            "skipped": skipped,
            "soft_deleted_count": len(marked),
            "skipped_count": len(skipped),
            "flags": task_state_store.load_model_flags(),
            "message": (
                f"已在 MakerHub 端删除 {len(marked)} 个模型，默认已从模型库隐藏。"
                if marked
                else "没有标记任何模型。"
            ),
        }
        append_business_log(
            "model",
            "models_soft_deleted",
            result["message"],
            requested_count=len(payload.model_dirs),
            soft_deleted_count=result.get("soft_deleted_count", 0),
            skipped_count=result.get("skipped_count", 0),
            model_dirs=payload.model_dirs,
        )
        return result

    return await run_task_api(_delete_models_payload)


@router.get("/models/flags")
async def get_model_flags():
    return task_state_store.load_model_flags()


@router.post("/models/flags/favorite")
async def update_model_favorite(payload: ModelFlagUpdateRequest, request: Request):
    _require_session_auth(request)
    flags = task_state_store.update_model_flag(payload.model_dir, "favorites", payload.value)
    append_business_log(
        "model",
        "favorite_flag_updated",
        "本地收藏标记已更新。",
        model_dir=payload.model_dir,
        value=payload.value,
    )
    return {
        "success": True,
        "model_dir": payload.model_dir,
        "favorite": payload.value,
        "flags": flags,
    }


@router.post("/models/flags/printed")
async def update_model_printed(payload: ModelFlagUpdateRequest, request: Request):
    _require_session_auth(request)
    flags = task_state_store.update_model_flag(payload.model_dir, "printed", payload.value)
    append_business_log(
        "model",
        "printed_flag_updated",
        "已打印标记已更新。",
        model_dir=payload.model_dir,
        value=payload.value,
    )
    return {
        "success": True,
        "model_dir": payload.model_dir,
        "printed": payload.value,
        "flags": flags,
    }


@router.post("/models/flags/deleted")
async def update_model_deleted(payload: ModelFlagUpdateRequest, request: Request):
    _require_session_auth(request)
    clean_model_dir = str(payload.model_dir or "").strip().strip("/")
    if not clean_model_dir:
        raise HTTPException(status_code=400, detail="模型目录不能为空。")
    detail = await run_web_io(get_model_detail, clean_model_dir, include_detail=False)
    if not detail:
        raise HTTPException(status_code=404, detail="模型不存在。")
    flags = task_state_store.update_model_flag(clean_model_dir, "deleted", payload.value)
    if payload.value:
        model_id = str(detail.get("id") or "").strip()
        origin_url = str(detail.get("origin_url") or "").strip()
        if model_id:
            task_state_store.remove_missing_3mf_for_model(model_id)
        task_state_store.remove_recent_failures_for_model(model_id, url=origin_url)
    invalidate_model_detail_cache(clean_model_dir)
    invalidate_archive_snapshot("model_deleted_flag_updated")
    message = "模型已恢复到模型库。" if not payload.value else "模型已标记为本地删除。"
    append_business_log(
        "model",
        "deleted_flag_updated",
        message,
        model_dir=clean_model_dir,
        value=payload.value,
    )
    return {
        "success": True,
        "model_dir": clean_model_dir,
        "deleted": payload.value,
        "flags": flags,
        "message": message,
    }


@router.post("/local-library/merge")
async def merge_local_library_models(payload: LocalModelMergeRequest, request: Request):
    _require_session_auth(request)
    return await run_task_api(
        merge_local_models,
        target_model_dir=payload.target_model_dir,
        source_model_dirs=payload.source_model_dirs,
        title=payload.title,
        cover_from_model_dir=payload.cover_from_model_dir,
        task_store=task_state_store,
    )


@router.post("/local-library/import")
async def import_local_library_files(
    request: Request,
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
):
    _require_session_auth(request)
    try:
        result = await run_task_api(
            upload_local_import_files,
            files=files,
            paths=paths,
            store=store,
            task_store=task_state_store,
        )
        if BACKGROUND_TASKS_ENABLED and result.get("trigger_organizer", True):
            try:
                await run_task_api(local_organizer.run_once)
                result["triggered"] = True
            except Exception as exc:
                result["triggered"] = False
                result["trigger_error"] = str(exc)
                append_business_log("organizer", "local_import_trigger_failed", str(exc), level="warning")
        else:
            result["triggered"] = False
        return result
    except ValueError as exc:
        append_business_log("organizer", "local_import_upload_failed", str(exc), level="error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/mobile-import/ping")
async def mobile_import_ping(request: Request):
    _require_mobile_import_token(request)
    return {
        "success": True,
        "message": "OK",
        "app": "makerhub",
        "app_version": APP_VERSION,
    }


@router.get("/mobile-import/ping-ipv4")
async def mobile_import_ping_ipv4(token: str = Query("")):
    try:
        _validate_mobile_import_token(token)
    except HTTPException:
        return "makerhub:unauthorized"
    _mark_mobile_import_used()
    append_business_log("organizer", "mobile_import_ping", "移动端导入地址探测成功。", channel="ipv4")
    return "makerhub:ok"


@router.post("/mobile-import/raw-ipv4")
async def mobile_import_raw_ipv4_file(request: Request, background_tasks: BackgroundTasks, token: str = Query("")):
    try:
        _validate_mobile_import_token(token)
    except HTTPException:
        return {
            "success": False,
            "message": "移动端导入 Token 无效。",
        }
    _mark_mobile_import_used()
    filename = str(
        request.headers.get("X-MakerHub-Filename")
        or request.headers.get("X-Filename")
        or request.query_params.get("filename")
        or ""
    ).strip() or "wechat-upload"
    content_length = str(request.headers.get("content-length") or "").strip()
    append_business_log(
        "organizer",
        "mobile_import_raw_started",
        "移动端原始文件上传开始。",
        channel="ipv4",
        filename=filename,
        content_length=content_length,
    )
    raw_body = await request.body()
    append_business_log(
        "organizer",
        "mobile_import_raw_received",
        "移动端原始文件请求体已接收。",
        channel="ipv4",
        filename=filename,
        size_bytes=len(raw_body),
    )
    upload, filename = _mobile_raw_upload_file(raw_body, filename)
    background_tasks.add_task(_run_mobile_import_background, [upload], [filename])
    return {
        "success": True,
        "message": "已上传",
        "background": True,
    }


@router.post("/mobile-import")
async def mobile_import_files(
    request: Request,
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
):
    _require_mobile_import_token(request)
    try:
        return await _run_mobile_import_upload(files, paths)
    except ValueError as exc:
        append_business_log("organizer", "mobile_import_upload_failed", str(exc), level="error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/mobile-import/raw")
async def mobile_import_raw_file(request: Request, background_tasks: BackgroundTasks):
    _require_mobile_import_token(request)
    filename = str(
        request.headers.get("X-MakerHub-Filename")
        or request.headers.get("X-Filename")
        or request.query_params.get("filename")
        or ""
    ).strip() or "wechat-upload"
    raw_body = await request.body()
    upload, filename = _mobile_raw_upload_file(raw_body, filename)
    background = str(request.query_params.get("background") or request.headers.get("X-MakerHub-Background") or "").strip().lower()
    if background in {"1", "true", "yes", "on"}:
        background_tasks.add_task(_run_mobile_import_background, [upload], [filename])
        return {
            "success": True,
            "message": "已上传",
            "background": True,
        }
    try:
        return await _run_mobile_import_upload([upload], [filename])
    except ValueError as exc:
        append_business_log("organizer", "mobile_import_upload_failed", str(exc), level="error")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tasks")
async def get_tasks_data():
    def _tasks_payload() -> dict:
        config = store.load()
        fallback_items = [item.model_dump() for item in config.missing_3mf]
        return build_tasks_payload(missing_fallback=fallback_items)

    return await run_ui_io(_tasks_payload)


@router.post("/tasks/recent-failures/clear")
async def clear_recent_archive_failures(request: Request):
    _require_session_auth(request)

    def _clear_payload() -> dict:
        queue = task_state_store.clear_archive_recent_failures()
        config = store.load()
        fallback_items = [item.model_dump() for item in config.missing_3mf]
        payload = build_tasks_payload(missing_fallback=fallback_items)
        payload["cleared_count"] = int(queue.get("cleared_count") or 0)
        payload["message"] = "已清除最近失败记录。"
        payload["success"] = True
        return payload

    result = await run_task_api(_clear_payload)
    append_business_log(
        "archive",
        "recent_failures_cleared",
        result.get("message") or "最近失败记录已清除。",
        cleared_count=result.get("cleared_count"),
    )
    return result


@router.get("/remote-refresh")
async def get_remote_refresh_data():
    def _remote_refresh_payload() -> dict:
        config = store.load()
        return {
            "config": config.remote_refresh.model_dump(),
            "state": remote_refresh_manager.state_payload(),
        }

    return await run_ui_io(_remote_refresh_payload)


@router.post("/remote-refresh/run")
async def run_remote_refresh(request: Request):
    _require_session_auth(request)

    def _manual_trigger_payload() -> dict:
        return remote_refresh_manager.trigger_manual_refresh()

    return await run_task_api(_manual_trigger_payload)


@router.post("/tasks/organize/clear")
async def clear_organize_tasks(request: Request):
    _require_session_auth(request)
    cleared = task_state_store.save_organize_tasks({"items": []})
    append_business_log("organizer", "tasks_cleared", "本地整理任务记录已清空。")
    return {
        "success": True,
        "message": "已清空本地整理任务记录。",
        "organize_tasks": cleared,
    }


@router.get("/subscriptions")
async def get_subscriptions_data():
    return await run_web_io(subscription_manager.list_payload)


@router.get("/logs")
async def get_logs_data(
    file: str = Query("business.log", description="日志文件名"),
    limit: int = Query(300, ge=1, le=2000, description="最多返回行数"),
    q: str = Query("", description="日志内容搜索"),
):
    return await run_web_io(read_log_entries, file_name=file, limit=limit, query=q)


@router.post("/subscriptions")
async def create_subscription(payload: SubscriptionCreateRequest, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(
            subscription_manager.create_subscription,
            url=payload.url,
            cron=payload.cron,
            name=payload.name,
            enabled=payload.enabled,
            initialize_from_source=payload.initialize_from_source,
        )
        append_business_log(
            "subscription",
            "created",
            result.get("message") or "订阅已创建。",
            url=payload.url,
            name=payload.name,
            enabled=payload.enabled,
            initialize_from_source=payload.initialize_from_source,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "create_failed", str(exc), level="error", url=payload.url, name=payload.name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: str, payload: SubscriptionUpdateRequest, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(
            subscription_manager.update_subscription,
            subscription_id,
            url=payload.url,
            name=payload.name,
            cron=payload.cron,
            enabled=payload.enabled,
        )
        append_business_log(
            "subscription",
            "updated",
            result.get("message") or "订阅已更新。",
            subscription_id=subscription_id,
            url=payload.url,
            name=payload.name,
            enabled=payload.enabled,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log(
            "subscription",
            "update_failed",
            str(exc),
            level="error",
            subscription_id=subscription_id,
            url=payload.url,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(subscription_manager.delete_subscription, subscription_id)
        append_business_log(
            "subscription",
            "deleted",
            result.get("message") or "订阅已删除。",
            subscription_id=subscription_id,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "delete_failed", str(exc), level="error", subscription_id=subscription_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/subscriptions/{subscription_id}/sync")
async def sync_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        result = await run_task_api(subscription_manager.request_sync, subscription_id)
        append_business_log(
            "subscription",
            "sync_requested",
            result.get("message") or "订阅同步已触发。",
            subscription_id=subscription_id,
        )
        return result
    except (ValueError, RuntimeError) as exc:
        append_business_log("subscription", "sync_request_failed", str(exc), level="error", subscription_id=subscription_id)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/events/archive")
async def stream_archive_events(request: Request):
    async def event_stream():
        snapshot = await run_ui_io(_archive_event_snapshot)
        previous_active = dict(snapshot["active"])
        previous_organize_success = set(snapshot["organize_success"])

        yield (
            "event: ready\n"
            f"data: {json.dumps({'running_count': snapshot['running_count'], 'queued_count': snapshot['queued_count'], 'failed_count': snapshot['failed_count']}, ensure_ascii=False)}\n\n"
        )

        heartbeat_tick = 0
        while True:
            if await request.is_disconnected():
                break

            snapshot = await run_ui_io(_archive_event_snapshot)
            current_active = dict(snapshot["active"])
            recent_failures = set(snapshot["recent_failures"])
            current_organize_success = set(snapshot["organize_success"])
            completed = []

            for identity, metadata in previous_active.items():
                if identity in current_active:
                    continue

                task_mode = str(metadata.get("mode") or "")
                task_url = str(metadata.get("url") or "")
                if not task_mode and "/models/" in task_url:
                    task_mode = "single_model"
                if task_mode != "single_model":
                    continue
                if identity in recent_failures:
                    continue

                completed.append(
                    {
                        "id": identity,
                        "url": task_url,
                        "title": str(metadata.get("title") or ""),
                    }
                )

            for success_id in sorted(current_organize_success - previous_organize_success):
                completed.append(
                    {
                        "id": success_id,
                        "url": "",
                        "title": "本地整理完成",
                        "kind": "local_organize",
                    }
                )

            if completed:
                yield (
                    "event: archive_completed\n"
                    f"data: {json.dumps({'completed': completed, 'running_count': snapshot['running_count'], 'queued_count': snapshot['queued_count'], 'failed_count': snapshot['failed_count']}, ensure_ascii=False)}\n\n"
                )

            previous_active = current_active
            previous_organize_success = current_organize_success
            heartbeat_tick += 1
            if heartbeat_tick >= 15:
                heartbeat_tick = 0
                yield "event: ping\ndata: {}\n\n"

            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/tasks/missing-3mf/retry")
async def retry_missing_3mf(payload: Missing3mfRetryRequest, request: Request):
    _require_session_auth(request)
    result = await run_task_api(
        crawler.retry_missing_3mf,
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id=payload.instance_id,
    )
    append_business_log(
        "missing_3mf",
        "retry_requested",
        result.get("message") or "缺失 3MF 重试已请求。",
        accepted=bool(result.get("accepted")),
        model_id=payload.model_id,
        model_url=payload.model_url,
        instance_id=payload.instance_id,
    )
    return result


@router.post("/tasks/missing-3mf/retry-all")
async def retry_all_missing_3mf(request: Request):
    _require_session_auth(request)
    result = await run_task_api(crawler.retry_all_missing_3mf)
    append_business_log(
        "missing_3mf",
        "retry_all_requested",
        result.get("message") or "缺失 3MF 全部重试已请求。",
        accepted_count=result.get("accepted_count"),
        queued_count=result.get("queued_count"),
        failed_count=result.get("failed_count"),
    )
    return result


@router.post("/tasks/missing-3mf/cancel")
async def cancel_missing_3mf(payload: Missing3mfCancelRequest, request: Request):
    _require_session_auth(request)
    result = await run_task_api(
        crawler.cancel_missing_3mf,
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id=payload.instance_id,
    )
    append_business_log(
        "missing_3mf",
        "cancel_requested",
        result.get("message") or "缺失 3MF 取消已请求。",
        success=bool(result.get("success")),
        removed_count=result.get("removed_count"),
        model_id=payload.model_id,
        model_url=payload.model_url,
        instance_id=payload.instance_id,
    )
    return result


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    def _archive_model_payload() -> dict:
        batch_preview = None
        archive_mode = detect_archive_mode(payload.url)
        if payload.create_subscription and archive_mode in BATCH_TASK_MODES:
            batch_preview = crawler.manager.peek_batch_preview(
                payload.preview_token,
                payload.url,
                mode=archive_mode,
            )
            if payload.preview_token and batch_preview is None:
                raise HTTPException(status_code=400, detail="预扫描结果已失效，请重新扫描后再确认提交。")

        response = crawler.manager.submit(payload.url, preview_token=payload.preview_token)
        if (
            payload.create_subscription
            and response.get("accepted") is not False
            and archive_mode in BATCH_TASK_MODES
        ):
            try:
                subscription_result = subscription_manager.upsert_from_archive(
                    url=payload.url,
                    mode=archive_mode,
                    discovered_items=list((batch_preview or {}).get("discovered_items") or []),
                    name=str(
                        payload.subscription_name
                        or (batch_preview or {}).get("source_name")
                        or ""
                    ).strip(),
                )
                subscription = subscription_result.get("subscription") or {}
                subscription_name = str(subscription.get("name") or "").strip()
                response["subscription"] = subscription
                response["subscription_created"] = bool(subscription_result.get("created"))
                response["subscription_message"] = (
                    f"已自动创建订阅「{subscription_name}」。"
                    if subscription_result.get("created")
                    else f"已自动更新订阅「{subscription_name}」。"
                )
                response["message"] = (
                    f"{response.get('message') or '归档任务已加入队列。'} "
                    f"{response['subscription_message']}"
                ).strip()
            except Exception as exc:
                response["subscription_error"] = str(exc)
                response["message"] = (
                    f"{response.get('message') or '归档任务已加入队列。'} "
                    f"但订阅写入失败：{exc}"
                ).strip()
                append_business_log(
                    "archive",
                    "archive_subscribe_failed",
                    str(exc),
                    level="error",
                    url=payload.url,
                    mode=archive_mode,
                )
        append_business_log(
            "archive",
            "archive_submitted",
            response.get("message") or "归档提交完成。",
            accepted=bool(response.get("accepted")),
            url=payload.url,
            mode=response.get("mode") or archive_mode,
            task_id=response.get("task_id"),
            create_subscription=payload.create_subscription,
            subscription_created=response.get("subscription_created"),
        )
        return response

    return await run_task_api(_archive_model_payload)


@router.get("/admin/archive/repair-3mf")
async def get_archive_3mf_repair_status(request: Request):
    _require_session_auth(request)
    return await run_ui_io(read_archive_repair_status)


@router.post("/admin/archive/repair-3mf")
async def repair_archive_3mf(request: Request):
    global archive_repair_process

    _require_session_auth(request)
    async with archive_repair_start_lock:
        state = read_archive_repair_status()
        if state.get("running"):
            state.update(
                {
                    "accepted": False,
                    "message": "全库 3MF 映射修复正在后台执行，请稍后查看状态或日志。",
                }
            )
            return state

        run_id = f"repair-{int(time.time() * 1000)}"
        started_at = _now_iso()
        process = Process(
            target=run_archive_repair_job,
            args=(run_id, started_at),
            daemon=True,
        )
        process.start()
        archive_repair_process = process
        state = write_archive_repair_status(
            {
                "running": True,
                "started_at": started_at,
                "finished_at": "",
                "last_error": "",
                "run_id": run_id,
                "pid": int(process.pid or 0),
                "last_result": {},
            }
        )

    append_business_log(
        "archive_repair",
        "repair_requested",
        "已提交全库 3MF 映射修复，后台开始执行。",
        run_id=run_id,
    )
    state.update(
        {
            "accepted": True,
            "message": "修复任务已提交到后台，请稍后查看日志或状态接口。",
        }
    )
    return state


@router.get("/admin/archive/profile-backfill")
async def get_archive_profile_backfill_status(request: Request):
    _require_session_auth(request)
    status = await run_ui_io(read_profile_backfill_status)
    return _compact_profile_backfill_status(status)


@router.post("/admin/archive/profile-backfill")
async def start_archive_profile_backfill(request: Request):
    _require_session_auth(request)
    async with profile_backfill_start_lock:
        state = read_profile_backfill_status()
        if state.get("running"):
            state.update(
                {
                    "accepted": False,
                    "message": "现有库信息补全正在扫描并持续入队，请稍后刷新状态。",
                }
            )
            return _compact_profile_backfill_status(state)

        started_at = china_now_iso()
        state = write_profile_backfill_status(
            {
                "running": True,
                "started_at": started_at,
                "finished_at": "",
                "last_error": "",
                "last_result": {},
            }
        )

    state.update(
        {
            "accepted": True,
            "message": "现有库信息补全扫描已提交，等待后台 worker 执行；缺失模型会继续加入归档队列。",
        }
    )
    return _compact_profile_backfill_status(state)


def _compact_profile_backfill_status(status: dict) -> dict:
    payload = dict(status or {})
    result = payload.get("last_result")
    if isinstance(result, dict):
        compact_result = dict(result)
        items = compact_result.get("items")
        if isinstance(items, list) and len(items) > 50:
            compact_result["items"] = items[:50]
            compact_result["items_total"] = len(items)
            compact_result["items_truncated"] = True
        payload["last_result"] = compact_result
    return payload
@router.post("/archive/preview")
async def preview_archive_model(payload: ArchiveRequest):
    response = await run_task_api(crawler.preview_archive, payload.url)
    append_business_log(
        "archive",
        "archive_preview",
        response.get("message") or "归档预扫描完成。",
        accepted=bool(response.get("accepted")),
        url=payload.url,
        mode=response.get("mode"),
        discovered_count=response.get("discovered_count"),
        expected_total=response.get("expected_total"),
        queued_count=response.get("queued_count"),
        archived_count=response.get("archived_count"),
        new_count=response.get("new_count"),
    )
    return response
