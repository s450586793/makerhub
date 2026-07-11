from __future__ import annotations

from dataclasses import dataclass
import json
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from app.core.timezone import now_iso
from app.schemas.models import ProxyConfig
from app.services.batch_discovery import discover_cookie_account_profile
from app.services.cookie_utils import sanitize_cookie_header
from app.services.proxy_policy import proxy_mapping
from app.services.source_health import probe_cookie_auth_status


MW_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}
PLATFORM_LABELS = {
    "cn": "国区",
    "global": "国际",
}
PLATFORM_ORIGINS = {
    "cn": "https://makerworld.com.cn",
    "global": "https://makerworld.com",
}
SMS_CODE_ENDPOINTS = {
    "cn": (
        "https://api.bambulab.cn/v1/user-service/user/sendsmscode",
        "https://makerworld.com.cn/api/v1/user-service/user/sendsmscode",
    ),
    "global": (
        "https://api.bambulab.com/v1/user-service/user/sendemail/code",
        "https://makerworld.com/api/v1/user-service/user/sendemail/code",
    ),
}
CODE_LOGIN_ENDPOINTS = {
    "cn": (
        "https://api.bambulab.cn/v1/user-service/user/signuporlogin",
    ),
    "global": (
        "https://api.bambulab.com/v1/user-service/user/signuporlogin",
    ),
}
TICKET_ENDPOINTS = {
    "cn": "https://api.bambulab.cn/v1/user-service/user/ticket",
    "global": "https://api.bambulab.com/v1/user-service/user/ticket",
}
MAKERWORLD_TICKET_ENDPOINTS = {
    "cn": "https://makerworld.com.cn/api/sign-in/ticket",
    "global": "https://makerworld.com/api/sign-in/ticket",
}
CONSENT_FORMS = {
    "cn": {
        "tou": {"formId": "TOU-CN", "op": "Opt-in", "key": "tou"},
        "privacy": {"formId": "PrivacyPolicy-CN", "op": "Opt-in", "key": "privacy"},
    },
    "global": {
        "tou": {"formId": "TOU", "op": "Opt-in", "key": "tou"},
        "privacy": {"formId": "PrivacyPolicy", "op": "Opt-in", "key": "privacy"},
    },
}
LOGIN_CONTEXT_TTL_SECONDS = 10 * 60
_LOGIN_CONTEXT_LOCK = threading.Lock()
_LOGIN_CONTEXTS: dict[tuple[str, str], dict[str, Any]] = {}


@dataclass
class OnlineAccountLoginResult:
    platform: str
    username: str
    cookie: str
    display_name: str = ""
    account_id: str = ""
    handle: str = ""
    avatar_url: str = ""
    status: str = "ok"
    message: str = ""
    login_url: str = ""
    auth_payload: dict[str, Any] | None = None
    cookie_items: list[dict[str, str]] | None = None


class OnlineAccountLoginError(RuntimeError):
    pass


class OnlineAccountSmsCodeError(RuntimeError):
    pass


def _normalize_platform(platform: str) -> str:
    return "global" if str(platform or "").strip().lower() == "global" else "cn"


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text[:240]


def _response_payload(response: requests.Response) -> Any:
    try:
        return response.json() if response.text else {}
    except ValueError:
        return {}


def _response_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg", "detail", "error", "error_msg"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    body = payload.get("body")
    if isinstance(body, dict):
        return _response_message(body)
    data = payload.get("data")
    if isinstance(data, dict):
        return _response_message(data)
    return ""


def _looks_like_html_response(response: requests.Response) -> bool:
    content_type = str(response.headers.get("Content-Type") or "").lower()
    text = (response.text or "").lstrip().lower()
    return "text/html" in content_type or text.startswith("<!doctype") or text.startswith("<html")


def _html_response_error(response: requests.Response) -> str:
    text = (response.text or "").lower()
    verification_markers = (
        "cf-browser-verification",
        "cf-chl",
        "cf_clearance",
        "captcha",
        "cloudflare",
        "security check",
        "verify you are human",
        "verification required",
        "challenge-platform",
    )
    if any(marker in text for marker in verification_markers):
        return "返回网页验证页。"
    return "返回网页登录页或非 JSON 页面。"


