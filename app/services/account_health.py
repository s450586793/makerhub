from __future__ import annotations

from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.timezone import now_iso as china_now_iso
from app.services.state_contracts import ACCOUNT_HEALTH_STATE_KEY
from app.services.three_mf import normalize_makerworld_source


ACCOUNT_HEALTH_PLATFORMS = ("cn", "global")
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
ALLOWED_THREE_MF_GATE_STATES = {
    "open",
    "daily_limit",
    "verification_required",
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
    "daily_limit": {"state": "daily_limit", "status": "到达每日上限", "tone": "warning"},
    "cookie_invalid": {"state": "cookie_invalid", "status": "Cookie 异常", "tone": "danger"},
    "network_error": {"state": "network_error", "status": "网络异常", "tone": "warning"},
    "unknown": {"state": "unknown", "status": "未检测", "tone": "neutral"},
}
THREE_MF_GATE_CARD_META = {
    "open": ACCOUNT_HEALTH_CARD_META["ok"],
    "daily_limit": ACCOUNT_HEALTH_CARD_META["daily_limit"],
    "verification_required": ACCOUNT_HEALTH_CARD_META["verification_required"],
    "cookie_invalid": ACCOUNT_HEALTH_CARD_META["cookie_invalid"],
    "network_error": ACCOUNT_HEALTH_CARD_META["network_error"],
    "unknown": ACCOUNT_HEALTH_CARD_META["unknown"],
}


def _empty_snapshot(platform: str) -> dict[str, Any]:
    return {
        "platform": platform,
        "status": "unknown",
        "reason": "",
        "source": "system",
        "detail": "",
        "model_url": "",
        "model_id": "",
        "instance_id": "",
        "three_mf_gate": "open",
        "three_mf_reason": "",
        "three_mf_detail": "",
        "updated_at": "",
    }


def _default_payload() -> dict[str, Any]:
    return {platform: _empty_snapshot(platform) for platform in ACCOUNT_HEALTH_PLATFORMS}


def normalize_account_platform(platform: Any = "", url: Any = "") -> str:
    platform_text = str(platform or "").strip().lower()
    if platform_text in {"global", "intl", "international", "mw_global", "makerworld_global"}:
        return "global"
    if platform_text in {"cn", "mw_cn", "makerworld_cn"}:
        return "cn"
    normalized = normalize_makerworld_source(source=platform, url=url)
    return normalized if normalized in ACCOUNT_HEALTH_PLATFORMS else "cn"


def normalize_account_health_status(status: Any) -> str:
    normalized = str(status or "").strip().lower()
    normalized = ACCOUNT_HEALTH_STATUS_ALIASES.get(normalized, normalized)
    if normalized in ALLOWED_ACCOUNT_HEALTH_STATUSES:
        return normalized
    return "unknown"


def normalize_three_mf_gate(gate: Any) -> str:
    normalized = str(gate or "").strip().lower()
    normalized = ACCOUNT_HEALTH_STATUS_ALIASES.get(normalized, normalized)
    if normalized == "ok":
        return "open"
    if normalized in ALLOWED_THREE_MF_GATE_STATES:
        return normalized
    return "unknown"


def load_account_health() -> dict[str, Any]:
    payload = _default_payload()
    stored = load_database_json_state(ACCOUNT_HEALTH_STATE_KEY, payload)
    normalized = _default_payload()
    for platform in ACCOUNT_HEALTH_PLATFORMS:
        snapshot = stored.get(platform) if isinstance(stored.get(platform), dict) else {}
        normalized[platform] = _normalize_snapshot(platform, snapshot)
    return normalized


