"""MakerWorld single-model archive control plane.

Browser-facing MakerWorld operations live here. Static assets still use the
streaming AssetDownloader path exposed by the compatibility helpers.
"""

import hashlib
import json
import math
import os
import shutil
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import requests

from app.services.asset_downloader import (
    AssetDownloadError,
    download_file,
    download_with_fresh_session,
    run_asset_tasks,
    safe_asset_url,
)
from app.services.business_logs import append_business_log
from app.services.cookie_utils import parse_cookie_values, sanitize_cookie_header
from app.services.legacy_archiver import (
    COMMENT_ASSET_DOWNLOAD_WORKERS,
    BINARY_TRANSFER_TIMEOUT_SECONDS,
    IMAGE_ASSET_DOWNLOAD_WORKERS,
    PROFILE_DETAIL_SCHEMA_VERSION,
    FAKE_THREE_MF_DOWNLOAD_MESSAGE,
    MAKERWORLD_API_BROWSER_HEADERS,
    SAFE_REASON_CODES,
    SAFE_VERIFICATION_CHALLENGE_TYPES,
    SAFE_VERIFICATION_PROVIDERS,
    SHARED_AVATAR_REL_DIR,
    VOLATILE_ASSET_QUERY_KEYS,
    VOLATILE_ASSET_QUERY_PREFIXES,
    _archive_root_from_comment_out_dir,
    _build_existing_comment_lookup,
    _build_existing_media_lookup,
    _build_existing_instance_index,
    _comment_local_asset_path,
    _comment_model_root,
    _comment_resource_stats,
    _download_asset_with_fresh_session,
    _emit_stage_progress,
    _extract_auth_token,
    _existing_media_ref,
    _find_existing_instance,
    _is_three_mf_fake_download_target,
    _list_directory_entries,
    IMAGE_TRANSFER_TIMEOUT_SECONDS,
    _fake_three_mf_download_url,
    _log_perf,
    _log_asset_download_failure,
    _makerworld_not_found_message,
    _match_existing_media_item_from_lookup,
    _mark_instance_3mf_download_failed,
    _media_item_local_exists,
    _missing_3mf_failure_for_skipped_fetch,
    _missing_3mf_instances,
    _record_missing_3mf_summary,
    _session_cookie_header,
    _should_pause_three_mf_fetch,
    _shared_avatar_dir,
    _shared_avatar_rel_path,
    _stage_percent,
    _trace_url,
    _wait_before_three_mf_download,
    _write_fake_three_mf_file,
    build_meta,
    choose_archive_base_name,
    choose_unique_instance_filename,
    collect_design_images,
    collect_instance_media,
    emit_progress,
    ensure_dir,
    extract_author,
    extract_design_attachments,
    extract_instances,
    fake_three_mf_downloads_enabled,
    format_duration,
    load_existing_meta,
    log,
    log_section,
    parse_cookies,
    parse_summary,
    pick_ext_from_url,
    normalize_profile_details,
    normalize_profile_rating,
    rebuild_once,
    sanitize_filename,
    summarize_cookie_header,
)
from app.services.makerworld_browser_client import (
    MakerWorldBrowserError,
    makerworld_browser_get_json,
    makerworld_browser_get_text,
)
from app.services.makerworld_parsers.comments import (
    _collect_comment_tree,
    _collect_comments_from_payload,
    _comment_list_payload_declares_empty,
    _comment_list_payload_hit_count,
    _comment_list_payload_total,
    _comment_numeric,
    _comment_reply_count,
    _comment_reply_items,
    _comment_tree_items,
    _count_comment_threads,
    _extract_comment_reply_payload_has_more,
    _merge_threaded_comment_list,
    extract_comment_list_items as _extract_comment_list_items,
    extract_comment_replies as _extract_comment_replies_from_payload,
    extract_comment_sections as _extract_comment_sections,
    normalize_threaded_comments,
    resolve_comment_count as _resolved_comment_count,
)
from app.services.makerworld_parsers.model import (
    append_api_base_candidate as _append_api_base_candidate,
    design_payload_error as _design_payload_error,
    extract_api_host as _extract_api_host,
    extract_design_from_next_data,
    extract_next_data,
    is_cloudflare_challenge as _is_cloudflare_challenge,
    is_makerworld_not_found_page as _is_makerworld_not_found_page,
    normalize_design_payload_identity as _normalize_design_payload_identity,
    parse_design_id as _parse_design_id,
    unwrap_design_payload as _unwrap_design_payload,
)
from app.services.three_mf import (
    THREE_MF_NOT_DOWNLOADABLE_STATE,
    describe_three_mf_failure,
    is_three_mf_daily_download_limited,
    is_three_mf_download_prohibited,
    merge_three_mf_failure,
    normalize_makerworld_source,
    normalize_three_mf_failure_state,
)
from app.services.three_mf_quota import reserve_three_mf_download_slot

def fetch_html_with_browser(session: requests.Session, url: str, raw_cookie: str) -> Optional[str]:
    cookie_header = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    try:
        return makerworld_browser_get_text(
            url,
            raw_cookie=cookie_header,
            headers=headers,
            session=session,
        )
    except MakerWorldBrowserError as exc:
        log("CloakBrowser 获取页面失败:", exc)
        raise


def _session_cookie_header(session: requests.Session) -> str:
    try:
        return "; ".join(
            f"{cookie.name}={cookie.value}"
            for cookie in session.cookies
            if getattr(cookie, "name", "") and getattr(cookie, "value", "") is not None
        )
    except Exception:
        return ""


def _makerworld_not_found_message() -> str:
    return "模型页面返回 404，可能已下架、设为私有或转为草稿。"


def fetch_design_from_api(
    session: requests.Session,
    raw_cookie: str,
    url: str,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> Optional[dict]:
    design_id = _parse_design_id(url)
    if not design_id:
        return None
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://makerworld.com.cn"
    source = normalize_makerworld_source(url=url)
    bases = []
    if source == "global":
        _append_api_base_candidate(bases, "https://api.bambulab.com", source)
    elif source == "cn":
        _append_api_base_candidate(bases, "https://api.bambulab.cn", source)
    else:
        _append_api_base_candidate(bases, "https://api.bambulab.cn", source)
        _append_api_base_candidate(bases, "https://api.bambulab.com", source)
    _append_api_base_candidate(bases, api_host_hint or "", source)
    _append_api_base_candidate(bases, origin, source)

    endpoints = []
    for base in bases:
        host = urlparse(base).netloc.lower()
        if host.startswith("api.bambulab."):
            path_templates = [
                "/v1/design-service/design/{id}",
                "/v1/design-service/design/{id}/detail",
                "/v1/design-service/design/{id}/detail?source=web",
                "/v1/design-service/design/{id}?lang=zh",
            ]
        else:
            path_templates = [
                "/api/v1/design-service/design/{id}",
                "/api/v1/design-service/design/{id}/detail",
                "/api/v1/design-service/design/{id}/detail?source=web",
                "/api/v1/design-service/design/{id}?lang=zh",
                "/v1/design-service/design/{id}",
                "/v1/design-service/design/{id}/detail",
            ]
        for path in path_templates:
            candidate = f"{base.rstrip('/')}{path.format(id=design_id)}"
            if candidate not in endpoints:
                endpoints.append(candidate)
    cookie_header = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": url,
        "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    for api_url in endpoints:
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=cookie_header,
                headers=headers,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError as exc:
            log(logger, "CloakBrowser API 请求失败，尝试下一个候选:", api_url, exc)
            continue
        if isinstance(payload, dict):
            design = _unwrap_design_payload(payload)
            if design:
                payload_error = _design_payload_error(design, url)
                if payload_error:
                    log(logger, "CloakBrowser API 返回的模型数据无效，跳过:", api_url, payload_error)
                else:
                    _normalize_design_payload_identity(design, url)
                    return design
    return None




def _build_existing_comment_lookup(items: object) -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for item in _comment_tree_items(items):
        comment_id = str(item.get("id") or "").strip()
        if comment_id and comment_id not in lookup:
            lookup[comment_id] = item
    return lookup


def _archive_root_from_comment_out_dir(out_dir: Path) -> Path:
    model_root = out_dir.parent if out_dir.name == "images" else out_dir
    return model_root.parent


def _shared_avatar_dir(out_dir: Path) -> Path:
    return _archive_root_from_comment_out_dir(out_dir) / SHARED_AVATAR_REL_DIR


def _avatar_cache_key(avatar_url: str) -> str:
    normalized = _normalize_url_value(avatar_url) or str(avatar_url or "").strip()
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()


def _avatar_cache_filename(avatar_url: str, fallback_path: Optional[Path] = None) -> str:
    ext = pick_ext_from_url(avatar_url, "")
    if not ext and fallback_path is not None:
        ext = fallback_path.suffix.lower().lstrip(".")
    if not ext:
        ext = "jpg"
    return f"{_avatar_cache_key(avatar_url)}.{ext}"


def _shared_avatar_rel_path(filename: str) -> str:
    return f"{SHARED_AVATAR_REL_DIR}/{filename}"


def _comment_model_root(out_dir: Path) -> Path:
    return out_dir.parent if out_dir.name == "images" else out_dir


def _comment_local_asset_path(out_dir: Path, rel_path: str = "", local_name: str = "") -> Optional[Path]:
    model_root = _comment_model_root(out_dir)
    archive_root = _archive_root_from_comment_out_dir(out_dir)
    candidates: list[Path] = []
    clean_rel = str(rel_path or "").strip().lstrip("/")
    clean_local = str(local_name or "").strip().lstrip("/")
    if clean_rel:
        if clean_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            candidates.append(archive_root / clean_rel)
        else:
            candidates.append(model_root / clean_rel)
    if clean_local:
        candidates.append(out_dir / Path(clean_local).name)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _avatar_rel_path_exists(out_dir: Path, rel_path: str = "") -> bool:
    return _comment_local_asset_path(out_dir, rel_path=rel_path) is not None


def _copy_existing_avatar_to_shared(out_dir: Path, avatar_url: str, existing_author: dict) -> Optional[str]:
    if not isinstance(existing_author, dict):
        return None
    existing_rel = str(existing_author.get("avatarRelPath") or "").strip()
    existing_local = str(existing_author.get("avatarLocal") or "").strip()
    if existing_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/") and _avatar_rel_path_exists(out_dir, existing_rel):
        return existing_rel
    source = _comment_local_asset_path(out_dir, rel_path=existing_rel, local_name=existing_local)
    if source is None:
        return None
    filename = _avatar_cache_filename(avatar_url, source)
    target = _shared_avatar_dir(out_dir) / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temp_target = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            shutil.copy2(source, temp_target)
            temp_target.replace(target)
        except Exception:
            try:
                if temp_target.exists():
                    temp_target.unlink()
            except Exception:
                pass
            return None
    return _shared_avatar_rel_path(filename)


def _apply_author_avatar_ref(author: dict, rel_path: str) -> None:
    filename = Path(rel_path).name
    author["avatarLocal"] = filename
    author["avatarRelPath"] = rel_path


def _apply_comment_image_ref(image: dict, local_name: str, rel_path: str) -> None:
    image["localName"] = local_name
    image["relPath"] = rel_path


def _comment_resource_stats(comments: list[dict]) -> dict[str, int]:
    items = list(_comment_tree_items(comments))
    avatar_urls: list[str] = []
    image_urls: list[str] = []
    shared_avatar_refs = 0
    local_image_refs = 0
    for item in items:
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or "").strip()
        if avatar_url:
            avatar_urls.append(_normalize_url_value(avatar_url) or avatar_url)
        if str(author.get("avatarRelPath") or "").strip().startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            shared_avatar_refs += 1
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for image in images:
            if not isinstance(image, dict):
                continue
            image_url = str(image.get("url") or "").strip()
            if image_url:
                image_urls.append(_normalize_url_value(image_url) or image_url)
            if str(image.get("relPath") or image.get("localName") or "").strip():
                local_image_refs += 1
    return {
        "comment_roots": len(comments or []),
        "comment_total": len(items),
        "reply_total": max(len(items) - len(comments or []), 0),
        "avatar_urls": len(avatar_urls),
        "unique_avatar_urls": len(set(avatar_urls)),
        "comment_images": len(image_urls),
        "unique_comment_image_urls": len(set(image_urls)),
        "shared_avatar_refs": shared_avatar_refs,
        "local_comment_image_refs": local_image_refs,
    }


