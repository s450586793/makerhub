import asyncio
import base64
import json
import time
from datetime import datetime
from urllib.request import Request as UrlRequest, urlopen

import requests
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.store import JsonStore
from app.core.settings import APP_VERSION
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
    SubscriptionUpdateRequest,
    ThemeSettingsUpdate,
    UserSettingsUpdate,
)
from app.schemas.models import OrganizeTask
from app.services.catalog import (
    build_dashboard_payload,
    build_models_payload,
    build_tasks_payload,
    delete_archived_models,
    get_model_comments_page,
    get_model_detail,
)
from app.services.crawler import LegacyCrawlerBridge
from app.services.business_logs import append_business_log, read_log_entries
from app.services.local_organizer import LocalOrganizerService
from app.services.model_attachments import create_manual_attachment, delete_manual_attachment
from app.services.remote_refresh import RemoteRefreshManager
from app.services.auth import AuthManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore
from app.services.archive_worker import BATCH_TASK_MODES, detect_archive_mode


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
)
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/s450586793/makerhub/main/VERSION"
GITHUB_VERSION_API_URL = "https://api.github.com/repos/s450586793/makerhub/contents/VERSION?ref=main"
GITHUB_VERSION_CACHE_TTL_SECONDS = 300
github_version_cache = {
    "version": "",
    "checked_at": 0.0,
    "checked_at_iso": "",
    "error": "",
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
COOKIE_TEST_ENDPOINTS = (
    ("消息计数", "/api/v1/user-service/my/message/count"),
    ("个人偏好", "/api/v1/design-user-service/my/preference"),
)


def _task_identity(item: dict) -> str:
    return str(item.get("id") or item.get("url") or item.get("title") or "")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_latest_github_version() -> str:
    request = UrlRequest(
        GITHUB_VERSION_URL,
        headers={
            "User-Agent": "makerhub-version-check",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=8) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        value = response.read().decode(charset, errors="ignore").strip()
        if value:
            return value

    api_request = UrlRequest(
        GITHUB_VERSION_API_URL,
        headers={
            "User-Agent": "makerhub-version-check",
            "Accept": "application/vnd.github+json",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(api_request, timeout=8) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = json.loads(response.read().decode(charset, errors="ignore"))
        content = str(payload.get("content") or "").strip()
        encoding = str(payload.get("encoding") or "").strip().lower()
        if not content:
            raise ValueError("GitHub API 未返回 VERSION 内容")
        if encoding == "base64":
            return base64.b64decode(content).decode("utf-8", errors="ignore").strip()
        return content.strip()


async def _get_github_version_status(force: bool = False) -> dict:
    now = time.time()
    checked_at = float(github_version_cache.get("checked_at") or 0)
    if not force and checked_at and now - checked_at < GITHUB_VERSION_CACHE_TTL_SECONDS:
        return {
            "github_latest_version": str(github_version_cache.get("version") or ""),
            "github_version_checked_at": str(github_version_cache.get("checked_at_iso") or ""),
            "github_version_error": str(github_version_cache.get("error") or ""),
            "github_update_available": bool(
                str(github_version_cache.get("version") or "").strip()
                and str(github_version_cache.get("version") or "").strip() != APP_VERSION
            ),
        }

    checked_at_iso = _now_iso()
    try:
        version = await asyncio.to_thread(_read_latest_github_version)
        if not version:
            raise ValueError("GitHub VERSION 为空")
        github_version_cache.update(
            {
                "version": version,
                "checked_at": now,
                "checked_at_iso": checked_at_iso,
                "error": "",
            }
        )
    except Exception as exc:
        github_version_cache.update(
            {
                "version": "",
                "checked_at": now,
                "checked_at_iso": checked_at_iso,
                "error": str(exc),
            }
        )

    return {
        "github_latest_version": str(github_version_cache.get("version") or ""),
        "github_version_checked_at": str(github_version_cache.get("checked_at_iso") or ""),
        "github_version_error": str(github_version_cache.get("error") or ""),
        "github_update_available": bool(
            str(github_version_cache.get("version") or "").strip()
            and str(github_version_cache.get("version") or "").strip() != APP_VERSION
        ),
    }


def _with_version_status(payload: dict, version_status: dict) -> dict:
    return {
        **payload,
        "github_latest_version": str(version_status.get("github_latest_version") or ""),
        "github_version_checked_at": str(version_status.get("github_version_checked_at") or ""),
        "github_version_error": str(version_status.get("github_version_error") or ""),
        "github_update_available": bool(version_status.get("github_update_available")),
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


def _cookie_base_url(platform: str) -> str:
    return "https://makerworld.com" if str(platform or "").strip() == "global" else "https://makerworld.com.cn"


def _run_cookie_test(payload: CookieTestRequest) -> dict:
    raw_cookie = str(payload.cookie or "").strip()
    if not raw_cookie:
        raise ValueError("请先填写 Cookie。")

    base_url = _cookie_base_url(payload.platform)
    proxies = _build_proxy_mapping(payload.proxy) if bool(payload.proxy.enabled) else {}
    session = _make_test_session()
    results: list[dict] = []
    try:
        for name, path in COOKIE_TEST_ENDPOINTS:
            url = f"{base_url}{path}"
            started = time.perf_counter()
            try:
                response = session.get(
                    url,
                    headers={
                        "Referer": f"{base_url}/",
                        "Origin": base_url,
                        "Cookie": raw_cookie,
                    },
                    proxies=proxies or None,
                    timeout=(6, 12),
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                content_type = str(response.headers.get("content-type") or "").lower()
                body_preview = (response.text or "")[:160]
                looks_like_html = "<html" in body_preview.lower() or "<!doctype html" in body_preview.lower()
                ok = response.status_code < 400 and not looks_like_html
                result = {
                    "target": name,
                    "url": url,
                    "ok": ok,
                    "status_code": int(response.status_code),
                    "elapsed_ms": elapsed_ms,
                    "content_type": content_type[:80],
                }
                if not ok:
                    result["error"] = (
                        "返回了 HTML 页面，通常表示 Cookie 失效、风控校验未通过，或代理未生效。"
                        if looks_like_html
                        else f"接口返回状态码 {response.status_code}。"
                    )
                results.append(result)
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
    platform_label = "国际" if payload.platform == "global" else "国内"
    if success_count == len(results):
        message = f"{platform_label} Cookie 测试成功，认证接口可正常访问。"
    elif success_count > 0:
        message = f"{platform_label} Cookie 部分成功，{success_count}/{len(results)} 个接口可访问。"
    else:
        message = f"{platform_label} Cookie 测试失败，认证接口未返回有效结果。"
    return {
        "ok": ok,
        "message": message,
        "platform": payload.platform,
        "results": results,
        "success_count": success_count,
        "target_count": len(results),
        "used_proxy": bool(payload.proxy.enabled and proxies),
    }


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
        "missing_3mf": [item.model_dump() for item in config.missing_3mf],
        "organizer": config.organizer.model_dump(),
        "remote_refresh": config.remote_refresh.model_dump(),
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
    config = store.load() if identity else None
    payload = {
        "app_version": APP_VERSION,
        "session": _session_payload(identity, config=config),
        "theme_preference": config.user.theme_preference if config else "",
    }
    return _with_version_status(payload, await _get_github_version_status())


@router.get("/config")
async def get_config():
    return _with_version_status(_public_config_payload(store.load()), await _get_github_version_status())


@router.post("/config/cookies")
async def save_cookies(payload: list[CookiePair], request: Request):
    _require_session_auth(request)
    config = store.load()
    config.cookies = payload
    append_business_log(
        "settings",
        "cookies_saved",
        "Cookie 配置已保存。",
        count=len(payload),
        platforms=[item.platform for item in payload],
    )
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


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
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/proxy/test")
async def test_proxy(payload: ProxyConfig, request: Request):
    _require_session_auth(request)
    try:
        result = await asyncio.to_thread(_run_proxy_test, payload)
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
        result = await asyncio.to_thread(_run_cookie_test, payload)
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
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/user")
async def save_user(payload: UserSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.username = payload.username.strip() or "admin"
    config.user.display_name = payload.display_name.strip() or "Admin"
    config.user.password_hint = payload.password_hint.strip()
    append_business_log("settings", "user_saved", "用户信息已保存。", username=config.user.username)
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/theme")
async def save_theme(payload: ThemeSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.theme_preference = payload.theme_preference
    append_business_log("settings", "theme_saved", "主题设置已保存。", theme_preference=payload.theme_preference)
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


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
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


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
        "远端刷新设置已保存。",
        enabled=payload.enabled,
        cron=payload.cron,
        batch_size=payload.batch_size,
        next_run_at=state.get("next_run_at"),
    )
    return _with_version_status(_public_config_payload(config), await _get_github_version_status())


@router.get("/dashboard")
async def get_dashboard_data():
    config = store.load()
    return build_dashboard_payload(config)


@router.get("/models")
async def get_models_data(
    q: str = Query("", description="搜索标题、作者、标签"),
    source: str = Query("all", description="全部 / 国内 / 国际 / 本地"),
    tag: str = Query("", description="按标签过滤"),
    sort: str = Query("collectDate", description="collectDate / downloads / likes / prints"),
    page: int = Query(1, ge=1, description="分页页码"),
    page_size: int = Query(8, ge=1, le=120, description="每页数量"),
):
    return build_models_payload(
        q=q,
        source=source,
        tag=tag,
        sort_key=sort,
        page=page,
        page_size=page_size,
    )


@router.get("/models/{model_dir:path}/comments")
async def get_model_detail_comments(
    model_dir: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    payload = get_model_comments_page(model_dir, offset=offset, limit=limit)
    if payload is None:
        raise HTTPException(status_code=404, detail="模型不存在。")
    return payload


@router.get("/models/{model_dir:path}")
async def get_model_detail_data(model_dir: str):
    detail = get_model_detail(model_dir)
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
        attachment = create_manual_attachment(model_dir, file, name=name, category=category)
        detail = get_model_detail(model_dir)
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
        removed = delete_manual_attachment(model_dir, attachment_id)
        detail = get_model_detail(model_dir)
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
    result = delete_archived_models(payload.model_dirs)

    for item in result.get("removed") or []:
        model_id = str(item.get("id") or "").strip()
        if model_id:
            task_state_store.remove_missing_3mf_for_model(model_id)

    task_state_store.remove_model_flags(payload.model_dirs)

    result["success"] = result.get("removed_count", 0) > 0
    sidecar_count = int(result.get("sidecar_removed_count") or 0)
    result["message"] = (
        f"已删除 {result.get('removed_count', 0)} 个模型，清理 {sidecar_count} 个遗留资源。"
        if result.get("removed_count", 0)
        else "没有删除任何模型。"
    )
    append_business_log(
        "model",
        "models_deleted",
        result["message"],
        requested_count=len(payload.model_dirs),
        removed_count=result.get("removed_count", 0),
        sidecar_removed_count=sidecar_count,
        model_dirs=payload.model_dirs,
    )
    return result


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
    config = store.load()
    fallback_items = [item.model_dump() for item in config.missing_3mf]
    return build_tasks_payload(missing_fallback=fallback_items)


@router.get("/remote-refresh")
async def get_remote_refresh_data():
    config = store.load()
    return {
        "config": config.remote_refresh.model_dump(),
        "state": remote_refresh_manager.state_payload(),
    }


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
    return subscription_manager.list_payload()


@router.get("/logs")
async def get_logs_data(
    file: str = Query("business.log", description="日志文件名"),
    limit: int = Query(300, ge=1, le=2000, description="最多返回行数"),
    q: str = Query("", description="日志内容搜索"),
):
    return read_log_entries(file_name=file, limit=limit, query=q)


@router.post("/subscriptions")
async def create_subscription(payload: SubscriptionCreateRequest, request: Request):
    _require_session_auth(request)
    try:
        result = subscription_manager.create_subscription(
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
        result = subscription_manager.update_subscription(
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
        result = subscription_manager.delete_subscription(subscription_id)
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
        result = subscription_manager.request_sync(subscription_id)
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
        snapshot = _archive_event_snapshot()
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

            snapshot = _archive_event_snapshot()
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
    result = crawler.retry_missing_3mf(
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
    result = crawler.retry_all_missing_3mf()
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
    result = crawler.cancel_missing_3mf(
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


@router.post("/archive/preview")
async def preview_archive_model(payload: ArchiveRequest):
    response = crawler.preview_archive(payload.url)
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
