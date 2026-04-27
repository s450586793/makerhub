import asyncio
import base64
import json
import re
import time
from multiprocessing import Process

import requests
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.store import JsonStore
from app.core.settings import APP_VERSION
from app.core.timezone import now_iso as china_now_iso
from app.schemas.models import (
    ArchiveRequest,
    Missing3mfCancelRequest,
    CookiePair,
    CookieTestRequest,
    Missing3mfRetryRequest,
    ModelDeleteRequest,
    ModelFlagUpdateRequest,
    NotificationConfig,
    ProxyConfig,
    RemoteRefreshConfig,
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
    queue_profile_backfill,
    read_profile_backfill_status,
    write_profile_backfill_status,
)
from app.services.subscriptions import SubscriptionManager
from app.services.source_library import (
    build_source_group_models_payload,
    build_source_library_payload,
    build_state_group_models_payload,
)
from app.services.source_health import probe_cookie_auth_status
from app.services.task_state import TaskStateStore
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
profile_backfill_task: asyncio.Task | None = None
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
        "remote_refresh_state": task_state_store.load_remote_refresh_state(),
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


@router.get("/tasks")
async def get_tasks_data():
    def _tasks_payload() -> dict:
        config = store.load()
        fallback_items = [item.model_dump() for item in config.missing_3mf]
        return build_tasks_payload(missing_fallback=fallback_items)

    return await run_ui_io(_tasks_payload)


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
    global profile_backfill_task
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
        profile_backfill_task = asyncio.create_task(_run_profile_backfill_background())

    state.update(
        {
            "accepted": True,
            "message": "现有库信息补全扫描已在后台启动，请稍后刷新状态；缺失模型会继续加入归档队列。",
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


async def _run_profile_backfill_background() -> None:
    try:
        await run_task_api(queue_profile_backfill, crawler.manager)
    except Exception:
        # queue_profile_backfill writes last_error itself; keep the background
        # task from surfacing an unhandled asyncio exception.
        pass


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