def _download_asset_with_fresh_session(base_session: requests.Session, url: str, dest: Path) -> None:
    download_with_fresh_session(base_session, url, dest, download_func=download_file)


def _log_asset_download_failure(message: str, url: str, error: Exception, logger=None) -> None:
    log(logger, message, safe_asset_url(url), error)


def _download_comment_assets(tasks: list[dict], progress_callback, progress_start: int, progress_end: int) -> dict[str, int]:
    def on_progress(completed: int, total: int) -> None:
        if completed == 1 or completed == total or completed % 5 == 0:
            _emit_stage_progress(
                progress_callback,
                progress_start,
                progress_end,
                completed,
                total,
                "正在下载评论资源",
            )

    return run_asset_tasks(
        tasks,
        max_workers=COMMENT_ASSET_DOWNLOAD_WORKERS,
        on_progress=on_progress,
        on_error=lambda task, exc: _log_asset_download_failure(
            task.get("error_message") or "评论资源下载失败，保留原始链接：",
            task.get("url") or "",
            exc,
        ),
    )


def _download_image_assets(
    tasks: list[dict],
    progress_callback,
    progress_start: int,
    progress_end: int,
    message: str,
) -> dict[str, int]:
    return run_asset_tasks(
        tasks,
        max_workers=IMAGE_ASSET_DOWNLOAD_WORKERS,
        on_progress=lambda completed, total: _emit_stage_progress(
            progress_callback,
            progress_start,
            progress_end,
            completed,
            total,
            message,
        ),
        on_error=lambda task, exc: _log_asset_download_failure(
            task.get("error_message") or "图片下载失败，保留原始链接：",
            task.get("url") or "",
            exc,
        ),
    )


def _apply_existing_comment_assets(
    comments: list[dict],
    existing_comment_lookup: dict[str, dict],
    out_dir: Path,
    *,
    migrate_avatars: bool = True,
) -> dict[str, int]:
    stats = {
        "avatar_existing_reused": 0,
        "avatar_shared_reused": 0,
        "avatar_shared_migrated": 0,
        "comment_image_existing_reused": 0,
    }
    for item in _comment_tree_items(comments):
        existing = existing_comment_lookup.get(str(item.get("id") or "").strip()) or {}
        existing_author = existing.get("author") if isinstance(existing.get("author"), dict) else {}
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or existing_author.get("avatarUrl") or "").strip()
        existing_avatar_rel = str(existing_author.get("avatarRelPath") or "").strip()
        existing_avatar_matches = _media_item_remote_matches(existing_author, avatar_url, url_fields=("avatarUrl", "url"))
        migrated_rel = (
            _copy_existing_avatar_to_shared(out_dir, avatar_url, existing_author)
            if migrate_avatars and avatar_url and existing_avatar_matches
            else None
        )
        if migrated_rel:
            _apply_author_avatar_ref(author, migrated_rel)
            if existing_avatar_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
                stats["avatar_shared_reused"] += 1
            else:
                stats["avatar_shared_migrated"] += 1
        else:
            if existing_avatar_matches and str(existing_author.get("avatarLocal") or "").strip():
                author["avatarLocal"] = str(existing_author.get("avatarLocal") or "").strip()
                stats["avatar_existing_reused"] += 1
            if existing_avatar_matches and str(existing_author.get("avatarRelPath") or "").strip():
                author["avatarRelPath"] = str(existing_author.get("avatarRelPath") or "").strip()

        existing_images = existing.get("images") if isinstance(existing.get("images"), list) else []
        existing_image_lookup = _build_existing_media_lookup(existing_images, url_fields=("url", "originalUrl"))
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for img_idx, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            existing_image = _match_existing_media_item_from_lookup(
                existing_image_lookup,
                url=str(image.get("url") or ""),
                index=img_idx,
            )
            existing_image_matches = _media_item_remote_matches(
                existing_image,
                str(image.get("url") or ""),
                url_fields=("url", "originalUrl"),
            )
            if existing_image_matches and str(existing_image.get("localName") or "").strip():
                image["localName"] = str(existing_image.get("localName") or "").strip()
                stats["comment_image_existing_reused"] += 1
            if existing_image_matches and str(existing_image.get("relPath") or "").strip():
                image["relPath"] = str(existing_image.get("relPath") or "").strip()
    return stats




def _normalize_service_base(base: Optional[str]) -> str:
    normalized = str(base or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("http://") and not normalized.startswith("https://"):
        normalized = f"https://{normalized}"
    return normalized.rstrip("/")


def _comment_api_base_candidates(source_url: str, api_host_hint: Optional[str] = None) -> List[str]:
    normalized_source = normalize_makerworld_source(url=source_url)
    if normalized_source == "global":
        preferred_site = "https://makerworld.com"
        preferred_api = "https://api.bambulab.com"
    else:
        preferred_site = "https://makerworld.com.cn"
        preferred_api = "https://api.bambulab.cn"

    parsed = urlparse(str(source_url or "").strip())
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else preferred_site
    )

    bases: List[str] = []
    for candidate in (preferred_api, api_host_hint, origin, preferred_site):
        normalized = _normalize_service_base(candidate)
        if normalized and normalized not in bases:
            bases.append(normalized)
    return bases


def _comment_service_endpoint_candidates(
    source_url: str,
    path: str,
    *,
    api_host_hint: Optional[str] = None,
) -> List[str]:
    clean_path = "/" + str(path or "").lstrip("/")
    endpoints: List[str] = []
    for base in _comment_api_base_candidates(source_url, api_host_hint=api_host_hint):
        host = urlparse(base).netloc.lower()
        service_prefixes = (
            ("/v1/comment-service",)
            if host.startswith("api.bambulab.")
            else ("/api/v1/comment-service", "/v1/comment-service")
        )
        for service_prefix in service_prefixes:
            candidate = f"{base}{service_prefix}{clean_path}"
            if candidate not in endpoints:
                endpoints.append(candidate)
    return endpoints


def _build_makerworld_api_headers(session: requests.Session, referer: str) -> dict[str, str]:
    headers = dict(MAKERWORLD_API_BROWSER_HEADERS)
    effective_referer = str(referer or "").strip() or "https://makerworld.com.cn/"
    parsed = urlparse(effective_referer)
    headers["Referer"] = effective_referer
    headers["Origin"] = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://makerworld.com.cn"
    )
    headers["User-Agent"] = session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)")
    return headers


def _looks_like_html_response(text: object) -> bool:
    if not isinstance(text, str):
        return False
    lowered = text.lstrip().lower()
    return lowered.startswith("<!doctype html") or lowered.startswith("<html")




def _fetch_comment_reply_payload(
    session: requests.Session,
    source_url: str,
    root_comment_id: str,
    *,
    params: Optional[dict[str, object]] = None,
    api_host_hint: Optional[str] = None,
    reply_kind: str = "comment",
) -> Optional[object]:
    headers = _build_makerworld_api_headers(session, source_url)
    reply_path = (
        f"/rating/{root_comment_id}/reply"
        if str(reply_kind or "").strip().lower() == "rating"
        else f"/comment/{root_comment_id}/reply"
    )
    fallback_payload: Optional[object] = None
    for api_url in _comment_service_endpoint_candidates(
        source_url,
        reply_path,
        api_host_hint=api_host_hint,
    ):
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=_session_cookie_header(session),
                headers=headers,
                params=params or None,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError:
            continue
        if payload is not None:
            if _extract_comment_replies_from_payload(payload, root_comment_id):
                return payload
            if fallback_payload is None:
                fallback_payload = payload
    return fallback_payload


_COMMENT_LIST_PAGE_LIMIT = 100
_COMMENT_LIST_MAX_PAGES = 250


