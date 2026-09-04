from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app.core.settings import ROOT_DIR, STATE_DIR
from app.schemas.models import ProxyConfig
from app.services.cookie_utils import extract_auth_token, parse_cookie_values, sanitize_cookie_header
from app.services.online_accounts import (
    MAKERWORLD_TICKET_ENDPOINTS,
    MW_BROWSER_HEADERS,
    PLATFORM_ORIGINS,
    TICKET_ENDPOINTS,
)
from app.services.proxy_policy import proxy_mapping, proxy_url
from app.services.resource_limiter import resource_slot


BRIDGE_SCRIPT = ROOT_DIR / "app" / "services" / "cloakbrowser_bridge.mjs"
DEFAULT_TIMEOUT_SECONDS = 30
AUTHORIZATION_TIMEOUT_SECONDS = 90
AUTO_VERIFY_TIMEOUT_SECONDS = 50
AUTHORIZATION_BRIDGE_CLEANUP_MARGIN_SECONDS = 40
AUTHORIZATION_TRANSIENT_RETRY_DELAYS_SECONDS = (2.0, 5.0)
PROFILE_RECOVERY_COOLDOWN_SECONDS = 60
CLOAKBROWSER_IDLE_SECONDS_ENV = "MAKERHUB_CLOAKBROWSER_IDLE_SECONDS"
DEFAULT_CLOAKBROWSER_IDLE_SECONDS = 30 * 60
CLOAKBROWSER_AUTOMATION_IDLE_SECONDS_ENV = "MAKERHUB_CLOAKBROWSER_AUTOMATION_IDLE_SECONDS"
DEFAULT_CLOAKBROWSER_AUTOMATION_IDLE_SECONDS = 2 * 60
MANAGED_PROFILE_LAUNCH_ARGS = ("--disable-gpu",)
AUTOMATION_ACTIVITY_DETAILS = frozenset({"fetch"})
TRANSIENT_BRIDGE_ERROR_MARKERS = (
    "超时",
    "timeout",
    "timed out",
    "network.enable",
    "disconnected",
    "connection closed",
    "target closed",
    "websocket",
    "socket hang up",
    "econnreset",
)
TRANSIENT_PROFILE_ERROR_MARKERS = (
    *TRANSIENT_BRIDGE_ERROR_MARKERS,
    "already running",
    "xvnc",
    "resource temporarily unavailable",
    "eagain",
    "http 5",
)
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
ALLOWED_BROWSER_FETCH_HEADERS = {
    "accept",
    "accept-language",
    "authorization",
    "origin",
    "referer",
    "token",
    "x-access-token",
    "x-app-name",
    "x-app-version",
    "x-bbl-app-source",
    "x-bbl-client-type",
    "x-bbl-client-name",
    "x-bbl-client-version",
    "x-bbl-captcha-result",
    "x-token",
}

class CloakBrowserError(RuntimeError):
    pass


class CloakBrowserUnavailable(CloakBrowserError):
    pass


class CloakBrowserBridgeError(CloakBrowserUnavailable):
    pass


@dataclass(frozen=True)
class CloakBrowserProfile:
    id: str
    name: str
    status: str = "stopped"
    cdp_url: str = ""
    proxy: str = ""
    launch_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class CloakBrowserSessionResult:
    profile_id: str
    cookie: str
    current_url: str = ""
    public_url: str = ""
    launched_here: bool = False
    navigation_error: str = ""


@dataclass(frozen=True)
class CloakBrowserFetchResult:
    profile_id: str
    url: str
    status_code: int
    content_type: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)


def normalize_platform(platform: str) -> str:
    return "global" if str(platform or "").strip().lower() == "global" else "cn"


def _hostname_matches_domains(hostname: str, domains: tuple[str, ...]) -> bool:
    clean_hostname = str(hostname or "").strip().lower().lstrip(".")
    return any(
        clean_hostname == domain or clean_hostname.endswith(f".{domain}")
        for domain in domains
    )