def save_account_health(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _default_payload()
    for platform in ACCOUNT_HEALTH_PLATFORMS:
        snapshot = payload.get(platform) if isinstance(payload.get(platform), dict) else {}
        normalized[platform] = _normalize_snapshot(platform, snapshot)
    return save_database_json_state(ACCOUNT_HEALTH_STATE_KEY, normalized)


def get_account_health(platform: Any) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    payload = load_account_health()
    return dict(payload.get(normalized_platform) or _empty_snapshot(normalized_platform))


def update_account_health(
    platform: Any,
    *,
    status: Any,
    reason: Any = "",
    source: Any = "system",
    detail: Any = "",
    model_url: Any = "",
    model_id: Any = "",
    instance_id: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform, url=model_url)
    payload = load_account_health()
    current = dict(payload.get(normalized_platform) or _empty_snapshot(normalized_platform))
    current.update(
        {
            "platform": normalized_platform,
            "status": status,
            "reason": reason,
            "source": source,
            "detail": detail,
            "model_url": model_url,
            "model_id": model_id,
            "instance_id": instance_id,
            "updated_at": updated_at,
        }
    )
    payload[normalized_platform] = _normalize_snapshot(
        normalized_platform,
        current,
        fill_updated_at=True,
    )
    save_account_health(payload)
    return dict(payload[normalized_platform])


def update_three_mf_gate(
    platform: Any,
    *,
    gate: Any,
    reason: Any = "",
    detail: Any = "",
    source: Any = "three_mf_gate",
    model_url: Any = "",
    model_id: Any = "",
    instance_id: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform, url=model_url)
    payload = load_account_health()
    current = dict(payload.get(normalized_platform) or _empty_snapshot(normalized_platform))
    current.update(
        {
            "platform": normalized_platform,
            "status": current.get("status") if current.get("status") != "unknown" else "ok",
            "source": source,
            "detail": detail,
            "model_url": model_url,
            "model_id": model_id,
            "instance_id": instance_id,
            "three_mf_gate": gate,
            "three_mf_reason": reason,
            "three_mf_detail": detail,
            "updated_at": updated_at,
        }
    )
    payload[normalized_platform] = _normalize_snapshot(
        normalized_platform,
        current,
        fill_updated_at=True,
    )
    save_account_health(payload)
    return dict(payload[normalized_platform])


def open_three_mf_gate(
    platform: Any,
    *,
    source: Any = "three_mf_gate",
    detail: Any = "",
    model_url: Any = "",
    model_id: Any = "",
    instance_id: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform, url=model_url)
    payload = load_account_health()
    current = dict(payload.get(normalized_platform) or _empty_snapshot(normalized_platform))
    current.update(
        {
            "platform": normalized_platform,
            "source": source,
            "detail": detail,
            "model_url": model_url,
            "model_id": model_id,
            "instance_id": instance_id,
            "three_mf_gate": "open",
            "three_mf_reason": "",
            "three_mf_detail": "",
            "updated_at": updated_at,
        }
    )
    payload[normalized_platform] = _normalize_snapshot(
        normalized_platform,
        current,
        fill_updated_at=True,
    )
    save_account_health(payload)
    return dict(payload[normalized_platform])


def mark_account_ok(
    platform: Any,
    *,
    source: Any = "system",
    detail: Any = "",
    model_url: Any = "",
    model_id: Any = "",
    instance_id: Any = "",
    updated_at: Any = "",
) -> dict[str, Any]:
    snapshot = update_account_health(
        platform,
        status="ok",
        reason="",
        source=source,
        detail=detail,
        model_url=model_url,
        model_id=model_id,
        instance_id=instance_id,
        updated_at=updated_at,
    )
    normalized_platform = normalize_account_platform(platform, url=model_url)
    payload = load_account_health()
    current = dict(payload.get(normalized_platform) or snapshot)
    current["three_mf_gate"] = "open"
    current["three_mf_reason"] = ""
    current["three_mf_detail"] = ""
    payload[normalized_platform] = _normalize_snapshot(normalized_platform, current)
    save_account_health(payload)
    return dict(payload[normalized_platform])


def snapshot_to_source_card(platform: Any, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_platform = normalize_account_platform(platform)
    current = _normalize_snapshot(normalized_platform, snapshot or get_account_health(normalized_platform))
    gate = current["three_mf_gate"]
    card_state = gate if gate != "open" else current["status"]
    meta = THREE_MF_GATE_CARD_META.get(card_state, ACCOUNT_HEALTH_CARD_META.get(current["status"], ACCOUNT_HEALTH_CARD_META["unknown"]))
    return {
        "key": normalized_platform,
        "title": PLATFORM_TITLES.get(normalized_platform, normalized_platform),
        "status": meta["status"],
        "detail": current["three_mf_detail"] or current["detail"],
        "tone": meta["tone"],
        "state": meta["state"],
        "account_status": current["status"],
        "three_mf_gate": current["three_mf_gate"],
        "three_mf_reason": current["three_mf_reason"],
        "checks": [],
        "url": PLATFORM_URLS.get(normalized_platform, ""),
        "action_label": "打开官网",
        "updated_at": current["updated_at"],
        "reason": current["reason"],
        "source": current["source"],
    }


def _normalize_snapshot(
    platform: str,
    snapshot: dict[str, Any] | None,
    *,
    fill_updated_at: bool = False,
) -> dict[str, Any]:
    current = _empty_snapshot(platform)
    current.update(snapshot or {})
    current["platform"] = platform
    current["status"] = normalize_account_health_status(current.get("status"))
    current["reason"] = str(current.get("reason") or "")
    current["source"] = str(current.get("source") or "system")
    current["detail"] = str(current.get("detail") or "")
    current["model_url"] = str(current.get("model_url") or "")
    current["model_id"] = str(current.get("model_id") or "")
    current["instance_id"] = str(current.get("instance_id") or "")
    current["three_mf_gate"] = normalize_three_mf_gate(current.get("three_mf_gate") or "open")
    current["three_mf_reason"] = str(current.get("three_mf_reason") or "")
    current["three_mf_detail"] = str(current.get("three_mf_detail") or "")
    current["updated_at"] = str(current.get("updated_at") or (china_now_iso() if fill_updated_at else ""))
    return current