def _fetch_comment_list_payload(
    session: requests.Session,
    source_url: str,
    design_id: str,
    *,
    offset: int,
    limit: int,
    api_host_hint: Optional[str] = None,
) -> Optional[object]:
    if not design_id:
        return None
    headers = _build_makerworld_api_headers(session, source_url)
    params: dict[str, object] = {
        "designId": design_id,
        "offset": max(int(offset or 0), 0),
        "limit": max(min(int(limit or _COMMENT_LIST_PAGE_LIMIT), _COMMENT_LIST_PAGE_LIMIT), 1),
        "type": 0,
        "sort": 0,
    }
    fallback_payload: Optional[object] = None
    for api_url in _comment_service_endpoint_candidates(
        source_url,
        "/commentandrating",
        api_host_hint=api_host_hint,
    ):
        try:
            payload = makerworld_browser_get_json(
                api_url,
                raw_cookie=_session_cookie_header(session),
                headers=headers,
                params=params,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError:
            continue
        if payload is not None:
            if _extract_comment_list_items(payload):
                return payload
            if fallback_payload is None or (
                _comment_list_payload_total(payload) > _comment_list_payload_total(fallback_payload)
                or _comment_list_payload_hit_count(payload) > _comment_list_payload_hit_count(fallback_payload)
            ):
                fallback_payload = payload
    return fallback_payload




def _fetch_paginated_comment_list(
    comments: List[dict],
    session: requests.Session,
    design: dict,
    source_url: str,
    *,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> tuple[List[dict], dict[str, int]]:
    design_id = str(design.get("id") or _parse_design_id(source_url) or "").strip()
    if not design_id or not source_url:
        return comments, {"pages": 0, "roots": 0, "total": 0}

    fetched_comments: List[dict] = []
    offset = 0
    total = 0
    total_known = False
    pages = 0
    seen_offsets: set[int] = set()
    list_started_at = time.perf_counter()

    for _ in range(_COMMENT_LIST_MAX_PAGES):
        if offset in seen_offsets:
            break
        seen_offsets.add(offset)
        payload = _fetch_comment_list_payload(
            session,
            source_url,
            design_id,
            offset=offset,
            limit=_COMMENT_LIST_PAGE_LIMIT,
            api_host_hint=api_host_hint,
        )
        if payload is None:
            break

        hit_count = _comment_list_payload_hit_count(payload)
        payload_total = _comment_list_payload_total(payload)
        if payload_total > 0 or _comment_list_payload_declares_empty(payload):
            total_known = True
            total = max(total, payload_total)
        if hit_count <= 0 and total <= 0:
            break
        page_items = _extract_comment_list_items(payload)
        if not page_items and hit_count <= 0:
            break

        if page_items:
            fetched_comments = _merge_threaded_comment_list(fetched_comments, page_items)
        pages += 1

        step = max(hit_count, len(page_items), 1)
        offset += step
        if total > 0 and offset >= total:
            break
        if total <= 0 and hit_count < _COMMENT_LIST_PAGE_LIMIT:
            break

    if fetched_comments:
        comments = _merge_threaded_comment_list(comments, fetched_comments)

    _log_perf(
        "comments.fetch_pages",
        list_started_at,
        logger=logger,
        pages=pages,
        roots=len(fetched_comments),
        total=total,
        total_known=total_known,
    )
    return comments, {"pages": pages, "roots": len(fetched_comments), "total": total, "total_known": total_known}


def _hydrate_missing_comment_replies(
    comments: List[dict],
    session: requests.Session,
    source_url: str,
    *,
    api_host_hint: Optional[str] = None,
    logger=None,
) -> tuple[List[dict], dict[str, int]]:
    if not comments or not source_url:
        return comments, {"roots": 0, "replies": 0}

    hydrated: List[dict] = []
    hydrated_root_count = 0
    hydrated_reply_count = 0
    fetch_started_at = time.perf_counter()

    for root in comments:
        if not isinstance(root, dict):
            continue
        normalized_root = dict(root)
        root_comment_id = str(normalized_root.get("id") or normalized_root.get("commentId") or "").strip()
        existing_replies = _comment_reply_items(normalized_root)
        expected_reply_count = _comment_reply_count(normalized_root)
        merged_replies = _merge_threaded_comment_list([], existing_replies)
        root_comment_source = str(normalized_root.get("commentSource") or "").strip().lower()
        root_rating_id = str(normalized_root.get("ratingId") or "").strip()
        is_rating_root = root_comment_source == "rating" or (
            bool(root_rating_id)
            and root_rating_id == root_comment_id
            and _comment_numeric(normalized_root.get("rating")) > 0
        )
        cursor_param = "msgRatingReplyId" if is_rating_root else "msgCommentReplyId"

        if root_comment_id and expected_reply_count > len(merged_replies):
            page_limit = min(max(expected_reply_count, 20), 200)
            after: Optional[int] = None
            last_reply_id = ""
            seen_offsets: set[int] = set()

            for _ in range(5):
                params: dict[str, object] = {"limit": page_limit}
                if after is not None:
                    params["after"] = after
                if last_reply_id:
                    params[cursor_param] = last_reply_id

                payload = _fetch_comment_reply_payload(
                    session,
                    source_url,
                    root_comment_id,
                    params=params,
                    api_host_hint=api_host_hint,
                    reply_kind="rating" if is_rating_root else "comment",
                )
                if payload is None:
                    break

                fetched_replies = _extract_comment_replies_from_payload(payload, root_comment_id)
                if not fetched_replies:
                    break

                before_count = len(merged_replies)
                merged_replies = _merge_threaded_comment_list(merged_replies, fetched_replies)
                if len(merged_replies) > before_count:
                    last_reply = merged_replies[-1] if merged_replies else {}
                    last_reply_id = str(last_reply.get("id") or last_reply_id).strip()

                if len(merged_replies) >= expected_reply_count:
                    break

                has_more = _extract_comment_reply_payload_has_more(payload)
                if has_more is False:
                    break
                if has_more is None and len(fetched_replies) < page_limit:
                    break

                next_after = len(merged_replies)
                if next_after in seen_offsets:
                    break
                seen_offsets.add(next_after)
                after = next_after

        if len(merged_replies) > len(existing_replies):
            hydrated_root_count += 1
            hydrated_reply_count += max(len(merged_replies) - len(existing_replies), 0)
        if merged_replies:
            normalized_root["replies"] = merged_replies
        elif "replies" in normalized_root:
            normalized_root["replies"] = []
        normalized_root["replyCount"] = max(len(merged_replies), expected_reply_count)
        hydrated.append(normalized_root)

    _log_perf(
        "comments.fetch_replies",
        fetch_started_at,
        logger=logger,
        roots=hydrated_root_count,
        replies=hydrated_reply_count,
    )
    return hydrated, {"roots": hydrated_root_count, "replies": hydrated_reply_count}


def collect_comments(
    next_data: dict,
    design: dict,
    session: requests.Session,
    out_dir: Path,
    progress_callback=None,
    progress_start: int = 50,
    progress_end: int = 55,
    download_assets: bool = True,
    existing_comments: Optional[List[dict]] = None,
    api_host_hint: Optional[str] = None,
) -> dict:
    emit_progress(progress_callback, progress_start, "正在整理评论数据")
    total_started_at = time.perf_counter()
    comments: List[dict] = []
    seen: dict[str, dict] = {}

    section_lookup_started_at = time.perf_counter()
    candidate_sections = _extract_comment_sections(next_data)
    candidate_sections.extend(_extract_comment_sections(design))
    unique_sections: List[object] = []
    seen_sections: set[int] = set()
    for section in candidate_sections:
        marker = id(section)
        if marker in seen_sections:
            continue
        seen_sections.add(marker)
        unique_sections.append(section)
    _log_perf(
        "comments.find_sections",
        section_lookup_started_at,
        sections=len(unique_sections),
    )

    extract_started_at = time.perf_counter()
    search_mode = "candidate_sections" if unique_sections else "full_scan"
    if unique_sections:
        comments = _extract_comment_list_items(unique_sections)
    if not comments:
        search_mode = "full_scan"
        comments = _extract_comment_list_items([next_data, design])
    comments, page_fetch_stats = _fetch_paginated_comment_list(
        comments,
        session,
        design,
        str(design.get("url") or next_data.get("url") or ""),
        api_host_hint=api_host_hint,
    )
    existing_comment_items = existing_comments if isinstance(existing_comments, list) else []
    if existing_comment_items:
        comments = _merge_threaded_comment_list(existing_comment_items, comments)
    comments, reply_fetch_stats = _hydrate_missing_comment_replies(
        comments,
        session,
        str(design.get("url") or next_data.get("url") or ""),
        api_host_hint=api_host_hint,
    )
    comment_total = _count_comment_threads(comments)
    _log_perf(
        "comments.extract",
        extract_started_at,
        mode=search_mode,
        comments=comment_total,
        roots=len(comments),
        sections=len(unique_sections),
        fetched_pages=page_fetch_stats.get("pages") or 0,
        fetched_roots=page_fetch_stats.get("roots") or 0,
        fetched_total=page_fetch_stats.get("total") or 0,
        hydrated_roots=reply_fetch_stats.get("roots") or 0,
        hydrated_replies=reply_fetch_stats.get("replies") or 0,
    )

    comment_count = _resolved_comment_count(
        unique_sections=unique_sections,
        next_data=next_data,
        design=design,
        comment_total=comment_total,
        page_fetch_stats=page_fetch_stats,
    )

    existing_comment_lookup = _build_existing_comment_lookup(existing_comments)
    existing_asset_stats = _apply_existing_comment_assets(comments, existing_comment_lookup, out_dir)
    asset_stats = {
        **_comment_resource_stats(comments),
        **existing_asset_stats,
        "avatar_cache_hits": 0,
        "comment_image_cache_hits": 0,
        "avatar_download_tasks": 0,
        "comment_image_download_tasks": 0,
        "download_tasks": 0,
        "download_completed": 0,
        "download_failed": 0,
        "deduped_downloads": 0,
    }

    if not download_assets:
        emit_progress(progress_callback, progress_end, "评论整理完成")
        _log_perf(
            "comments.total",
            total_started_at,
            mode=search_mode,
            comments=comment_total,
            roots=len(comments),
            assets=0,
            download_assets=False,
        )
        return {
            "count": max(comment_count, comment_total),
            "items": comments,
            "assetStats": asset_stats,
        }

    avatar_tasks: dict[str, dict] = {}
    image_tasks: dict[str, dict] = {}
    for idx, item in enumerate(_comment_tree_items(comments), start=1):
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        avatar_url = str(author.get("avatarUrl") or "").strip()
        if avatar_url:
            current_rel = str(author.get("avatarRelPath") or "").strip()
            if current_rel.startswith(f"{SHARED_AVATAR_REL_DIR}/") and _avatar_rel_path_exists(out_dir, current_rel):
                continue
            avatar_name = _avatar_cache_filename(avatar_url)
            avatar_rel = _shared_avatar_rel_path(avatar_name)
            avatar_target = _shared_avatar_dir(out_dir) / avatar_name
            if avatar_target.exists():
                asset_stats["avatar_cache_hits"] += 1
                _apply_author_avatar_ref(author, avatar_rel)
            else:
                avatar_key = avatar_rel
                if avatar_key not in avatar_tasks:
                    avatar_tasks[avatar_key] = {
                        "url": avatar_url,
                        "apply": [],
                        "error_message": "评论头像下载失败，保留原始链接：",
                        "download": lambda s=session, u=avatar_url, d=avatar_target: _download_asset_with_fresh_session(s, u, d),
                    }
                avatar_tasks[avatar_key]["apply"].append(
                    lambda author=author, rel=avatar_rel: _apply_author_avatar_ref(author, rel)
                )
        images = item.get("images") if isinstance(item.get("images"), list) else []
        for img_idx, image in enumerate(images, start=1):
            if not isinstance(image, dict):
                continue
            url = str(image.get("url") or "").strip()
            if not url:
                continue
            existing_rel = str(image.get("relPath") or "").strip()
            existing_local = str(image.get("localName") or "").strip()
            if _comment_local_asset_path(out_dir, rel_path=existing_rel, local_name=existing_local) is not None:
                continue
            image_name = f"comment_{idx:02d}_img_{img_idx:02d}.{pick_ext_from_url(url)}"
            image_rel = f"images/{image_name}"
            image_target = out_dir / image_name
            normalized_image_url = _normalize_url_value(url) or url
            if image_target.exists() and (existing_rel or existing_local):
                asset_stats["comment_image_cache_hits"] += 1
                _apply_comment_image_ref(image, image_name, image_rel)
            elif normalized_image_url in image_tasks:
                existing_image_task = image_tasks[normalized_image_url]
                existing_image_task["apply"].append(
                    lambda image=image, name=existing_image_task["local_name"], rel=existing_image_task["rel_path"]: _apply_comment_image_ref(image, name, rel)
                )
            else:
                image_tasks[normalized_image_url] = {
                    "url": url,
                    "local_name": image_name,
                    "rel_path": image_rel,
                    "apply": [
                        lambda image=image, name=image_name, rel=image_rel: _apply_comment_image_ref(image, name, rel)
                    ],
                    "error_message": "评论图片下载失败，保留原始链接：",
                    "download": lambda s=session, u=url, d=image_target: _download_asset_with_fresh_session(s, u, d),
                }

    asset_tasks = list(avatar_tasks.values()) + list(image_tasks.values())
    download_stats = _download_comment_assets(asset_tasks, progress_callback, progress_start, progress_end)
    asset_stats["avatar_download_tasks"] = len(avatar_tasks)
    asset_stats["comment_image_download_tasks"] = len(image_tasks)
    asset_stats["download_tasks"] = len(asset_tasks)
    asset_stats["download_completed"] = int(download_stats.get("completed") or 0)
    asset_stats["download_failed"] = int(download_stats.get("failed") or 0)
    skipped_or_reused = (
        int(asset_stats.get("avatar_existing_reused") or 0)
        + int(asset_stats.get("avatar_shared_reused") or 0)
        + int(asset_stats.get("avatar_shared_migrated") or 0)
        + int(asset_stats.get("comment_image_existing_reused") or 0)
        + int(asset_stats.get("avatar_cache_hits") or 0)
        + int(asset_stats.get("comment_image_cache_hits") or 0)
    )
    requested_resources = int(asset_stats.get("avatar_urls") or 0) + int(asset_stats.get("comment_images") or 0)
    asset_stats["deduped_downloads"] = max(requested_resources - skipped_or_reused - len(asset_tasks), 0)

    emit_progress(progress_callback, progress_end, "评论整理完成")
    _log_perf(
        "comments.total",
        total_started_at,
        mode=search_mode,
        comments=comment_total,
        roots=len(comments),
        assets=len(asset_tasks),
    )

    return {
        "count": max(comment_count, comment_total),
        "items": comments,
        "assetStats": asset_stats,
    }


def _normalize_api_base(base: Optional[str]) -> Optional[str]:
    if not base:
        return None
    base = base.strip()
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"https://{base}"
    return base.rstrip("/")


def _unique_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in seq:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _build_instance_api_candidates(
    inst_id: int,
    api_url: Optional[str],
    origin: Optional[str],
    api_host_hint: Optional[str],
) -> List[str]:
    candidates = []
    source = (
        normalize_makerworld_source(url=origin)
        or normalize_makerworld_source(url=api_url)
        or normalize_makerworld_source(url=api_host_hint)
    )

    bases = []
    preferred_api_bases = (
        ["https://api.bambulab.com", "https://api.bambulab.cn"]
        if source == "global"
        else ["https://api.bambulab.cn", "https://api.bambulab.com"]
    )
    for base in [api_host_hint, *preferred_api_bases, origin]:
        normalized = _normalize_api_base(base)
        if normalized and normalized not in bases:
            bases.append(normalized)

    for base in bases:
        host = urlparse(base).netloc.lower()
        if "api.bambulab" in host:
            path_templates = [
                "/v1/design-service/instance/{id}/f3mf",
            ]
        elif "makerworld.com" in host:
            path_templates = [
                "/api/v1/design-service/instance/{id}/f3mf",
            ]
        else:
            path_templates = [
                "/api/v1/design-service/instance/{id}/f3mf",
                "/v1/design-service/instance/{id}/f3mf",
            ]
        for path in path_templates:
            for file_type in ("3mf", ""):
                candidates.append(
                    f"{base}{path.format(id=inst_id)}?type=download&fileType={file_type}"
                )

    if api_url:
        candidates.append(api_url)

    return _unique_preserve(candidates)


def _extract_instance_download_hint(payload: object) -> tuple[str, str, str]:
    best_name = ""
    best_url = ""
    best_api_url = ""
    best_score = -1
    stack: list[tuple[object, int]] = [(payload, 0)]
    seen: set[int] = set()

    while stack:
        current, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(current, (dict, list)):
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)

        if isinstance(current, dict):
            name = str(
                current.get("name")
                or current.get("fileName")
                or current.get("filename")
                or current.get("file_name")
                or current.get("title")
                or ""
            ).strip()
            file_type = str(current.get("fileType") or current.get("type") or current.get("ext") or "").strip().lower()
            api_url = _normalize_url_value(
                current.get("apiUrl")
                or current.get("api_url")
                or current.get("downloadApiUrl")
                or current.get("download_api_url")
            )
            explicit_url = _normalize_url_value(
                current.get("downloadUrl")
                or current.get("download_url")
                or current.get("downloadURL")
            )
            loose_url = _normalize_url_value(current.get("url") or current.get("src") or current.get("href"))

            candidate_url = ""
            if explicit_url:
                if _looks_like_instance_api_url(explicit_url):
                    api_url = api_url or explicit_url
                else:
                    candidate_url = explicit_url
            elif loose_url:
                if _looks_like_instance_api_url(loose_url):
                    api_url = api_url or loose_url
                elif _looks_like_3mf_file_url(loose_url) or file_type == "3mf" or name.lower().endswith(".3mf"):
                    candidate_url = loose_url

            score = 0
            if explicit_url and candidate_url:
                score += 60
            elif candidate_url:
                score += 35
            if _looks_like_3mf_file_url(candidate_url):
                score += 30
            if file_type == "3mf":
                score += 20
            if name.lower().endswith(".3mf"):
                score += 20
            score -= depth

            if candidate_url and score > best_score:
                best_name = name
                best_url = candidate_url
                best_api_url = api_url
                best_score = score
            elif api_url and not best_api_url:
                best_api_url = api_url

            for value in current.values():
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))
        elif isinstance(current, list):
            for value in current:
                if isinstance(value, (dict, list)):
                    stack.append((value, depth + 1))

    return best_name, best_url, best_api_url


