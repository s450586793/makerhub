from __future__ import annotations

from dataclasses import dataclass
import json
import os
import subprocess
import threading
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app.core.settings import ROOT_DIR
from app.schemas.models import ProxyConfig
from app.services.cookie_utils import extract_auth_token, parse_cookie_values, sanitize_cookie_header
from app.services.online_accounts import (
    MAKERWORLD_TICKET_ENDPOINTS,
    MW_BROWSER_HEADERS,
    PLATFORM_ORIGINS,
    TICKET_ENDPOINTS,
)
from app.services.proxy_policy import proxy_mapping


BRIDGE_SCRIPT = ROOT_DIR / "app" / "services" / "cloakbrowser_bridge.mjs"
DEFAULT_TIMEOUT_SECONDS = 30
PROFILE_NAMES = {
    "cn": "MakerHub CN",
    "global": "MakerHub Global",
}
PROFILE_LOCALES = {
    "cn": "zh-CN",
    "global": "en-US",
}
PROFILE_TIMEZONES = {
    "cn": "Asia/Shanghai",
    "global": "UTC",
}
PLATFORM_DOMAINS = {
    "cn": ("makerworld.com.cn", "bambulab.cn"),
    "global": ("makerworld.com", "bambulab.com"),
}

_OPERATION_LOCKS_LOCK = threading.Lock()
_OPERATION_LOCKS: dict[str, threading.Lock] = {}


class CloakBrowserError(RuntimeError):
    pass


class CloakBrowserUnavailable(CloakBrowserError):
    pass


class CloakBrowserBridgeError(CloakBrowserError):
    pass


@dataclass(frozen=True)
class CloakBrowserProfile:
    id: str
    name: str
    status: str = "stopped"
    cdp_url: str = ""


@dataclass(frozen=True)
class CloakBrowserSessionResult:
    profile_id: str
    cookie: str
    current_url: str = ""
    public_url: str = ""
    launched_here: bool = False
    navigation_error: str = ""


def normalize_platform(platform: str) -> str:
    return "global" if str(platform or "").strip().lower() == "global" else "cn"


def _hostname_matches_domains(hostname: str, domains: tuple[str, ...]) -> bool:
    clean_hostname = str(hostname or "").strip().lower().lstrip(".")
    return any(
        clean_hostname == domain or clean_hostname.endswith(f".{domain}")
        for domain in domains
    )


def cloakbrowser_configured() -> bool:
    url = str(os.getenv("MAKERHUB_CLOAKBROWSER_URL") or "").strip()
    token = str(os.getenv("MAKERHUB_CLOAKBROWSER_AUTH_TOKEN") or "").strip()
    return bool(url and token)


def cloakbrowser_public_url() -> str:
    return str(os.getenv("MAKERHUB_CLOAKBROWSER_PUBLIC_URL") or "").strip().rstrip("/")


def _configured_url() -> str:
    raw = str(os.getenv("MAKERHUB_CLOAKBROWSER_URL") or "").strip().rstrip("/")
    if not raw:
        raise CloakBrowserUnavailable("指纹浏览器服务未配置。")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CloakBrowserUnavailable("MAKERHUB_CLOAKBROWSER_URL 格式无效。")
    return raw


def _timeout_seconds() -> int:
    raw = str(os.getenv("MAKERHUB_CLOAKBROWSER_TIMEOUT") or "").strip()
    try:
        value = int(float(raw)) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(min(value, 120), 5)


def _auth_token() -> str:
    token = str(os.getenv("MAKERHUB_CLOAKBROWSER_AUTH_TOKEN") or "").strip()
    if not token:
        raise CloakBrowserUnavailable("MAKERHUB_CLOAKBROWSER_AUTH_TOKEN 未配置。")
    return token


def _request_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_auth_token()}"}


