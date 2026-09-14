from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from app.core.store import JsonStore
from app.core.settings import STATE_DIR
from app.services.resource_limiter import resource_slot
from app.services.cloakbrowser_session import (
    CloakBrowserBridgeError,
    CloakBrowserError,
    CloakBrowserUnavailable,
    browser_cookie_items,
    browser_fetch,
)
from app.services.three_mf import normalize_makerworld_source


class MakerWorldBrowserError(RuntimeError):
    pass


class MakerWorldBrowserJsonError(MakerWorldBrowserError):
    pass


_BROWSER_RETRY_DELAYS = (2, 5, 15, 30)


def _read_browser_retry(platform: str) -> dict:
    try:
        value = json.loads((STATE_DIR / f"browser_get_retry_{platform}.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _wait_browser_retry(platform: str) -> None:
    state = _read_browser_retry(platform)
    try:
        retry_at = float(state.get("retry_at") or 0)
        delay = min(max(retry_at - time.time(), 0), _BROWSER_RETRY_DELAYS[-1]) if math.isfinite(retry_at) else 0
    except (TypeError, ValueError):
        delay = 0
    if delay:
        time.sleep(delay)


def wait_for_makerworld_browser_retry(url: str) -> None:
    platform = normalize_makerworld_source(url=url)
    if platform in {"cn", "global"}:
        with resource_slot(f"makerworld_browser_get_{platform}"):
            _wait_browser_retry(platform)


def _record_browser_result(platform: str, *, failed: bool) -> None:
    path = STATE_DIR / f"browser_get_retry_{platform}.json"
    if not failed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    previous = _read_browser_retry(platform)
    try:
        failures = max(0, int(previous.get("failures") or 0))
    except (TypeError, ValueError):
        failures = 0
    failures = min(failures + 1, len(_BROWSER_RETRY_DELAYS))
    value = {"failures": failures, "retry_at": time.time() + _BROWSER_RETRY_DELAYS[failures - 1]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


@dataclass(frozen=True)
class MakerWorldBrowserResponse:
    url: str
    status_code: int
    content_type: str
    text: str
    profile_id: str
    headers: dict[str, str] = field(default_factory=dict)


def _url_with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parsed = urlparse(str(url or ""))
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((str(key), str(item)) for item in value if item is not None)
        else:
            pairs.append((str(key), str(value)))
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


def _linked_profile(platform: str) -> tuple[str, bool, Any]:
    config = JsonStore().load()
    proxy_config = getattr(config, "proxy", None)
    for item in config.cookies:
        if str(getattr(item, "platform", "") or "").strip().lower() != platform:
            continue
        profile_id = str(getattr(item, "browser_profile_id", "") or "").strip()
        return profile_id, bool(profile_id), proxy_config
    return "", False, proxy_config


def _headers_for_profile(
    headers: dict[str, str] | None,
    *,
    linked: bool,
) -> dict[str, str]:
    if not linked:
        return dict(headers or {})
    stale_auth_headers = {
        "authorization",
        "cookie",
        "token",
        "user-agent",
        "x-access-token",
        "x-token",
    }
    return {
        str(name): str(value)
        for name, value in (headers or {}).items()
        if value is not None and str(name).strip().lower() not in stale_auth_headers
    }


def _is_retryable_browser_error(exc: CloakBrowserError) -> bool:
    detail = str(exc or "").lower()
    if isinstance(exc, CloakBrowserBridgeError):
        return any(
            marker in detail
            for marker in (
                "超时",
                "timeout",
                "timed out",
                "disconnected",
                "connection closed",
                "target closed",
                "websocket",
            )
        )
    if not isinstance(exc, CloakBrowserUnavailable):
        return False
    return bool(
        re.search(r"http\s+5\d\d", detail)
        or "连接指纹浏览器失败" in detail
        or "connection" in detail
        or "timeout" in detail
        or "timed out" in detail
    )


def _safe_browser_error(exc: CloakBrowserError) -> MakerWorldBrowserError:
    detail = str(exc or "")
    status_match = re.search(r"http\s+(\d{3})", detail, flags=re.I)
    if status_match:
        return MakerWorldBrowserError(
            f"CloakBrowser 返回 HTTP {status_match.group(1)}。"
        )
    lowered = detail.lower()
    if "超时" in detail or "timeout" in lowered or "timed out" in lowered:
        return MakerWorldBrowserError("CloakBrowser 请求超时。")
    if any(
        marker in lowered
        for marker in (
            "connection",
            "disconnected",
            "target closed",
            "websocket",
        )
    ) or "连接指纹浏览器失败" in detail:
        return MakerWorldBrowserError("CloakBrowser 连接中断。")
    if isinstance(exc, CloakBrowserUnavailable):
        return MakerWorldBrowserError("CloakBrowser 服务暂时不可用。")
    return MakerWorldBrowserError("CloakBrowser 请求失败。")


def makerworld_browser_get(
    url: str,
    *,
    raw_cookie: str = "",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    session: requests.Session | None = None,
    platform: str = "",
    timeout_seconds: int | None = None,
) -> MakerWorldBrowserResponse:
    del session  # 保留旧调用形状；登录态由持久化浏览器 profile 持有。
    target_url = _url_with_params(url, params)
    clean_platform = normalize_makerworld_source(source=platform, url=target_url)
    if clean_platform not in {"cn", "global"}:
        raise MakerWorldBrowserError("无法识别 MakerWorld 请求所属平台。")
    try:
        profile_id, linked, proxy_config = _linked_profile(clean_platform)
    except Exception as exc:
        raise MakerWorldBrowserError("读取 MakerWorld 浏览器配置失败。") from exc
    cookie_items = [] if linked else browser_cookie_items(raw_cookie, clean_platform)
    request_headers = _headers_for_profile(headers, linked=linked)

    with resource_slot(f"makerworld_browser_get_{clean_platform}"):
        for attempt in range(2):
            _wait_browser_retry(clean_platform)
            try:
                result = browser_fetch(
                    clean_platform,
                    target_url,
                    profile_id=profile_id,
                    proxy_config=proxy_config,
                    headers=request_headers,
                    cookie_items=cookie_items,
                    timeout_seconds=timeout_seconds,
                )
            except CloakBrowserError as exc:
                retryable = _is_retryable_browser_error(exc)
                _record_browser_result(clean_platform, failed=retryable)
                if attempt == 0 and retryable:
                    continue
                raise _safe_browser_error(exc) from exc
            _record_browser_result(clean_platform, failed=result.status_code >= 500)
            if result.status_code >= 500 and attempt == 0:
                continue
            return MakerWorldBrowserResponse(
                url=result.url,
                status_code=result.status_code,
                content_type=result.content_type,
                text=result.text,
                profile_id=result.profile_id,
                headers=dict(result.headers),
            )

    raise MakerWorldBrowserError("CloakBrowser 请求失败。")


def makerworld_browser_get_text(url: str, **kwargs: Any) -> str:
    return makerworld_browser_get(url, **kwargs).text


def makerworld_browser_get_json(
    url: str,
    *,
    allow_non_json: bool = False,
    **kwargs: Any,
) -> Any:
    response = makerworld_browser_get(url, **kwargs)
    raw = str(response.text or "").strip()
    if not raw:
        if allow_non_json:
            return None
        raise MakerWorldBrowserJsonError("CloakBrowser 响应为空。")
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        if allow_non_json:
            return None
        raise MakerWorldBrowserJsonError(
            "CloakBrowser 响应不是有效 JSON。"
        ) from exc