def _looks_like_html(text: str) -> bool:
    if not text:
        return False
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or "<html" in head


def _stringify_3mf_failure_payload(payload) -> str:
    if payload in ("", None):
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False)
    except Exception:
        return str(payload)


def _extract_three_mf_verification_payload(payload) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    captcha_id = str(
        payload.get("captchaId")
        or payload.get("captcha_id")
        or payload.get("captchaID")
        or ""
    ).strip()
    if not captcha_id:
        return {}
    return {
        "captcha_id": captcha_id,
        "provider": "geetest",
    }


def _classify_3mf_fetch_failure(
    *,
    status_code: int = 0,
    text: str = "",
    payload=None,
    cloudflare: bool = False,
    source: str = "",
) -> dict:
    raw_text = str(text or "").strip()
    payload_text = _stringify_3mf_failure_payload(payload)
    combined = " ".join(part for part in (raw_text, payload_text) if part).lower()
    normalized_source = normalize_makerworld_source(source=source)

    if is_three_mf_daily_download_limited(combined):
        return {
            "state": "download_limited",
            "message": describe_three_mf_failure("download_limited", source=normalized_source),
        }
    if status_code == 418 or any(
        keyword in combined
        for keyword in (
            "captcha",
            "verification required",
            "verify you are human",
            "security check",
            "challenge",
        )
    ):
        result = {
            "state": "verification_required",
            "message": describe_three_mf_failure("verification_required", source=normalized_source),
        }
        verification = _extract_three_mf_verification_payload(payload)
        if verification:
            result["verification"] = verification
        return result
    if "please log in to download models" in combined or "log in to download models" in combined:
        return {
            "state": "auth_required",
            "message": describe_three_mf_failure("auth_required", source=normalized_source),
        }
    if status_code in {401, 403}:
        return {
            "state": "auth_required",
            "message": describe_three_mf_failure("auth_required", source=normalized_source),
        }
    if _is_makerworld_not_found_page(raw_text):
        return {
            "state": "not_found",
            "message": _makerworld_not_found_message(),
        }
    if status_code == 404 or "route not found" in combined or "\"detail\":\"not found\"" in combined or "not found" in combined:
        return {
            "state": "not_found",
            "message": describe_three_mf_failure("not_found", source=normalized_source),
        }
    if cloudflare or _is_cloudflare_challenge(raw_text) or _looks_like_html(raw_text):
        return {
            "state": "cloudflare",
            "message": describe_three_mf_failure("cloudflare", source=normalized_source),
        }
    if status_code >= 400:
        return {
            "state": "http_error",
            "message": f"下载 3MF 失败：上游返回 HTTP {status_code}。",
        }
    return {
        "state": "missing",
        "message": "未获取到 3MF 下载地址。",
    }


def _should_stop_three_mf_fetch(failure: Optional[dict]) -> bool:
    return _should_pause_three_mf_fetch(failure)


def _summarize_three_mf_fetch_attempts(attempts: list[dict]) -> str:
    if not attempts:
        return "no-attempts"
    status_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    host_counts: dict[str, int] = {}
    for attempt in attempts:
        status = str(attempt.get("status") or "error")
        state = str(attempt.get("state") or "unknown")
        host = urlparse(str(attempt.get("url") or "")).netloc or "unknown-host"
        status_counts[status] = status_counts.get(status, 0) + 1
        state_counts[state] = state_counts.get(state, 0) + 1
        host_counts[host] = host_counts.get(host, 0) + 1

    def _compact(values: dict[str, int]) -> str:
        return ",".join(f"{key}:{count}" for key, count in sorted(values.items())[:8])

    return f"attempts={len(attempts)} statuses={_compact(status_counts)} states={_compact(state_counts)} hosts={_compact(host_counts)}"


def _browser_three_mf_authorization_failure(
    *,
    status_code: int = 0,
    text: str = "",
    payload: Optional[dict] = None,
    source: str = "",
) -> dict:
    failure = _classify_3mf_fetch_failure(
        status_code=status_code,
        text=text,
        payload=payload,
        source=source,
    )
    if str(failure.get("state") or "") in {"auth_required", "cookie_invalid", "cloudflare", "verification_required"}:
        return {
            "state": "verification_required",
            "message": "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
        }
    return failure


def _normalized_auto_verification_diagnostics(verification: object) -> dict:
    if not isinstance(verification, dict):
        return {"attempted": False, "completed": False, "fields": {}}

    def _safe_code(value: object, allowed: frozenset[str]) -> str:
        if isinstance(value, str) and value in allowed:
            return value
        return "unknown"

    def _bounded_attempts(value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, min(value, 99))
        if not isinstance(value, float) or not math.isfinite(value):
            return 0
        if value <= 0:
            return 0
        if value >= 99:
            return 99
        return int(value)

    def _bounded_confidence(value: object) -> float:
        if isinstance(value, bool):
            return 0.0
        if isinstance(value, int):
            return float(max(0, min(value, 1)))
        if not isinstance(value, float) or not math.isfinite(value):
            return 0.0
        return round(max(0.0, min(float(value), 1.0)), 2)

    return {
        "attempted": verification.get("attempted") is True,
        "completed": verification.get("completed") is True,
        "fields": {
            "provider": _safe_code(verification.get("provider"), SAFE_VERIFICATION_PROVIDERS),
            "challenge_type": _safe_code(verification.get("challenge_type"), SAFE_VERIFICATION_CHALLENGE_TYPES),
            "attempts": _bounded_attempts(verification.get("attempts")),
            "reason": _safe_code(verification.get("reason"), SAFE_REASON_CODES),
            "confidence": _bounded_confidence(verification.get("confidence")),
        },
    }