def _validate_browser_fetch_url(url: str, platform: str) -> str:
    clean_platform = normalize_platform(platform)
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise CloakBrowserError("指纹浏览器抓取目标地址无效。") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or not _hostname_matches_domains(hostname, PLATFORM_DOMAINS[clean_platform])
    ):
        raise CloakBrowserError("指纹浏览器抓取目标地址不属于当前 MakerWorld 平台。")
    return parsed._replace(fragment="").geturl()


def _safe_browser_fetch_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {
        str(name).strip(): str(value)
        for name, value in (headers or {}).items()
        if str(name).strip().lower() in ALLOWED_BROWSER_FETCH_HEADERS and value is not None
    }


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


def _env_bool(env_name: str, default: bool) -> bool:
    raw = str(os.getenv(env_name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _authorization_bridge_timeout_seconds(navigation_timeout_ms: int) -> int:
    try:
        milliseconds = max(int(navigation_timeout_ms), 15_000)
    except (TypeError, ValueError):
        milliseconds = DEFAULT_TIMEOUT_SECONDS * 1000
    navigation_seconds = (milliseconds + 999) // 1000
    first_response_seconds = max(
        AUTHORIZATION_TIMEOUT_SECONDS,
        navigation_seconds,
    )
    return (
        navigation_seconds
        + first_response_seconds
        + AUTO_VERIFY_TIMEOUT_SECONDS
        + AUTHORIZATION_BRIDGE_CLEANUP_MARGIN_SECONDS
    )


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
        if response.status_code >= 500:
            raise CloakBrowserUnavailable(f"指纹浏览器返回 HTTP {response.status_code}{detail}")
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
        proxy=str(payload.get("proxy") or "").strip(),
        launch_args=tuple(
            str(item).strip()
            for item in (
                payload.get("launch_args")
                if isinstance(payload.get("launch_args"), list)
                else []
            )
            if str(item).strip()
        ),
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


def _managed_profile_proxy(
    platform: str,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
) -> str | None:
    """Return the MakerHub-managed proxy for a profile, if that profile owns one."""
    if normalize_platform(platform) != "global":
        return None
    active_proxy = proxy_config
    if active_proxy is None:
        try:
            from app.core.store import JsonStore

            active_proxy = JsonStore().load().proxy
        except Exception:
            active_proxy = None
    return proxy_url(active_proxy, PLATFORM_ORIGINS["global"], platform="global")


def _profile_uses_proxy(profile: CloakBrowserProfile, proxy: str) -> bool:
    return str(profile.proxy or "").strip() == str(proxy or "").strip()


def _managed_profile_launch_args(profile: CloakBrowserProfile) -> tuple[str, ...]:
    launch_args = list(profile.launch_args)
    for required_arg in MANAGED_PROFILE_LAUNCH_ARGS:
        if required_arg not in launch_args:
            launch_args.append(required_arg)
    return tuple(launch_args)


def _ensure_managed_profile_launch_args(profile: CloakBrowserProfile) -> CloakBrowserProfile:
    launch_args = _managed_profile_launch_args(profile)
    if profile.status == "running" or launch_args == profile.launch_args:
        return profile
    payload = _request(
        "PUT",
        f"/api/profiles/{profile.id}",
        json_payload={"launch_args": list(launch_args)},
    )
    updated = _profile_from_payload(payload)
    if updated is None:
        raise CloakBrowserError("指纹浏览器更新 profile 资源参数后没有返回有效 profile。")
    return updated


def ensure_profile(
    platform: str,
    profile_id: str = "",
    *,
    browser_proxy: str | None = None,
) -> CloakBrowserProfile:
    clean_platform = normalize_platform(platform)
    clean_profile_id = str(profile_id or "").strip()
    if clean_profile_id:
        profile_payload = _request("GET", f"/api/profiles/{clean_profile_id}")
        profile = _profile_from_payload(profile_payload)
        if profile is not None:
            if isinstance(profile_payload, dict) and _matches_managed_profile(profile_payload, clean_platform):
                profile = _ensure_managed_profile_launch_args(profile)
            return profile

    profiles = _request("GET", "/api/profiles")
    if isinstance(profiles, list):
        for payload in profiles:
            if isinstance(payload, dict) and _matches_managed_profile(payload, clean_platform):
                profile = _profile_from_payload(payload)
                if profile is not None:
                    return _ensure_managed_profile_launch_args(profile)

    payload_data: dict[str, Any] = {
        "name": PROFILE_NAMES[clean_platform],
        "locale": PROFILE_LOCALES[clean_platform],
        "timezone": PROFILE_TIMEZONES[clean_platform],
        "platform": "windows",
        "humanize": True,
        "human_preset": "careful",
        "headless": False,
        "auto_launch": False,
        "launch_args": list(MANAGED_PROFILE_LAUNCH_ARGS),
        "clipboard_sync": True,
        "notes": f"MakerHub managed {clean_platform} account profile",
        "tags": [
            {"tag": "makerhub", "color": "#22c55e"},
            {"tag": clean_platform, "color": "#64748b"},
        ],
    }
    if browser_proxy is not None:
        payload_data["proxy"] = str(browser_proxy or "").strip() or None
    payload = _request("POST", "/api/profiles", json_payload=payload_data)
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
            proxy=profile.proxy,
            launch_args=profile.launch_args,
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


def _stop_profile_for_proxy_change(profile: CloakBrowserProfile) -> CloakBrowserProfile:
    stop_profile(profile.id)
    deadline = time.monotonic() + min(max(_timeout_seconds(), 5), 15)
    while time.monotonic() < deadline:
        current = _profile_from_payload(_request("GET", f"/api/profiles/{profile.id}")) or profile
        if current.status == "stopped":
            return current
        time.sleep(0.2)
    raise CloakBrowserUnavailable("指纹浏览器正在切换全局站代理，请稍后重试。")


def _update_profile_proxy(profile: CloakBrowserProfile, proxy: str) -> CloakBrowserProfile:
    payload = _request(
        "PUT",
        f"/api/profiles/{profile.id}",
        json_payload={"proxy": str(proxy or "").strip() or None},
    )
    updated = _profile_from_payload(payload)
    if updated is None:
        raise CloakBrowserError("指纹浏览器更新全局站代理后没有返回有效 profile。")
    return updated


def _profile_resource_name(platform: str, profile_id: str = "") -> str:
    # CloakBrowser Manager proxies every profile through one service. Serializing
    # only per platform still allows CN and Global CDP sessions to overload it.
    return "cloakbrowser_manager"


def _profile_activity_path(platform: str) -> Path:
    return STATE_DIR / "cloakbrowser_activity" / f"{normalize_platform(platform)}.marker"


def touch_profile_activity(
    platform: str,
    *,
    now: float | None = None,
    detail: str | None = None,
) -> None:
    marker_path = _profile_activity_path(platform)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        if detail is None:
            marker_path.touch(exist_ok=True)
        else:
            marker_path.write_text(str(detail or "").strip().lower()[:64], encoding="utf-8")
        if now is not None:
            os.utime(marker_path, (float(now), float(now)))
    except OSError:
        pass


def profile_activity_at(platform: str) -> float:
    try:
        return float(_profile_activity_path(platform).stat().st_mtime)
    except OSError:
        return 0.0


def profile_activity_detail(platform: str) -> str:
    try:
        return _profile_activity_path(platform).read_text(encoding="utf-8").strip().lower()[:64]
    except (OSError, UnicodeError):
        return ""


def _clear_profile_activity(platform: str) -> None:
    try:
        _profile_activity_path(platform).unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _profile_operation(platform: str, profile_id: str = "", *, detail: str):
    clean_platform = normalize_platform(platform)
    touch_profile_activity(clean_platform, detail=detail)
    try:
        with resource_slot(
            _profile_resource_name(clean_platform, profile_id),
            detail=detail,
            priority=100 if detail == "click" else 0,
        ):
            touch_profile_activity(clean_platform, detail=detail)
            yield
    finally:
        touch_profile_activity(clean_platform, detail=detail)


def _cloakbrowser_idle_seconds(value: int | None = None) -> int:
    raw = value if value is not None else os.getenv(CLOAKBROWSER_IDLE_SECONDS_ENV)
    try:
        return max(int(raw if raw not in (None, "") else DEFAULT_CLOAKBROWSER_IDLE_SECONDS), 0)
    except (TypeError, ValueError):
        return DEFAULT_CLOAKBROWSER_IDLE_SECONDS


def _cloakbrowser_automation_idle_seconds(value: int | None = None) -> int:
    raw = value if value is not None else os.getenv(CLOAKBROWSER_AUTOMATION_IDLE_SECONDS_ENV)
    try:
        return max(
            int(raw if raw not in (None, "") else DEFAULT_CLOAKBROWSER_AUTOMATION_IDLE_SECONDS),
            0,
        )
    except (TypeError, ValueError):
        return DEFAULT_CLOAKBROWSER_AUTOMATION_IDLE_SECONDS


def _profile_idle_timeout(
    platform: str,
    *,
    idle_seconds: int,
    automation_idle_seconds: int,
) -> int:
    if profile_activity_detail(platform) in AUTOMATION_ACTIVITY_DETAILS:
        return automation_idle_seconds
    return idle_seconds


def _managed_profile_platform(payload: dict[str, Any]) -> str:
    for platform in ("cn", "global"):
        if _matches_managed_profile(payload, platform):
            return platform
    return ""


def stop_idle_profiles(
    *,
    idle_seconds: int | None = None,
    automation_idle_seconds: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    timeout = _cloakbrowser_idle_seconds(idle_seconds)
    automation_timeout = _cloakbrowser_automation_idle_seconds(automation_idle_seconds)
    result: dict[str, Any] = {
        "idle_seconds": timeout,
        "automation_idle_seconds": automation_timeout,
        "checked_count": 0,
        "initialized_count": 0,
        "stopped_count": 0,
        "stopped_profiles": [],
    }
    if (timeout <= 0 and automation_timeout <= 0) or not cloakbrowser_configured():
        return result
    current_time = float(now if now is not None else time.time())
    try:
        profiles = _request("GET", "/api/profiles")
    except CloakBrowserError as exc:
        result["error"] = str(exc)[:240]
        return result
    if not isinstance(profiles, list):
        return result

    for payload in profiles:
        if not isinstance(payload, dict):
            continue
        profile = _profile_from_payload(payload)
        platform = _managed_profile_platform(payload)
        if profile is None or not platform or profile.status != "running":
            continue
        result["checked_count"] += 1
        activity_at = profile_activity_at(platform)
        if activity_at <= 0:
            touch_profile_activity(platform, now=current_time)
            result["initialized_count"] += 1
            continue
        profile_timeout = _profile_idle_timeout(
            platform,
            idle_seconds=timeout,
            automation_idle_seconds=automation_timeout,
        )
        if profile_timeout <= 0 or current_time - activity_at < profile_timeout:
            continue

        with resource_slot(_profile_resource_name(platform, profile.id), detail="idle-stop"):
            activity_at = profile_activity_at(platform)
            profile_timeout = _profile_idle_timeout(
                platform,
                idle_seconds=timeout,
                automation_idle_seconds=automation_timeout,
            )
            if (
                activity_at <= 0
                or profile_timeout <= 0
                or current_time - activity_at < profile_timeout
            ):
                continue
            current = _profile_from_payload(_request("GET", f"/api/profiles/{profile.id}"))
            if current is None or current.status != "running":
                continue
            stop_profile(profile.id)
            _clear_profile_activity(platform)
            result["stopped_profiles"].append(profile.id)
            result["stopped_count"] += 1
    return result


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


def _is_transient_bridge_error(exc: CloakBrowserBridgeError) -> bool:
    detail = str(exc or "").strip().lower()
    return any(marker in detail for marker in TRANSIENT_BRIDGE_ERROR_MARKERS)


def _is_transient_profile_error(exc: CloakBrowserError) -> bool:
    detail = str(exc or "").strip().lower()
    return any(marker in detail for marker in TRANSIENT_PROFILE_ERROR_MARKERS)


def _profile_recovery_marker_path(profile_id: str) -> Path:
    digest = hashlib.sha256(str(profile_id or "").encode("utf-8")).hexdigest()[:24]
    return STATE_DIR / "cloakbrowser_recovery" / f"{digest}.marker"


def _profile_recovery_cooldown_active(profile_id: str) -> bool:
    marker_path = _profile_recovery_marker_path(profile_id)
    try:
        if marker_path.stat().st_mtime + PROFILE_RECOVERY_COOLDOWN_SECONDS > time.time():
            return True
        marker_path.unlink(missing_ok=True)
    except OSError:
        return False
    return False


def _mark_profile_recovery_attempt(profile_id: str) -> None:
    marker_path = _profile_recovery_marker_path(profile_id)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.touch()
    except OSError:
        pass


def _clear_profile_recovery_attempt(profile_id: str) -> None:
    try:
        _profile_recovery_marker_path(profile_id).unlink(missing_ok=True)
    except OSError:
        pass


def _profile_cooldown_error() -> CloakBrowserUnavailable:
    return CloakBrowserUnavailable(
        "指纹浏览器启动刚刚失败，已暂停自动重试，请稍后再试。"
    )


def _ensure_running_profile(
    platform: str,
    profile_id: str = "",
    *,
    proxy_config: ProxyConfig | dict[str, Any] | None = None,
    allow_recovery_restart: bool = False,
) -> tuple[CloakBrowserProfile, CloakBrowserProfile, bool]:
    clean_profile_id = str(profile_id or "").strip()
    if (
        clean_profile_id
        and not allow_recovery_restart
        and _profile_recovery_cooldown_active(clean_profile_id)
    ):
        raise _profile_cooldown_error()
    try:
        managed_proxy = _managed_profile_proxy(platform, proxy_config)
        profile = ensure_profile(platform, clean_profile_id, browser_proxy=managed_proxy)
    except CloakBrowserError as exc:
        if clean_profile_id and _is_transient_profile_error(exc):
            _mark_profile_recovery_attempt(clean_profile_id)
        raise
    if not allow_recovery_restart and _profile_recovery_cooldown_active(profile.id):
        raise _profile_cooldown_error()
    if managed_proxy is not None and not _profile_uses_proxy(profile, managed_proxy):
        if profile.status == "running":
            profile = _stop_profile_for_proxy_change(profile)
        profile = _update_profile_proxy(profile, managed_proxy)
    try:
        running, launched_here = launch_profile(profile)
    except CloakBrowserError as exc:
        if _is_transient_profile_error(exc):
            _mark_profile_recovery_attempt(profile.id)
            raise CloakBrowserUnavailable(
                "指纹浏览器启动失败，已暂停自动重试，请稍后再试。"
            ) from exc
        raise
    return profile, running, launched_here


def _run_bridge_with_profile_recovery(
    platform: str,
    running: CloakBrowserProfile,
    payload: dict[str, Any],
    *,
    timeout_seconds: int | None = None,
    allow_profile_restart: bool = True,
) -> tuple[CloakBrowserProfile, bool, dict[str, Any]]:
    if allow_profile_restart and _profile_recovery_cooldown_active(running.id):
        raise CloakBrowserUnavailable(
            "指纹浏览器连接仍未恢复，profile 刚刚自动重启过，请稍后重试。"
        )
    try:
        return running, False, _run_bridge(payload, timeout_seconds=timeout_seconds)
    except CloakBrowserBridgeError as exc:
        if not _is_transient_bridge_error(exc) or not allow_profile_restart:
            raise

    _mark_profile_recovery_attempt(running.id)
    try:
        stop_profile(running.id)
        _recovered_profile, recovered_running, _launched = _ensure_running_profile(
            platform,
            running.id,
            allow_recovery_restart=True,
        )
        retry_payload = dict(payload)
        retry_payload["cdp_url"] = (
            f"{_configured_url()}/api/profiles/{recovered_running.id}/cdp"
        )
        result = _run_bridge(retry_payload, timeout_seconds=timeout_seconds)
    except CloakBrowserBridgeError as retry_exc:
        if not _is_transient_bridge_error(retry_exc):
            _clear_profile_recovery_attempt(running.id)
            raise
        raise CloakBrowserUnavailable(
            "指纹浏览器连接异常，自动重启 profile 后仍未恢复，请稍后重试。"
        ) from retry_exc
    except CloakBrowserError as recovery_exc:
        raise CloakBrowserUnavailable(
            "指纹浏览器连接异常，自动重启 profile 失败，请稍后重试。"
        ) from recovery_exc

    _clear_profile_recovery_attempt(running.id)
    return recovered_running, True, result


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


def _bridge_payload(
    profile_id: str,
    *,
    action: str,
    cookies: list[dict[str, Any]] | None = None,
    target_url: str = "",
    model_url: str = "",
    instance_id: str = "",
    platform: str = "",
) -> dict[str, Any]:
    return {
        "action": action,
        "cdp_url": f"{_configured_url()}/api/profiles/{profile_id}/cdp",
        "auth_token": _auth_token(),
        "cookies": cookies or [],
        "target_url": str(target_url or "").strip(),
        "model_url": str(model_url or "").strip(),
        "instance_id": str(instance_id or "").strip(),
        "platform": normalize_platform(platform) if platform else "",
        "navigation_timeout_ms": max(_timeout_seconds() * 1000, 15000),
        "authorization_timeout_ms": AUTHORIZATION_TIMEOUT_SECONDS * 1000,
        "auto_verify_3mf": _env_bool("MAKERHUB_AUTO_VERIFY_3MF", False),
    }


def browser_fetch(
    platform: str,
    url: str,
    *,
    profile_id: str = "",
    headers: dict[str, str] | None = None,
    cookie_items: list[dict[str, Any]] | None = None,
    timeout_seconds: int | None = None,
) -> CloakBrowserFetchResult:
    clean_platform = normalize_platform(platform)
    clean_url = _validate_browser_fetch_url(url, clean_platform)
    try:
        operation_timeout = int(timeout_seconds or _timeout_seconds())
    except (TypeError, ValueError):
        operation_timeout = _timeout_seconds()
    operation_timeout = max(min(operation_timeout, 180), 5)
    clean_profile_id = str(profile_id or "").strip()

    with _profile_operation(clean_platform, clean_profile_id, detail="fetch"):
        if clean_profile_id:
            running = CloakBrowserProfile(
                id=clean_profile_id,
                name=PROFILE_NAMES[clean_platform],
                status="running",
                cdp_url=f"{_configured_url()}/api/profiles/{clean_profile_id}/cdp",
            )
        else:
            _profile, running, _launched_here = _ensure_running_profile(
                clean_platform,
                clean_profile_id,
            )
        payload = _bridge_payload(
            running.id,
            action="fetch",
            cookies=_normalize_structured_cookie_items(cookie_items, clean_platform),
            target_url=clean_url,
            platform=clean_platform,
        )
        payload["headers"] = _safe_browser_fetch_headers(headers)
        payload["navigation_timeout_ms"] = operation_timeout * 1000
        try:
            running, _restarted, result = _run_bridge_with_profile_recovery(
                clean_platform,
                running,
                payload,
                timeout_seconds=max(operation_timeout + 30, operation_timeout * 2),
                allow_profile_restart=False,
            )
        except CloakBrowserBridgeError:
            if not clean_profile_id:
                raise
            _profile, running, _launched_here = _ensure_running_profile(
                clean_platform,
                clean_profile_id,
            )
            payload["cdp_url"] = f"{_configured_url()}/api/profiles/{running.id}/cdp"
            running, _restarted, result = _run_bridge_with_profile_recovery(
                clean_platform,
                running,
                payload,
                timeout_seconds=max(operation_timeout + 30, operation_timeout * 2),
                allow_profile_restart=False,
            )

    final_url = _validate_browser_fetch_url(str(result.get("url") or clean_url), clean_platform)
    try:
        status_code = max(int(result.get("status_code") or 0), 0)
    except (TypeError, ValueError):
        status_code = 0
    response_headers = result.get("headers") if isinstance(result.get("headers"), dict) else {}
    return CloakBrowserFetchResult(
        profile_id=running.id,
        url=final_url,
        status_code=status_code,
        content_type=str(result.get("content_type") or "")[:240],
        text=str(result.get("text") or ""),
        headers={
            str(name).lower(): str(value)[:1000]
            for name, value in response_headers.items()
            if str(name).lower() in {"content-type", "retry-after", "location"}
        },
    )


def _is_browser_three_mf_authorization_url(url: str, platform: str) -> bool:
    clean_platform = normalize_platform(platform)
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    return (
        parsed.scheme == "https"
        and _hostname_matches_domains(hostname, PLATFORM_DOMAINS[clean_platform])
        and path.startswith(("/v1/design-service/instance/", "/api/v1/design-service/instance/"))
        and path.endswith("/f3mf")
    )


def _browser_model_page_url(model_url: str, platform: str, instance_id: str) -> str:
    clean_model_url = str(model_url or "").strip()
    clean_instance_id = str(instance_id or "").strip()
    if not clean_model_url:
        raise CloakBrowserError("缺少 3MF 对应的模型页面。")
    try:
        parsed = urlparse(clean_model_url)
    except ValueError as exc:
        raise CloakBrowserError("3MF 对应的模型页面无效。") from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not _hostname_matches_domains(hostname, PLATFORM_DOMAINS[platform])
        or "/models/" not in parsed.path
    ):
        raise CloakBrowserError("3MF 对应的模型页面不属于当前平台。")
    base_url = parsed._replace(fragment="").geturl()
    return f"{base_url}#profileId-{clean_instance_id}" if clean_instance_id else base_url


def browser_authorize_3mf_download(
    platform: str,
    api_url: str,
    *,
    profile_id: str,
    model_url: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    """Use the saved browser profile to click one model instance's 3MF action.

    The bridge only returns a sanitized download payload. Browser cookies and tokens
    remain inside the profile and are never passed back to the archive request.
    """
    clean_platform = normalize_platform(platform)
    clean_profile_id = str(profile_id or "").strip()
    clean_api_url = str(api_url or "").strip()
    clean_instance_id = str(instance_id or "").strip()
    if not clean_profile_id:
        raise CloakBrowserUnavailable("当前平台未关联指纹浏览器 profile。")
    if not _is_browser_three_mf_authorization_url(clean_api_url, clean_platform):
        raise CloakBrowserError("3MF 浏览器授权地址无效。")
    page_url = _browser_model_page_url(model_url, clean_platform, clean_instance_id)

    with _profile_operation(clean_platform, clean_profile_id, detail="click"):
        for attempt in range(len(AUTHORIZATION_TRANSIENT_RETRY_DELAYS_SECONDS) + 1):
            try:
                _profile, running, _launched_here = _ensure_running_profile(
                    clean_platform,
                    clean_profile_id,
                    allow_recovery_restart=attempt > 0,
                )
                bridge_payload = _bridge_payload(
                    running.id,
                    action="click",
                    target_url=clean_api_url,
                    model_url=page_url,
                    instance_id=clean_instance_id,
                    platform=clean_platform,
                )
                running, _restarted, result = _run_bridge_with_profile_recovery(
                    clean_platform,
                    running,
                    bridge_payload,
                    timeout_seconds=_authorization_bridge_timeout_seconds(
                        bridge_payload["navigation_timeout_ms"]
                    ),
                    allow_profile_restart=False,
                )
                _clear_profile_recovery_attempt(running.id)
                break
            except CloakBrowserError as exc:
                if (
                    attempt >= len(AUTHORIZATION_TRANSIENT_RETRY_DELAYS_SECONDS)
                    or not _is_transient_profile_error(exc)
                ):
                    raise
                _mark_profile_recovery_attempt(clean_profile_id)
                time.sleep(AUTHORIZATION_TRANSIENT_RETRY_DELAYS_SECONDS[attempt])

    try:
        status_code = int(result.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    return {
        "status_code": max(status_code, 0),
        "payload": payload,
        "text": str(result.get("text") or "")[:4096],
        "verification": {
            key: verification[key]
            for key in (
                "attempted",
                "completed",
                "provider",
                "challenge_type",
                "attempts",
                "reason",
                "confidence",
            )
            if key in verification
        },
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
    with _profile_operation(clean_platform, profile_id, detail="synchronize"):
        _profile, running, launched_here = _ensure_running_profile(
            clean_platform,
            profile_id,
            proxy_config=proxy_config,
        )
        ticket_url = makerworld_ticket_url(clean_platform, raw_cookie, proxy_config)
        target_url = ticket_url or PLATFORM_ORIGINS[clean_platform]
        try:
            running, restarted, snapshot = _run_bridge_with_profile_recovery(
                clean_platform,
                running,
                _bridge_payload(
                    running.id,
                    action="seed",
                    cookies=browser_cookie_items(raw_cookie, clean_platform, structured_items),
                    target_url=target_url,
                    platform=clean_platform,
                )
            )
            launched_here = launched_here or restarted
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
    with _profile_operation(clean_platform, profile_id, detail="prepare-login"):
        _profile, running, launched_here = _ensure_running_profile(
            clean_platform,
            profile_id,
            proxy_config=proxy_config,
        )
        target_url = makerworld_ticket_url(clean_platform, raw_cookie, proxy_config) or browser_login_url(clean_platform)
        action = "seed" if raw_cookie and target_url != browser_login_url(clean_platform) else "login"
        running, restarted, snapshot = _run_bridge_with_profile_recovery(
            clean_platform,
            running,
            _bridge_payload(
                running.id,
                action=action,
                cookies=browser_cookie_items(raw_cookie, clean_platform) if raw_cookie else [],
                target_url=target_url,
                platform=clean_platform,
            )
        )
        launched_here = launched_here or restarted
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
    with _profile_operation(clean_platform, profile_id, detail="collect"):
        _profile, running, launched_here = _ensure_running_profile(clean_platform, profile_id)
        running, restarted, snapshot = _run_bridge_with_profile_recovery(
            clean_platform,
            running,
            _bridge_payload(
                running.id,
                action="sync",
                target_url=f"{PLATFORM_ORIGINS[clean_platform]}/zh",
                platform=clean_platform,
            )
        )
        launched_here = launched_here or restarted
        return CloakBrowserSessionResult(
            profile_id=running.id,
            cookie=_cookie_header_from_snapshot(snapshot, clean_platform),
            current_url=str(snapshot.get("current_url") or ""),
            public_url=cloakbrowser_public_url(),
            launched_here=launched_here,
            navigation_error=str(snapshot.get("navigation_error") or "")[:240],
        )
