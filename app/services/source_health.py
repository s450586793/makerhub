from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import requests

from app.core.settings import STATE_DIR, ensure_app_dirs
from app.core.timezone import now as china_now, parse_datetime
from app.services.cookie_utils import sanitize_cookie_header
from app.services.proxy_policy import effective_proxy_cache_state, proxy_mapping
from app.services.scrapling_fetch import fetch_text as scrapling_fetch_text, scrapling_only
from app.services.three_mf import (
    describe_three_mf_failure,
    merge_three_mf_failure,
    normalize_makerworld_source,
    normalize_three_mf_failure_state,
    three_mf_failure_priority,
)


THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
SOURCE_HEALTH_CACHE_TTL_SECONDS = 60
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
        ("消息计数", "https://makerworld.com.cn/api/v1/user-service/my/message/count"),
        ("个人偏好", "https://makerworld.com.cn/api/v1/design-user-service/my/preference"),
    ),
    "global": (
        ("消息计数", "https://makerworld.com/api/v1/user-service/my/message/count"),
        ("个人偏好", "https://makerworld.com/api/v1/design-user-service/my/preference"),
    ),
}
SOURCE_HEALTH_LABELS = {
    "cn": "国内站",
    "global": "国际站",
}
SOURCE_HEALTH_CACHE_LOCK = threading.RLock()
SOURCE_HEALTH_CACHE: dict[str, dict[str, Any]] = {}


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


