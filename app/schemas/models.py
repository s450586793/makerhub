import os
from typing import List, Literal

from pydantic import BaseModel, Field

from app.core.security import default_admin_password_hash
from app.core.settings import ARCHIVE_DIR, CONFIG_DIR, LOCAL_DIR, LOGS_DIR, STATE_DIR


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(min(value, maximum), minimum)


class CookiePair(BaseModel):
    platform: str
    cookie: str = ""


class ProxyConfig(BaseModel):
    enabled: bool = False
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""


class CookieTestRequest(BaseModel):
    platform: Literal["cn", "global"] = "cn"
    cookie: str = ""
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


class NotificationConfig(BaseModel):
    enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    webhook_url: str = ""


class SharingConfig(BaseModel):
    public_base_url: str = ""
    default_expires_days: int = Field(default=7, ge=1, le=90)
    include_images: bool = True
    include_model_files: bool = True
    model_file_types: List[str] = Field(default_factory=lambda: ["3mf", "stl", "step", "obj"])
    include_attachments: bool = True
    attachment_file_types: List[str] = Field(default_factory=lambda: ["pdf", "excel"])
    include_comments: bool = True


class UserProfile(BaseModel):
    username: str = "admin"
    display_name: str = "Admin"
    password_hint: str = "请改成强密码"
    theme_preference: Literal["light", "dark", "auto"] = "auto"
    password_hash: str = Field(default_factory=default_admin_password_hash)
    password_updated_at: str = ""


class UserSettingsUpdate(BaseModel):
    username: str = "admin"
    display_name: str = "Admin"
    password_hint: str = "请改成强密码"


class ThemeSettingsUpdate(BaseModel):
    theme_preference: Literal["light", "dark", "auto"] = "auto"


class SystemUpdateRequest(BaseModel):
    target_version: str = ""
    force: bool = False


class ApiTokenRecord(BaseModel):
    id: str
    name: str
    token_prefix: str
    token_hash: str
    created_at: str
    last_used_at: str = ""
    disabled: bool = False


