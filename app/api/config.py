from fastapi import APIRouter

from app.core.store import JsonStore
from app.schemas.models import AppConfig, ArchiveRequest, CookiePair, NotificationConfig, ProxyConfig, UserProfile
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


@router.post("/archive")
async def archive_model(payload: ArchiveRequest):
    return crawler.archive(payload.url)

