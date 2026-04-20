import hashlib
import json
import threading
import time
from datetime import datetime
from typing import Any

import requests

from app.core.settings import STATE_DIR, ensure_app_dirs
from app.services.three_mf import normalize_makerworld_source


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
THREE_MF_PROBES = {
    "cn": {
        "origin": "https://makerworld.com.cn",
        "url": "https://api.bambulab.cn/v1/design-service/instance/2667239/f3mf?type=download&fileType=",
    },
    "global": {
        "origin": "https://makerworld.com",
        "url": "https://api.bambulab.com/v1/design-service/instance/358109/f3mf?type=download&fileType=",
    },
}
AUTH_PROBES = {
    "cn": (
        "https://makerworld.com.cn/api/v1/user-service/my/message/count",
        "https://makerworld.com.cn/api/v1/design-user-service/my/preference",
    ),
    "global": (
        "https://makerworld.com/api/v1/user-service/my/message/count",
        "https://makerworld.com/api/v1/design-user-service/my/preference",
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
    session.headers.update(
        {
            **MW_BROWSER_HEADERS,
            "User-Agent": MW_BROWSER_USER_AGENT,
        }
    )
    return session


def _build_proxy_mapping(proxy_config: Any) -> dict[str, str]:
    if not proxy_config or not bool(getattr(proxy_config, "enabled", False)):
        return {}
    http_proxy = str(getattr(proxy_config, "http_proxy", "") or "").strip()
    https_proxy = str(getattr(proxy_config, "https_proxy", "") or "").strip()
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    elif http_proxy:
        proxies["https"] = http_proxy
    return proxies


def _parse_cookie_values(raw_cookie: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(raw_cookie or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            values[clean_key] = value.strip()
    return values


def _extract_auth_token(raw_cookie: str) -> str:
    cookies = _parse_cookie_values(raw_cookie)
    return (
        cookies.get("token")
        or cookies.get("access_token")
        or cookies.get("accessToken")
        or ""
    )


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


def _extract_download_url(payload: Any) -> str:
    current = payload
    if isinstance(payload, dict):
        current = payload.get("data") or payload.get("result") or payload
    if not isinstance(current, dict):
        return ""
    return str(
        current.get("url")
        or current.get("downloadUrl")
        or current.get("download_url")
        or current.get("downloadURL")
        or ""
    ).strip()


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
        if limited_until:
            try:
                if datetime.fromisoformat(limited_until) <= datetime.now():
                    state["active"] = False
            except ValueError:
                state["active"] = False
    return state


def _limit_guard_for_platform(platform: str) -> dict[str, Any]:
    state = _read_limit_guard()
    if not bool(state.get("active")):
        return {}
    source = normalize_makerworld_source(url=state.get("model_url"))
    if source == platform:
        return state
    return {}


def _build_request_headers(origin: str, raw_cookie: str) -> dict[str, str]:
    headers = {
        "Referer": f"{origin}/",
        "Origin": origin,
        "Cookie": raw_cookie,
    }
    token = _extract_auth_token(raw_cookie)
    if token:
        headers.update(
            {
                "Authorization": f"Bearer {token}",
                "token": token,
                "X-Token": token,
                "X-Access-Token": token,
            }
        )
    return headers


def _classify_probe_response(response: requests.Response) -> dict[str, str]:
    preview = (response.text or "")[:400]
    combined = preview.lower()

    if response.status_code == 418 or _contains_verification_markers(preview) or _looks_like_html(preview):
        return {
            "state": "verification_required",
            "status": "需要验证",
            "detail": "",
        }
    if "每日下载上限" in combined or ("daily" in combined and "download" in combined and "limit" in combined):
        return {
            "state": "download_limited",
            "status": "到达每日上限",
            "detail": "",
        }
    if "please log in to download models" in combined or "log in to download models" in combined or response.status_code in {401, 403}:
        return {
            "state": "auth_required",
            "status": "Cookie 失效",
            "detail": "",
        }

    if response.status_code < 400:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if _extract_download_url(payload):
            return {
                "state": "ok",
                "status": "连接正常",
                "detail": "",
            }

    return {
        "state": "http_error",
        "status": "连接异常",
        "detail": f"下载接口返回 HTTP {response.status_code}。",
    }


def _probe_3mf_endpoint(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, str]:
    probe = THREE_MF_PROBES.get(platform)
    if not probe:
        return {"state": "http_error", "status": "连接异常", "detail": "缺少探针配置。"}

    session = _make_session()
    proxies = _build_proxy_mapping(proxy_config)
    try:
        response = session.get(
            str(probe.get("url") or ""),
            headers=_build_request_headers(str(probe.get("origin") or ""), raw_cookie),
            proxies=proxies or None,
            timeout=(6, 15),
        )
        return _classify_probe_response(response)
    except Exception as exc:
        return {
            "state": "http_error",
            "status": "连接异常",
            "detail": _safe_error_message(exc),
        }
    finally:
        session.close()


def _classify_auth_probe_result(result: dict[str, Any]) -> str:
    if bool(result.get("ok")):
        return "ok"
    lowered = str(result.get("error") or "").lower()
    if _contains_verification_markers(lowered) or "html" in lowered:
        return "verification_required"
    status_code = int(result.get("status_code") or 0)
    if status_code in {401, 403}:
        return "auth_required"
    return "http_error"


def _probe_auth_endpoints(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, str]:
    urls = AUTH_PROBES.get(platform) or ()
    if not urls:
        return {"state": "http_error", "status": "连接异常", "detail": "缺少认证探针配置。"}

    session = _make_session()
    proxies = _build_proxy_mapping(proxy_config)
    headers = _build_request_headers(THREE_MF_PROBES.get(platform, {}).get("origin", ""), raw_cookie)
    states: list[str] = []
    try:
        for url in urls:
            try:
                response = session.get(
                    url,
                    headers=headers,
                    proxies=proxies or None,
                    timeout=(6, 12),
                )
                preview = (response.text or "")[:240]
                looks_like_html = _looks_like_html(preview)
                result = {
                    "ok": response.status_code < 400 and not looks_like_html,
                    "status_code": int(response.status_code),
                    "error": "html challenge" if looks_like_html else "",
                }
            except Exception as exc:
                result = {
                    "ok": False,
                    "error": _safe_error_message(exc),
                }
            states.append(_classify_auth_probe_result(result))
    finally:
        session.close()

    if "ok" in states:
        return {
            "state": "ok",
            "status": "连接正常",
            "detail": "",
        }
    if "verification_required" in states:
        return {
            "state": "verification_required",
            "status": "需要验证",
            "detail": "",
        }
    if "auth_required" in states:
        return {
            "state": "auth_required",
            "status": "Cookie 失效",
            "detail": "",
        }
    return {
        "state": "http_error",
        "status": "连接异常",
        "detail": "认证接口暂时不可用。",
    }


def _cache_key(platform: str, raw_cookie: str, proxy_config: Any) -> str:
    proxy_state = json.dumps(
        {
            "enabled": bool(getattr(proxy_config, "enabled", False)),
            "http": str(getattr(proxy_config, "http_proxy", "") or ""),
            "https": str(getattr(proxy_config, "https_proxy", "") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    cookie_hash = hashlib.sha1(str(raw_cookie or "").encode("utf-8", errors="ignore")).hexdigest()
    return f"{platform}:{cookie_hash}:{proxy_state}"


def _probe_platform_status(platform: str, raw_cookie: str, proxy_config: Any) -> dict[str, str]:
    if not raw_cookie.strip():
        return {
            "state": "missing_cookie",
            "status": "未配置 Cookie",
            "detail": "还没有保存对应站点的 Cookie。",
        }

    limit_guard = _limit_guard_for_platform(platform)
    if limit_guard:
        return {
            "state": "download_limited",
            "status": "到达每日上限",
            "detail": "",
        }

    cache_key = _cache_key(platform, raw_cookie, proxy_config)
    now = time.time()
    with SOURCE_HEALTH_CACHE_LOCK:
        cached = SOURCE_HEALTH_CACHE.get(cache_key)
        if cached and now - float(cached.get("checked_at") or 0) < SOURCE_HEALTH_CACHE_TTL_SECONDS:
            return dict(cached.get("payload") or {})

    payload = _probe_3mf_endpoint(platform, raw_cookie, proxy_config)
    if payload.get("state") == "http_error":
        fallback = _probe_auth_endpoints(platform, raw_cookie, proxy_config)
        if fallback.get("state") in {"ok", "verification_required", "auth_required"}:
            payload = fallback

    with SOURCE_HEALTH_CACHE_LOCK:
        SOURCE_HEALTH_CACHE[cache_key] = {
            "checked_at": now,
            "payload": payload,
        }
    return dict(payload)


def build_source_health_cards(config: Any) -> list[dict[str, Any]]:
    cookie_map = {item.platform: item.cookie for item in getattr(config, "cookies", [])}
    cards: list[dict[str, Any]] = []
    for platform in ("cn", "global"):
        probe = _probe_platform_status(platform, str(cookie_map.get(platform) or ""), getattr(config, "proxy", None))
        state = str(probe.get("state") or "").strip()
        cards.append(
            {
                "key": platform,
                "title": SOURCE_HEALTH_LABELS.get(platform, platform),
                "status": str(probe.get("status") or "连接异常"),
                "detail": str(probe.get("detail") or "").strip(),
                "tone": "ok" if state == "ok" else "danger",
            }
        )
    return cards