def _request(method: str, path: str, *, json_payload: dict[str, Any] | None = None) -> Any:
    try:
        response = requests.request(
            method,
            f"{_configured_url()}{path}",
            headers=_request_headers(),
            json=json_payload,
            timeout=_timeout_seconds(),
        )
    except CloakBrowserError:
        raise
    except Exception as exc:
        raise CloakBrowserUnavailable(f"连接指纹浏览器失败：{exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        message = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = str(payload.get("detail") or payload.get("message") or "").strip()
        except ValueError:
            message = ""
        detail = f"：{message}" if message else ""
        raise CloakBrowserError(f"指纹浏览器返回 HTTP {response.status_code}{detail}")
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise CloakBrowserError("指纹浏览器返回了无效 JSON。") from exc


def _profile_from_payload(payload: Any) -> CloakBrowserProfile | None:
    if not isinstance(payload, dict):
        return None
    profile_id = str(payload.get("id") or payload.get("profile_id") or "").strip()
    if not profile_id:
        return None
    return CloakBrowserProfile(
        id=profile_id,
        name=str(payload.get("name") or "").strip(),
        status=str(payload.get("status") or "stopped").strip().lower() or "stopped",
        cdp_url=str(payload.get("cdp_url") or "").strip(),
    )


def _matches_managed_profile(payload: dict[str, Any], platform: str) -> bool:
    if str(payload.get("name") or "").strip() == PROFILE_NAMES[platform]:
        return True
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
    tag_names = {
        str(item.get("tag") or "").strip().lower()
        for item in tags
        if isinstance(item, dict)
    }
    return "makerhub" in tag_names and platform in tag_names


def ensure_profile(platform: str, profile_id: str = "") -> CloakBrowserProfile:
    clean_platform = normalize_platform(platform)
    clean_profile_id = str(profile_id or "").strip()
    if clean_profile_id:
        profile = _profile_from_payload(_request("GET", f"/api/profiles/{clean_profile_id}"))
        if profile is not None:
            return profile

    profiles = _request("GET", "/api/profiles")
    if isinstance(profiles, list):
        for payload in profiles:
            if isinstance(payload, dict) and _matches_managed_profile(payload, clean_platform):
                profile = _profile_from_payload(payload)
                if profile is not None:
                    return profile

    payload = _request(
        "POST",
        "/api/profiles",
        json_payload={
            "name": PROFILE_NAMES[clean_platform],
            "locale": PROFILE_LOCALES[clean_platform],
            "timezone": PROFILE_TIMEZONES[clean_platform],
            "platform": "windows",
            "humanize": True,
            "human_preset": "careful",
            "headless": False,
            "auto_launch": False,
            "clipboard_sync": True,
            "notes": f"MakerHub managed {clean_platform} account profile",
            "tags": [
                {"tag": "makerhub", "color": "#22c55e"},
                {"tag": clean_platform, "color": "#64748b"},
            ],
        },
    )
    profile = _profile_from_payload(payload)
    if profile is None:
        raise CloakBrowserError("指纹浏览器创建 profile 后没有返回有效 ID。")
    return profile


def launch_profile(profile: CloakBrowserProfile) -> tuple[CloakBrowserProfile, bool]:
    current = _profile_from_payload(_request("GET", f"/api/profiles/{profile.id}")) or profile
    if current.status == "running":
        return current, False
    payload = _request("POST", f"/api/profiles/{profile.id}/launch")
    launched = _profile_from_payload(payload)
    if launched is None:
        launched = CloakBrowserProfile(
            id=profile.id,
            name=profile.name,
            status="running",
            cdp_url=f"/api/profiles/{profile.id}/cdp",
        )
    return launched, True


def stop_profile(profile_id: str) -> None:
    clean_profile_id = str(profile_id or "").strip()
    if not clean_profile_id:
        return
    try:
        _request("POST", f"/api/profiles/{clean_profile_id}/stop")
    except CloakBrowserError:
        return


def _operation_lock(platform: str) -> threading.Lock:
    clean_platform = normalize_platform(platform)
    with _OPERATION_LOCKS_LOCK:
        lock = _OPERATION_LOCKS.get(clean_platform)
        if lock is None:
            lock = threading.Lock()
            _OPERATION_LOCKS[clean_platform] = lock
        return lock


def _safe_bridge_env() -> dict[str, str]:
    allowed = {"PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "NODE_OPTIONS"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run_bridge(payload: dict[str, Any], *, timeout_seconds: int | None = None) -> dict[str, Any]:
    if not str(payload.get("auth_token") or "").strip():
        raise CloakBrowserUnavailable("MAKERHUB_CLOAKBROWSER_AUTH_TOKEN 未配置。")
    if not BRIDGE_SCRIPT.is_file():
        raise CloakBrowserBridgeError("指纹浏览器 CDP bridge 脚本不存在。")
    try:
        result = subprocess.run(
            ["node", BRIDGE_SCRIPT.as_posix()],
            cwd=ROOT_DIR.as_posix(),
            env=_safe_bridge_env(),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout_seconds or max(_timeout_seconds() * 2, 30),
            check=False,
        )
    except FileNotFoundError as exc:
        raise CloakBrowserBridgeError("MakerHub 容器缺少 Node.js，无法连接指纹浏览器。") from exc
    except subprocess.TimeoutExpired as exc:
        raise CloakBrowserBridgeError("指纹浏览器 CDP 操作超时。") from exc
    if result.returncode != 0:
        message = str(result.stderr or "指纹浏览器 CDP 操作失败。").strip()[:400]
        raise CloakBrowserBridgeError(message)
    try:
        output = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CloakBrowserBridgeError("指纹浏览器 CDP bridge 返回了无效 JSON。") from exc
    if not isinstance(output, dict) or not output.get("ok"):
        raise CloakBrowserBridgeError(str(output.get("message") if isinstance(output, dict) else "CDP 操作失败。")[:400])
    return output


def _cookie_items_from_header(raw_cookie: str, platform: str) -> list[dict[str, Any]]:
    clean_platform = normalize_platform(platform)
    values = parse_cookie_values(raw_cookie)
    items: list[dict[str, Any]] = []
    for name, value in values.items():
        for domain in PLATFORM_DOMAINS[clean_platform]:
            items.append(
                {
                    "name": name,
                    "value": value,
                    "domain": f".{domain}",
                    "path": "/",
                    "secure": True,
                }
            )
    return items


def _normalize_structured_cookie_items(items: list[dict[str, Any]] | None, platform: str) -> list[dict[str, Any]]:
    clean_platform = normalize_platform(platform)
    allowed_domains = PLATFORM_DOMAINS[clean_platform]
    normalized: list[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name or value == "":
            continue
        domain = str(item.get("domain") or "").strip().lower()
        if domain and not _hostname_matches_domains(domain, allowed_domains):
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "path": str(item.get("path") or "/").strip() or "/",
            "secure": bool(item.get("secure", True)),
        }
        if domain:
            cookie["domain"] = domain
        else:
            cookie["url"] = PLATFORM_ORIGINS[clean_platform]
        normalized.append(cookie)
    return normalized


def browser_cookie_items(
    raw_cookie: str,
    platform: str,
    structured_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items = _normalize_structured_cookie_items(structured_items, platform)
    seen = {(item.get("name"), item.get("domain"), item.get("url")) for item in items}
    for item in _cookie_items_from_header(raw_cookie, platform):
        key = (item.get("name"), item.get("domain"), item.get("url"))
        if key not in seen:
            items.append(item)
            seen.add(key)
    return items


def _apply_cookie_header_to_session(session: requests.Session, raw_cookie: str, platform: str) -> None:
    clean_platform = normalize_platform(platform)
    for name, value in parse_cookie_values(raw_cookie).items():
        for domain in PLATFORM_DOMAINS[clean_platform]:
            try:
                session.cookies.set(name, value, domain=f".{domain}", path="/")
            except Exception:
                continue


def makerworld_ticket_url(
    platform: str,
    raw_cookie: str,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
) -> str:
    clean_platform = normalize_platform(platform)
    clean_cookie = sanitize_cookie_header(raw_cookie)
    if not clean_cookie:
        return ""
    session = requests.Session()
    session.trust_env = False
    session.headers.update(MW_BROWSER_HEADERS)
    token = extract_auth_token(clean_cookie)
    if token:
        session.headers.update({"Authorization": f"Bearer {token}"})
    _apply_cookie_header_to_session(session, clean_cookie, clean_platform)
    ticket_endpoint = TICKET_ENDPOINTS[clean_platform]
    try:
        response = session.get(
            ticket_endpoint,
            proxies=proxy_mapping(
                proxy_config,
                ticket_endpoint,
                platform=clean_platform,
                allow_domestic_proxy=True,
            ) or None,
            timeout=(8, 20),
        )
        if response.status_code >= 400:
            return ""
        payload = response.json() if response.text else {}
    except Exception:
        return ""
    finally:
        session.close()
    if not isinstance(payload, dict):
        return ""
    ticket = str(payload.get("ticket") or "").strip()
    data = payload.get("data")
    if not ticket and isinstance(data, dict):
        ticket = str(data.get("ticket") or "").strip()
    if not ticket:
        return ""
    origin = PLATFORM_ORIGINS[clean_platform]
    return (
        f"{MAKERWORLD_TICKET_ENDPOINTS[clean_platform]}"
        f"?to={quote(f'{origin}/zh', safe='')}&ticket={quote(ticket, safe='')}"
    )


def browser_login_url(platform: str) -> str:
    clean_platform = normalize_platform(platform)
    origin = PLATFORM_ORIGINS[clean_platform]
    login_origin = "https://bambulab.com" if clean_platform == "global" else "https://bambulab.cn"
    return f"{login_origin}/zh-cn/sign-in?ticket=1&to={quote(f'{origin}/api/sign-in/ticket?to={origin}/zh', safe='')}"


def _cookie_header_from_snapshot(snapshot: dict[str, Any], platform: str) -> str:
    clean_platform = normalize_platform(platform)
    allowed_domains = PLATFORM_DOMAINS[clean_platform]
    values: dict[str, str] = {}
    cookies = snapshot.get("cookies") if isinstance(snapshot.get("cookies"), list) else []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip().lower().lstrip(".")
        if domain and not _hostname_matches_domains(domain, allowed_domains):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name and value != "":
            values[name] = value
    storage = snapshot.get("storage") if isinstance(snapshot.get("storage"), list) else []
    for entry in storage:
        if not isinstance(entry, dict):
            continue
        origin = str(entry.get("origin") or "").strip()
        origin_host = (urlparse(origin).hostname or "").lower()
        if not origin_host or not _hostname_matches_domains(origin_host, allowed_domains):
            continue
        for bucket_name in ("local", "session"):
            bucket = entry.get(bucket_name) if isinstance(entry.get(bucket_name), dict) else {}
            for key, value in bucket.items():
                clean_key = str(key or "").strip()
                clean_value = str(value or "").strip()
                lowered = clean_key.lower()
                if not clean_value:
                    continue
                if lowered in {"token", "accesstoken", "access_token"}:
                    values.setdefault("token", clean_value)
                elif lowered in {"refreshtoken", "refresh_token"}:
                    values.setdefault("refreshToken", clean_value)
    return sanitize_cookie_header("; ".join(f"{key}={value}" for key, value in values.items()))


def _bridge_payload(profile_id: str, *, action: str, cookies: list[dict[str, Any]] | None = None, target_url: str = "") -> dict[str, Any]:
    return {
        "action": action,
        "cdp_url": f"{_configured_url()}/api/profiles/{profile_id}/cdp",
        "auth_token": _auth_token(),
        "cookies": cookies or [],
        "target_url": str(target_url or "").strip(),
        "navigation_timeout_ms": max(_timeout_seconds() * 1000, 15000),
    }


def synchronize_browser_session(
    platform: str,
    raw_cookie: str,
    *,
    profile_id: str = "",
    structured_items: list[dict[str, Any]] | None = None,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
    stop_when_done: bool = True,
) -> CloakBrowserSessionResult:
    clean_platform = normalize_platform(platform)
    with _operation_lock(clean_platform):
        profile = ensure_profile(clean_platform, profile_id)
        running, launched_here = launch_profile(profile)
        ticket_url = makerworld_ticket_url(clean_platform, raw_cookie, proxy_config)
        target_url = ticket_url or PLATFORM_ORIGINS[clean_platform]
        try:
            snapshot = _run_bridge(
                _bridge_payload(
                    running.id,
                    action="seed",
                    cookies=browser_cookie_items(raw_cookie, clean_platform, structured_items),
                    target_url=target_url,
                )
            )
            current_url = str(snapshot.get("current_url") or "")
            current_host = (urlparse(current_url).hostname or "").lower()
            makerworld_domain = PLATFORM_DOMAINS[clean_platform][0]
            if not ticket_url:
                raise CloakBrowserError("未获取到 MakerWorld 登录 ticket，需要在指纹浏览器中确认登录。")
            if not current_host.endswith(makerworld_domain):
                raise CloakBrowserError("MakerWorld ticket 未完成站点登录，需要在指纹浏览器中确认。")
            cookie = _cookie_header_from_snapshot(snapshot, clean_platform) or sanitize_cookie_header(raw_cookie)
            return CloakBrowserSessionResult(
                profile_id=running.id,
                cookie=cookie,
                current_url=current_url,
                public_url=cloakbrowser_public_url(),
                launched_here=launched_here,
                navigation_error=str(snapshot.get("navigation_error") or "")[:240],
            )
        finally:
            if stop_when_done and launched_here:
                stop_profile(running.id)


def prepare_browser_login(
    platform: str,
    raw_cookie: str = "",
    *,
    profile_id: str = "",
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
) -> CloakBrowserSessionResult:
    clean_platform = normalize_platform(platform)
    with _operation_lock(clean_platform):
        profile = ensure_profile(clean_platform, profile_id)
        running, launched_here = launch_profile(profile)
        target_url = makerworld_ticket_url(clean_platform, raw_cookie, proxy_config) or browser_login_url(clean_platform)
        snapshot = _run_bridge(
            _bridge_payload(
                running.id,
                action="seed",
                cookies=browser_cookie_items(raw_cookie, clean_platform) if raw_cookie else [],
                target_url=target_url,
            )
        )
        return CloakBrowserSessionResult(
            profile_id=running.id,
            cookie=_cookie_header_from_snapshot(snapshot, clean_platform),
            current_url=str(snapshot.get("current_url") or ""),
            public_url=cloakbrowser_public_url(),
            launched_here=launched_here,
            navigation_error=str(snapshot.get("navigation_error") or "")[:240],
        )


def collect_browser_session(platform: str, profile_id: str) -> CloakBrowserSessionResult:
    clean_platform = normalize_platform(platform)
    with _operation_lock(clean_platform):
        profile = ensure_profile(clean_platform, profile_id)
        running, launched_here = launch_profile(profile)
        snapshot = _run_bridge(_bridge_payload(running.id, action="snapshot"))
        return CloakBrowserSessionResult(
            profile_id=running.id,
            cookie=_cookie_header_from_snapshot(snapshot, clean_platform),
            current_url=str(snapshot.get("current_url") or ""),
            public_url=cloakbrowser_public_url(),
            launched_here=launched_here,
            navigation_error=str(snapshot.get("navigation_error") or "")[:240],
        )