def _is_success_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    message = _response_message(payload).lower()
    if "success" in payload:
        return bool(payload.get("success"))
    if "ok" in payload:
        return bool(payload.get("ok"))
    code = payload.get("code")
    if code is not None:
        return str(code) in {"0", "200", "20000"}
    return not message or message in {"ok", "success", "succeed", "successful"}


def _is_authoritative_user_service_endpoint(url: str) -> bool:
    return "api.bambulab." in str(url or "") and "/v1/user-service/user/" in str(url or "")


def _session_from_platform(platform: str) -> requests.Session:
    clean_platform = _normalize_platform(platform)
    session = requests.Session()
    session.trust_env = False
    session.headers.update(MW_BROWSER_HEADERS)
    origin = PLATFORM_ORIGINS[clean_platform]
    login_origin = "https://bambulab.cn" if clean_platform == "cn" else "https://bambulab.com"
    session.headers.update(
        {
            "Origin": login_origin,
            "Referer": f"{login_origin}/zh-cn/sign-in?ticket=1&to={quote(f'{origin}/api/sign-in/ticket?to={origin}/zh', safe='')}",
        }
    )
    return session


def _post_json(
    session: requests.Session,
    url: str,
    payload: dict[str, Any],
    *,
    platform: str,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
    allow_redirects: bool = True,
) -> requests.Response:
    return session.post(
        url,
        json=payload,
        proxies=proxy_mapping(proxy_config, url, platform=platform, allow_domestic_proxy=True) or None,
        timeout=(8, 20),
        allow_redirects=allow_redirects,
    )


def _code_login_payload(url: str, platform: str, username: str, code: str) -> dict[str, Any]:
    del url
    return {
        "account": username,
        "code": code,
        "consentBody": _consent_body("register_or_login", platform),
    }


