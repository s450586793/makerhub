from typing import List

from pydantic import BaseModel, Field


class CookiePair(BaseModel):
    platform: str
    cookie: str = ""


class ProxyConfig(BaseModel):
    enabled: bool = False
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""


class NotificationConfig(BaseModel):
    enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""


class UserProfile(BaseModel):
    username: str = "admin"
    display_name: str = "Admin"
    password_hint: str = "请改成强密码"


class ArchiveRequest(BaseModel):
    url: str


class Missing3mfItem(BaseModel):
    model_id: str
    title: str
    status: str = "missing"


class OrganizeTask(BaseModel):
    source_dir: str = ""
    target_dir: str = ""
    move_files: bool = True


class AppConfig(BaseModel):
    cookies: List[CookiePair] = Field(default_factory=lambda: [
        CookiePair(platform="cn"),
        CookiePair(platform="global"),
    ])
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    user: UserProfile = Field(default_factory=UserProfile)
    missing_3mf: List[Missing3mfItem] = Field(default_factory=list)
    organizer: OrganizeTask = Field(default_factory=OrganizeTask)

