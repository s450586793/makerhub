from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.store import JsonStore
from app.core.timezone import now_iso as china_now_iso, parse_datetime
from app.schemas.models import CookiePair
from app.services.account_health import update_account_health, update_three_mf_gate
from app.services.business_logs import append_business_log
from app.services.cookie_utils import sanitize_cookie_header
from app.services.online_accounts import online_account_metadata_from_cookie
from app.services.state_contracts import DASHBOARD_STATE_KEY
from app.services.state_events import publish_state_event


ACCOUNT_COOKIE_MAINTENANCE_STATE_KEY = "account_cookie_maintenance"
DEFAULT_ACCOUNT_COOKIE_CHECK_INTERVAL_HOURS = 12
FAILED_ACCOUNT_STATUSES = {
    "verification_required",
    "cloudflare",
    "auth_required",
    "cookie_invalid",
    "missing_cookie",
}


def _empty_state() -> dict[str, Any]:
    return {
        "last_run_at": "",
        "last_result": {},
    }


def _load_state() -> dict[str, Any]:
    payload = load_database_json_state(ACCOUNT_COOKIE_MAINTENANCE_STATE_KEY, _empty_state())
    if not isinstance(payload, dict):
        payload = {}
    state = _empty_state()
    state.update(payload)
    if not isinstance(state.get("last_result"), dict):
        state["last_result"] = {}
    return state


def _save_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = _empty_state()
    state.update(payload or {})
    if not isinstance(state.get("last_result"), dict):
        state["last_result"] = {}
    return save_database_json_state(ACCOUNT_COOKIE_MAINTENANCE_STATE_KEY, state)


def _is_due(last_run_at: Any, *, now_iso: str, interval_hours: int) -> bool:
    last_run = parse_datetime(last_run_at)
    now = parse_datetime(now_iso)
    if last_run is None or now is None:
        return True
    return now - last_run >= timedelta(hours=max(int(interval_hours or 0), 1))


def _platform_label(platform: str) -> str:
    return "国际站" if platform == "global" else "国内站"


def _metadata_status(metadata: dict[str, Any]) -> str:
    status = str((metadata or {}).get("status") or "").strip().lower()
    if status == "cloudflare":
        return "verification_required"
    if status in {"auth_required", "missing_cookie"}:
        return "cookie_invalid"
    return status or "unknown"


def _metadata_message(platform: str, metadata: dict[str, Any]) -> str:
    message = str((metadata or {}).get("message") or "").strip()
    if message:
        return message
    status = _metadata_status(metadata)
    if status == "verification_required":
        return f"{_platform_label(platform)}需要完成 Cloudflare 验证。"
    if status == "cookie_invalid":
        return f"{_platform_label(platform)} Cookie 失效，请重新验证后保存。"
    if status == "ok":
        return f"{_platform_label(platform)}账号可用，Cookie 已保存。"
    return f"{_platform_label(platform)}账号定时检测未能确认状态。"


def _merge_cookie_metadata(existing: CookiePair, metadata: dict[str, Any]) -> CookiePair:
    def field_value(key: str) -> str:
        if key in metadata:
            value = str(metadata.get(key) or "")
            if value or key not in {"username", "display_name", "account_id", "handle", "avatar_url"}:
                return value
        return str(getattr(existing, key, "") or "")

    return CookiePair(
        platform=existing.platform,
        cookie=sanitize_cookie_header(existing.cookie),
        username=field_value("username"),
        display_name=field_value("display_name"),
        account_id=field_value("account_id"),
        handle=field_value("handle"),
        avatar_url=field_value("avatar_url"),
        status=field_value("status"),
        message=field_value("message"),
        created_at=field_value("created_at"),
        updated_at=field_value("updated_at"),
        last_login_at=field_value("last_login_at"),
        last_tested_at=field_value("last_tested_at"),
    )


def _publish_dashboard_refresh() -> None:
    publish_state_event(DASHBOARD_STATE_KEY, "account_cookie_maintenance.completed", {"reason": "account_cookie_maintenance"})


def run_account_cookie_maintenance_once(
    *,
    store: JsonStore | None = None,
    interval_hours: int = DEFAULT_ACCOUNT_COOKIE_CHECK_INTERVAL_HOURS,
    force: bool = False,
) -> dict[str, Any]:
    now = china_now_iso()
    state = _load_state()
    if not force and not _is_due(state.get("last_run_at"), now_iso=now, interval_hours=interval_hours):
        return {
            "checked": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "skipped_reason": "interval",
            "last_run_at": str(state.get("last_run_at") or ""),
        }

    config_store = store or JsonStore()
    config = config_store.load()
    cookies = list(getattr(config, "cookies", []) or [])
    next_cookies: list[CookiePair] = []
    result: dict[str, Any] = {
        "checked": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "items": [],
        "last_run_at": now,
    }

    for cookie_pair in cookies:
        platform = "global" if str(cookie_pair.platform or "").strip().lower() == "global" else "cn"
        raw_cookie = sanitize_cookie_header(cookie_pair.cookie)
        if not raw_cookie:
            next_cookies.append(cookie_pair)
            result["skipped"] += 1
            continue

        try:
            metadata = online_account_metadata_from_cookie(
                platform=platform,
                username=cookie_pair.username,
                cookie=raw_cookie,
                proxy_config=config.proxy,
            )
        except Exception as exc:
            metadata = {
                "platform": platform,
                "username": cookie_pair.username,
                "status": "network_error",
                "message": str(exc)[:240] or "账号定时检测失败。",
                "last_tested_at": now,
                "updated_at": now,
            }

        status = _metadata_status(metadata)
        message = _metadata_message(platform, metadata)
        metadata = {
            **metadata,
            "platform": platform,
            "status": status,
            "message": message,
            "last_tested_at": str(metadata.get("last_tested_at") or now),
            "updated_at": str(metadata.get("updated_at") or now),
        }
        next_cookies.append(_merge_cookie_metadata(cookie_pair, metadata))
        result["checked"] += 1
        item = {"platform": platform, "status": status, "message": message}
        result["items"].append(item)

        update_account_health(
            platform,
            status=status,
            reason="scheduled_cookie_check",
            source="scheduled_cookie_check",
            detail=message,
            updated_at=now,
        )
        if status in FAILED_ACCOUNT_STATUSES:
            result["failed"] += 1
            update_three_mf_gate(
                platform,
                gate=status,
                reason="scheduled_cookie_check",
                detail=message,
                source="scheduled_cookie_check",
                updated_at=now,
            )
        elif status == "ok":
            result["ok"] += 1

    config.cookies = next_cookies
    config_store.save(config)
    _save_state({"last_run_at": now, "last_result": result})
    if result["checked"]:
        append_business_log(
            "settings",
            "online_account_cookie_maintenance",
            "线上账号 Cookie 定时检测已完成。",
            checked=int(result["checked"]),
            ok=int(result["ok"]),
            failed=int(result["failed"]),
            skipped=int(result["skipped"]),
        )
        _publish_dashboard_refresh()
    return result
