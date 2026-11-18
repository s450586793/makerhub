from __future__ import annotations

from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso
from app.schemas.models import CookiePair
from app.services.cloakbrowser_session import CloakBrowserSessionResult
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header


def confirm_browser_session(
    store: JsonStore,
    platform: str,
    expected: CookiePair,
    result: CloakBrowserSessionResult,
) -> bool:
    # 仅接收严格站点登录检查后的新鲜结果，不用普通页面/API 请求作为登录证据。
    profile_id = str(expected.browser_profile_id or "").strip()
    expected_cookie = sanitize_cookie_header(expected.cookie)
    expected_token = extract_auth_token(expected_cookie)
    if (platform not in {"cn", "global"} or expected.platform != platform
            or expected.browser_status != "action_required" or not profile_id or not expected_token
            or result.profile_id != profile_id or extract_auth_token(result.cookie) != expected_token):
        return False
    expected_message = expected.browser_message
    expected_synced_at = expected.browser_synced_at
    confirmed = False

    def confirm(config):
        nonlocal confirmed
        account = next((item for item in config.cookies if item.platform == platform), None)
        if (account is None or account.browser_profile_id != profile_id
                or sanitize_cookie_header(account.cookie) != expected_cookie
                or account.browser_status != "action_required"
                or account.browser_message != expected_message or account.browser_synced_at != expected_synced_at):
            return config
        account.browser_status = "synced"
        account.browser_message = "已确认关联指纹浏览器的当前登录态。"
        account.browser_synced_at = china_now_iso()
        confirmed = True
        return config

    store.update(confirm)
    return confirmed
