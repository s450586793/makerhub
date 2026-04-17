import asyncio
import base64
import json
import time
from datetime import datetime
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.store import JsonStore
from app.core.settings import APP_VERSION
from app.schemas.models import (
    ArchiveRequest,
    Missing3mfCancelRequest,
    CookiePair,
    Missing3mfRetryRequest,
    ModelDeleteRequest,
    ModelFlagUpdateRequest,
    NotificationConfig,
    ProxyConfig,
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
from app.services.local_organizer import LocalOrganizerService
from app.services.model_attachments import create_manual_attachment, delete_manual_attachment
from app.services.auth import AuthManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore


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
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/s450586793/makerhub/main/VERSION"
GITHUB_VERSION_API_URL = "https://api.github.com/repos/s450586793/makerhub/contents/VERSION?ref=main"
GITHUB_VERSION_CACHE_TTL_SECONDS = 300
github_version_cache = {
    "version": "",
    "checked_at": 0.0,
    "checked_at_iso": "",
    "error": "",
}


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
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/proxy")
async def save_proxy(payload: ProxyConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.proxy = payload
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/notifications")
async def save_notifications(payload: NotificationConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.notifications = payload
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/user")
async def save_user(payload: UserSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.username = payload.username.strip() or "admin"
    config.user.display_name = payload.display_name.strip() or "Admin"
    config.user.password_hint = payload.password_hint.strip()
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/theme")
async def save_theme(payload: ThemeSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.theme_preference = payload.theme_preference
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


@router.post("/config/organizer")
async def save_organizer(payload: OrganizeTask, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.organizer = payload
    return _with_version_status(_public_config_payload(store.save(config)), await _get_github_version_status())


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
    return result


@router.get("/models/flags")
async def get_model_flags():
    return task_state_store.load_model_flags()


@router.post("/models/flags/favorite")
async def update_model_favorite(payload: ModelFlagUpdateRequest, request: Request):
    _require_session_auth(request)
    flags = task_state_store.update_model_flag(payload.model_dir, "favorites", payload.value)
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


@router.post("/tasks/organize/clear")
async def clear_organize_tasks(request: Request):
    _require_session_auth(request)
    cleared = task_state_store.save_organize_tasks({"items": []})
    return {
        "success": True,
        "message": "已清空本地整理任务记录。",
        "organize_tasks": cleared,
    }


@router.get("/subscriptions")
async def get_subscriptions_data():
    return subscription_manager.list_payload()


@router.post("/subscriptions")
async def create_subscription(payload: SubscriptionCreateRequest, request: Request):
    _require_session_auth(request)
    try:
        return subscription_manager.create_subscription(
            url=payload.url,
            cron=payload.cron,
            name=payload.name,
            enabled=payload.enabled,
            initialize_from_source=payload.initialize_from_source,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/subscriptions/{subscription_id}")
async def update_subscription(subscription_id: str, payload: SubscriptionUpdateRequest, request: Request):
    _require_session_auth(request)
    try:
        return subscription_manager.update_subscription(
            subscription_id,
            url=payload.url,
            name=payload.name,
            cron=payload.cron,
            enabled=payload.enabled,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/subscriptions/{subscription_id}")
async def delete_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        return subscription_manager.delete_subscription(subscription_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/subscriptions/{subscription_id}/sync")
async def sync_subscription(subscription_id: str, request: Request):
    _require_session_auth(request)
    try:
        return subscription_manager.request_sync(subscription_id)
    except (ValueError, RuntimeError) as exc:
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
    return crawler.retry_missing_3mf(
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id=payload.instance_id,
    )


@router.post("/tasks/missing-3mf/retry-all")
async def retry_all_missing_3mf(request: Request):
    _require_session_auth(request)
    return crawler.retry_all_missing_3mf()


@router.post("/tasks/missing-3mf/cancel")
async def cancel_missing_3mf(payload: Missing3mfCancelRequest, request: Request):
    _require_session_auth(request)
    return crawler.cancel_missing_3mf(
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id=payload.instance_id,
    )


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    response = crawler.manager.submit(payload.url, preview_token=payload.preview_token)
    return response


@router.post("/archive/preview")
async def preview_archive_model(payload: ArchiveRequest):
    return crawler.preview_archive(payload.url)