def _log_auto_verification_result(diagnostics: dict, *, signed_url_available: bool) -> None:
    fields = diagnostics.get("fields") if isinstance(diagnostics.get("fields"), dict) else {}
    try:
        if diagnostics.get("completed") is True and signed_url_available:
            append_business_log(
                "archive",
                "cloakbrowser_auto_verification_completed",
                "指纹浏览器已完成 3MF 自动验证。",
                level="info",
                **fields,
            )
        elif diagnostics.get("attempted") is True and not signed_url_available:
            append_business_log(
                "archive",
                "cloakbrowser_auto_verification_fallback",
                "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
                level="warning",
                **fields,
            )
    except Exception:
        pass


def browser_authorize_3mf_download(
    platform: str,
    api_url: str,
    *,
    profile_id: str,
    model_url: str = "",
    instance_id: str = "",
) -> dict:
    # Import lazily because cloakbrowser_session reaches legacy_archiver through account discovery.
    from app.services.cloakbrowser_session import browser_authorize_3mf_download as authorize

    return authorize(
        platform,
        api_url,
        profile_id=profile_id,
        model_url=model_url,
        instance_id=instance_id,
    )


def fetch_instance_3mf(
    session: requests.Session,
    inst_id: int,
    raw_cookie: str,
    api_url: str = None,
    api_host_hint: Optional[str] = None,
    origin: Optional[str] = None,
    captcha_result_header: str = "",
    browser_authorization: bool = False,
    browser_profile_id: str = "",
    model_page_url: str = "",
):
    """
    获取实例的 3MF 下载地址，允许外部传入 api_url，并自动回退不同 API Host。
    返回: (name, url, used_api_url, failure_info)
    """
    if fake_three_mf_downloads_enabled():
        fake_name = f"{sanitize_filename(str(inst_id or 'model')) or 'model'}.3mf"
        return fake_name, _fake_three_mf_download_url(inst_id), api_url or "", {
            "state": "available",
            "message": FAKE_THREE_MF_DOWNLOAD_MESSAGE,
        }
    effective_cookie = sanitize_cookie_header(raw_cookie) or _session_cookie_header(session)
    candidates = _build_instance_api_candidates(inst_id, api_url, origin, api_host_hint)
    auth_token = _extract_auth_token(effective_cookie)
    last_error = None
    last_failure = {"state": "missing", "message": "未获取到 3MF 下载地址。"}
    attempts: list[dict] = []
    source_hint = (
        normalize_makerworld_source(url=origin)
        or normalize_makerworld_source(url=api_url)
        or normalize_makerworld_source(url=api_host_hint)
    )
    if candidates:
        _wait_before_three_mf_download(f"获取下载地址 {inst_id}")
    if browser_authorization:
        candidate = candidates[0] if candidates else api_url or ""
        if not candidate:
            return "", "", "", {"state": "missing", "message": "未获取到 3MF 下载地址。"}
        try:
            browser_result = browser_authorize_3mf_download(
                source_hint or normalize_makerworld_source(url=candidate),
                candidate,
                profile_id=browser_profile_id,
                model_url=model_page_url or origin or "",
                instance_id=str(inst_id or ""),
            )
        except Exception as exc:
            from app.services.cloakbrowser_session import CloakBrowserError

            if not isinstance(exc, CloakBrowserError):
                raise
            error_detail = str(exc).strip()[:300]
            log(None, "指纹浏览器 3MF 授权失败:", error_detail or type(exc).__name__)
            try:
                append_business_log(
                    "archive",
                    "cloakbrowser_3mf_authorization_error",
                    "指纹浏览器暂时无法完成 3MF 授权。",
                    level="warning",
                    instance_id=str(inst_id or ""),
                    model_url=_trace_url(model_page_url or origin or ""),
                    error=error_detail,
                )
            except Exception:
                pass
            return "", "", candidate, {
                "state": "http_error",
                "message": "指纹浏览器暂时无法完成 3MF 授权，将稍后自动重试。",
            }
        browser_payload = browser_result.get("payload") if isinstance(browser_result.get("payload"), dict) else {}
        verification_diagnostics = _normalized_auto_verification_diagnostics(browser_result.get("verification"))
        name, url = _extract_instance_download(browser_payload)
        if url:
            _log_auto_verification_result(verification_diagnostics, signed_url_available=True)
            return name, url, candidate, {"state": "available", "message": ""}
        failure = _browser_three_mf_authorization_failure(
            status_code=int(browser_result.get("status_code") or 0),
            text=str(browser_result.get("text") or ""),
            payload=browser_payload,
            source=source_hint or candidate,
        )
        _log_auto_verification_result(verification_diagnostics, signed_url_available=False)
        return "", "", candidate, failure
    for candidate in candidates:
        candidate_source = source_hint or normalize_makerworld_source(url=candidate)
        cookie_header = effective_cookie
        headers = {
            **MAKERWORLD_API_BROWSER_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": origin or "https://makerworld.com.cn/",
            "Origin": origin or "https://makerworld.com.cn",
            "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0 (MW-Fetcher)"),
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            headers["token"] = auth_token
            headers["X-Token"] = auth_token
            headers["X-Access-Token"] = auth_token
        clean_captcha_result = str(captcha_result_header or "").strip()
        if clean_captcha_result:
            headers["x-bbl-captcha-result"] = clean_captcha_result
        try:
            data = makerworld_browser_get_json(
                candidate,
                raw_cookie=cookie_header,
                headers=headers,
                session=session,
                allow_non_json=True,
            )
        except MakerWorldBrowserError as e:
            last_error = e
            failure = _classify_3mf_fetch_failure(text=str(e), source=candidate_source)
            last_failure = merge_three_mf_failure(last_failure, failure)
            attempts.append({"method": "cloakbrowser", "url": candidate, "status": "exception", "state": failure.get("state")})
            if _should_stop_three_mf_fetch(failure):
                log("3MF 获取失败", inst_id, str(failure.get("state") or "missing"), str(failure.get("message") or ""))
                return "", "", candidate, last_failure
            continue
        if not isinstance(data, dict):
            failure = _classify_3mf_fetch_failure(text="", source=candidate_source)
            last_failure = merge_three_mf_failure(last_failure, failure)
            attempts.append({"method": "cloakbrowser", "url": candidate, "status": "non-json", "state": failure.get("state")})
            continue
        name, url = _extract_instance_download(data)
        if url:
            return name, url, candidate, {"state": "available", "message": ""}
        text_preview = json.dumps(data, ensure_ascii=False)[:400]
        failure = _classify_3mf_fetch_failure(
            status_code=200,
            text=text_preview,
            payload=data,
            source=candidate_source,
        )
        last_failure = merge_three_mf_failure(last_failure, failure)
        attempts.append({"method": "cloakbrowser", "url": candidate, "status": 200, "state": failure.get("state")})
        if _should_stop_three_mf_fetch(failure):
            log("3MF 获取失败", inst_id, str(failure.get("state") or "missing"), str(failure.get("message") or ""))
            return "", "", candidate, last_failure

    if _should_stop_three_mf_fetch(last_failure):
        log("3MF 获取失败", inst_id, str(last_failure.get("state") or "missing"), str(last_failure.get("message") or ""))
        return "", "", candidates[-1] if candidates else api_url or "", last_failure
    log("3MF 获取失败", inst_id, _summarize_three_mf_fetch_attempts(attempts), str(last_failure.get("message") or ""))
    return "", "", api_url or "", last_failure


@dataclass(frozen=True)
class ArchiveDependencies:
    fetch_html_with_browser: Callable[..., Optional[str]]
    fetch_design_from_api: Callable[..., Optional[dict]]
    collect_comments: Callable[..., dict]
    fetch_instance_3mf: Callable[..., tuple]
    reserve_three_mf_download_slot: Callable[..., dict]


DEFAULT_ARCHIVE_DEPENDENCIES = ArchiveDependencies(
    fetch_html_with_browser=fetch_html_with_browser,
    fetch_design_from_api=fetch_design_from_api,
    collect_comments=collect_comments,
    fetch_instance_3mf=fetch_instance_3mf,
    reserve_three_mf_download_slot=reserve_three_mf_download_slot,
)


def _current_archive_dependencies() -> ArchiveDependencies:
    return ArchiveDependencies(
        fetch_html_with_browser=fetch_html_with_browser,
        fetch_design_from_api=fetch_design_from_api,
        collect_comments=collect_comments,
        fetch_instance_3mf=fetch_instance_3mf,
        reserve_three_mf_download_slot=reserve_three_mf_download_slot,
    )


def archive_dependencies_with_overrides(**overrides: Callable[..., Any]) -> ArchiveDependencies:
    return replace(DEFAULT_ARCHIVE_DEPENDENCIES, **overrides)


