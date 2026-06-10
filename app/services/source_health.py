from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import requests

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.timezone import now as china_now, parse_datetime
from app.services.cookie_utils import extract_auth_token, sanitize_cookie_header
from app.services.proxy_policy import effective_proxy_cache_state, proxy_mapping
from app.services.scrapling_fetch import fetch_text as scrapling_fetch_text, scrapling_only
from app.services.state_events import publish_state_event
from app.services.three_mf import (
    describe_three_mf_failure,
    merge_three_mf_failure,
    normalize_makerworld_source,
    normalize_three_mf_failure_state,
    three_mf_failure_priority,
)


THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
THREE_MF_LIMIT_GUARD_KEY = "three_mf_limit_guard"
SOURCE_HEALTH_CACHE_TTL_SECONDS = 60
SOURCE_HEALTH_STALE_TTL_SECONDS = 10 * 60
MW_BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "X-BBL-Client-Type": "web",
    "X-BBL-Client-Version": "00.00.00.01",
    "X-BBL-App-Source": "makerworld",
    "X-BBL-Client-Name": "MakerWorld",
}
MW_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
PLATFORM_ORIGINS = {
    "cn": "https://makerworld.com.cn",
    "global": "https://makerworld.com",
}
AUTH_PROBES = {
    "cn": (
        ("消息计数", "https://api.bambulab.cn/v1/user-service/my/message/count"),
        ("个人偏好", "https://api.bambulab.cn/v1/design-user-service/my/preference"),
    ),
    "global": (
        ("消息计数", "https://api.bambulab.com/v1/user-service/my/message/count"),
        ("个人偏好", "https://api.bambulab.com/v1/design-user-service/my/preference"),
    ),
}
WEB_PROBES = {
    "cn": "https://makerworld.com.cn/zh",
    "global": "https://makerworld.com/zh",
}
SOURCE_HEALTH_LABELS = {
    "cn": "国内站",
    "global": "国际站",
}
SOURCE_HEALTH_CACHE_LOCK = threading.RLock()
SOURCE_HEALTH_CACHE: dict[str, dict[str, Any]] = {}
SOURCE_HEALTH_REFRESHING_KEYS: set[str] = set()


def _make_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            **MW_BROWSER_HEADERS,
            "User-Agent": MW_BROWSER_USER_AGENT,
        }
    )
    return session


def _build_proxy_mapping(
    proxy_config: Any,
    target_url: str = "",
    *,
    platform: str = "",
    allow_domestic_proxy: bool = False,
) -> dict[str, str]:
    return proxy_mapping(proxy_config, target_url, platform=platform, allow_domestic_proxy=allow_domestic_proxy)


def _looks_like_html(text: str) -> bool:
    if not text:
        return False
    lowered = text.lstrip()[:200].lower()
    return lowered.startswith("<!doctype html") or "<html" in lowered


def _contains_verification_markers(text: str) -> bool:
    lowered = str(text or "").lower()
    markers = (
        "cf-browser-verification",
        "cf-chl",
        "__cf_bm",
        "cf_clearance",
        "captcha",
        "cloudflare",
        "security check",
        "verify you are human",
        "verification required",
        "challenge-platform",
    )
    return any(marker in lowered for marker in markers)


def _html_failure_kind(text: str) -> str:
    return "verification_required" if _contains_verification_markers(text) else "html_response"


def _auth_probe_result_from_response(
    *,
    name: str,
    url: str,
    status_code: int,
    text: str,
    headers: Any,
    elapsed_ms: float,
    engine: str = "",
) -> dict[str, Any]:
    preview = (text or "")[:240]
    looks_like_html = _looks_like_html(preview)
    verification_marker = _contains_verification_markers(preview)
    status_value = int(status_code or 0)
    ok = status_value > 0 and status_value < 400 and not looks_like_html
    result = {
        "target": name,
        "url": url,
        "ok": ok,
        "status_code": status_value,
        "elapsed_ms": elapsed_ms,
        "content_type": str((headers or {}).get("content-type") or "").lower()[:80],
    }
    if engine:
        result["engine"] = engine
    if not ok:
        if looks_like_html:
            result["failure_kind"] = _html_failure_kind(preview)
            result["error"] = (
                "返回了验证页面，通常表示需要完成网页验证。"
                if verification_marker
                else "认证探针返回了网页页面，可能是站点接口改版、代理跳转或登录页；请优先看其它认证接口是否成功。"
            )
        elif status_code in {401, 403}:
            result["failure_kind"] = "auth_required"
            result["error"] = f"接口返回状态码 {status_code}。"
        else:
            result["failure_kind"] = "http_error"
            result["error"] = f"接口返回状态码 {status_code}。"
    return result