class ApiTokenView(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: str
    last_used_at: str = ""
    disabled: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ApiTokenCreateRequest(BaseModel):
    name: str = ""


class ArchiveRequest(BaseModel):
    url: str
    preview_token: str = ""
    create_subscription: bool = False
    subscription_name: str = ""


class ModelDeleteRequest(BaseModel):
    model_dirs: List[str] = Field(default_factory=list)


class ModelFlagUpdateRequest(BaseModel):
    model_dir: str = ""
    value: bool = False


class ShareOptions(BaseModel):
    expires_days: int = Field(default=7, ge=1, le=90)
    include_images: bool = True
    include_model_files: bool = True
    model_file_types: List[str] = Field(default_factory=lambda: ["3mf", "stl", "step", "obj"])
    include_attachments: bool = True
    attachment_file_types: List[str] = Field(default_factory=lambda: ["pdf", "excel"])
    include_comments: bool = True


class ShareCreateRequest(BaseModel):
    model_dirs: List[str] = Field(default_factory=list)
    options: ShareOptions = Field(default_factory=ShareOptions)


class ShareReceiveRequest(BaseModel):
    share_code: str = ""


class ShareDeleteExpiredRequest(BaseModel):
    include_expired: bool = False


class LocalModelMergeRequest(BaseModel):
    target_model_dir: str = ""
    source_model_dirs: List[str] = Field(default_factory=list)
    title: str = ""
    cover_from_model_dir: str = ""


class LocalModelDescriptionUpdateRequest(BaseModel):
    description: str = ""


class LocalModelFileDeleteRequest(BaseModel):
    instance_key: str = ""


class LocalModelImageDeleteRequest(BaseModel):
    rel_path: str = ""


class Missing3mfItem(BaseModel):
    model_id: str
    title: str
    status: str = "missing"
    model_url: str = ""
    instance_id: str = ""
    message: str = ""
    updated_at: str = ""


class Missing3mfRetryRequest(BaseModel):
    model_id: str = ""
    model_url: str = ""
    title: str = ""
    instance_id: str = ""


class Missing3mfCancelRequest(BaseModel):
    model_id: str = ""
    model_url: str = ""
    title: str = ""
    instance_id: str = ""


class OrganizeTask(BaseModel):
    source_dir: str = str(LOCAL_DIR)
    target_dir: str = str(ARCHIVE_DIR)
    move_files: bool = True


class RemoteRefreshConfig(BaseModel):
    enabled: bool = True
    cron: str = "0 0 * * *"


class ThreeMfDownloadLimitsConfig(BaseModel):
    cn_daily_limit: int = Field(default=100, ge=0, le=100000)
    global_daily_limit: int = Field(default=100, ge=0, le=100000)


class AdvancedRuntimeConfig(BaseModel):
    remote_refresh_model_workers: int = Field(
        default_factory=lambda: _env_int("MAKERHUB_REMOTE_REFRESH_MODEL_WORKERS", 2, 1, 4),
        ge=1,
        le=4,
    )
    makerworld_request_limit: int = Field(
        default_factory=lambda: _env_int("MAKERHUB_LIMIT_MAKERWORLD_REQUESTS", 2, 1, 8),
        ge=1,
        le=8,
    )
    comment_asset_download_limit: int = Field(
        default_factory=lambda: _env_int("MAKERHUB_LIMIT_COMMENT_ASSETS", 4, 1, 16),
        ge=1,
        le=16,
    )
    three_mf_download_limit: int = Field(
        default_factory=lambda: _env_int("MAKERHUB_LIMIT_THREE_MF_DOWNLOADS", 1, 1, 4),
        ge=1,
        le=4,
    )
    disk_io_limit: int = Field(
        default_factory=lambda: _env_int("MAKERHUB_LIMIT_DISK_IO", 1, 1, 4),
        ge=1,
        le=4,
    )


class SubscriptionSettingsConfig(BaseModel):
    default_cron: str = "0 */6 * * *"
    default_enabled: bool = True
    default_initialize_from_source: bool = True
    card_sort: Literal["recent", "models", "followers"] = "recent"
    hide_disabled_from_cards: bool = False


class SubscriptionRecord(BaseModel):
    id: str
    name: str = ""
    url: str
    mode: Literal["author_upload", "collection_models"]
    cron: str = "0 */6 * * *"
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class SubscriptionCreateRequest(BaseModel):
    name: str = ""
    url: str
    cron: str = "0 */6 * * *"
    enabled: bool = True
    initialize_from_source: bool = True


class SubscriptionUpdateRequest(BaseModel):
    url: str
    name: str = ""
    cron: str = "0 */6 * * *"
    enabled: bool = True


class SubscriptionSourceItem(BaseModel):
    model_id: str = ""
    url: str = ""
    task_key: str = ""


class SubscriptionStateItem(BaseModel):
    id: str
    status: Literal["idle", "running", "success", "error"] = "idle"
    running: bool = False
    next_run_at: str = ""
    manual_requested_at: str = ""
    last_run_at: str = ""
    last_success_at: str = ""
    last_error_at: str = ""
    last_message: str = ""
    last_discovered_count: int = 0
    last_new_count: int = 0
    last_enqueued_count: int = 0
    last_deleted_count: int = 0
    current_items: List[SubscriptionSourceItem] = Field(default_factory=list)
    tracked_items: List[SubscriptionSourceItem] = Field(default_factory=list)


class SubscriptionSettingsUpdate(BaseModel):
    default_cron: str = "0 */6 * * *"
    default_enabled: bool = True
    default_initialize_from_source: bool = True
    card_sort: Literal["recent", "models", "followers"] = "recent"
    hide_disabled_from_cards: bool = False


class RuntimePaths(BaseModel):
    config_dir: str = str(CONFIG_DIR)
    logs_dir: str = str(LOGS_DIR)
    state_dir: str = str(STATE_DIR)
    archive_dir: str = str(ARCHIVE_DIR)
    local_dir: str = str(LOCAL_DIR)


class AppConfig(BaseModel):
    cookies: List[CookiePair] = Field(default_factory=lambda: [
        CookiePair(platform="cn"),
        CookiePair(platform="global"),
    ])
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    sharing: SharingConfig = Field(default_factory=SharingConfig)
    user: UserProfile = Field(default_factory=UserProfile)
    api_tokens: List[ApiTokenRecord] = Field(default_factory=list)
    subscriptions: List[SubscriptionRecord] = Field(default_factory=list)
    subscription_settings: SubscriptionSettingsConfig = Field(default_factory=SubscriptionSettingsConfig)
    missing_3mf: List[Missing3mfItem] = Field(default_factory=list)
    organizer: OrganizeTask = Field(default_factory=OrganizeTask)
    remote_refresh: RemoteRefreshConfig = Field(default_factory=RemoteRefreshConfig)
    three_mf_limits: ThreeMfDownloadLimitsConfig = Field(default_factory=ThreeMfDownloadLimitsConfig)
    advanced: AdvancedRuntimeConfig = Field(default_factory=AdvancedRuntimeConfig)
    paths: RuntimePaths = Field(default_factory=RuntimePaths)