def _build_proxy_mapping(proxy_config: Any, target_url: str = "", *, platform: str = "") -> dict[str, str]:
    return proxy_mapping(proxy_config, target_url, platform=platform)


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
            result["failure_kind"] = "verification_required"
            result["error"] = (
                "返回了验证页面，通常表示需要完成网页验证。"
                if verification_marker
                else "认证探针返回了网页页面，可能是站点接口改版、代理跳转或风控页面；请优先看其它认证接口是否成功。"
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
    if THREE_MF_LIMIT_GUARD_PATH.exists():
        try:
            existing = json.loads(THREE_MF_LIMIT_GUARD_PATH.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                current.update(existing)
        except (OSError, json.JSONDecodeError):
            pass
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
    THREE_MF_LIMIT_GUARD_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def _read_limit_guard() -> dict[str, Any]:
    ensure_app_dirs()
    if not THREE_MF_LIMIT_GUARD_PATH.exists():
        return _base_limit_guard()

    try:
        payload = json.loads(THREE_MF_LIMIT_GUARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _base_limit_guard()

    state = _base_limit_guard()
    if isinstance(payload, dict):
        state.update(payload)

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
    return headers


def _classify_auth_probe_result(result: dict[str, Any]) -> str:
    if bool(result.get("ok")):
        return "ok"
    failure_kind = str(result.get("failure_kind") or "").strip()
    if failure_kind:
        return failure_kind
    lowered = str(result.get("error") or "").lower()
    if _contains_verification_markers(lowered) or "html" in lowered:
        return "verification_required"
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
        return f"{platform_label} Cookie 测试成功，认证接口可正常访问。"
    if success_count > 0:
        return f"{platform_label} Cookie 部分成功，{success_count}/{target_count} 个接口可访问。"
    if state == "missing_cookie":
        return f"请先填写{platform_label} Cookie。"
    if state == "verification_required":
        return "MakerWorld 需要验证，前往官网任意下载一个模型。"
    if state == "auth_required":
        return f"{platform_label} Cookie 失效，请重新获取并保存 Cookie。"
    if state == "download_limited":
        return f"{platform_label}站已到达 3MF 每日下载上限，过零点后会自动恢复。"
    return f"{platform_label} Cookie 测试失败，认证接口未返回有效结果。"


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


def _probe_auth_endpoints(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    probes = AUTH_PROBES.get(platform) or ()
    if not probes:
        return _empty_cookie_auth_payload(platform, "http_error", "连接异常", "缺少认证探针配置。")

    session = _make_session()
    proxies = _build_proxy_mapping(proxy_config, platform=platform)
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


def _cache_key(platform: str, raw_cookie: str, proxy_config: Any) -> str:
    proxy_state = json.dumps(effective_proxy_cache_state(proxy_config, platform=platform), ensure_ascii=False, sort_keys=True)
    cookie_hash = hashlib.sha1(str(raw_cookie or "").encode("utf-8", errors="ignore")).hexdigest()
    return f"{platform}:{cookie_hash}:{proxy_state}"


def probe_cookie_auth_status(
    platform: str,
    raw_cookie: str,
    proxy_config: Any,
    *,
    include_limit_guard: bool = False,
    use_cache: bool = False,
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

    cache_key = _cache_key(platform_key, normalized_cookie, proxy_config)
    now = time.time()
    if use_cache:
        with SOURCE_HEALTH_CACHE_LOCK:
            cached = SOURCE_HEALTH_CACHE.get(cache_key)
            if cached and now - float(cached.get("checked_at") or 0) < SOURCE_HEALTH_CACHE_TTL_SECONDS:
                return dict(cached.get("payload") or {})

    payload = _probe_auth_endpoints(platform_key, normalized_cookie, proxy_config)

    if use_cache:
        with SOURCE_HEALTH_CACHE_LOCK:
            SOURCE_HEALTH_CACHE[cache_key] = {
                "checked_at": now,
                "payload": payload,
            }
    return dict(payload)


def _probe_platform_status(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, Any]:
    return probe_cookie_auth_status(
        platform,
        raw_cookie,
        proxy_config,
        include_limit_guard=True,
        use_cache=True,
    )


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
        if state not in {"download_limited", "verification_required", "cloudflare", "auth_required"}:
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
    if state == "download_limited":
        return "到达每日上限"
    if state in {"verification_required", "cloudflare"}:
        return "需要验证"
    if state == "auth_required":
        return "Cookie 失效"
    if state == "probe_limited":
        return "部分受限"
    return "连接异常"


def build_source_health_cards(
    config: Any,
    missing_3mf_items: list[dict[str, Any]] | None = None,
    *,
    remote_refresh_state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cookie_map = {item.platform: item.cookie for item in getattr(config, "cookies", [])}
    platforms = ("cn", "global")
    missing_overrides = _build_missing_3mf_overrides(missing_3mf_items)

    def build_card(platform: str) -> dict[str, Any]:
        probe = _probe_platform_status(platform, str(cookie_map.get(platform) or ""), getattr(config, "proxy", None))
        state = str(probe.get("state") or "").strip()
        detail = str(probe.get("detail") or "").strip()
        override = missing_overrides.get(platform) or {}
        override_state = str(override.get("state") or "").strip()
        override_applied = False
        if (
            override_state
            and state != "missing_cookie"
            and three_mf_failure_priority(override_state) >= three_mf_failure_priority(state)
        ):
            state = override_state
            detail = str(override.get("message") or "").strip()
            override_applied = True
        if state in {"verification_required", "cloudflare"}:
            detail = describe_three_mf_failure(state, detail, url=PLATFORM_ORIGINS.get(platform, ""))
        if not detail and state == "verification_required":
            detail = "MakerWorld 需要验证，前往官网任意下载一个模型。"
        status = _status_text_from_failure_state(state) if override_applied else str(probe.get("status") or "连接异常")
        tone = "ok" if state == "ok" else "danger"
        if not override_applied:
            state, status, detail, tone = _soften_probe_verification_with_remote_health(
                platform,
                state,
                status,
                detail,
                tone,
                remote_refresh_state,
            )
        return {
            "key": platform,
            "title": SOURCE_HEALTH_LABELS.get(platform, platform),
            "status": status,
            "detail": detail,
            "tone": tone,
            "state": state,
            "url": PLATFORM_ORIGINS.get(platform, ""),
            "action_label": "去验证" if state in {"verification_required", "cloudflare"} else "打开官网",
        }

    with ThreadPoolExecutor(max_workers=len(platforms)) as executor:
        results = list(executor.map(build_card, platforms))
    return results