def _safe_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text[:240]


def _base_limit_guard() -> dict[str, Any]:
    return {
        "active": False,
        "limited_until": "",
        "last_hit_at": "",
        "message": "",
        "reason": "",
        "model_id": "",
        "model_url": "",
        "instance_id": "",
    }


def _limit_guard_now(reference: datetime | None = None) -> datetime:
    return china_now()


def _parse_limit_guard_time(value: str) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _write_limit_guard(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    current = _base_limit_guard()
    current.update(load_database_json_state(THREE_MF_LIMIT_GUARD_KEY, current))
    current.update(
        {
            "active": bool(payload.get("active", current.get("active"))),
            "limited_until": str(payload.get("limited_until", current.get("limited_until")) or ""),
            "last_hit_at": str(payload.get("last_hit_at", current.get("last_hit_at")) or ""),
            "message": str(payload.get("message", current.get("message")) or ""),
            "reason": str(payload.get("reason", current.get("reason")) or ""),
            "model_id": str(payload.get("model_id", current.get("model_id")) or ""),
            "model_url": str(payload.get("model_url", current.get("model_url")) or ""),
            "instance_id": str(payload.get("instance_id", current.get("instance_id")) or ""),
        }
    )
    return save_database_json_state(THREE_MF_LIMIT_GUARD_KEY, current)


def _read_limit_guard() -> dict[str, Any]:
    ensure_app_dirs()
    state = _base_limit_guard()
    state.update(load_database_json_state(THREE_MF_LIMIT_GUARD_KEY, state))

    if bool(state.get("active")):
        limited_until = str(state.get("limited_until") or "").strip()
        parsed_until = _parse_limit_guard_time(limited_until)
        if parsed_until is None:
            return _write_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
        if parsed_until <= _limit_guard_now(parsed_until):
            return _write_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
    return state


def _limit_guard_for_platform(platform: str) -> dict[str, Any]:
    state = _read_limit_guard()
    if not bool(state.get("active")):
        return {}
    source = normalize_makerworld_source(url=state.get("model_url"))
    if source == platform:
        return state
    return {}


def _limit_guard_message(state: dict[str, Any]) -> str:
    source = normalize_makerworld_source(url=state.get("model_url"))
    base_message = str(state.get("message") or "").strip() or describe_three_mf_failure(
        "download_limited",
        source=source,
    )
    if "自动重试暂停至" in base_message:
        base_message = base_message.split("自动重试暂停至", 1)[0].rstrip("，,。 ")
    limited_until = str(state.get("limited_until") or "").strip()
    if not limited_until:
        return base_message
    parsed_until = _parse_limit_guard_time(limited_until)
    if parsed_until is None:
        return base_message
    until_text = parsed_until.strftime("%Y-%m-%d %H:%M")
    return f"{base_message.rstrip('。')}，自动重试暂停至 {until_text}。"


def _build_request_headers(origin: str, raw_cookie: str) -> dict[str, str]:
    cookie_header = sanitize_cookie_header(raw_cookie)
    headers = {
        "Referer": f"{origin}/",
        "Origin": origin,
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
        auth_token = extract_auth_token(cookie_header)
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            headers["token"] = auth_token
            headers["X-Token"] = auth_token
            headers["X-Access-Token"] = auth_token
    return headers


def _classify_auth_probe_result(result: dict[str, Any]) -> str:
    if bool(result.get("ok")):
        return "ok"
    failure_kind = str(result.get("failure_kind") or "").strip()
    if failure_kind:
        return failure_kind
    lowered = str(result.get("error") or "").lower()
    if _contains_verification_markers(lowered):
        return "verification_required"
    if "html" in lowered or "网页页面" in lowered or "登录页" in lowered:
        return "html_response"
    status_code = int(result.get("status_code") or 0)
    if status_code in {401, 403}:
        return "auth_required"
    return "http_error"


def _platform_cookie_label(platform: str) -> str:
    return "国际" if str(platform or "").strip() == "global" else "国内"


def _build_cookie_auth_message(platform: str, payload: dict[str, Any]) -> str:
    platform_label = _platform_cookie_label(platform)
    success_count = int(payload.get("success_count") or 0)
    target_count = int(payload.get("target_count") or 0)
    state = str(payload.get("state") or "")
    if target_count > 0 and success_count == target_count:
        return f"{platform_label}账号可用，Cookie 已保存。"
    if success_count > 0:
        return f"{platform_label}账号已保存，部分账号信息暂时读取失败；可以点击同步重试。"
    if state == "missing_cookie":
        return f"请先填写{platform_label} Cookie。"
    if state == "verification_required":
        return "MakerWorld 需要验证，前往官网任意下载一个模型。"
    if state == "auth_required":
        return f"{platform_label} Cookie 失效，请重新获取并保存 Cookie。"
    if state == "html_response":
        return f"{platform_label}账号已保存，但暂时无法读取账号信息；可以点击同步重试。"
    if state == "download_limited":
        return f"{platform_label}站已到达 3MF 每日下载上限，过零点后会自动恢复。"
    return f"{platform_label}账号测试失败，暂时无法确认 Cookie 是否可用。"


def _empty_cookie_auth_payload(platform: str, state: str, status: str, detail: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "platform": platform,
        "state": state,
        "status": status,
        "detail": detail,
        "results": [],
        "success_count": 0,
        "target_count": 0,
        "used_proxy": False,
    }
    payload["message"] = _build_cookie_auth_message(platform, payload)
    return payload


def _probe_auth_endpoints(
    platform: str,
    raw_cookie: str,
    proxy_config: Any,
    *,
    allow_domestic_proxy: bool = False,
) -> dict[str, Any]:
    probes = AUTH_PROBES.get(platform) or ()
    if not probes:
        return _empty_cookie_auth_payload(platform, "http_error", "连接异常", "缺少认证探针配置。")

    session = _make_session()
    proxies = _build_proxy_mapping(proxy_config, platform=platform, allow_domestic_proxy=allow_domestic_proxy)
    headers = _build_request_headers(PLATFORM_ORIGINS.get(platform, ""), raw_cookie)
    states: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        for name, url in probes:
            started = time.perf_counter()
            try:
                scrapling_result = scrapling_fetch_text(
                    headers=headers,
                    proxy_config=proxy_config,
                    raw_cookie=raw_cookie,
                    timeout=12,
                    url=url,
                    expect_json=True,
                    allow_domestic_proxy=allow_domestic_proxy,
                )
                if scrapling_result.ok:
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                    result = _auth_probe_result_from_response(
                        name=name,
                        url=url,
                        status_code=int(scrapling_result.status_code or 0),
                        text=scrapling_result.text or "",
                        headers=scrapling_result.headers or {},
                        elapsed_ms=elapsed_ms,
                        engine=scrapling_result.engine,
                    )
                    results.append(result)
                    states.append(_classify_auth_probe_result(result))
                    continue
                if scrapling_only():
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                    result = _auth_probe_result_from_response(
                        name=name,
                        url=url,
                        status_code=int(scrapling_result.status_code or 0),
                        text=scrapling_result.text or "",
                        headers=scrapling_result.headers or {},
                        elapsed_ms=elapsed_ms,
                        engine=scrapling_result.engine,
                    )
                    if not result.get("error") or result.get("failure_kind") == "http_error":
                        result["error"] = scrapling_result.error or "Scrapling 抓取失败。"
                    results.append(result)
                    states.append(_classify_auth_probe_result(result))
                    continue
                response = session.get(
                    url,
                    headers=headers,
                    proxies=proxies or None,
                    timeout=(6, 12),
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                result = _auth_probe_result_from_response(
                    name=name,
                    url=url,
                    status_code=int(response.status_code),
                    text=response.text or "",
                    headers=response.headers,
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
                result = {
                    "target": name,
                    "url": url,
                    "ok": False,
                    "elapsed_ms": elapsed_ms,
                    "failure_kind": "http_error",
                    "error": _safe_error_message(exc),
                }
            results.append(result)
            states.append(_classify_auth_probe_result(result))
    finally:
        session.close()

    success_count = sum(1 for item in results if item.get("ok"))
    if "ok" in states:
        payload = {
            "ok": True,
            "platform": platform,
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }
    elif "verification_required" in states:
        payload = {
            "ok": False,
            "platform": platform,
            "state": "verification_required",
            "status": "需要验证",
            "detail": "",
        }
    elif "auth_required" in states:
        payload = {
            "ok": False,
            "platform": platform,
            "state": "auth_required",
            "status": "Cookie 失效",
            "detail": "",
        }
    elif "html_response" in states:
        payload = {
            "ok": False,
            "platform": platform,
            "state": "html_response",
            "status": "接口受限",
            "detail": "认证接口返回了登录页或网页页面，但未检测到验证码/风控标记。",
        }
    else:
        payload = {
            "ok": False,
            "platform": platform,
            "state": "http_error",
            "status": "连接异常",
            "detail": "认证接口暂时不可用。",
        }

    payload.update(
        {
            "results": results,
            "success_count": success_count,
            "target_count": len(results),
            "used_proxy": bool(proxies),
        }
    )
    payload["message"] = _build_cookie_auth_message(platform, payload)
    return payload


def _cache_key(
    kind: str,
    platform: str,
    raw_cookie: str,
    proxy_config: Any,
    *,
    allow_domestic_proxy: bool = False,
) -> str:
    proxy_state = json.dumps(
        effective_proxy_cache_state(proxy_config, platform=platform, allow_domestic_proxy=allow_domestic_proxy),
        ensure_ascii=False,
        sort_keys=True,
    )
    cookie_hash = hashlib.sha1(str(raw_cookie or "").encode("utf-8", errors="ignore")).hexdigest()
    clean_kind = str(kind or "account").strip().lower() or "account"
    return f"{clean_kind}:{platform}:{cookie_hash}:{proxy_state}"


def _cached_source_health_payload(cache_key: str, *, max_age_seconds: float) -> dict[str, Any] | None:
    now = time.time()
    with SOURCE_HEALTH_CACHE_LOCK:
        cached = SOURCE_HEALTH_CACHE.get(cache_key)
        if not cached:
            return None
        age_seconds = now - float(cached.get("checked_at") or 0)
        if age_seconds < 0 or age_seconds > max_age_seconds:
            return None
        payload = dict(cached.get("payload") or {})
    payload["cached"] = True
    payload["cache_age_seconds"] = max(0, int(age_seconds))
    return payload


def _save_source_health_payload(cache_key: str, payload: dict[str, Any]) -> None:
    with SOURCE_HEALTH_CACHE_LOCK:
        SOURCE_HEALTH_CACHE[cache_key] = {
            "checked_at": time.time(),
            "payload": dict(payload or {}),
        }


def _async_refresh_source_health(
    cache_key: str,
    kind: str,
    platform: str,
    raw_cookie: str,
    proxy_config: Any,
) -> None:
    def _refresh() -> None:
        try:
            try:
                payload = (
                    _probe_platform_web_page(platform, raw_cookie, proxy_config)
                    if str(kind or "").strip().lower() == "web"
                    else _probe_auth_endpoints(platform, raw_cookie, proxy_config)
                )
            except Exception as exc:
                payload = {
                    "ok": False,
                    "platform": platform,
                    "state": "http_error",
                    "status": "连接异常",
                    "detail": _safe_error_message(exc),
                }
            _save_source_health_payload(cache_key, payload)
            publish_state_event(
                "dashboard",
                "state.changed",
                {"reason": "source_health_refreshed", "kind": kind, "platform": platform},
            )
        finally:
            with SOURCE_HEALTH_CACHE_LOCK:
                SOURCE_HEALTH_REFRESHING_KEYS.discard(cache_key)

    with SOURCE_HEALTH_CACHE_LOCK:
        if cache_key in SOURCE_HEALTH_REFRESHING_KEYS:
            return
        SOURCE_HEALTH_REFRESHING_KEYS.add(cache_key)
    thread = threading.Thread(target=_refresh, name=f"makerhub-source-health-{platform}", daemon=True)
    thread.start()


def _checking_source_health_payload(platform: str) -> dict[str, Any]:
    return _empty_cookie_auth_payload(
        platform,
        "checking",
        "检测中",
        "正在后台刷新源站状态，首页先使用其它快照数据。",
    )


def _web_probe_payload_from_response(
    *,
    platform: str,
    url: str,
    status_code: int,
    text: str,
    headers: Any,
    elapsed_ms: float,
    engine: str = "",
) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    preview = (text or "")[:2000]
    result = {
        "target": "网页入口",
        "url": url,
        "ok": False,
        "status_code": int(status_code or 0),
        "elapsed_ms": elapsed_ms,
        "content_type": str((headers or {}).get("content-type") or "").lower()[:80],
    }
    if engine:
        result["engine"] = engine

    if _contains_verification_markers(preview):
        result["failure_kind"] = "verification_required"
        result["error"] = "网页入口返回验证页面。"
        return {
            "ok": False,
            "platform": platform_key,
            "state": "verification_required",
            "status": "需要验证",
            "detail": "MakerWorld 网页入口返回验证页，请前往官网手动完成验证。",
            "results": [result],
        }

    if int(status_code or 0) in {401, 403}:
        result["failure_kind"] = "auth_required"
        result["error"] = f"网页入口返回状态码 {int(status_code or 0)}。"
        return {
            "ok": False,
            "platform": platform_key,
            "state": "auth_required",
            "status": "Cookie 失效",
            "detail": "MakerWorld 网页入口拒绝当前 Cookie，请重新获取并保存 Cookie。",
            "results": [result],
        }

    if int(status_code or 0) and int(status_code or 0) < 400:
        result["ok"] = True
        return {
            "ok": True,
            "platform": platform_key,
            "state": "ok",
            "status": "访问正常",
            "detail": "",
            "results": [result],
        }

    result["failure_kind"] = "http_error"
    result["error"] = f"网页入口返回状态码 {int(status_code or 0)}。"
    return {
        "ok": False,
        "platform": platform_key,
        "state": "http_error",
        "status": "连接异常",
        "detail": result["error"],
        "results": [result],
    }


def _probe_platform_web_page(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    url = WEB_PROBES.get(platform_key) or PLATFORM_ORIGINS.get(platform_key, "")
    if not url:
        return _empty_cookie_auth_payload(platform_key, "http_error", "连接异常", "缺少网页探针配置。")

    session = _make_session()
    headers = _build_request_headers(PLATFORM_ORIGINS.get(platform_key, ""), raw_cookie)
    proxies = _build_proxy_mapping(proxy_config, url, platform=platform_key)
    started = time.perf_counter()
    try:
        scrapling_result = scrapling_fetch_text(
            headers=headers,
            proxy_config=proxy_config,
            raw_cookie=raw_cookie,
            timeout=10,
            url=url,
            expect_json=False,
        )
        should_use_scrapling_result = (
            scrapling_result.ok
            or scrapling_only()
            or (
                int(scrapling_result.status_code or 0) in {401, 403}
                and (scrapling_result.text or scrapling_result.error)
            )
            or _contains_verification_markers(scrapling_result.text or "")
        )
        if should_use_scrapling_result:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            payload = _web_probe_payload_from_response(
                platform=platform_key,
                url=url,
                status_code=int(scrapling_result.status_code or 0),
                text=scrapling_result.text or "",
                headers=scrapling_result.headers or {},
                elapsed_ms=elapsed_ms,
                engine=scrapling_result.engine,
            )
            if payload.get("state") == "http_error" and scrapling_result.error:
                payload["detail"] = scrapling_result.error
                payload["results"][0]["error"] = scrapling_result.error
            return payload
        else:
            response = session.get(
                url,
                headers=headers,
                proxies=proxies or None,
                timeout=(5, 10),
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            return _web_probe_payload_from_response(
                platform=platform_key,
                url=url,
                status_code=int(response.status_code),
                text=response.text or "",
                headers=response.headers,
                elapsed_ms=elapsed_ms,
            )
    except Exception as exc:
        return {
            "ok": False,
            "platform": platform_key,
            "state": "http_error",
            "status": "连接异常",
            "detail": _safe_error_message(exc),
            "results": [{
                "target": "网页入口",
                "url": url,
                "ok": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "failure_kind": "http_error",
                "error": _safe_error_message(exc),
            }],
        }
    finally:
        session.close()


def probe_cookie_auth_status(
    platform: str,
    raw_cookie: str,
    proxy_config: Any,
    *,
    include_limit_guard: bool = False,
    use_cache: bool = False,
    allow_domestic_proxy: bool = False,
) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    normalized_cookie = sanitize_cookie_header(raw_cookie)
    if not normalized_cookie:
        return _empty_cookie_auth_payload(platform_key, "missing_cookie", "未配置 Cookie", "还没有保存对应站点的 Cookie。")

    if include_limit_guard:
        limit_guard = _limit_guard_for_platform(platform_key)
        if limit_guard:
            return _empty_cookie_auth_payload(
                platform_key,
                "download_limited",
                "到达每日上限",
                _limit_guard_message(limit_guard),
            )

    cache_key = _cache_key("account", platform_key, normalized_cookie, proxy_config, allow_domestic_proxy=allow_domestic_proxy)
    if use_cache:
        cached_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_CACHE_TTL_SECONDS)
        if cached_payload:
            return cached_payload

    payload = _probe_auth_endpoints(
        platform_key,
        normalized_cookie,
        proxy_config,
        allow_domestic_proxy=allow_domestic_proxy,
    )

    if use_cache:
        _save_source_health_payload(cache_key, payload)
    return dict(payload)


def _probe_platform_status(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    return probe_cookie_auth_status(
        platform,
        raw_cookie,
        proxy_config,
        include_limit_guard=True,
        use_cache=True,
    )


def _probe_platform_status_snapshot(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    normalized_cookie = sanitize_cookie_header(raw_cookie)
    if not normalized_cookie:
        return _empty_cookie_auth_payload(platform_key, "missing_cookie", "未配置 Cookie", "还没有保存对应站点的 Cookie。")

    limit_guard = _limit_guard_for_platform(platform_key)
    if limit_guard:
        return _empty_cookie_auth_payload(
            platform_key,
            "download_limited",
            "到达每日上限",
            _limit_guard_message(limit_guard),
        )

    cache_key = _cache_key("account", platform_key, normalized_cookie, proxy_config)
    fresh_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_CACHE_TTL_SECONDS)
    if fresh_payload:
        return fresh_payload

    stale_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_STALE_TTL_SECONDS)
    _async_refresh_source_health(cache_key, "account", platform_key, normalized_cookie, proxy_config)
    if stale_payload:
        stale_payload["stale"] = True
        return stale_payload
    return _checking_source_health_payload(platform_key)


def _probe_platform_web_status(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    cache_key = _cache_key("web", platform_key, sanitize_cookie_header(raw_cookie), proxy_config)
    cached_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_CACHE_TTL_SECONDS)
    if cached_payload:
        return cached_payload
    payload = _probe_platform_web_page(platform_key, raw_cookie, proxy_config)
    _save_source_health_payload(cache_key, payload)
    return dict(payload)


def _probe_platform_web_status_snapshot(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    normalized_cookie = sanitize_cookie_header(raw_cookie)
    cache_key = _cache_key("web", platform_key, normalized_cookie, proxy_config)
    fresh_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_CACHE_TTL_SECONDS)
    if fresh_payload:
        return fresh_payload

    stale_payload = _cached_source_health_payload(cache_key, max_age_seconds=SOURCE_HEALTH_STALE_TTL_SECONDS)
    _async_refresh_source_health(cache_key, "web", platform_key, normalized_cookie, proxy_config)
    if stale_payload:
        stale_payload["stale"] = True
        return stale_payload
    return {}


def _recent_remote_refresh_health(platform: str, remote_refresh_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(remote_refresh_state, dict):
        return {}
    platform_key = "global" if str(platform or "").strip() == "global" else "cn"
    success_count = 0
    failure_count = 0
    verification_failures = 0
    for raw_item in remote_refresh_state.get("recent_items") or []:
        if not isinstance(raw_item, dict):
            continue
        item_url = str(raw_item.get("url") or "").strip()
        if normalize_makerworld_source(url=item_url) != platform_key:
            continue
        status = str(raw_item.get("status") or "").strip()
        if status in {"success", "source_deleted"}:
            success_count += 1
        elif status == "failed":
            failure_count += 1
            state = normalize_three_mf_failure_state(
                "missing",
                raw_item.get("message") or "",
                url=item_url,
            )
            if state in {"verification_required", "cloudflare"}:
                verification_failures += 1
    total = success_count + failure_count
    success_rate = (success_count / total) if total else 0.0
    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "verification_failures": verification_failures,
        "total": total,
        "success_rate": success_rate,
    }


def _soften_probe_verification_with_remote_health(
    platform: str,
    state: str,
    status: str,
    detail: str,
    tone: str,
    remote_refresh_state: dict[str, Any] | None,
) -> tuple[str, str, str, str]:
    if state not in {"verification_required", "cloudflare", "auth_required"}:
        return state, status, detail, tone
    health = _recent_remote_refresh_health(platform, remote_refresh_state)
    success_count = int(health.get("success_count") or 0)
    total = int(health.get("total") or 0)
    success_rate = float(health.get("success_rate") or 0)
    if success_count < 5 or total < 5 or success_rate < 0.6:
        return state, status, detail, tone
    softened_state = "probe_limited"
    softened_status = "部分受限"
    softened_detail = (
        f"认证探针返回验证页，但最近源端刷新同站点 {success_count}/{total} 个成功，"
        "归档能力仍可用；如需下载 3MF 或刷新失败项，再前往官网验证。"
    )
    return softened_state, softened_status, softened_detail, "warning"


def _build_missing_3mf_overrides(items: list[dict[str, Any]] | None) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    limit_guards: dict[str, dict[str, Any]] = {}
    for raw_item in items or []:
        if not isinstance(raw_item, dict):
            continue
        state = normalize_three_mf_failure_state(
            raw_item.get("status") or raw_item.get("downloadState") or "",
            raw_item.get("message") or raw_item.get("downloadMessage") or "",
            url=raw_item.get("model_url") or raw_item.get("url") or "",
        )
        if state != "download_limited":
            continue
        platform = normalize_makerworld_source(url=raw_item.get("model_url"))
        if platform not in {"cn", "global"}:
            continue
        if state == "download_limited":
            if platform not in limit_guards:
                limit_guards[platform] = _limit_guard_for_platform(platform)
            limit_guard = limit_guards.get(platform) or {}
            if not limit_guard:
                continue
            limit_message = _limit_guard_message(limit_guard)
        else:
            limit_message = ""
        message = describe_three_mf_failure(
            state,
            raw_item.get("message") or raw_item.get("downloadMessage") or "",
            url=raw_item.get("model_url") or "",
            limit_message=limit_message,
        )
        overrides[platform] = merge_three_mf_failure(
            overrides.get(platform),
            {
                "state": state,
                "message": message,
            },
        )
    return overrides


def _status_text_from_failure_state(state: str) -> str:
    if state == "checking":
        return "检测中"
    if state == "download_limited":
        return "到达每日上限"
    if state in {"verification_required", "cloudflare"}:
        return "需要验证"
    if state == "auth_required":
        return "Cookie 失效"
    if state == "html_response":
        return "接口受限"
    if state == "probe_limited":
        return "部分受限"
    return "连接异常"


def _tone_from_state(state: str) -> str:
    if state == "ok":
        return "ok"
    if state in {"probe_limited", "html_response"}:
        return "warning"
    if state == "missing_cookie":
        return "neutral"
    if state == "checking":
        return "neutral"
    return "danger"


def _source_health_check(
    *,
    source: str,
    label: str,
    state: str,
    status: str,
    detail: str = "",
    tone: str = "",
) -> dict[str, str]:
    clean_state = str(state or "").strip()
    clean_status = str(status or "").strip() or _status_text_from_failure_state(clean_state)
    return {
        "source": str(source or "").strip(),
        "label": str(label or "").strip(),
        "state": clean_state,
        "status": clean_status,
        "detail": str(detail or "").strip(),
        "tone": str(tone or "").strip() or _tone_from_state(clean_state),
    }


def _check_priority(check: dict[str, Any]) -> tuple[int, int, int]:
    tone_score = {
        "danger": 30,
        "warning": 20,
        "neutral": 10,
        "ok": 0,
    }.get(str(check.get("tone") or "").strip(), 0)
    state_score = three_mf_failure_priority(check.get("state"))
    source_score = {
        "account": 2,
        "web": 2,
        "download": 1,
    }.get(str(check.get("source") or "").strip(), 0)
    return (tone_score, state_score, source_score)


def _primary_source_health_check(checks: list[dict[str, str]]) -> dict[str, str]:
    if not checks:
        return _source_health_check(
            source="account",
            label="账号",
            state="http_error",
            status="连接异常",
            detail="状态检测未返回结果。",
        )
    account_check = next((item for item in checks if item.get("source") == "account"), None)
    if account_check and account_check.get("state") == "ok":
        non_web_checks = [item for item in checks if item.get("source") != "web"]
        if non_web_checks:
            return max(non_web_checks, key=_check_priority)
    return max(checks, key=_check_priority)


def _prefixed_status(check: dict[str, str], checks: list[dict[str, str]]) -> str:
    status = str(check.get("status") or "").strip() or "连接异常"
    state = str(check.get("state") or "").strip()
    if state == "ok":
        return status
    label = str(check.get("label") or "").strip()
    if not label:
        return status
    separator = " " if status[:1].isascii() else ""
    return f"{label}{separator}{status}"


def build_source_health_cards(
    config: Any,
    missing_3mf_items: list[dict[str, Any]] | None = None,
    *,
    remote_refresh_state: dict[str, Any] | None = None,
    prefer_cached: bool = False,
) -> list[dict[str, Any]]:
    cookie_map = {item.platform: item.cookie for item in getattr(config, "cookies", [])}
    platforms = ("cn", "global")
    missing_overrides = _build_missing_3mf_overrides(missing_3mf_items)

    def build_card(platform: str) -> dict[str, Any]:
        raw_cookie = str(cookie_map.get(platform) or "")
        probe = (
            _probe_platform_status_snapshot(platform, raw_cookie, getattr(config, "proxy", None))
            if prefer_cached
            else _probe_platform_status(platform, raw_cookie, getattr(config, "proxy", None))
        )
        account_state = str(probe.get("state") or "").strip()
        account_detail = str(probe.get("detail") or "").strip()
        account_status = str(probe.get("status") or "连接异常")
        account_tone = _tone_from_state(account_state)
        account_state, account_status, account_detail, account_tone = _soften_probe_verification_with_remote_health(
            platform,
            account_state,
            account_status,
            account_detail,
            account_tone,
            remote_refresh_state,
        )
        checks = [
            _source_health_check(
                source="account",
                label="账号",
                state=account_state,
                status=account_status,
                detail=account_detail,
                tone=account_tone,
            )
        ]
        web_probe = {}
        if sanitize_cookie_header(raw_cookie):
            web_probe = (
                _probe_platform_web_status_snapshot(platform, raw_cookie, getattr(config, "proxy", None))
                if prefer_cached
                else _probe_platform_web_status(platform, raw_cookie, getattr(config, "proxy", None))
            )
        web_state = str(web_probe.get("state") or "").strip()
        if web_state and web_state != "ok" and account_state != "ok":
            checks.append(
                _source_health_check(
                    source="web",
                    label="网页",
                    state=web_state,
                    status=str(web_probe.get("status") or _status_text_from_failure_state(web_state)),
                    detail=str(web_probe.get("detail") or "").strip(),
                    tone=_tone_from_state(web_state),
                )
            )
        override = missing_overrides.get(platform) or {}
        override_state = str(override.get("state") or "").strip()
        if override_state:
            download_detail = str(override.get("message") or "").strip()
            if override_state in {"verification_required", "cloudflare"}:
                download_detail = describe_three_mf_failure(
                    override_state,
                    download_detail,
                    url=PLATFORM_ORIGINS.get(platform, ""),
                )
            if not download_detail and override_state == "verification_required":
                download_detail = "MakerWorld 需要验证，前往官网任意下载一个模型。"
            checks.append(
                _source_health_check(
                    source="download",
                    label="3MF 下载",
                    state=override_state,
                    status=_status_text_from_failure_state(override_state),
                    detail=download_detail,
                    tone=_tone_from_state(override_state),
                )
            )

        primary = _primary_source_health_check(checks)
        state = str(primary.get("state") or "").strip()
        status = _prefixed_status(primary, checks)
        detail = str(primary.get("detail") or "").strip()
        tone = str(primary.get("tone") or "").strip() or _tone_from_state(state)
        card = {
            "key": platform,
            "title": SOURCE_HEALTH_LABELS.get(platform, platform),
            "status": status,
            "detail": detail,
            "tone": tone,
            "state": state,
            "checks": checks,
        }
        manual_homepage_states = {"verification_required", "cloudflare", "auth_required"}
        if any(item.get("state") in manual_homepage_states for item in checks):
            card["url"] = PLATFORM_ORIGINS.get(platform, "")
            card["action_label"] = "访问主页"
        else:
            card["url"] = PLATFORM_ORIGINS.get(platform, "")
            card["action_label"] = "打开官网"
        return card

    with ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        results = list(executor.map(build_card, platforms))
    return results