def _consent_body(scene: str, platform: str) -> str:
    forms = CONSENT_FORMS[_normalize_platform(platform)]
    return json.dumps(
        {
            "version": 1,
            "scene": scene,
            "formList": [forms["tou"], forms["privacy"]],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = [payload]
    for key in ("data", "result", "profile", "user", "account", "body"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for node in candidates:
        for key in ("token", "accessToken", "access_token", "idToken", "id_token"):
            value = str(node.get(key) or "").strip()
            if value:
                return value
    return ""


def _extract_refresh_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates: list[Any] = [payload]
    for key in ("data", "result", "profile", "user", "account", "body"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for node in candidates:
        for key in ("refreshToken", "refresh_token"):
            value = str(node.get(key) or "").strip()
            if value:
                return value
    return ""


def _extract_profile(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    candidates: list[dict[str, Any]] = [payload]
    for key in ("data", "result", "profile", "user", "account", "body"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    best: dict[str, str] = {}
    best_score = -1
    for node in candidates:
        uid = str(node.get("uid") or node.get("userId") or node.get("id") or node.get("accountId") or "").strip()
        handle = str(node.get("handle") or node.get("userHandle") or node.get("username") or "").strip().lstrip("@")
        name = str(node.get("name") or node.get("nickname") or node.get("displayName") or "").strip()
        avatar = str(node.get("avatar") or node.get("avatarUrl") or node.get("avatar_url") or "").strip()
        score = sum((4 if uid else 0, 4 if handle else 0, 2 if name else 0, 1 if avatar else 0))
        if score > best_score:
            best_score = score
            best = {
                "uid": uid,
                "handle": handle,
                "name": name,
                "avatar_url": avatar,
            }
    return best


def _merge_cookie_header(session: requests.Session, token: str = "", refresh_token: str = "") -> str:
    parts: list[str] = []
    seen_names: set[str] = set()
    for cookie in session.cookies:
        name = str(cookie.name or "").strip()
        value = str(cookie.value or "").strip()
        if name and value:
            seen_names.add(name)
            parts.append(f"{name}={value}")
    if token and "token" not in seen_names:
        parts.append(f"token={token}")
    if refresh_token and "refreshToken" not in seen_names:
        parts.append(f"refreshToken={refresh_token}")
    return sanitize_cookie_header("; ".join(parts))


def _cookie_with_response_tokens(session: requests.Session, payload: Any) -> str:
    return _merge_cookie_header(session, _extract_token(payload), _extract_refresh_token(payload))


def _login_account_label(platform: str) -> str:
    return "邮箱" if _normalize_platform(platform) == "global" else "手机号"


def _clean_login_account(platform: str, username: str) -> str:
    clean_platform = _normalize_platform(platform)
    clean_username = str(username or "").strip()
    if not clean_username:
        raise OnlineAccountLoginError(f"请填写{_login_account_label(clean_platform)}。")
    if clean_platform == "cn" and not re.fullmatch(r"1\d{10}", clean_username):
        raise OnlineAccountLoginError("请输入 11 位中国大陆手机号。")
    if clean_platform == "global" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean_username):
        raise OnlineAccountLoginError("请输入有效邮箱地址。")
    return clean_username


def _session_cookie_items(session: requests.Session) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        cookies = list(session.cookies)
    except Exception:
        cookies = []
    for cookie in cookies:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        if not name or value == "":
            continue
        items.append(
            {
                "name": name,
                "value": value,
                "domain": str(getattr(cookie, "domain", "") or "").strip(),
                "path": str(getattr(cookie, "path", "") or "/").strip() or "/",
            }
        )
    return items


def _remember_login_context(platform: str, account: str, session: requests.Session) -> None:
    cookie_items = _session_cookie_items(session)
    if not cookie_items:
        return
    key = (_normalize_platform(platform), str(account or "").strip())
    expires_at = time.monotonic() + LOGIN_CONTEXT_TTL_SECONDS
    with _LOGIN_CONTEXT_LOCK:
        _LOGIN_CONTEXTS[key] = {"expires_at": expires_at, "cookies": cookie_items}


def _apply_login_context(platform: str, account: str, session: requests.Session) -> None:
    key = (_normalize_platform(platform), str(account or "").strip())
    now = time.monotonic()
    with _LOGIN_CONTEXT_LOCK:
        expired_keys = [
            item_key
            for item_key, context in _LOGIN_CONTEXTS.items()
            if float(context.get("expires_at") or 0) <= now
        ]
        for item_key in expired_keys:
            _LOGIN_CONTEXTS.pop(item_key, None)
        context = _LOGIN_CONTEXTS.get(key)
        if not context:
            return
        cookie_items = list(context.get("cookies") or [])
    for item in cookie_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name or value == "":
            continue
        domain = str(item.get("domain") or "").strip()
        path = str(item.get("path") or "/").strip() or "/"
        try:
            if domain:
                session.cookies.set(name, value, domain=domain, path=path)
            else:
                session.cookies.set(name, value, path=path)
        except Exception:
            continue


def send_online_account_sms_code(
    *,
    platform: str,
    phone: str,
    email: str = "",
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_platform = _normalize_platform(platform)
    clean_account = str((email if clean_platform == "global" else phone) or phone or email or "").strip()
    if not clean_account:
        raise OnlineAccountSmsCodeError(f"请填写{_login_account_label(clean_platform)}。")
    if clean_platform == "cn" and not re.fullmatch(r"1\d{10}", clean_account):
        raise OnlineAccountSmsCodeError("请输入 11 位中国大陆手机号。")
    if clean_platform == "global" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", clean_account):
        raise OnlineAccountSmsCodeError("请输入有效邮箱地址。")

    errors: list[str] = []
    session = _session_from_platform(clean_platform)
    try:
        for endpoint in SMS_CODE_ENDPOINTS[clean_platform]:
            request_payload = (
                {"email": clean_account, "type": "codeLogin"}
                if clean_platform == "global"
                else {"phone": clean_account, "type": "codeLogin"}
            )
            try:
                response = _post_json(
                    session,
                    endpoint,
                    request_payload,
                    platform=clean_platform,
                    proxy_config=proxy_config,
                )
            except Exception as exc:
                errors.append(f"{endpoint}: {_safe_error_message(exc)}")
                continue
            if _looks_like_html_response(response):
                errors.append(f"{endpoint}: {_html_response_error(response)}")
                if _is_authoritative_user_service_endpoint(endpoint):
                    break
                continue
            payload = _response_payload(response)
            message = _response_message(payload)
            if response.status_code >= 400 or not _is_success_payload(payload):
                errors.append(f"{endpoint}: HTTP {response.status_code} {message}".strip())
                if _is_authoritative_user_service_endpoint(endpoint):
                    break
                continue
            _remember_login_context(clean_platform, clean_account, session)
            return {
                "ok": True,
                "platform": clean_platform,
                "phone": clean_account if clean_platform == "cn" else "",
                "email": clean_account if clean_platform == "global" else "",
                "message": "验证码已发送，请查看邮箱。" if clean_platform == "global" else "验证码已发送，请查看手机短信。",
            }
    finally:
        session.close()

    hint = "验证码发送失败，官方网页可正常打开时，通常是验证码接口未返回可用 JSON 或接口参数已调整。"
    if errors:
        hint = f"{hint} 最近一次返回：{errors[-1]}"
    raise OnlineAccountSmsCodeError(hint)


def _exchange_makerworld_ticket(
    session: requests.Session,
    *,
    platform: str,
    proxy_config: ProxyConfig | dict[str, Any] | None,
) -> None:
    ticket_url = TICKET_ENDPOINTS[platform]
    try:
        ticket_response = session.get(
            ticket_url,
            proxies=proxy_mapping(proxy_config, ticket_url, platform=platform, allow_domestic_proxy=True) or None,
            timeout=(8, 20),
        )
    except Exception:
        return
    if ticket_response.status_code >= 400 or _looks_like_html_response(ticket_response):
        return
    ticket_payload = _response_payload(ticket_response)
    ticket = ""
    if isinstance(ticket_payload, dict):
        ticket = str(ticket_payload.get("ticket") or "").strip()
        data = ticket_payload.get("data")
        if not ticket and isinstance(data, dict):
            ticket = str(data.get("ticket") or "").strip()
    if not ticket:
        return

    makerworld_ticket_url = f"{MAKERWORLD_TICKET_ENDPOINTS[platform]}?to={quote(f'{PLATFORM_ORIGINS[platform]}/zh', safe='')}"
    try:
        session.get(
            makerworld_ticket_url,
            params={"ticket": ticket},
            proxies=proxy_mapping(proxy_config, makerworld_ticket_url, platform=platform, allow_domestic_proxy=True) or None,
            timeout=(8, 20),
            allow_redirects=True,
        )
    except Exception:
        return


def login_online_account(
    *,
    platform: str,
    username: str,
    password: str,
    verification_code: str = "",
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
    verify_cookie: bool = True,
) -> OnlineAccountLoginResult:
    clean_platform = _normalize_platform(platform)
    clean_username = _clean_login_account(clean_platform, username)
    clean_code = str(verification_code or password or "").strip()
    if not clean_code:
        raise OnlineAccountLoginError("请填写验证码。")

    errors: list[str] = []
    session = _session_from_platform(clean_platform)
    _apply_login_context(clean_platform, clean_username, session)
    try:
        for login_url in CODE_LOGIN_ENDPOINTS[clean_platform]:
            try:
                response = _post_json(
                    session,
                    login_url,
                    _code_login_payload(login_url, clean_platform, clean_username, clean_code),
                    platform=clean_platform,
                    proxy_config=proxy_config,
                    allow_redirects=True,
                )
            except Exception as exc:
                errors.append(f"{login_url}: {_safe_error_message(exc)}")
                continue

            if _looks_like_html_response(response):
                errors.append(f"{login_url}: {_html_response_error(response)}")
                if _is_authoritative_user_service_endpoint(login_url):
                    break
                continue
            response_payload = _response_payload(response)
            if response.status_code >= 400 or not _is_success_payload(response_payload):
                message = _response_message(response_payload)
                errors.append(f"{login_url}: HTTP {response.status_code} {message}".strip())
                if _is_authoritative_user_service_endpoint(login_url):
                    break
                continue

            if verify_cookie:
                _exchange_makerworld_ticket(session, platform=clean_platform, proxy_config=proxy_config)
            cookie = _cookie_with_response_tokens(session, response_payload)
            if not cookie:
                errors.append(f"{login_url}: 未返回可保存的 Cookie。")
                continue

            auth_payload: dict[str, Any] = {}
            if verify_cookie:
                auth_payload = probe_cookie_auth_status(
                    clean_platform,
                    cookie,
                    proxy_config or ProxyConfig(),
                    include_limit_guard=False,
                    use_cache=False,
                    allow_domestic_proxy=True,
                )
            auth_ok = bool(auth_payload.get("ok")) if verify_cookie else False
            status = "ok" if auth_ok else str(auth_payload.get("state") or "checking")
            message = f"{PLATFORM_LABELS[clean_platform]}{_login_account_label(clean_platform)}登录成功，Cookie 已保存。"
            if not verify_cookie:
                message = (
                    f"{PLATFORM_LABELS[clean_platform]}{_login_account_label(clean_platform)}登录已返回 Cookie，"
                    "已先保存；账号可用性会在后台检测。"
                )
            elif not auth_ok:
                probe_message = str(auth_payload.get("message") or "账号测试失败").strip()
                message = (
                    f"{PLATFORM_LABELS[clean_platform]}{_login_account_label(clean_platform)}登录已返回 Cookie，"
                    f"已先保存；{probe_message}，暂时无法确认 Cookie 是否可用。"
                )

            profile = _extract_profile(response_payload)
            if verify_cookie:
                try:
                    cookie_profile = discover_cookie_account_profile(clean_platform, cookie)
                except Exception:
                    cookie_profile = {}
                for key, value in cookie_profile.items():
                    if value and not profile.get(key):
                        profile[key] = value

            return OnlineAccountLoginResult(
                platform=clean_platform,
                username=clean_username,
                cookie=cookie,
                display_name=str(profile.get("name") or clean_username),
                account_id=str(profile.get("uid") or ""),
                handle=str(profile.get("handle") or ""),
                avatar_url=str(profile.get("avatar_url") or ""),
                status=status,
                message=message,
                login_url=login_url,
                auth_payload=auth_payload,
                cookie_items=_session_cookie_items(session),
            )
    finally:
        session.close()

    hint = (
        f"{_login_account_label(clean_platform)}验证码登录失败。请确认验证码未过期，且已勾选用户协议和隐私政策；"
        "请用 MakerHub 当前弹窗重新发送验证码后尽快提交；"
        "如果官网可以正常登录，多半是自动接口未返回可保存的 JSON/Cookie。"
    )
    if errors:
        hint = f"{hint} 最近一次返回：{errors[-1]}"
    raise OnlineAccountLoginError(hint)


def online_account_metadata_from_cookie(
    *,
    platform: str,
    username: str = "",
    cookie: str,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
) -> dict[str, str]:
    clean_platform = _normalize_platform(platform)
    clean_cookie = sanitize_cookie_header(cookie)
    timestamp = now_iso()
    result = {
        "platform": clean_platform,
        "username": str(username or "").strip(),
        "display_name": "",
        "account_id": "",
        "handle": "",
        "avatar_url": "",
        "status": "untested" if clean_cookie else "",
        "message": "",
        "last_tested_at": "",
        "updated_at": timestamp,
    }
    if not clean_cookie:
        return result

    auth_payload = probe_cookie_auth_status(
        clean_platform,
        clean_cookie,
        proxy_config or ProxyConfig(),
        include_limit_guard=False,
        use_cache=False,
        allow_domestic_proxy=True,
    )
    result["status"] = "ok" if auth_payload.get("ok") else str(auth_payload.get("state") or "error")
    result["message"] = str(auth_payload.get("message") or "")
    result["last_tested_at"] = timestamp
    try:
        profile = discover_cookie_account_profile(clean_platform, clean_cookie)
    except Exception:
        profile = {}
    result["display_name"] = str(profile.get("name") or result["username"] or "").strip()
    result["account_id"] = str(profile.get("uid") or "").strip()
    result["handle"] = str(profile.get("handle") or "").strip()
    result["avatar_url"] = str(profile.get("avatar_url") or "").strip()
    return result
