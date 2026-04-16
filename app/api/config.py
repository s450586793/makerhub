import asyncio
import json

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
from app.services.catalog import build_dashboard_payload, build_models_payload, build_tasks_payload, delete_archived_models, get_model_detail
from app.services.crawler import LegacyCrawlerBridge
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


def _task_identity(item: dict) -> str:
    return str(item.get("id") or item.get("url") or item.get("title") or "")


def _archive_event_snapshot() -> dict:
    queue = task_state_store.load_archive_queue()

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

    return {
        "active": active,
        "recent_failures": recent_failures,
        "running_count": int(queue.get("running_count") or 0),
        "queued_count": int(queue.get("queued_count") or 0),
        "failed_count": int(queue.get("failed_count") or 0),
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
    return {
        "app_version": APP_VERSION,
        "session": _session_payload(identity, config=config),
        "theme_preference": config.user.theme_preference if config else "",
    }


@router.get("/config")
async def get_config():
    return _public_config_payload(store.load())


@router.post("/config/cookies")
async def save_cookies(payload: list[CookiePair], request: Request):
    _require_session_auth(request)
    config = store.load()
    config.cookies = payload
    return _public_config_payload(store.save(config))


@router.post("/config/proxy")
async def save_proxy(payload: ProxyConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.proxy = payload
    return _public_config_payload(store.save(config))


@router.post("/config/notifications")
async def save_notifications(payload: NotificationConfig, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.notifications = payload
    return _public_config_payload(store.save(config))


@router.post("/config/user")
async def save_user(payload: UserSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.username = payload.username.strip() or "admin"
    config.user.display_name = payload.display_name.strip() or "Admin"
    config.user.password_hint = payload.password_hint.strip()
    return _public_config_payload(store.save(config))


@router.post("/config/theme")
async def save_theme(payload: ThemeSettingsUpdate, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.user.theme_preference = payload.theme_preference
    return _public_config_payload(store.save(config))


@router.post("/config/organizer")
async def save_organizer(payload: OrganizeTask, request: Request):
    _require_session_auth(request)
    config = store.load()
    config.organizer = payload
    return _public_config_payload(store.save(config))


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

            if completed:
                yield (
                    "event: archive_completed\n"
                    f"data: {json.dumps({'completed': completed, 'running_count': snapshot['running_count'], 'queued_count': snapshot['queued_count'], 'failed_count': snapshot['failed_count']}, ensure_ascii=False)}\n\n"
                )

            previous_active = current_active
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
