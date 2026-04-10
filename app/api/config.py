from fastapi import APIRouter, Query

from app.core.store import JsonStore
from app.schemas.models import AppConfig, ArchiveRequest, CookiePair, NotificationConfig, ProxyConfig, UserProfile
from app.schemas.models import OrganizeTask
from app.services.catalog import build_dashboard_payload, build_models_payload, build_tasks_payload
from app.services.crawler import LegacyCrawlerBridge


router = APIRouter(prefix="/api")
store = JsonStore()
crawler = LegacyCrawlerBridge()


@router.get("/config", response_model=AppConfig)
async def get_config():
    return store.load()


@router.post("/config/cookies", response_model=AppConfig)
async def save_cookies(payload: list[CookiePair]):
    config = store.load()
    config.cookies = payload
    return store.save(config)


@router.post("/config/proxy", response_model=AppConfig)
async def save_proxy(payload: ProxyConfig):
    config = store.load()
    config.proxy = payload
    return store.save(config)


@router.post("/config/notifications", response_model=AppConfig)
async def save_notifications(payload: NotificationConfig):
    config = store.load()
    config.notifications = payload
    return store.save(config)


@router.post("/config/user", response_model=AppConfig)
async def save_user(payload: UserProfile):
    config = store.load()
    config.user = payload
    return store.save(config)


@router.post("/config/organizer", response_model=AppConfig)
async def save_organizer(payload: OrganizeTask):
    config = store.load()
    config.organizer = payload
    return store.save(config)


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


@router.get("/tasks")
async def get_tasks_data():
    config = store.load()
    fallback_items = [item.model_dump() for item in config.missing_3mf]
    return build_tasks_payload(missing_fallback=fallback_items)


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    return crawler.archive(payload.url)
