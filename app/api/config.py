from fastapi import APIRouter, HTTPException, Query, Request

from app.core.store import JsonStore
from app.core.settings import APP_VERSION
from app.schemas.models import (
    ArchiveRequest,
    CookiePair,
    Missing3mfRetryRequest,
    ModelDeleteRequest,
    NotificationConfig,
    ProxyConfig,
    ThemeSettingsUpdate,
    UserSettingsUpdate,
)
from app.schemas.models import OrganizeTask
from app.services.catalog import build_dashboard_payload, build_models_payload, build_tasks_payload, delete_archived_models, get_model_detail
from app.services.crawler import LegacyCrawlerBridge
from app.services.auth import AuthManager
from app.services.task_state import TaskStateStore


router = APIRouter(prefix="/api")
store = JsonStore()
crawler = LegacyCrawlerBridge()
auth_manager = AuthManager(store=store)
task_state_store = TaskStateStore()


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
        "missing_3mf": [item.model_dump() for item in config.missing_3mf],
        "organizer": config.organizer.model_dump(),
        "paths": config.paths.model_dump(),
    }


def _require_session_auth(request: Request) -> None:
    identity = getattr(request.state, "auth_identity", None) or {}
    if identity.get("kind") != "session":
        raise HTTPException(status_code=403, detail="此操作需要登录会话。")


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
):
    return build_models_payload(q=q, source=source, tag=tag, sort_key=sort)


@router.get("/models/{model_dir:path}")
async def get_model_detail_data(model_dir: str):
    detail = get_model_detail(model_dir)
    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在。")
    return detail


@router.post("/models/delete")
async def delete_models(payload: ModelDeleteRequest, request: Request):
    _require_session_auth(request)
    result = delete_archived_models(payload.model_dirs)

    for item in result.get("removed") or []:
        model_id = str(item.get("id") or "").strip()
        if model_id:
            task_state_store.remove_missing_3mf_for_model(model_id)

    result["success"] = result.get("removed_count", 0) > 0
    result["message"] = (
        f"已删除 {result.get('removed_count', 0)} 个模型。"
        if result.get("removed_count", 0)
        else "没有删除任何模型。"
    )
    return result


@router.get("/tasks")
async def get_tasks_data():
    config = store.load()
    fallback_items = [item.model_dump() for item in config.missing_3mf]
    return build_tasks_payload(missing_fallback=fallback_items)


@router.post("/tasks/missing-3mf/retry")
async def retry_missing_3mf(payload: Missing3mfRetryRequest, request: Request):
    _require_session_auth(request)
    return crawler.retry_missing_3mf(
        model_url=payload.model_url,
        model_id=payload.model_id,
        title=payload.title,
        instance_id="",
    )


@router.post("/tasks/missing-3mf/retry-all")
async def retry_all_missing_3mf(request: Request):
    _require_session_auth(request)
    return crawler.retry_all_missing_3mf()


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    return crawler.archive(payload.url)
