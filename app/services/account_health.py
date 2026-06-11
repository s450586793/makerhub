from __future__ import annotations

from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.services.state_contracts import ACCOUNT_HEALTH_STATE_KEY
from app.services.three_mf import normalize_makerworld_source


PLATFORM_URLS = {
    "cn": "https://makerworld.com.cn",
    "global": "https://makerworld.com",
}
PLATFORM_TITLES = {
    "cn": "国内站",
    "global": "国际站",
}
ALLOWED_ACCOUNT_HEALTH_STATUSES = {
    "ok",
    "verification_required",
    "daily_limit",
    "cookie_invalid",
    "network_error",
    "unknown",
}
ACCOUNT_HEALTH_STATUS_ALIASES = {
    "cloudflare": "verification_required",
    "auth_required": "cookie_invalid",
    "download_limited": "daily_limit",
    "missing_cookie": "cookie_invalid",
    "http_error": "network_error",
}
ACCOUNT_HEALTH_CARD_META = {
    "ok": {"state": "ok", "status": "正常", "tone": "ok"},
    "verification_required": {"state": "verification_required", "status": "需要验证", "tone": "danger"},
    "daily_limit": {"state": "daily_limit", "status": "达到限额", "tone": "warning"},
    "cookie_invalid": {"state": "cookie_invalid", "status": "Cookie 失效", "tone": "danger"},
    "network_error": {"state": "network_error", "status": "连接异常", "tone": "danger"},
    "unknown": {"state": "unknown", "status": "未知", "tone": "neutral"},
}


def _empty_snapshot(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "status": "unknown",
        "reason": "",
        "source": "system",
        "detail": "",
        "model_url": "",
        "updated_at": "",
    }


def _default_payload() -> dict[str, Any]:
    return {
        "platforms": {
            "cn": _empty_snapshot("cn"),
            "global": _empty_snapshot("global"),
        }
    }


def normalize_account_platform(platform: Any = "", url: Any = "") -> str:
    platform_text = str(platform or "").strip().lower()
    if platform_text in {"global", "intl", "international", "mw_global", "makerworld_global"}:
        return "global"
    if platform_text in {"cn", "mw_cn", "makerworld_cn"}:
        return "cn"
    normalized = normalize_makerworld_source(source=platform, url=url)
    return normalized if normalized in PLATFORM_URLS else "cn"


def normalize_account_health_status(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    normalized = ACCOUNT_HEALTH_STATUS_ALIASES.get(normalized, normalized)
    if normalized in ALLOWED_ACCOUNT_HEALTH_STATUSES:
        return normalized
    return "unknown"


def load_account_health() -> dict[str, Any]:
    payload = _default_payload()
    stored = load_database_json_state(ACCOUNT_HEALTH_STATE_KEY, payload)
    platforms = stored.get("platforms") if isinstance(stored.get("platforms"), dict) else {}
    normalized = _default_payload()
    for platform in ("cn", "global"):
        snapshot = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
        normalized["platforms"][platform] = _normalize_snapshot(platform, snapshot)
    return normalized


def save_account_health(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _default_payload()
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
    for platform in ("cn", "global"):
        snapshot = platforms.get(platform) if isinstance(platforms.get(platform), dict) else {}
        normalized["platforms"][platform] = _normalize_snapshot(platform, snapshot)
    return save_database_json_state(ACCOUNT_HEALTH_STATE_KEY, normalized)


def get_account_health(platform: Any) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    payload = load_account_health()
    return dict(payload["platforms"].get(normalized_platform) or _empty_snapshot(normalized_platform))


def update_account_health(
    platform: Any,
    *,
    status: Any,
    reason: Any = "",
    source: Any = "system",
    detail: Any = "",
    model_url: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform, url=model_url)
    payload = load_account_health()
    payload["platforms"][normalized_platform] = _normalize_snapshot(
        normalized_platform,
        {
            "platform": normalized_platform,
            "status": status,
            "reason": reason,
            "source": source,
            "detail": detail,
            "model_url": model_url,
            "updated_at": updated_at,
        },
    )
    return save_account_health(payload)


def mark_account_ok(
    platform: Any,
    *,
    source: Any = "system",
    detail: Any = "",
    model_url: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    return update_account_health(
        platform,
        status="ok",
        reason="",
        source=source,
        detail=detail,
        model_url=model_url,
        updated_at=updated_at,
    )


def snapshot_to_source_card(platform: Any, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    current = _normalize_snapshot(normalized_platform, snapshot or get_account_health(normalized_platform))
    meta = ACCOUNT_HEALTH_CARD_META.get(current["status"], ACCOUNT_HEALTH_CARD_META["unknown"])
    return {
        "key": normalized_platform,
        "title": PLATFORM_TITLES.get(normalized_platform, normalized_platform),
        "status": meta["status"],
        "detail": current["detail"],
        "tone": meta["tone"],
        "state": meta["state"],
        "checks": [],
        "url": PLATFORM_URLS.get(normalized_platform, ""),
        "action_label": "打开官网",
        "updated_at": current["updated_at"],
        "reason": current["reason"],
        "source": current["source"],
    }


def _normalize_snapshot(platform: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
    current = _empty_snapshot(platform)
    current.update(snapshot or {})
    current["platform"] = platform
    current["status"] = normalize_account_health_status(current.get("status"))
    current["reason"] = str(current.get("reason") or "")
    current["source"] = str(current.get("source") or "system")
    current["detail"] = str(current.get("detail") or "")
    current["model_url"] = str(current.get("model_url") or "")
    current["updated_at"] = str(current.get("updated_at") or "")
    return current