def _archive_model(
    url: str,
    cookie: str,
    download_dir: Path,
    logs_dir: Path,
    logger=None,
    existing_root: Optional[Path] = None,
    progress_callback=None,
    skip_three_mf_fetch: bool = False,
    three_mf_skip_message: str = "",
    download_assets: bool = True,
    download_comment_assets: Optional[bool] = None,
    collect_comments_data: bool = True,
    rebuild_archive: bool = True,
    record_missing_3mf_log: bool = True,
    three_mf_skip_state: str = "",
    three_mf_daily_limit_cn: int = 100,
    three_mf_daily_limit_global: int = 100,
    existing_model_dir: str = "",
    three_mf_captcha_result_header: str = "",
    browser_three_mf_authorization: bool = False,
    browser_profile_id: str = "",
    instance_ids: Optional[list[str]] = None,
    _dependencies: Optional[ArchiveDependencies] = None,
):
    """
    对外主入口：采集 + 下载文件 + 生成 meta，并整理归档目录。
    返回: {base_name, work_dir, missing_3mf, action}
    """
    dependencies = _dependencies or _current_archive_dependencies()
    archive_started_at = time.perf_counter()
    timings_ms: dict[str, float] = {}
    # 采集阶段
    out_root = download_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    comment_download_assets = download_assets if download_comment_assets is None else bool(download_comment_assets)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (MW-Fetcher)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    })
    raw_cookie_header = (cookie or "").strip()
    parsed_cookies = parse_cookies(raw_cookie_header)
    sess.cookies.update(parsed_cookies)

    fetch_url = url.split("#", 1)[0]
    emit_progress(progress_callback, 5, "准备抓取模型页面")
    log(logger, "获取页面:", fetch_url)
    log(logger, "请求头:", sess.headers)
    log(logger, "请求 Cookie:", summarize_cookie_header(raw_cookie_header, parsed_cookies))

    fetch_started_at = time.perf_counter()
    emit_progress(progress_callback, 12, "正在获取模型页面")
    html_text = dependencies.fetch_html_with_browser(sess, fetch_url, raw_cookie_header)
    if not html_text:
        raise RuntimeError("CloakBrowser 获取模型页面失败，请检查指纹浏览器服务和当前平台 profile 状态。")
    elif "__NEXT_DATA__" not in html_text and "__NUXT__" not in html_text:
        log(logger, "CloakBrowser 页面未包含 __NEXT_DATA__ 或 __NUXT__。")
    timings_ms["fetch_html"] = _log_perf(
        "archive.fetch_html",
        fetch_started_at,
        logger=logger,
        via="cloakbrowser",
        html_bytes=len(html_text or ""),
    )

    has_app_data = "__NEXT_DATA__" in html_text or "__NUXT__" in html_text
    is_cloudflare_challenge = _is_cloudflare_challenge(html_text)
    is_makerworld_not_found = _is_makerworld_not_found_page(html_text)
    if not has_app_data:
        log(logger, "页面未包含 __NEXT_DATA__，前 300 字符:", (html_text or "")[:300])
    if is_cloudflare_challenge and not has_app_data:
        log(logger, "疑似 Cloudflare 验证拦截，请更新 cookie 中的 cf_clearance")
    if is_makerworld_not_found and not has_app_data:
        log(logger, "模型页面返回 404，可能已下架、设为私有或转为草稿")

    design = None
    design_payload_error = ""
    api_fallback_reason = ""
    next_data = {}
    next_data_started_at = time.perf_counter()
    try:
        next_data = extract_next_data(html_text)
        design = extract_design_from_next_data(next_data)
        if design is not None:
            design_payload_error = _design_payload_error(design, fetch_url)
            if design_payload_error:
                api_fallback_reason = f"页面内嵌模型数据无效：{design_payload_error}"
                log(logger, f"{api_fallback_reason}，正在改用接口抓取")
                design = None
            else:
                _normalize_design_payload_identity(design, fetch_url)
        if design is None:
            if not api_fallback_reason:
                if is_makerworld_not_found:
                    api_fallback_reason = "模型页面返回 404"
                elif is_cloudflare_challenge:
                    api_fallback_reason = "页面疑似验证拦截"
                elif not has_app_data:
                    api_fallback_reason = "页面未包含内嵌模型数据"
                elif next_data:
                    api_fallback_reason = "页面内嵌数据未定位到模型"
                else:
                    api_fallback_reason = "页面内嵌数据为空"
            log(logger, f"{api_fallback_reason}，正在改用接口抓取")
    except Exception as e:
        api_fallback_reason = f"页面内嵌数据解析失败：{e}"
        log(logger, f"{api_fallback_reason}，正在改用接口抓取")
    timings_ms["extract_next_data"] = _log_perf(
        "archive.extract_next_data",
        next_data_started_at,
        logger=logger,
        found=bool(next_data),
        design_found=bool(design),
    )

    api_host_hint = _extract_api_host(html_text)
    if design is None:
        fallback_message = api_fallback_reason or "页面内嵌数据不完整"
        emit_progress(progress_callback, 22, f"{fallback_message}，正在改用接口抓取")
        api_fallback_started_at = time.perf_counter()
        design = dependencies.fetch_design_from_api(
            sess,
            raw_cookie_header,
            fetch_url,
            api_host_hint=api_host_hint,
            logger=logger,
        )
        timings_ms["fetch_design_api"] = _log_perf(
            "archive.fetch_design_api",
            api_fallback_started_at,
            logger=logger,
            success=bool(design),
        )

    if design is None:
        if is_makerworld_not_found:
            raise RuntimeError(_makerworld_not_found_message())
        if is_cloudflare_challenge:
            raise RuntimeError("页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试")
        if design_payload_error:
            raise RuntimeError(f"源端返回的模型数据无效：{design_payload_error}，请更新 Cookie 或完成 MakerWorld 验证后重试")
        raise RuntimeError("未能解析模型数据，请确认 cookie/页面结构")

    design["url"] = url
    emit_progress(progress_callback, 30, "已解析模型信息，准备下载资源")

    design_id = design.get("id") or _parse_design_id(url)
    if design_id is None:
        raise RuntimeError("未获取到模型 ID")
    title = design.get("title") or "model"
    base_name, action = choose_archive_base_name(
        design_id,
        title,
        existing_root=existing_root,
        existing_model_dir=existing_model_dir,
    )
    work_dir = out_root / base_name
    existing_meta = load_existing_meta(work_dir) if action == "updated" else {}
    images_dir = work_dir / "images"
    ensure_dir(images_dir)

    author = extract_author(design, html_text)
    existing_author = existing_meta.get("author") if isinstance(existing_meta.get("author"), dict) else {}
    if author.get("avatarUrl"):
        author_avatar_matches = _media_item_remote_matches(existing_author, author["avatarUrl"], url_fields=("avatarUrl", "url"))
        if download_assets:
            if (
                author_avatar_matches
                and _media_item_local_exists(images_dir, existing_author, rel_fields=("avatarRelPath",), local_fields=("avatarLocal",))
            ):
                author["avatarLocal"] = str(existing_author.get("avatarLocal") or "").strip()
                author["avatarRelPath"] = str(existing_author.get("avatarRelPath") or "").strip()
            else:
                ext = pick_ext_from_url(author["avatarUrl"])
                fname = f"author_avatar.{ext}"
                try:
                    download_file(
                        sess,
                        author["avatarUrl"],
                        images_dir / fname,
                        overwrite=True,
                        max_duration=IMAGE_TRANSFER_TIMEOUT_SECONDS,
                    )
                    author["avatarLocal"] = fname
                    author["avatarRelPath"] = f"images/{fname}"
                except Exception as exc:
                    _log_asset_download_failure(
                        "作者头像下载失败，保留原始链接：",
                        author["avatarUrl"],
                        exc,
                        logger=logger,
                    )
        else:
            if author_avatar_matches and str(existing_author.get("avatarLocal") or "").strip():
                author["avatarLocal"] = str(existing_author.get("avatarLocal") or "").strip()
            if author_avatar_matches and str(existing_author.get("avatarRelPath") or "").strip():
                author["avatarRelPath"] = str(existing_author.get("avatarRelPath") or "").strip()

    emit_progress(progress_callback, 40, "正在整理摘要与设计图片")
    summary_started_at = time.perf_counter()
    summary = parse_summary(
        design,
        base_name,
        sess,
        images_dir,
        progress_callback=progress_callback,
        progress_start=40,
        progress_end=45,
        download_assets=download_assets,
        existing_meta=existing_meta,
    )
    timings_ms["parse_summary"] = _log_perf(
        "archive.parse_summary",
        summary_started_at,
        logger=logger,
        summary_images=len(summary.get("summaryImages") or []),
    )
    design_images_started_at = time.perf_counter()
    design_images, cover_meta = collect_design_images(
        design,
        sess,
        images_dir,
        base_name,
        progress_callback=progress_callback,
        progress_start=45,
        progress_end=50,
        download_assets=download_assets,
        existing_images=existing_meta.get("designImages") if isinstance(existing_meta.get("designImages"), list) else [],
    )
    timings_ms["collect_design_images"] = _log_perf(
        "archive.collect_design_images",
        design_images_started_at,
        logger=logger,
        design_images=len(design_images),
    )
    attachments_started_at = time.perf_counter()
    attachments = extract_design_attachments(design)
    existing_attachment_lookup = _build_existing_media_lookup(
        existing_meta.get("attachments") if isinstance(existing_meta.get("attachments"), list) else [],
        url_fields=("url", "downloadUrl"),
    )
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_url = str(attachment.get("url") or "").strip()
        existing_attachment = _match_existing_media_item_from_lookup(
            existing_attachment_lookup,
            url=attachment_url,
        )
        if _media_item_remote_matches(existing_attachment, attachment_url, url_fields=("url", "downloadUrl")):
            if str(existing_attachment.get("localName") or "").strip():
                attachment["localName"] = str(existing_attachment.get("localName") or "").strip()
            if str(existing_attachment.get("relPath") or "").strip():
                attachment["relPath"] = str(existing_attachment.get("relPath") or "").strip()
    timings_ms["extract_attachments"] = _log_perf(
        "archive.extract_attachments",
        attachments_started_at,
        logger=logger,
        attachments=len(attachments),
    )
    if download_assets and attachments:
        attachment_download_started_at = time.perf_counter()
        files_dir = work_dir / "file"
        ensure_dir(files_dir)
        for idx, attachment in enumerate(attachments, start=1):
            if not isinstance(attachment, dict):
                continue
            attachment_url = str(attachment.get("url") or "").strip()
            local_name = str(attachment.get("localName") or "").strip()
            if not attachment_url or not local_name:
                continue
            existing_attachment = _match_existing_media_item_from_lookup(
                existing_attachment_lookup,
                url=attachment_url,
            )
            if (
                _media_item_remote_matches(existing_attachment, attachment_url, url_fields=("url", "downloadUrl"))
                and _media_item_local_exists(work_dir / "file", existing_attachment, rel_fields=("relPath",), local_fields=("localName", "fileName"))
            ):
                continue
            emit_progress(
                progress_callback,
                50,
                f"正在下载附件（{idx}/{len(attachments)}）",
                {"current": idx, "total": len(attachments)},
            )
            try:
                download_file(
                    sess,
                    attachment_url,
                    files_dir / local_name,
                    overwrite=True,
                    max_duration=BINARY_TRANSFER_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                _log_asset_download_failure(
                    "附件下载失败，保留原始链接：",
                    attachment_url,
                    exc,
                    logger=logger,
                )
        timings_ms["download_attachments"] = _log_perf(
            "archive.download_attachments",
            attachment_download_started_at,
            logger=logger,
            attachments=len(attachments),
        )
    comments_started_at = time.perf_counter()
    if collect_comments_data:
        comments_bundle = dependencies.collect_comments(
            next_data,
            design,
            sess,
            images_dir,
            progress_callback=progress_callback,
            progress_start=50,
            progress_end=55,
            download_assets=comment_download_assets,
            existing_comments=existing_meta.get("comments") if isinstance(existing_meta.get("comments"), list) else [],
            api_host_hint=api_host_hint,
        )
    else:
        existing_stats = existing_meta.get("stats") if isinstance(existing_meta.get("stats"), dict) else {}
        try:
            existing_comment_count = int(existing_stats.get("comments") or 0)
        except (TypeError, ValueError):
            existing_comment_count = 0
        comments_bundle = {
            "count": max(existing_comment_count, 0),
            "items": existing_meta.get("comments") if isinstance(existing_meta.get("comments"), list) else [],
            "assetStats": {},
        }
    timings_ms["collect_comments"] = _log_perf(
        "archive.collect_comments",
        comments_started_at,
        logger=logger,
        comments=len(comments_bundle.get("items") or []),
        comment_count=comments_bundle.get("count") or 0,
        skipped=not collect_comments_data,
    )
    emit_progress(progress_callback, 55, "摘要、图片与评论整理完成")

    parsed_origin = urlparse(fetch_url)
    origin = f"{parsed_origin.scheme}://{parsed_origin.netloc}" if parsed_origin.scheme and parsed_origin.netloc else "https://makerworld.com.cn"
    makerworld_source = normalize_makerworld_source(url=fetch_url) or normalize_makerworld_source(url=origin)

    inst_list = []
    planned_instances_dir = work_dir / "instances"
    existing_instance_index = _build_existing_instance_index(existing_meta.get("instances"))
    extracted_instances = extract_instances(design)
    three_mf_download_prohibited = is_three_mf_download_prohibited(design)
    target_instance_ids = {
        str(item or "").strip()
        for item in (instance_ids or [])
        if str(item or "").strip()
    }
    if target_instance_ids:
        extracted_instances = [
            inst
            for inst in extracted_instances
            if str(
                inst.get("id")
                or inst.get("instanceId")
                or inst.get("profileId")
                or inst.get("profile_id")
                or ""
            ).strip() in target_instance_ids
        ]
    total_instances = max(len(extracted_instances), 1)
    instance_stage_started_at = time.perf_counter()
    payload_hint_hits = 0
    existing_hint_hits = 0
    fetched_hint_hits = 0
    api_fetch_attempts = 0
    existing_planned_instance_files = {
        path.name
        for path in _list_directory_entries(planned_instances_dir)
        if path.is_file()
    }
    reserved_planned_instance_names: set[str] = set()
    three_mf_fetch_paused = bool(skip_three_mf_fetch)
    normalized_three_mf_skip_state = str(three_mf_skip_state or "").strip()
    if skip_three_mf_fetch and not normalized_three_mf_skip_state:
        normalized_three_mf_skip_state = "pending_download"
    skipped_due_limit = 0
    for idx, inst in enumerate(extracted_instances, start=1):
        inst_id = inst.get("id") or inst.get("instanceId")
        if inst_id is None:
            continue
        existing_inst = _find_existing_instance(inst, existing_instance_index)
        emit_progress(
            progress_callback,
            55 + min(int(idx * 20 / total_instances), 20),
            f"正在处理实例信息（{idx}/{len(extracted_instances)}）",
            {"current": idx, "total": len(extracted_instances)},
        )
        plates, pics = collect_instance_media(
            inst,
            sess,
            images_dir,
            base_name,
            download_assets=download_assets,
            existing_instance=existing_inst,
        )
        hinted_name, hinted_url, hinted_api_url = _extract_instance_download_hint(inst)
        api_url = (
            hinted_api_url
            or inst.get("apiUrl")
            or existing_inst.get("apiUrl")
            or f"{origin}/api/v1/design-service/instance/{inst_id}/f3mf?type=download&fileType="
        )
        name3mf = hinted_name or str(existing_inst.get("name") or "").strip()
        url3mf = hinted_url or str(existing_inst.get("downloadUrl") or "").strip()
        used_api_url = api_url
        failure_info = (
            {"state": "available", "message": ""}
            if url3mf
            else {
                "state": str(existing_inst.get("downloadState") or "missing"),
                "message": str(existing_inst.get("downloadMessage") or "未获取到 3MF 下载地址。"),
            }
        )
        existing_file_name = str(existing_inst.get("fileName") or "").strip()
        existing_file_available = bool(existing_file_name and (planned_instances_dir / existing_file_name).exists())
        if three_mf_download_prohibited:
            if not existing_file_available:
                name3mf = ""
                url3mf = ""
            failure_info = {
                "state": THREE_MF_NOT_DOWNLOADABLE_STATE,
                "message": describe_three_mf_failure(THREE_MF_NOT_DOWNLOADABLE_STATE),
            }
        elif fake_three_mf_downloads_enabled() and not three_mf_fetch_paused:
            name3mf, url3mf, used_api_url, failure_info = dependencies.fetch_instance_3mf(
                sess,
                inst_id,
                raw_cookie_header,
                api_url,
                api_host_hint=api_host_hint,
                origin=origin,
                captcha_result_header=three_mf_captcha_result_header,
                browser_authorization=browser_three_mf_authorization,
                browser_profile_id=browser_profile_id,
                model_page_url=fetch_url,
            )
            if url3mf:
                fetched_hint_hits += 1
            elif _should_pause_three_mf_fetch(failure_info):
                three_mf_fetch_paused = True
                normalized_three_mf_skip_state = str(failure_info.get("state") or "").strip()
                three_mf_skip_message = str(failure_info.get("message") or three_mf_skip_message or "")
        elif three_mf_fetch_paused and url3mf and not existing_file_available:
            url3mf = ""
            skipped_due_limit += 1
            failure_info = _missing_3mf_failure_for_skipped_fetch(
                skip_state=normalized_three_mf_skip_state,
                skip_message=three_mf_skip_message,
                existing_state=existing_inst.get("downloadState"),
                existing_message=existing_inst.get("downloadMessage"),
                fetch_url=fetch_url,
            )
        elif hinted_url:
            payload_hint_hits += 1
        elif url3mf:
            existing_hint_hits += 1
        elif three_mf_fetch_paused:
            skipped_due_limit += 1
            failure_info = _missing_3mf_failure_for_skipped_fetch(
                skip_state=normalized_three_mf_skip_state,
                skip_message=three_mf_skip_message,
                existing_state=existing_inst.get("downloadState"),
                existing_message=existing_inst.get("downloadMessage"),
                fetch_url=fetch_url,
            )
        else:
            quota_limit = three_mf_daily_limit_global if makerworld_source == "global" else three_mf_daily_limit_cn
            quota_result = dependencies.reserve_three_mf_download_slot(
                source=makerworld_source,
                url=fetch_url,
                limit=quota_limit,
                model_id=str(design_id or ""),
                model_url=fetch_url,
                instance_id=str(inst_id or ""),
            )
            if not quota_result.get("allowed", True):
                failure_info = {
                    "state": "download_limited",
                    "message": str(quota_result.get("message") or ""),
                }
                three_mf_fetch_paused = True
                normalized_three_mf_skip_state = "download_limited"
                three_mf_skip_message = str(failure_info.get("message") or three_mf_skip_message or "")
                skipped_due_limit += 1
            else:
                api_fetch_attempts += 1
                name3mf, url3mf, used_api_url, failure_info = dependencies.fetch_instance_3mf(
                    sess,
                    inst_id,
                    raw_cookie_header,
                    api_url,
                    api_host_hint=api_host_hint,
                    origin=origin,
                    captcha_result_header=three_mf_captcha_result_header,
                    browser_authorization=browser_three_mf_authorization,
                    browser_profile_id=browser_profile_id,
                    model_page_url=fetch_url,
                )
                if url3mf:
                    fetched_hint_hits += 1
                elif _should_pause_three_mf_fetch(failure_info):
                    three_mf_fetch_paused = True
                    normalized_three_mf_skip_state = "download_limited"
                    if str(failure_info.get("state") or "").strip() != "download_limited":
                        normalized_three_mf_skip_state = str(failure_info.get("state") or "").strip()
                    three_mf_skip_message = str((failure_info or {}).get("message") or three_mf_skip_message or "")
        failure_state = str((failure_info or {}).get("state") or "").strip()
        failure_message = str((failure_info or {}).get("message") or "").strip()
        verification_info = (failure_info or {}).get("verification") if isinstance((failure_info or {}).get("verification"), dict) else {}
        captcha_id = str(verification_info.get("captcha_id") or "").strip()
        profile_details = normalize_profile_details(inst, plates, existing_inst)
        inst_record = {
            "id": inst_id,
            "profileId": inst.get("profileId") or inst.get("profile_id") or inst.get("profileID") or existing_inst.get("profileId") or existing_inst.get("profile_id") or existing_inst.get("profileID"),
            "title": inst.get("title") or inst.get("name") or existing_inst.get("title") or existing_inst.get("name"),
            "titleTranslated": inst.get("titleTranslated") or existing_inst.get("titleTranslated") or "",
            "publishTime": inst.get("publishTime") or inst.get("publishedAt") or existing_inst.get("publishTime") or existing_inst.get("publishedAt") or "",
            "machine": inst.get("machine") or inst.get("machineName") or inst.get("printerModel") or inst.get("printer") or inst.get("device") or existing_inst.get("machine") or existing_inst.get("machineName") or existing_inst.get("printerModel") or existing_inst.get("printer") or existing_inst.get("device") or "",
            "time": inst.get("time") or inst.get("timeText") or inst.get("durationText") or existing_inst.get("time") or existing_inst.get("timeText") or existing_inst.get("durationText") or (format_duration(profile_details.get("printTimeSeconds")) if profile_details.get("printTimeSeconds") else ""),
            "timeText": inst.get("timeText") or existing_inst.get("timeText") or "",
            "durationText": inst.get("durationText") or existing_inst.get("durationText") or "",
            "printTimeSeconds": profile_details.get("printTimeSeconds") or inst.get("printTimeSeconds") or inst.get("duration") or existing_inst.get("printTimeSeconds") or existing_inst.get("duration") or 0,
            "rating": normalize_profile_rating(
                inst.get("rating")
                or inst.get("score")
                or inst.get("stars")
                or existing_inst.get("rating")
                or existing_inst.get("score")
                or existing_inst.get("stars")
            ),
            "downloadCount": inst.get("downloadCount") or existing_inst.get("downloadCount") or 0,
            "printCount": inst.get("printCount") or existing_inst.get("printCount") or 0,
            "prediction": inst.get("prediction"),
            "weight": inst.get("weight"),
            "plateCount": profile_details.get("plateCount") or inst.get("plateCount") or inst.get("plateNum") or existing_inst.get("plateCount") or existing_inst.get("plateNum") or 0,
            "nozzleDiameter": profile_details.get("nozzleDiameter"),
            "filamentWeight": profile_details.get("filamentWeight"),
            "materialCnt": inst.get("materialCnt"),
            "materialColorCnt": inst.get("materialColorCnt"),
            "needAms": profile_details.get("needAms"),
            "cover": inst.get("cover") or inst.get("coverUrl") or existing_inst.get("cover") or existing_inst.get("coverUrl") or "",
            "previewImage": inst.get("previewImage") or existing_inst.get("previewImage") or "",
            "thumbnail": inst.get("thumbnail") or existing_inst.get("thumbnail") or "",
            "thumbnailUrl": inst.get("thumbnailUrl") or existing_inst.get("thumbnailUrl") or "",
            "plates": plates,
            "pictures": pics,
            "instanceFilaments": inst.get("instanceFilaments") or existing_inst.get("instanceFilaments") or [],
            "filaments": profile_details.get("filaments") or [],
            "profileDetails": profile_details,
            "profileDetailVersion": PROFILE_DETAIL_SCHEMA_VERSION,
            "summary": inst.get("summary") or existing_inst.get("summary") or "",
            "summaryTranslated": inst.get("summaryTranslated") or existing_inst.get("summaryTranslated") or "",
            "name": name3mf,
            "downloadUrl": url3mf,
            "apiUrl": used_api_url or api_url,
            "downloadState": "" if url3mf else failure_state,
            "downloadMessage": "" if url3mf else failure_message,
            "fileName": str(existing_inst.get("fileName") or "").strip(),
        }
        if captcha_id and not url3mf:
            inst_record["captchaId"] = captcha_id
            inst_record["verification"] = {
                "captcha_id": captcha_id,
                "provider": str(verification_info.get("provider") or "geetest"),
            }
        # 在构建 meta 阶段就写入 fileName，保证后续语义清晰：
        # title=展示名，name=来源名，fileName=本地真实文件名。
        inst_list.append(inst_record)
        inst_record["fileName"] = choose_unique_instance_filename(
            inst_record,
            inst_list,
            planned_instances_dir,
            inst_record.get("name") or "",
            reserved_names=reserved_planned_instance_names,
            existing_files=existing_planned_instance_files,
        )
        reserved_planned_instance_names.add(inst_record["fileName"])
    log(
        logger,
        "实例处理完成:",
        f"payload_hint={payload_hint_hits}",
        f"existing_hint={existing_hint_hits}",
        f"api_fetch_attempts={api_fetch_attempts}",
        f"api_fetch={fetched_hint_hits}",
        f"skipped_due_limit={skipped_due_limit}",
        f"total={len(inst_list)}",
    )
    timings_ms["process_instances"] = _log_perf(
        "archive.process_instances",
        instance_stage_started_at,
        logger=logger,
        total=len(inst_list),
        payload_hint=payload_hint_hits,
        existing_hint=existing_hint_hits,
        api_fetch_attempts=api_fetch_attempts,
        api_fetch=fetched_hint_hits,
        skipped_due_limit=skipped_due_limit,
    )

    meta = build_meta(
        design,
        summary,
        design_images,
        cover_meta,
        inst_list,
        author,
        base_name,
        attachments=attachments,
        comments_bundle=comments_bundle,
    )
    existing_collect_date = existing_meta.get("collectDate")
    if existing_collect_date not in (None, "", 0, "0"):
        meta["collectDate"] = existing_collect_date
    meta_path = work_dir / "meta.json"
    meta_write_started_at = time.perf_counter()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    timings_ms["write_meta"] = _log_perf("archive.write_meta", meta_write_started_at, logger=logger)
    emit_progress(progress_callback, 78, "元数据已生成，准备落盘")
    log(logger, "已保存 meta:", meta_path)

    # 归档整理
    work_dir.mkdir(parents=True, exist_ok=True)
    rebuild_stats: dict[str, int] = {}
    if rebuild_archive:
        log_section("归档整理阶段")
        try:
            rebuild_started_at = time.perf_counter()
            rebuild_result = rebuild_once(meta_path, progress_callback=progress_callback, logger=logger)
            rebuild_stats = rebuild_result if isinstance(rebuild_result, dict) else {}
            timings_ms["rebuild_once"] = _log_perf("archive.rebuild_once", rebuild_started_at, logger=logger)
        except Exception as e:
            log(logger, "归档目录整理失败:", e)
    else:
        log(logger, "已跳过归档目录整理。")

    # 缺失 3MF 记录（仅记录，没有下载 3mf）。这里必须以整理后的磁盘状态为准：
    # 直链已拿到但 3MF 文件没落地时，仍然要进入缺失重试队列。
    final_meta = meta
    try:
        if meta_path.exists():
            with meta_path.open("r", encoding="utf-8") as f:
                loaded_meta = json.load(f)
            if isinstance(loaded_meta, dict):
                final_meta = loaded_meta
    except Exception as e:
        log(logger, "读取整理后 meta 失败，使用内存元数据计算缺失 3MF:", e)
    final_instances = final_meta.get("instances") if isinstance(final_meta.get("instances"), list) else inst_list
    missing_3mf = _missing_3mf_instances(final_instances, work_dir, require_local_file=bool(rebuild_archive))
    if final_meta is not meta:
        try:
            meta_path.write_text(json.dumps(final_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log(logger, "写回缺失 3MF 状态失败:", e)
    if missing_3mf and record_missing_3mf_log:
        _record_missing_3mf_summary(logs_dir, base_name, missing_3mf, logger=logger)

    work_dir = meta_path.parent
    emit_progress(progress_callback, 100, "归档完成")
    timings_ms["total"] = _log_perf(
        "archive.total",
        archive_started_at,
        logger=logger,
        model_id=design_id,
        instances=len(inst_list),
        missing_3mf=len(missing_3mf),
    )
    return {
        "base_name": base_name,
        "work_dir": str(work_dir.resolve()),
        "missing_3mf": missing_3mf,
        "action": action,
        "model_id": design_id,
        "instances": inst_list,
        "stats": {
            "timings_ms": timings_ms,
            "comments": comments_bundle.get("assetStats") if isinstance(comments_bundle.get("assetStats"), dict) else {},
            "instances": {
                "total": len(inst_list),
                "payload_hint": payload_hint_hits,
                "existing_hint": existing_hint_hits,
                "api_fetch_attempts": api_fetch_attempts,
                "api_fetch": fetched_hint_hits,
                "skipped_due_limit": skipped_due_limit,
                "missing_3mf": len(missing_3mf),
                "three_mf_downloaded": int(rebuild_stats.get("three_mf_downloaded") or 0),
                "three_mf_existing": int(rebuild_stats.get("three_mf_existing") or 0),
            },
        },
    }


def archive_model(
    url: str,
    cookie: str,
    download_dir: Path,
    logs_dir: Path,
    logger=None,
    existing_root: Optional[Path] = None,
    progress_callback=None,
    skip_three_mf_fetch: bool = False,
    three_mf_skip_message: str = "",
    download_assets: bool = True,
    download_comment_assets: Optional[bool] = None,
    collect_comments_data: bool = True,
    rebuild_archive: bool = True,
    record_missing_3mf_log: bool = True,
    three_mf_skip_state: str = "",
    three_mf_daily_limit_cn: int = 100,
    three_mf_daily_limit_global: int = 100,
    existing_model_dir: str = "",
    three_mf_captcha_result_header: str = "",
    browser_three_mf_authorization: bool = False,
    browser_profile_id: str = "",
    instance_ids: Optional[list[str]] = None,
):
    return _archive_model(
        url=url,
        cookie=cookie,
        download_dir=download_dir,
        logs_dir=logs_dir,
        logger=logger,
        existing_root=existing_root,
        progress_callback=progress_callback,
        skip_three_mf_fetch=skip_three_mf_fetch,
        three_mf_skip_message=three_mf_skip_message,
        download_assets=download_assets,
        download_comment_assets=download_comment_assets,
        collect_comments_data=collect_comments_data,
        rebuild_archive=rebuild_archive,
        record_missing_3mf_log=record_missing_3mf_log,
        three_mf_skip_state=three_mf_skip_state,
        three_mf_daily_limit_cn=three_mf_daily_limit_cn,
        three_mf_daily_limit_global=three_mf_daily_limit_global,
        existing_model_dir=existing_model_dir,
        three_mf_captcha_result_header=three_mf_captcha_result_header,
        browser_three_mf_authorization=browser_three_mf_authorization,
        browser_profile_id=browser_profile_id,
        instance_ids=instance_ids,
    )


def archive_model_with_dependencies(
    dependencies: ArchiveDependencies,
    *args,
    **kwargs,
):
    return _archive_model(*args, **kwargs, _dependencies=dependencies)


def _extract_instance_download(data: object) -> tuple[str, str]:
    payload = data
    if isinstance(data, dict):
        payload = data.get("data") or data.get("result") or data
    if not isinstance(payload, dict):
        return "", ""
    name = (
        payload.get("name")
        or payload.get("fileName")
        or payload.get("filename")
        or payload.get("file_name")
        or ""
    )
    url = (
        payload.get("url")
        or payload.get("downloadUrl")
        or payload.get("download_url")
        or payload.get("downloadURL")
        or ""
    )
    return name or "", url or ""


def _normalize_url_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        return f"https:{raw}"
    return raw


def _normalize_asset_url_value(value: object) -> str:
    raw = _normalize_url_value(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.split("#", 1)[0]
    if not parsed.scheme and not parsed.netloc:
        return raw.split("#", 1)[0]
    kept_params: list[tuple[str, str]] = []
    for key, param_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        compact = lowered.replace("_", "").replace("-", "")
        if lowered in VOLATILE_ASSET_QUERY_KEYS or compact in VOLATILE_ASSET_QUERY_KEYS:
            continue
        if any(lowered.startswith(prefix) for prefix in VOLATILE_ASSET_QUERY_PREFIXES):
            continue
        kept_params.append((key, param_value))
    query = urlencode(sorted(kept_params))
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, query, ""))


def _media_item_remote_matches(item: object, url: str, *, url_fields: tuple[str, ...]) -> bool:
    if not isinstance(item, dict):
        return False
    normalized_url = _normalize_asset_url_value(url)
    if not normalized_url:
        return False
    return any(_normalize_asset_url_value(item.get(field)) == normalized_url for field in url_fields)


def _media_item_local_path(
    base_dir: Path,
    item: object,
    *,
    rel_fields: tuple[str, ...] = ("relPath",),
    local_fields: tuple[str, ...] = ("fileName", "localName"),
) -> Optional[Path]:
    if not isinstance(item, dict):
        return None
    model_root = base_dir.parent if base_dir.name in {"images", "file"} else base_dir
    for field in rel_fields:
        rel_path = str(item.get(field) or "").strip().lstrip("/")
        if not rel_path:
            continue
        if rel_path.startswith(f"{SHARED_AVATAR_REL_DIR}/"):
            return model_root.parent / rel_path
        return model_root / rel_path
    for field in local_fields:
        local_name = str(item.get(field) or "").strip()
        if local_name:
            return base_dir / Path(local_name).name
    return None


def _media_item_local_exists(
    base_dir: Path,
    item: object,
    *,
    rel_fields: tuple[str, ...] = ("relPath",),
    local_fields: tuple[str, ...] = ("fileName", "localName"),
) -> bool:
    path = _media_item_local_path(base_dir, item, rel_fields=rel_fields, local_fields=local_fields)
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _existing_media_ref(item: object, *, rel_field: str = "relPath", local_field: str = "fileName") -> tuple[str, str]:
    if not isinstance(item, dict):
        return "", ""
    rel_path = str(item.get(rel_field) or "").strip()
    local_name = str(item.get(local_field) or "").strip()
    if not rel_path and local_name:
        rel_path = f"images/{local_name}"
    return rel_path, local_name


def _looks_like_instance_api_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    return "/f3mf" in raw or ("design-service/instance/" in raw and "download" in raw)


def _looks_like_3mf_file_url(url: object) -> bool:
    raw = _normalize_url_value(url).lower()
    if not raw:
        return False
    markers = (
        ".3mf",
        "filetype=3mf",
        "/f3mf/download",
        "content-disposition=",
        "application%2fvnd.ms-package.3dmanufacturing",
        "application/vnd.ms-package.3dmanufacturing",
    )
    return any(marker in raw for marker in markers)


def _instance_identity_keys(inst: object) -> List[str]:
    if not isinstance(inst, dict):
        return []
    keys: List[str] = []
    field_names = (
        "id",
        "profileId",
        "profile_id",
        "profileID",
        "instanceId",
        "instance_id",
        "instanceID",
    )
    for field in field_names:
        value = str(inst.get(field) or "").strip()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    for field in ("fileName", "name", "title"):
        value = sanitize_filename(str(inst.get(field) or "").strip()).lower()
        if value:
            token = f"{field}:{value}"
            if token not in keys:
                keys.append(token)
    return keys


def _build_existing_instance_index(instances: object) -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    if not isinstance(instances, list):
        return index
    for item in instances:
        if not isinstance(item, dict):
            continue
        for key in _instance_identity_keys(item):
            index.setdefault(key, item)
    return index


def _find_existing_instance(inst: object, instance_index: Dict[str, dict]) -> dict:
    if not isinstance(inst, dict) or not instance_index:
        return {}
    for key in _instance_identity_keys(inst):
        matched = instance_index.get(key)
        if matched:
            return matched
    return {}
