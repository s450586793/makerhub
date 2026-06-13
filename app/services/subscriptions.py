import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from croniter import CroniterBadCronError, croniter

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import BACKGROUND_TASKS_ENABLED, LOGS_DIR, STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import ensure_timezone, now as china_now, now_iso as china_now_iso, parse_datetime
from app.schemas.models import SubscriptionRecord
from app.services.cookie_utils import sanitize_cookie_header
from app.services.archive_worker import ArchiveTaskManager, detect_archive_mode
from app.services.batch_discovery import (
    default_favorites_subscription_source,
    discover_cookie_account_home_summary,
    discover_cookie_account_profile,
    discover_cookie_followed_authors,
    discover_cookie_followed_authors_from_page,
    discover_cookie_followed_collections,
    extract_model_id,
    normalize_source_url,
)
from app.services.business_logs import append_business_log, append_structured_log
from app.services.catalog import get_archive_snapshot, invalidate_archive_snapshot, invalidate_model_detail_cache
from app.services.process_jobs import run_discover_batch_urls_job
from app.services.proxy_policy import temporary_proxy_env
from app.services.source_library import (
    build_subscription_overview_light_payload,
    build_subscription_overview_payload,
    refresh_source_preview_snapshots,
    refresh_subscription_source_metadata,
    source_identity_key,
    _default_favorites_identity_url,
    _save_source_metadata_item,
)
from app.services.task_state import TaskStateStore


DEFAULT_SUBSCRIPTION_CRON = "0 */6 * * *"
DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE = 8
MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE = 120
SUBSCRIPTION_MODES = {"author_upload", "collection_models"}
SUBSCRIPTION_LOG_PATH = LOGS_DIR / "subscriptions.log"
COOKIE_SOURCE_SYNC_STATE_PATH = STATE_DIR / "cookie_source_sync_state.json"
COOKIE_SOURCE_SYNC_STATE_KEY = "cookie_source_sync_state"
COOKIE_SOURCE_INVENTORY_PATH = STATE_DIR / "cookie_source_inventory.json"
COOKIE_SOURCE_INVENTORY_KEY = "cookie_source_inventory"
SUBSCRIPTION_POLL_SECONDS = 20
SUBSCRIPTION_RUNNING_STALE_AFTER = timedelta(hours=6)
COOKIE_SOURCE_SYNC_INTERVAL = timedelta(hours=6)
COLLECTION_PARTIAL_SCAN_MIN_TRACKED = 20
COLLECTION_PARTIAL_SCAN_RATIO = 0.5
STRICT_EXPECTED_TOTAL_SOURCES = {
    "author_upload_api_total",
    "collection_page_all_models",
    "collection_models_api_total",
    "collection_detail_api_total",
}


def _now() -> datetime:
    return china_now()


def _now_iso() -> str:
    return china_now_iso()


def _parse_iso(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _normalize_subscription_source_page(
    page: int = 1,
    page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE,
    *,
    max_page_size: int = MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE,
) -> tuple[int, int]:
    try:
        safe_page = int(page or 1)
    except (TypeError, ValueError):
        safe_page = 1
    try:
        safe_page_size = int(page_size or DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE)
    except (TypeError, ValueError):
        safe_page_size = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE
    return max(safe_page, 1), max(1, min(safe_page_size, max_page_size))


def _paginate_subscription_source_sections(
    sections: list[dict],
    *,
    page: int = 1,
    page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE,
    max_page_size: int = MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE,
) -> list[dict]:
    safe_page, safe_page_size = _normalize_subscription_source_page(page, page_size, max_page_size=max_page_size)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    paged_sections = []
    for section in sections or []:
        if not isinstance(section, dict) or section.get("key") != "subscription_sources":
            paged_sections.append(section)
            continue
        items = list(section.get("items") or [])
        visible_items = items[start:end]
        paged = dict(section)
        paged["items"] = visible_items
        paged["count"] = len(visible_items)
        paged["total"] = len(items)
        paged["page"] = safe_page
        paged["page_size"] = safe_page_size
        paged["has_more"] = end < len(items)
        paged_sections.append(paged)
    return paged_sections


def _append_subscription_log(event: str, **payload: Any) -> None:
    append_structured_log(SUBSCRIPTION_LOG_PATH.name, event, category="subscription", **payload)
    if event in {
        "initialized",
        "metadata_refreshed",
        "metadata_refresh_error",
        "preview_snapshots_refreshed",
        "preview_snapshot_refresh_error",
        "sync_start",
        "sync_done",
        "sync_error",
        "sync_partial_rejected",
        "scheduler_error",
    }:
        level = (
            "error"
            if event in {"sync_error", "sync_partial_rejected", "scheduler_error"}
            else "warning"
            if event in {"metadata_refresh_error", "preview_snapshot_refresh_error"}
            else "info"
        )
        message_map = {
            "initialized": "订阅初始化完成。",
            "metadata_refreshed": "订阅来源卡元数据已刷新。",
            "metadata_refresh_error": "订阅来源卡元数据刷新失败。",
            "preview_snapshots_refreshed": "订阅来源卡快照已刷新。",
            "preview_snapshot_refresh_error": "订阅来源卡快照刷新失败。",
            "sync_start": "订阅同步开始。",
            "sync_done": "订阅同步完成。",
            "sync_error": "订阅同步失败。",
            "sync_partial_rejected": "订阅扫描结果异常，已保留历史状态。",
            "scheduler_error": "订阅调度器异常。",
        }
        append_business_log("subscription", event, message_map.get(event, event), level=level, **payload)


def _read_cookie_source_sync_state() -> dict[str, Any]:
    return load_database_json_state(COOKIE_SOURCE_SYNC_STATE_KEY, {})


def _write_cookie_source_sync_state(payload: dict[str, Any]) -> dict[str, Any]:
    return save_database_json_state(COOKIE_SOURCE_SYNC_STATE_KEY, payload)


def _read_cookie_source_inventory_state() -> dict[str, Any]:
    payload = load_database_json_state(COOKIE_SOURCE_INVENTORY_KEY, {"platforms": {}, "updated_at": ""})
    platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
    return {
        "platforms": {str(key): value for key, value in platforms.items() if isinstance(value, dict)},
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _write_cookie_source_inventory_state(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "platforms": payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {},
        "updated_at": str(payload.get("updated_at") or _now_iso()),
    }
    return save_database_json_state(COOKIE_SOURCE_INVENTORY_KEY, normalized)


def cookie_source_sync_state_payload() -> dict[str, Any]:
    state = _read_cookie_source_sync_state()
    return {
        platform: dict(item)
        for platform, item in state.items()
        if platform in {"cn", "global"} and isinstance(item, dict)
    }


def cookie_source_inventory_payload() -> dict[str, Any]:
    return _read_cookie_source_inventory_state()


def _patch_cookie_source_inventory_state(platform: str, **changes: Any) -> dict[str, Any]:
    clean_platform = "global" if str(platform or "").strip().lower() == "global" else "cn"
    now_iso = _now_iso()
    state = _read_cookie_source_inventory_state()
    platforms = dict(state.get("platforms") or {})
    item = dict(platforms.get(clean_platform) or {})
    for key, value in changes.items():
        if value is None:
            continue
        item[key] = value
    item["platform"] = clean_platform
    item["updated_at"] = str(item.get("updated_at") or now_iso)
    platforms[clean_platform] = item
    return _write_cookie_source_inventory_state({"platforms": platforms, "updated_at": now_iso})


def _cookie_source_sync_due(platforms: set[str]) -> set[str]:
    state = _read_cookie_source_sync_state()
    now = _now()
    due: set[str] = set()
    for platform in sorted(platforms):
        item = state.get(platform) if isinstance(state.get(platform), dict) else {}
        requested_at = _parse_iso(str(item.get("requested_at") or ""))
        if requested_at is not None:
            due.add(platform)
            continue
        last_sync_at = _parse_iso(str(item.get("last_sync_at") or ""))
        if last_sync_at is None or now - last_sync_at >= COOKIE_SOURCE_SYNC_INTERVAL:
            due.add(platform)
    return due


def _cookie_source_sync_reason(platforms: set[str]) -> str:
    state = _read_cookie_source_sync_state()
    for platform in sorted(platforms):
        item = state.get(platform) if isinstance(state.get(platform), dict) else {}
        reason = str(item.get("requested_reason") or "").strip()
        if _parse_iso(str(item.get("requested_at") or "")) is not None and reason:
            return reason
    return "scheduled"


def _patch_cookie_source_sync_state(platform: str, **changes: Any) -> dict[str, Any]:
    clean_platform = "global" if str(platform or "").strip().lower() == "global" else "cn"
    state = _read_cookie_source_sync_state()
    item = dict(state.get(clean_platform) or {})
    for key, value in changes.items():
        if value is None:
            continue
        item[key] = value
    state[clean_platform] = item
    return _write_cookie_source_sync_state(state)


def _validate_cron(cron_expr: str) -> str:
    clean = str(cron_expr or "").strip() or DEFAULT_SUBSCRIPTION_CRON
    try:
        croniter(clean, _now())
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Cron 表达式无效：{exc}") from exc
    return clean


def _next_run_at(cron_expr: str, base: Optional[datetime] = None) -> str:
    normalized = _validate_cron(cron_expr)
    return ensure_timezone(croniter(normalized, base or _now()).get_next(datetime)).isoformat()


def _select_cookie(url: str, config) -> str:
    netloc = urlparse(url).netloc.lower()
    platform = "global" if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc else "cn"
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return sanitize_cookie_header(cookie_map.get(platform) or "")


def _account_profile_fallback(cookie_pair: Any) -> dict[str, str]:
    if cookie_pair is None:
        return {}
    account_id = str(getattr(cookie_pair, "account_id", "") or "").strip()
    handle = str(getattr(cookie_pair, "handle", "") or "").strip().lstrip("@")
    return {
        "uid": account_id,
        "handle": handle,
        "name": str(getattr(cookie_pair, "display_name", "") or getattr(cookie_pair, "username", "") or "").strip(),
        "avatar_url": str(getattr(cookie_pair, "avatar_url", "") or "").strip(),
    }


def _account_platform_label(platform: str) -> str:
    return "国际" if str(platform or "").strip().lower() == "global" else "国内"


def _account_sync_success_message(platform: str) -> str:
    return f"{_account_platform_label(platform)}账号已同步，账号信息已更新。"


def _merge_cookie_account_profile(discovered: dict[str, Any], fallback: dict[str, str]) -> dict[str, Any]:
    profile = dict(discovered or {})
    for key, value in (fallback or {}).items():
        if value and not str(profile.get(key) or "").strip():
            profile[key] = value
    merged: dict[str, Any] = {
        "platform": str(profile.get("platform") or ""),
        "uid": str(profile.get("uid") or ""),
        "handle": str(profile.get("handle") or "").lstrip("@"),
        "name": str(profile.get("name") or ""),
        "avatar_url": str(profile.get("avatar_url") or profile.get("avatar") or ""),
    }
    for key in ("follow_count", "liked_collection_count", "collection_count"):
        value = _first_non_negative_int(profile.get(key))
        if value is not None:
            merged[key] = value
    return merged


def _merge_cookie_account_summary(profile: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(profile or {})
    for key in ("uid", "handle", "name", "avatar_url"):
        value = str((summary or {}).get(key) or "").strip()
        if value and not str(merged.get(key) or "").strip():
            merged[key] = value
    result: dict[str, Any] = {
        "platform": str(merged.get("platform") or (summary or {}).get("platform") or ""),
        "uid": str(merged.get("uid") or ""),
        "handle": str(merged.get("handle") or "").lstrip("@"),
        "name": str(merged.get("name") or ""),
        "avatar_url": str(merged.get("avatar_url") or merged.get("avatar") or ""),
    }
    for key in ("follow_count", "liked_collection_count", "collection_count"):
        value = _first_non_negative_int(merged.get(key), (summary or {}).get(key))
        if value is not None:
            result[key] = value
    return result


def _first_non_negative_int(*values: Any) -> Optional[int]:
    first_zero: Optional[int] = None
    for value in values:
        if value is None or value == "":
            continue
        try:
            parsed = int(str(value).replace(",", "").strip())
        except Exception:
            continue
        if parsed > 0:
            return parsed
        if parsed == 0 and first_zero is None:
            first_zero = parsed
    return first_zero


def _canonical_subscription_url(url: str, mode: str = "") -> str:
    clean_url = normalize_source_url(url)
    if (mode or _detect_subscription_mode(clean_url)) == "collection_models":
        return _default_favorites_identity_url(clean_url)
    return clean_url


def _subscription_identity_url(url: str, mode: str = "") -> str:
    return _canonical_subscription_url(url, mode)


def _is_synthetic_user_handle(value: Any) -> bool:
    return re.fullmatch(r"user_\d+", str(value or "").strip().lstrip("@"), flags=re.I) is not None


def _source_url_has_synthetic_user_handle(url: str) -> bool:
    parsed = urlparse(normalize_source_url(url))
    return any(part.startswith("@") and _is_synthetic_user_handle(part[1:]) for part in parsed.path.split("/"))


def _is_legacy_error_synthetic_user_subscription(record: SubscriptionRecord, state: dict[str, Any], platform: str) -> bool:
    if str(record.mode or "").strip() != "author_upload":
        return False
    if _platform_for_url(record.url) != platform:
        return False
    if not _source_url_has_synthetic_user_handle(record.url):
        return False
    return str((state or {}).get("status") or "").strip() == "error"


def _subscription_identity_key(record: SubscriptionRecord) -> tuple[str, str]:
    return (str(record.mode or "").strip(), _subscription_identity_url(record.url, record.mode))


def _state_items_count(state: dict, key: str) -> int:
    value = state.get(key)
    return len(value) if isinstance(value, list) else 0


def _state_deleted_count(state: dict) -> int:
    current_items = _normalize_source_items(state.get("current_items") or [])
    tracked_items = _normalize_source_items(state.get("tracked_items") or [])
    return len(_deleted_source_items(current_items, tracked_items))


def _state_sort_time(value: Any) -> datetime:
    parsed = _parse_iso(str(value or ""))
    return parsed or datetime.min.replace(tzinfo=china_now().tzinfo)


def _pick_duplicate_subscription_state(states: list[dict]) -> dict:
    if not states:
        return {}
    return max(
        states,
        key=lambda item: (
            _state_items_count(item, "current_items"),
            -_state_deleted_count(item),
            _state_items_count(item, "tracked_items"),
            _state_sort_time(item.get("last_success_at") or item.get("last_run_at")),
        ),
    )


def _better_default_favorite_name(current: str, candidate: str) -> str:
    current_text = str(current or "").strip()
    candidate_text = str(candidate or "").strip()
    if not candidate_text:
        return current_text
    if not current_text:
        return candidate_text
    if "的收藏夹" in candidate_text and "的收藏夹" not in current_text:
        return candidate_text
    if "所有模型收藏夹" in candidate_text and "所有模型收藏夹" not in current_text:
        return candidate_text
    return current_text


def _platform_for_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return "global" if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc else "cn"


def _build_source_item(url: str) -> dict:
    normalized_url = normalize_source_url(url)
    model_id = extract_model_id(normalized_url)
    task_key = f"model:{model_id}" if model_id else normalized_url
    return {
        "model_id": model_id,
        "url": normalized_url,
        "task_key": task_key,
    }


def _normalize_source_items(items: list[Any]) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in items or []:
        if isinstance(raw, dict):
            if raw.get("task_key") or raw.get("url") or raw.get("model_id"):
                item = {
                    "model_id": str(raw.get("model_id") or "").strip(),
                    "url": normalize_source_url(str(raw.get("url") or "")),
                    "task_key": str(raw.get("task_key") or "").strip(),
                }
                for meta_key in ("source_order", "favorited_at", "source_position"):
                    if raw.get(meta_key) not in (None, ""):
                        item[meta_key] = raw.get(meta_key)
                if not item["task_key"]:
                    if item["model_id"]:
                        item["task_key"] = f"model:{item['model_id']}"
                    elif item["url"]:
                        item["task_key"] = item["url"]
            else:
                continue
        else:
            item = _build_source_item(str(raw or ""))
        key = str(item.get("task_key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    return normalized


def _merge_source_items(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in _normalize_source_items(group):
            key = item["task_key"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _deleted_source_items(current_items: list[dict], tracked_items: list[dict]) -> list[dict]:
    current_keys = {item["task_key"] for item in _normalize_source_items(current_items)}
    return [item for item in _normalize_source_items(tracked_items) if item["task_key"] not in current_keys]


def _source_item_lookup_keys(items: list[Any]) -> set[str]:
    keys: set[str] = set()
    for item in _normalize_source_items(items):
        task_key = str(item.get("task_key") or "").strip()
        model_id = str(item.get("model_id") or "").strip()
        url = normalize_source_url(str(item.get("url") or ""))
        if task_key:
            keys.add(task_key)
        if model_id:
            keys.add(f"model:{model_id}")
        if url:
            keys.add(url)
    return keys


def _archive_model_keys(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    model_id = str(item.get("id") or "").strip()
    origin_url = normalize_source_url(str(item.get("origin_url") or ""))
    if model_id:
        keys.add(f"model:{model_id}")
    if origin_url:
        keys.add(origin_url)
    return keys


def _strict_expected_total(discovered: dict) -> Optional[int]:
    if not isinstance(discovered, dict):
        return None
    expected_total = discovered.get("expected_total")
    if not isinstance(expected_total, int) or expected_total <= 0:
        return None
    expected_total_source = str(discovered.get("expected_total_source") or "").strip()
    if bool(discovered.get("strict_expected_total")) or expected_total_source in STRICT_EXPECTED_TOTAL_SOURCES:
        return expected_total
    return None


def _partial_scan_message(context: dict[str, Any]) -> str:
    reason = str(context.get("reason") or "")
    mode = str(context.get("mode") or "")
    current_count = int(context.get("current_count") or 0)
    source_label = "收藏夹" if mode == "collection_models" else "作者页" if mode == "author_upload" else "源端"
    if reason == "expected_total_mismatch":
        expected_total = int(context.get("expected_total") or 0)
        return (
            f"订阅扫描结果异常：源端显示 {expected_total} 个模型，本次仅扫描到 {current_count} 个，"
            f"疑似{source_label}接口返回不完整，已保留历史跟踪状态。"
        )

    previous_count = int(context.get("previous_count") or 0)
    return (
        f"订阅扫描结果异常：本次仅扫描到 {current_count} 个，低于历史跟踪 {previous_count} 个，"
        f"疑似{source_label}接口返回不完整，已保留历史跟踪状态。"
    )


def _subscription_partial_scan_context(
    subscription: SubscriptionRecord,
    current_items: list[dict],
    previous_state: dict,
    discovered: dict,
) -> Optional[dict[str, Any]]:
    normalized_current_items = _normalize_source_items(current_items)
    current_count = len(normalized_current_items)
    expected_total = _strict_expected_total(discovered)
    if expected_total is not None and current_count < expected_total:
        previous_tracked_items = _normalize_source_items(previous_state.get("tracked_items") or [])
        return {
            "reason": "expected_total_mismatch",
            "mode": subscription.mode,
            "current_count": current_count,
            "previous_count": len(previous_tracked_items),
            "expected_total": expected_total,
            "expected_total_source": str(discovered.get("expected_total_source") or ""),
            "previous_tracked_items": previous_tracked_items,
        }

    if subscription.mode != "collection_models":
        return None

    previous_tracked_items = _normalize_source_items(previous_state.get("tracked_items") or [])
    previous_count = len(previous_tracked_items)
    if previous_count < COLLECTION_PARTIAL_SCAN_MIN_TRACKED:
        return None
    threshold = max(5, int(previous_count * COLLECTION_PARTIAL_SCAN_RATIO))
    if current_count >= threshold:
        return None
    return {
        "reason": "history_drop",
        "mode": subscription.mode,
        "current_count": current_count,
        "previous_count": previous_count,
        "threshold": threshold,
        "expected_total": discovered.get("expected_total") if isinstance(discovered, dict) else None,
        "expected_total_source": str(discovered.get("expected_total_source") or "") if isinstance(discovered, dict) else "",
        "previous_tracked_items": previous_tracked_items,
    }


def _detect_subscription_mode(url: str) -> str:
    mode = detect_archive_mode(url)
    if mode in SUBSCRIPTION_MODES:
        return mode
    return ""


def _default_subscription_name(url: str, mode: str) -> str:
    parsed = urlparse(normalize_source_url(url))
    handle = ""
    for part in parsed.path.split("/"):
        if part.startswith("@"):
            handle = part.lstrip("@")
            break

    if mode == "collection_models":
        return f"{handle or 'MakerWorld'} 收藏夹订阅"
    if mode == "author_upload":
        return f"{handle or 'MakerWorld'} 作者订阅"
    return handle or parsed.netloc or "订阅任务"


def _subscription_import_name(source: dict[str, Any], mode: str, platform: str) -> str:
    title = str(source.get("title") or source.get("name") or "").strip()
    handle = str(source.get("handle") or "").strip().lstrip("@")
    site_label = "国际" if platform == "global" else "国内"
    if mode == "author_upload":
        return f"{title or handle or 'MakerWorld'} 作者订阅"
    if "的收藏夹" in title:
        return title
    if "所有模型" in title:
        return f"{site_label} {title}".strip()
    return f"{title or handle or 'MakerWorld'} 收藏夹订阅"


def _source_metadata_seed(source: dict[str, Any], mode: str, site: str, canonical_url: str) -> dict[str, Any]:
    title = str(source.get("title") or source.get("name") or "").strip()
    avatar_url = str(source.get("avatar_url") or source.get("avatar") or source.get("avatarUrl") or "").strip()
    cover_url = str(source.get("cover_url") or source.get("coverUrl") or source.get("imageUrl") or "").strip()
    count = source.get("count") or source.get("model_count") or source.get("remote_model_count")
    payload: dict[str, Any] = {
        "kind": "author" if mode == "author_upload" else "favorite" if "/collections/models" in canonical_url else "collection",
        "canonical_url": canonical_url,
        "site": site,
        "error": "",
    }
    if title:
        payload["title"] = title
    if avatar_url:
        payload["avatar_url"] = avatar_url
    if cover_url:
        payload["cover_url"] = cover_url
    if count not in (None, ""):
        payload["remote_model_count"] = count
    return payload


class SubscriptionManager:
    def __init__(
        self,
        archive_manager: ArchiveTaskManager,
        store: Optional[JsonStore] = None,
        task_store: Optional[TaskStateStore] = None,
        *,
        background_enabled: Optional[bool] = None,
    ) -> None:
        self.archive_manager = archive_manager
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self.background_enabled = BACKGROUND_TASKS_ENABLED if background_enabled is None else bool(background_enabled)
        self._loop_lock = threading.Lock()
        self._running_id = ""
        self._cookie_source_sync_running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._ensure_state_records()
        if not self.background_enabled:
            return
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, name="makerhub-subscriptions", daemon=True)
            self._thread.start()

    def list_payload(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE,
        limit: int = 0,
    ) -> dict:
        self._ensure_state_records()
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        items = [self._merge_subscription_record(item, state_map.get(item.id)) for item in config.subscriptions]
        overview = build_subscription_overview_payload(store=self.store, task_store=self.task_store)
        effective_page, effective_page_size = _normalize_subscription_source_page(page, page_size)
        effective_limit = max(int(limit or 0), 0)
        pagination_page = 1 if effective_limit > 0 else effective_page
        pagination_page_size = min(effective_limit, 2000) if effective_limit > 0 else effective_page_size
        sections = _paginate_subscription_source_sections(
            list(overview.get("sections") or []),
            page=pagination_page,
            page_size=pagination_page_size,
            max_page_size=2000 if effective_limit > 0 else MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE,
        )
        if effective_limit > 0:
            for section in sections:
                if not isinstance(section, dict) or section.get("key") != "subscription_sources":
                    continue
                section["page"] = effective_page
                section["page_size"] = effective_page_size
                section["has_more"] = min(effective_limit, 2000) < int(section.get("total") or 0)
        return {
            "items": items,
            "count": len(items),
            "summary": {
                "enabled": len([item for item in items if item["enabled"]]),
                "running": len([item for item in items if item["running"]]),
                "deleted_marked": sum(int(item.get("deleted_count") or 0) for item in items),
            },
            "sections": sections,
            "settings": overview.get("settings") or config.subscription_settings.model_dump(),
        }

    def list_light_payload(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE,
        limit: int = 0,
    ) -> dict:
        self._ensure_state_records()
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        items = [self._merge_subscription_record_light(item, state_map.get(item.id)) for item in config.subscriptions]
        overview = build_subscription_overview_light_payload(store=self.store, task_store=self.task_store)
        effective_page, effective_page_size = _normalize_subscription_source_page(page, page_size)
        effective_limit = max(int(limit or 0), 0)
        pagination_page = 1 if effective_limit > 0 else effective_page
        pagination_page_size = min(effective_limit, 2000) if effective_limit > 0 else effective_page_size
        sections = _paginate_subscription_source_sections(
            list(overview.get("sections") or []),
            page=pagination_page,
            page_size=pagination_page_size,
            max_page_size=2000 if effective_limit > 0 else MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE,
        )
        if effective_limit > 0:
            for section in sections:
                if not isinstance(section, dict) or section.get("key") != "subscription_sources":
                    continue
                section["page"] = effective_page
                section["page_size"] = effective_page_size
                section["has_more"] = min(effective_limit, 2000) < int(section.get("total") or 0)
        return {
            "items": items,
            "count": len(items),
            "summary": {
                "enabled": len([item for item in items if item["enabled"]]),
                "running": len([item for item in items if item["running"]]),
                "deleted_marked": sum(int(item.get("deleted_count") or 0) for item in items),
            },
            "sections": sections,
            "settings": overview.get("settings") or config.subscription_settings.model_dump(),
            "light": True,
        }

    def create_subscription(
        self,
        *,
        url: str,
        cron: str = DEFAULT_SUBSCRIPTION_CRON,
        name: str = "",
        enabled: bool = True,
        initialize_from_source: bool = True,
    ) -> dict:
        clean_url = _canonical_subscription_url(url)
        mode = _detect_subscription_mode(clean_url)
        if mode not in SUBSCRIPTION_MODES:
            raise ValueError("仅支持作者上传页或收藏夹模型页订阅。")

        normalized_cron = _validate_cron(cron)
        config = self.store.load()
        existing = next(
            (
                item
                for item in config.subscriptions
                if _subscription_identity_url(item.url, item.mode) == _subscription_identity_url(clean_url, mode)
            ),
            None,
        )
        now_iso = _now_iso()
        created_new = False
        initial_enqueue_count = 0

        if existing:
            existing.name = str(name or existing.name or _default_subscription_name(clean_url, mode)).strip()
            existing.cron = normalized_cron
            existing.enabled = bool(enabled)
            existing.updated_at = now_iso
            record = existing
        else:
            record = SubscriptionRecord(
                id=uuid.uuid4().hex,
                name=str(name or _default_subscription_name(clean_url, mode)).strip(),
                url=clean_url,
                mode=mode,
                cron=normalized_cron,
                enabled=bool(enabled),
                created_at=now_iso,
                updated_at=now_iso,
            )
            config.subscriptions.append(record)
            created_new = True

        self.store.save(config)

        try:
            if initialize_from_source and not self.background_enabled:
                initialized = self.task_store.patch_subscription_state(
                    record.id,
                    status="pending",
                    running=False,
                    manual_requested_at=now_iso,
                    next_run_at=now_iso,
                    last_message="订阅已创建，等待后台 worker 执行首次扫描。",
                )
            elif initialize_from_source:
                initialized = self._initialize_subscription_state(record)
                initialized = self._enqueue_initialized_subscription_items(record, initialized)
                initial_enqueue_count = int(initialized.get("last_enqueued_count") or 0)
                self._refresh_source_preview_snapshots(record.id, subscription=record)
            else:
                initialized = self.task_store.patch_subscription_state(
                    record.id,
                    status="idle",
                    running=False,
                    next_run_at=_next_run_at(record.cron),
                    last_message="订阅已创建，等待首次定时同步。",
                )
        except Exception:
            if created_new:
                rollback = self.store.load()
                rollback.subscriptions = [item for item in rollback.subscriptions if item.id != record.id]
                self.store.save(rollback)
                self.task_store.remove_subscription_state([record.id])
            raise

        self.start()
        payload = self.list_payload()
        return {
            "success": True,
            "subscription": next((item for item in payload["items"] if item["id"] == record.id), None),
            "subscriptions": payload,
            "message": (
                "订阅已创建，等待后台 worker 首次扫描。"
                if not existing and initialize_from_source and not self.background_enabled
                else "订阅已更新，等待后台 worker 首次扫描。"
                if initialize_from_source and not self.background_enabled
                else
                f"订阅已创建，首次扫描已入队 {initial_enqueue_count} 个模型。"
                if not existing and initialize_from_source
                else f"订阅已更新，首次扫描已入队 {initial_enqueue_count} 个模型。"
                if initialize_from_source
                else "订阅已创建。"
                if not existing
                else "订阅已更新。"
            ),
            "initialized": initialized,
        }

    def update_subscription(self, subscription_id: str, *, url: str, name: str, cron: str, enabled: bool) -> dict:
        clean_id = str(subscription_id or "").strip()
        if not clean_id:
            raise ValueError("缺少订阅 ID。")

        clean_url = _canonical_subscription_url(url)
        mode = _detect_subscription_mode(clean_url)
        if mode not in SUBSCRIPTION_MODES:
            raise ValueError("仅支持作者上传页或收藏夹模型页订阅。")

        normalized_cron = _validate_cron(cron)
        config = self.store.load()
        target = next((item for item in config.subscriptions if item.id == clean_id), None)
        if target is None:
            raise ValueError("订阅不存在。")

        duplicate = next(
            (
                item
                for item in config.subscriptions
                if item.id != clean_id
                and _subscription_identity_url(item.url, item.mode) == _subscription_identity_url(clean_url, mode)
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("该链接已存在订阅。")

        previous = target.model_copy(deep=True)
        url_changed = _subscription_identity_url(target.url, target.mode) != _subscription_identity_url(clean_url, mode)

        target.url = clean_url
        target.mode = mode
        target.name = str(name or target.name or _default_subscription_name(target.url, target.mode)).strip()
        target.cron = normalized_cron
        target.enabled = bool(enabled)
        target.updated_at = _now_iso()
        self.store.save(config)

        if url_changed and not self.background_enabled:
            self.task_store.patch_subscription_state(
                target.id,
                status="pending",
                running=False,
                manual_requested_at=_now_iso(),
                next_run_at=_now_iso(),
                last_message="订阅链接已更新，等待后台 worker 重新初始化。",
            )
        elif url_changed:
            try:
                self._initialize_subscription_state(target)
            except Exception:
                rollback = self.store.load()
                rollback_target = next((item for item in rollback.subscriptions if item.id == clean_id), None)
                if rollback_target is not None:
                    rollback_target.url = previous.url
                    rollback_target.mode = previous.mode
                    rollback_target.name = previous.name
                    rollback_target.cron = previous.cron
                    rollback_target.enabled = previous.enabled
                    rollback_target.updated_at = previous.updated_at
                    self.store.save(rollback)
                raise
            self.task_store.patch_subscription_state(
                target.id,
                last_message="订阅已更新，并已按新链接重新初始化。",
            )
            self._refresh_source_preview_snapshots(target.id, subscription=target)
        else:
            updates = {
                "next_run_at": _next_run_at(target.cron) if target.enabled else "",
                "last_message": "订阅已保存。",
            }
            if not target.enabled:
                updates["running"] = False
                updates["manual_requested_at"] = ""
            self.task_store.patch_subscription_state(target.id, **updates)

        self.start()

        return {
            "success": True,
            "subscription": next((item for item in self.list_payload()["items"] if item["id"] == clean_id), None),
            "message": "订阅已更新。",
        }

    def delete_subscription(self, subscription_id: str) -> dict:
        clean_id = str(subscription_id or "").strip()
        if not clean_id:
            raise ValueError("缺少订阅 ID。")

        config = self.store.load()
        before = len(config.subscriptions)
        config.subscriptions = [item for item in config.subscriptions if item.id != clean_id]
        if len(config.subscriptions) == before:
            raise ValueError("订阅不存在。")
        self.store.save(config)
        self.task_store.remove_subscription_state([clean_id])
        return {
            "success": True,
            "message": "订阅已删除。",
            "subscriptions": self.list_payload(),
        }

    def remove_account_imported_subscriptions(self, platform: str) -> dict:
        clean_platform = "global" if str(platform or "").strip().lower() == "global" else "cn"
        inventory = _read_cookie_source_inventory_state()
        platform_inventory = dict((inventory.get("platforms") or {}).get(clean_platform) or {})
        imported_sources = [
            item
            for item in platform_inventory.get("imported_sources") or []
            if isinstance(item, dict)
        ]
        imported_urls = {
            _subscription_identity_url(str(item.get("url") or ""), str(item.get("mode") or ""))
            for item in imported_sources
            if str(item.get("url") or "").strip()
        }
        imported_urls.update(
            _subscription_identity_url(str(url or ""), _detect_subscription_mode(str(url or "")))
            for url in platform_inventory.get("source_urls") or []
            if str(url or "").strip()
        )
        imported_urls = {url for url in imported_urls if url}

        imported_ids = {
            str(item.get("subscription_id") or "").strip()
            for item in imported_sources
            if str(item.get("subscription_id") or "").strip()
        }

        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {
            str(item.get("id") or ""): item
            for item in state_payload.get("items") or []
        }

        removed_records: list[SubscriptionRecord] = []
        kept_records: list[SubscriptionRecord] = []
        for record in config.subscriptions:
            record_url = _subscription_identity_url(record.url, record.mode)
            if (
                record.id in imported_ids
                or record_url in imported_urls
            ) and _platform_for_url(record.url) == clean_platform:
                removed_records.append(record)
                continue
            kept_records.append(record)

        removed_ids = [item.id for item in removed_records]
        source_keys: set[str] = set()
        for subscription_id in removed_ids:
            state = state_map.get(subscription_id) or {}
            source_keys.update(_source_item_lookup_keys(state.get("current_items") or []))
            source_keys.update(_source_item_lookup_keys(state.get("tracked_items") or []))

        deleted_model_dirs: list[str] = []
        if source_keys:
            archive_snapshot = get_archive_snapshot()
            for model in archive_snapshot.get("models") or []:
                model_dir = str(model.get("model_dir") or "").strip().strip("/")
                if not model_dir:
                    continue
                if not (_archive_model_keys(model) & source_keys):
                    continue

                self.task_store.update_model_flag(model_dir, "deleted", True)
                model_id = str(model.get("id") or "").strip()
                origin_url = normalize_source_url(str(model.get("origin_url") or ""))
                if model_id:
                    self.task_store.remove_missing_3mf_for_model(model_id)
                self.task_store.remove_recent_failures_for_model(model_id, url=origin_url)
                invalidate_model_detail_cache(model_dir)
                deleted_model_dirs.append(model_dir)

        if removed_records:
            config.subscriptions = kept_records
            self.store.save(config)
            self.task_store.remove_subscription_state(removed_ids)

        if deleted_model_dirs:
            invalidate_archive_snapshot("online_account_deleted")

        now_iso = _now_iso()
        platforms = dict(inventory.get("platforms") or {})
        platform_inventory.update(
            {
                "followed_authors": [],
                "followed_author_sources": {},
                "followed_collections": [],
                "followed_author_count": 0,
                "followed_collection_count": 0,
                "imported_sources": [],
                "source_urls": [],
                "last_status": "deleted",
                "last_message": "线上账号已删除，关注来源订阅已移除。",
                "deleted_at": now_iso,
                "updated_at": now_iso,
            }
        )
        platforms[clean_platform] = platform_inventory
        _write_cookie_source_inventory_state({"platforms": platforms, "updated_at": now_iso})
        _patch_cookie_source_sync_state(
            clean_platform,
            requested_at="",
            requested_reason="",
            last_status="deleted",
            last_message="线上账号已删除，关注来源同步已停止。",
        )

        _append_subscription_log(
            "online_account_sources_removed",
            platform=clean_platform,
            subscription_ids=removed_ids,
            subscription_count=len(removed_ids),
            local_deleted_count=len(deleted_model_dirs),
        )
        append_business_log(
            "subscription",
            "online_account_sources_removed",
            "线上账号删除后，已移除该账号导入的关注来源订阅，并把对应模型标记为本地删除。",
            platform=clean_platform,
            subscription_count=len(removed_ids),
            local_deleted_count=len(deleted_model_dirs),
        )

        return {
            "success": True,
            "platform": clean_platform,
            "removed_subscription_ids": removed_ids,
            "removed_subscription_count": len(removed_ids),
            "local_deleted_model_dirs": deleted_model_dirs,
            "local_deleted_count": len(deleted_model_dirs),
            "message": (
                f"已移除 {len(removed_ids)} 个账号关注来源订阅，并标记 {len(deleted_model_dirs)} 个模型为本地删除。"
            ),
        }

    def request_sync(self, subscription_id: str) -> dict:
        clean_id = str(subscription_id or "").strip()
        if not clean_id:
            raise ValueError("缺少订阅 ID。")

        config = self.store.load()
        target = next((item for item in config.subscriptions if item.id == clean_id), None)
        if target is None:
            raise ValueError("订阅不存在。")

        self.task_store.patch_subscription_state(
            clean_id,
            manual_requested_at=_now_iso(),
            last_message="已手动触发同步，等待调度器执行。",
        )
        self.start()
        if self.background_enabled:
            self._maybe_launch_due_sync()
        return {
            "success": True,
            "message": "订阅同步已触发。",
            "subscription": next((item for item in self.list_payload()["items"] if item["id"] == clean_id), None),
        }

    def pick_runtime_subscriptions(self, context: dict) -> list[dict]:
        source_id = str(context.get("source_id") or context.get("subscription_id") or "").strip()
        payload = self.list_payload()
        items = payload.get("items") if isinstance(payload, dict) else []
        candidates = [item for item in items or [] if isinstance(item, dict) and item.get("enabled", True)]
        if source_id:
            candidates = [
                item
                for item in candidates
                if str(item.get("subscription_id") or item.get("id") or "") == source_id
            ]
        return candidates

    def sync_subscription_runtime(self, item: dict, context: dict) -> dict:
        subscription_id = str(item.get("subscription_id") or item.get("id") or "").strip()
        if not subscription_id:
            return {"success": False, "message": "缺少订阅 ID。"}
        self._sync_subscription(subscription_id)
        return {"success": True, "subscription_id": subscription_id}

    def retry_error_subscriptions_for_platforms(self, platforms: set[str]) -> dict:
        normalized_platforms = {str(item or "").strip().lower() for item in platforms if str(item or "").strip()}
        normalized_platforms = {item for item in normalized_platforms if item in {"cn", "global"}}
        if not normalized_platforms:
            return {"queued_count": 0, "subscription_ids": []}

        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        now_iso = _now_iso()
        queued_ids: list[str] = []

        for item in config.subscriptions:
            if not item.enabled:
                continue
            if _platform_for_url(item.url) not in normalized_platforms:
                continue
            state = state_map.get(item.id) or {}
            if bool(state.get("running")) or str(state.get("status") or "") != "error":
                continue
            self.task_store.patch_subscription_state(
                item.id,
                manual_requested_at=now_iso,
                next_run_at=now_iso,
                last_message="Cookie 已更新，已自动安排失败订阅重试。",
            )
            queued_ids.append(item.id)

        if queued_ids:
            _append_subscription_log(
                "cookie_update_retry_queued",
                platforms=sorted(normalized_platforms),
                subscription_ids=queued_ids,
                count=len(queued_ids),
            )
            self.start()
            if self.background_enabled:
                self._maybe_launch_due_sync()

        return {"queued_count": len(queued_ids), "subscription_ids": queued_ids}

    def _remove_imported_synthetic_user_subscriptions(self, platform: str, inventory_item: dict[str, Any]) -> tuple[int, list[str]]:
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {
            str(item.get("id") or ""): item
            for item in state_payload.get("items") or []
            if isinstance(item, dict)
        }
        removed_ids: list[str] = []
        kept_records: list[SubscriptionRecord] = []
        for record in config.subscriptions:
            if _is_legacy_error_synthetic_user_subscription(record, state_map.get(record.id) or {}, platform):
                removed_ids.append(record.id)
                continue
            kept_records.append(record)

        if not removed_ids:
            return 0, []

        config.subscriptions = kept_records
        self.store.save(config)
        self.task_store.remove_subscription_state(removed_ids)
        _append_subscription_log(
            "cookie_source_synthetic_user_subscriptions_removed",
            platform=platform,
            count=len(removed_ids),
            subscription_ids=removed_ids,
        )
        append_business_log(
            "subscription",
            "cookie_source_synthetic_user_subscriptions_removed",
            "已清理账号关注同步误导入的无效 user_数字作者订阅。",
            platform=platform,
            subscription_count=len(removed_ids),
        )
        return len(removed_ids), removed_ids

    def request_cookie_source_sync(self, platforms: set[str], *, reason: str = "manual") -> dict:
        normalized_platforms = {str(item or "").strip().lower() for item in platforms if str(item or "").strip()}
        normalized_platforms = {item for item in normalized_platforms if item in {"cn", "global"}}
        if not normalized_platforms:
            return {"queued_count": 0, "platforms": []}

        now_iso = _now_iso()
        clean_reason = str(reason or "manual").strip() or "manual"
        for platform in sorted(normalized_platforms):
            _patch_cookie_source_sync_state(
                platform,
                requested_at=now_iso,
                requested_reason=clean_reason,
                last_status="pending",
                last_message="Cookie 已保存，关注作者和收藏夹同步已交给 worker 后台处理。",
            )

        _append_subscription_log(
            "cookie_source_sync_requested",
            platforms=sorted(normalized_platforms),
            reason=clean_reason,
            queued_count=len(normalized_platforms),
        )
        self.start()
        return {"queued_count": len(normalized_platforms), "platforms": sorted(normalized_platforms)}

    def sync_cookie_sources(self, platforms: set[str], *, reason: str = "manual") -> dict:
        with self._loop_lock:
            if getattr(self, "_cookie_source_sync_running", False):
                return {
                    "created_count": 0,
                    "updated_count": 0,
                    "platforms": [],
                    "skipped": True,
                    "message": "关注来源同步已在运行。",
                }
            self._cookie_source_sync_running = True

        try:
            return self._sync_cookie_sources(platforms, reason=reason)
        finally:
            with self._loop_lock:
                self._cookie_source_sync_running = False

    def _sync_cookie_sources(self, platforms: set[str], *, reason: str = "manual") -> dict:
        normalized_platforms = {str(item or "").strip().lower() for item in platforms if str(item or "").strip()}
        normalized_platforms = {item for item in normalized_platforms if item in {"cn", "global"}}
        if not normalized_platforms:
            return {"created_count": 0, "updated_count": 0, "platforms": []}

        config = self.store.load()
        cookie_pairs = {
            str(item.platform or "").strip().lower(): item
            for item in getattr(config, "cookies", [])
        }
        cookie_map = {
            platform: sanitize_cookie_header(getattr(item, "cookie", "") or "")
            for platform, item in cookie_pairs.items()
        }
        default_cron = _validate_cron(getattr(config.subscription_settings, "default_cron", "") or DEFAULT_SUBSCRIPTION_CRON)
        default_enabled = bool(getattr(config.subscription_settings, "default_enabled", True))
        now_iso = _now_iso()

        created_ids: list[str] = []
        updated_ids: list[str] = []
        platform_results: list[dict[str, Any]] = []
        changed = False

        for platform in sorted(normalized_platforms):
            raw_cookie = cookie_map.get(platform) or ""
            if not raw_cookie:
                platform_results.append(
                    {
                        "platform": platform,
                        "status": "skipped",
                        "message": "未配置 Cookie。",
                        "created_count": 0,
                        "updated_count": 0,
                    }
                )
                continue

            try:
                inventory = _read_cookie_source_inventory_state()
                previous_platform_inventory = dict((inventory.get("platforms") or {}).get(platform) or {})
                removed_synthetic_count, removed_synthetic_ids = self._remove_imported_synthetic_user_subscriptions(
                    platform,
                    previous_platform_inventory,
                )
                if removed_synthetic_count:
                    config = self.store.load()
                    changed = True

                with temporary_proxy_env(config.proxy, "", platform=platform):
                    profile = _merge_cookie_account_profile(
                        discover_cookie_account_profile(platform, raw_cookie),
                        _account_profile_fallback(cookie_pairs.get(platform)),
                    )
                    account_summary = discover_cookie_account_home_summary(platform, raw_cookie, profile)
                    profile = _merge_cookie_account_summary(profile, account_summary)
                    uid = str(profile.get("uid") or "").strip()
                    sources: list[dict[str, Any]] = []

                    default_favorites = default_favorites_subscription_source(platform, profile)
                    if default_favorites.get("url"):
                        sources.append({**default_favorites, "mode": "collection_models", "source_kind": "default_favorites"})

                    followed_authors_page = discover_cookie_followed_authors_from_page(platform, raw_cookie, profile)
                    followed_authors = discover_cookie_followed_authors(platform, raw_cookie, uid=uid)
                    followed_author_items: list[dict[str, Any]] = []
                    followed_author_urls: set[str] = set()
                    followed_author_keys: set[str] = set()
                    for author_result in (followed_authors_page, followed_authors):
                        for author in author_result.get("items") or []:
                            url = normalize_source_url(str(author.get("url") or ""))
                            if not url:
                                continue
                            author_uid = str(author.get("uid") or "").strip()
                            author_handle = str(author.get("handle") or "").strip().lower()
                            author_key = f"uid:{author_uid}" if author_uid else f"handle:{author_handle}" if author_handle else url
                            if not url or url in followed_author_urls or author_key in followed_author_keys:
                                continue
                            followed_author_urls.add(url)
                            followed_author_keys.add(author_key)
                            followed_author_items.append({**author, "url": url})
                    for item in followed_author_items:
                        if item.get("url"):
                            sources.append({**item, "mode": "author_upload", "source_kind": "followed_author"})

                    followed_collections = discover_cookie_followed_collections(platform, raw_cookie, uid=uid)
                    for item in followed_collections.get("items") or []:
                        if item.get("url"):
                            sources.append({**item, "mode": "collection_models", "source_kind": "followed_collection"})
                    followed_author_count = _first_non_negative_int(
                        profile.get("follow_count") if isinstance(profile, dict) else None,
                        account_summary.get("follow_count") if isinstance(account_summary, dict) else None,
                        (followed_authors_page or {}).get("total"),
                        (followed_authors or {}).get("total"),
                        (followed_authors_page or {}).get("count"),
                        (followed_authors or {}).get("count"),
                        len(followed_author_items),
                    ) or 0
                    followed_collection_count = _first_non_negative_int(
                        profile.get("liked_collection_count") if isinstance(profile, dict) else None,
                        account_summary.get("liked_collection_count") if isinstance(account_summary, dict) else None,
                        (followed_collections or {}).get("count"),
                        len((followed_collections or {}).get("items") or []),
                    ) or 0
                    skipped_author_count = max(followed_author_count - len(followed_author_items), 0)

                platform_created = 0
                platform_updated = 0
                seen_urls: set[str] = set()
                imported_sources: list[dict[str, Any]] = []
                for source in sources:
                    raw_url = normalize_source_url(str(source.get("url") or ""))
                    mode = str(source.get("mode") or _detect_subscription_mode(raw_url)).strip()
                    clean_url = _canonical_subscription_url(raw_url, mode)
                    if not clean_url or clean_url in seen_urls:
                        continue
                    seen_urls.add(clean_url)
                    if mode not in SUBSCRIPTION_MODES:
                        continue
                    source_key = source_identity_key(clean_url, mode)
                    if source_key:
                        _save_source_metadata_item(
                            source_key,
                            _source_metadata_seed(source, mode, platform, clean_url),
                        )
                    existing = next(
                        (
                            item
                            for item in config.subscriptions
                            if _subscription_identity_url(item.url, item.mode) == _subscription_identity_url(clean_url, mode)
                        ),
                        None,
                    )
                    name = _subscription_import_name(source, mode, platform)
                    if existing:
                        imported_sources.append(
                            {
                                "subscription_id": existing.id,
                                "url": clean_url,
                                "mode": mode,
                                "name": name or existing.name,
                                "source_kind": str(source.get("source_kind") or ""),
                                "created": False,
                            }
                        )
                        changed_existing = False
                        if name and (
                            not str(existing.name or "").strip()
                            or (
                                str(source.get("source_kind") or "") == "default_favorites"
                                and existing.name != name
                            )
                        ):
                            existing.name = name
                            changed_existing = True
                        if not existing.enabled and default_enabled:
                            existing.enabled = True
                            changed_existing = True
                        if changed_existing:
                            existing.updated_at = now_iso
                            changed = True
                            platform_updated += 1
                            updated_ids.append(existing.id)
                        continue

                    record = SubscriptionRecord(
                        id=uuid.uuid4().hex,
                        name=name or _default_subscription_name(clean_url, mode),
                        url=clean_url,
                        mode=mode,
                        cron=default_cron,
                        enabled=default_enabled,
                        created_at=now_iso,
                        updated_at=now_iso,
                    )
                    config.subscriptions.append(record)
                    changed = True
                    platform_created += 1
                    created_ids.append(record.id)
                    imported_sources.append(
                        {
                            "subscription_id": record.id,
                            "url": clean_url,
                            "mode": mode,
                            "name": record.name,
                            "source_kind": str(source.get("source_kind") or ""),
                            "created": True,
                        }
                    )
                    self.task_store.patch_subscription_state(
                        record.id,
                        status="pending" if default_enabled else "idle",
                        running=False,
                        manual_requested_at=now_iso if default_enabled else "",
                        next_run_at=now_iso if default_enabled else "",
                        last_message=(
                            "Cookie 保存后已自动导入订阅源，等待后台首次同步。"
                            if reason == "cookie_save"
                            else "关注来源刷新后已自动导入订阅源，等待后台首次同步。"
                        ),
                    )

                cookie_pair = cookie_pairs.get(platform)
                if cookie_pair is not None:
                    profile_updates = {
                        "display_name": str(profile.get("name") or "").strip(),
                        "account_id": uid,
                        "handle": str(profile.get("handle") or "").strip(),
                        "avatar_url": str(profile.get("avatar_url") or "").strip(),
                    }
                    for key, value in profile_updates.items():
                        if value and str(getattr(cookie_pair, key, "") or "").strip() != value:
                            setattr(cookie_pair, key, value)
                            changed = True
                    if (
                        profile_updates["display_name"]
                        and profile_updates["account_id"]
                        and profile_updates["handle"]
                        and profile_updates["avatar_url"]
                    ):
                        success_message = _account_sync_success_message(platform)
                        if str(getattr(cookie_pair, "message", "") or "").strip() != success_message:
                            cookie_pair.message = success_message
                            changed = True
                    if changed:
                        cookie_pair.updated_at = now_iso

                _patch_cookie_source_inventory_state(
                    platform,
                    account={
                        "uid": uid,
                        "handle": str(profile.get("handle") or ""),
                        "name": str(profile.get("name") or ""),
                        "avatar_url": str(profile.get("avatar_url") or profile.get("avatar") or ""),
                    },
                    default_favorites=default_favorites if isinstance(default_favorites, dict) else {},
                    followed_authors=followed_author_items,
                    followed_author_sources={
                        "page_count": int((followed_authors_page or {}).get("count") or 0),
                        "page_total": (followed_authors_page or {}).get("total"),
                        "api_count": int((followed_authors or {}).get("count") or 0),
                        "api_total": (followed_authors or {}).get("total"),
                    },
                    followed_collections=followed_collections.get("items") if isinstance(followed_collections, dict) else [],
                    followed_collection_count=followed_collection_count,
                    followed_author_count=followed_author_count,
                    imported_sources=imported_sources,
                    source_urls=[
                        item["url"]
                        for item in imported_sources
                        if isinstance(item, dict) and str(item.get("url") or "").strip()
                    ],
                    last_sync_at=now_iso,
                    last_reason=reason,
                    last_status="success",
                )
                _patch_cookie_source_sync_state(
                    platform,
                    requested_at="",
                    requested_reason="",
                    last_sync_at=now_iso,
                    last_status="success",
                    last_message="关注来源同步完成。",
                    last_created_count=platform_created,
                    last_updated_count=platform_updated,
                    last_removed_invalid_count=removed_synthetic_count,
                    default_favorites_found=bool(default_favorites.get("url")),
                    default_favorites_count=1 if default_favorites.get("url") else 0,
                    followed_author_count=followed_author_count,
                    imported_followed_author_count=len(followed_author_items),
                    skipped_followed_author_count=skipped_author_count,
                    followed_collection_count=followed_collection_count,
                    account_uid=uid,
                    account_handle=str(profile.get("handle") or ""),
                    account_name=str(profile.get("name") or ""),
                    account_avatar_url=str(profile.get("avatar_url") or ""),
                )
                platform_results.append(
                    {
                        "platform": platform,
                        "status": "success",
                        "created_count": platform_created,
                        "updated_count": platform_updated,
                        "removed_invalid_count": removed_synthetic_count,
                        "removed_invalid_subscription_ids": removed_synthetic_ids,
                        "default_favorites_found": bool(default_favorites.get("url")),
                        "default_favorites_count": 1 if default_favorites.get("url") else 0,
                        "followed_author_count": followed_author_count,
                        "imported_followed_author_count": len(followed_author_items),
                        "skipped_followed_author_count": skipped_author_count,
                        "followed_collection_count": followed_collection_count,
                    }
                )
            except Exception as exc:
                message = str(exc)[:240]
                _patch_cookie_source_inventory_state(
                    platform,
                    last_sync_at=now_iso,
                    last_reason=reason,
                    last_status="error",
                    last_message=message,
                )
                _patch_cookie_source_sync_state(
                    platform,
                    requested_at="",
                    requested_reason="",
                    last_sync_at=now_iso,
                    last_status="error",
                    last_message=message,
                )
                platform_results.append(
                    {
                        "platform": platform,
                        "status": "error",
                        "message": message,
                        "created_count": 0,
                        "updated_count": 0,
                    }
                )
                _append_subscription_log(
                    "cookie_source_sync_error",
                    platform=platform,
                    reason=reason,
                    error=message,
                )

        if changed:
            self.store.save(config)
            self._dedupe_default_favorites_subscriptions()

        result = {
            "created_count": len(created_ids),
            "updated_count": len(updated_ids),
            "subscription_ids": created_ids,
            "updated_subscription_ids": updated_ids,
            "platforms": platform_results,
        }
        _append_subscription_log(
            "cookie_source_sync_done",
            reason=reason,
            platforms=sorted(normalized_platforms),
            created_count=result["created_count"],
            updated_count=result["updated_count"],
            platform_results=platform_results,
        )
        self.start()
        if self.background_enabled and created_ids:
            self._maybe_launch_due_sync()
        return result

    def upsert_from_archive(
        self,
        *,
        url: str,
        mode: str,
        discovered_items: list[str],
        cron: str = DEFAULT_SUBSCRIPTION_CRON,
        name: str = "",
    ) -> dict:
        clean_url = _canonical_subscription_url(url, mode)
        clean_mode = mode if mode in SUBSCRIPTION_MODES else _detect_subscription_mode(clean_url)
        if clean_mode not in SUBSCRIPTION_MODES:
            raise ValueError("仅作者上传页或收藏夹模型页支持创建订阅。")

        normalized_cron = _validate_cron(cron)
        config = self.store.load()
        existing = next(
            (
                item
                for item in config.subscriptions
                if _subscription_identity_url(item.url, item.mode) == _subscription_identity_url(clean_url, clean_mode)
            ),
            None,
        )
        now_iso = _now_iso()

        if existing:
            existing.name = str(name or existing.name or _default_subscription_name(clean_url, clean_mode)).strip()
            existing.cron = normalized_cron
            existing.enabled = True
            existing.updated_at = now_iso
            record = existing
            created = False
        else:
            record = SubscriptionRecord(
                id=uuid.uuid4().hex,
                name=str(name or _default_subscription_name(clean_url, clean_mode)).strip(),
                url=clean_url,
                mode=clean_mode,
                cron=normalized_cron,
                enabled=True,
                created_at=now_iso,
                updated_at=now_iso,
            )
            config.subscriptions.append(record)
            created = True

        self.store.save(config)

        normalized_items = _normalize_source_items(discovered_items)
        existing_state = self._state_by_id(record.id)
        tracked_items = _merge_source_items(existing_state.get("tracked_items") or [], normalized_items)
        deleted_items = _deleted_source_items(normalized_items, tracked_items)
        self.task_store.patch_subscription_state(
            record.id,
            status="success",
            running=False,
            next_run_at=now_iso,
            manual_requested_at="",
            last_run_at=now_iso,
            last_success_at=now_iso,
            last_message="归档时已同步订阅基线，已安排一次校验同步。",
            last_discovered_count=len(normalized_items),
            last_new_count=0,
            last_enqueued_count=0,
            last_deleted_count=len(deleted_items),
            current_items=normalized_items,
            tracked_items=tracked_items,
        )
        self.start()
        return {
            "created": created,
            "subscription": next((item for item in self.list_payload()["items"] if item["id"] == record.id), None),
        }

    def _run_loop(self) -> None:
        while True:
            try:
                self._ensure_state_records()
                self._maybe_sync_cookie_sources()
                self._maybe_launch_due_sync()
            except Exception as exc:
                _append_subscription_log("scheduler_error", error=str(exc))
            time.sleep(SUBSCRIPTION_POLL_SECONDS)

    def _maybe_sync_cookie_sources(self) -> None:
        config = self.store.load()
        platforms = {
            str(item.platform or "").strip().lower()
            for item in getattr(config, "cookies", [])
            if str(item.platform or "").strip().lower() in {"cn", "global"}
            and sanitize_cookie_header(item.cookie)
        }
        due_platforms = _cookie_source_sync_due(platforms)
        if not due_platforms:
            return
        self.sync_cookie_sources(due_platforms, reason=_cookie_source_sync_reason(due_platforms))

    def _ensure_state_records(self) -> None:
        self._dedupe_default_favorites_subscriptions()
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_ids = {str(item.get("id") or "") for item in state_payload.get("items") or []}

        for item in config.subscriptions:
            if item.id in state_ids:
                continue
            self.task_store.patch_subscription_state(
                item.id,
                status="idle",
                running=False,
                next_run_at=_next_run_at(item.cron) if item.enabled else "",
                last_message="订阅已创建，等待首次同步。",
            )

        config_ids = {item.id for item in config.subscriptions}
        stale_ids = [item_id for item_id in state_ids if item_id and item_id not in config_ids]
        if stale_ids:
            self.task_store.remove_subscription_state(stale_ids)

        self._recover_stale_running_states(config)

    def _dedupe_default_favorites_subscriptions(self) -> None:
        config = self.store.load()
        seen: dict[tuple[str, str], SubscriptionRecord] = {}
        duplicate_ids: list[str] = []
        changed = False
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        state_replacements: dict[str, dict] = {}

        for record in list(config.subscriptions):
            canonical_url = _canonical_subscription_url(record.url, record.mode)
            if canonical_url and canonical_url != str(record.url or "").strip():
                record.url = canonical_url
                changed = True
            key = _subscription_identity_key(record)
            if key[0] != "collection_models" or not key[1] or "/collections/models" not in key[1]:
                continue
            existing = seen.get(key)
            if existing is None:
                seen[key] = record
                continue
            keep = existing
            drop = record
            keep_state = state_map.get(keep.id) or {}
            drop_state = state_map.get(drop.id) or {}
            if _pick_duplicate_subscription_state([keep_state, drop_state]) is drop_state:
                keep, drop = drop, keep
                seen[key] = keep
            keep.name = _better_default_favorite_name(keep.name, drop.name)
            keep.enabled = bool(keep.enabled or drop.enabled)
            keep.updated_at = max(str(keep.updated_at or ""), str(drop.updated_at or "")) or _now_iso()
            duplicate_ids.append(drop.id)
            chosen_state = _pick_duplicate_subscription_state([keep_state, drop_state])
            if chosen_state:
                state_replacements[keep.id] = {**chosen_state, "id": keep.id}
            changed = True

        if duplicate_ids:
            config.subscriptions = [item for item in config.subscriptions if item.id not in set(duplicate_ids)]
        if changed:
            self.store.save(config)
        for target_id, state_item in state_replacements.items():
            self.task_store.upsert_subscription_state(state_item)
        if duplicate_ids:
            self.task_store.remove_subscription_state(duplicate_ids)
            _append_subscription_log(
                "default_favorites_deduped",
                kept_count=len(seen),
                removed_ids=duplicate_ids,
            )

    def _running_state_is_stale(self, state: dict, now: datetime) -> bool:
        last_run_at = _parse_iso(str(state.get("last_run_at") or ""))
        if last_run_at is None:
            return True
        return now - last_run_at >= SUBSCRIPTION_RUNNING_STALE_AFTER

    def _recover_stale_running_states(self, config) -> None:
        records = {item.id: item for item in config.subscriptions}
        state_payload = self.task_store.load_subscriptions_state()
        now = _now()
        now_iso = now.isoformat()

        with self._loop_lock:
            active_running_id = self._running_id

        for state in state_payload.get("items") or []:
            if not state.get("running"):
                continue

            subscription_id = str(state.get("id") or "").strip()
            record = records.get(subscription_id)
            if record is None:
                continue

            is_active_runner = bool(active_running_id and subscription_id == active_running_id)
            if is_active_runner and not self._running_state_is_stale(state, now):
                continue

            if is_active_runner:
                with self._loop_lock:
                    if self._running_id == subscription_id:
                        self._running_id = ""

            next_run_at = _next_run_at(record.cron, now) if record.enabled else ""
            self.task_store.patch_subscription_state(
                subscription_id,
                status="error",
                running=False,
                manual_requested_at=now_iso if record.enabled else "",
                next_run_at=next_run_at,
                last_error_at=now_iso,
                last_message="上次订阅同步中断，已恢复并重新加入调度。",
            )
            _append_subscription_log(
                "sync_recovered",
                subscription_id=subscription_id,
                url=record.url,
                last_run_at=str(state.get("last_run_at") or ""),
                active_runner=is_active_runner,
            )

    def _state_by_id(self, subscription_id: str) -> dict:
        state_payload = self.task_store.load_subscriptions_state()
        for item in state_payload.get("items") or []:
            if str(item.get("id") or "") == subscription_id:
                return item
        return {}

    def _maybe_launch_due_sync(self) -> None:
        with self._loop_lock:
            if self._running_id:
                return

            config = self.store.load()
            state_payload = self.task_store.load_subscriptions_state()
            state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
            now = _now()
            due: list[tuple[float, SubscriptionRecord]] = []

            for item in config.subscriptions:
                state = state_map.get(item.id) or {}
                if state.get("running"):
                    continue

                manual_requested_at = _parse_iso(str(state.get("manual_requested_at") or ""))
                if manual_requested_at is not None:
                    due.append((manual_requested_at.timestamp(), item))
                    continue

                if not item.enabled:
                    continue

                next_run_at = _parse_iso(str(state.get("next_run_at") or ""))
                if next_run_at is None or next_run_at <= now:
                    due.append(((next_run_at or now).timestamp(), item))

            if not due:
                return

            due.sort(key=lambda pair: pair[0])
            target = due[0][1]
            self._running_id = target.id
            runner = threading.Thread(
                target=self._run_subscription_sync,
                args=(target.id,),
                name=f"makerhub-subscription-{target.id[:8]}",
                daemon=True,
            )
            runner.start()

    def _run_subscription_sync(self, subscription_id: str) -> None:
        try:
            self._sync_subscription(subscription_id)
        finally:
            with self._loop_lock:
                if self._running_id == subscription_id:
                    self._running_id = ""

    def _sync_subscription(self, subscription_id: str) -> None:
        config = self.store.load()
        subscription = next((item for item in config.subscriptions if item.id == subscription_id), None)
        if subscription is None:
            self.task_store.remove_subscription_state([subscription_id])
            return

        started_at = _now()
        started_at_iso = started_at.isoformat()
        previous_state = self._state_by_id(subscription.id)
        manual_requested_at = str(previous_state.get("manual_requested_at") or "").strip()
        self.task_store.patch_subscription_state(
            subscription.id,
            status="running",
            running=True,
            last_run_at=started_at_iso,
            manual_requested_at="",
            last_message="正在扫描订阅源。",
        )
        _append_subscription_log("sync_start", subscription_id=subscription.id, url=subscription.url, mode=subscription.mode)

        try:
            discovered = self._discover_subscription_items(subscription)
            current_items = _normalize_source_items(discovered.get("items") or [])
            partial_context = _subscription_partial_scan_context(subscription, current_items, previous_state, discovered)
            if partial_context is not None:
                failed_at = _now()
                previous_count = int(partial_context["previous_count"])
                current_count = int(partial_context["current_count"])
                restored_items = partial_context["previous_tracked_items"]
                fallback_items = restored_items if restored_items else current_items
                message = _partial_scan_message(partial_context)
                self.task_store.patch_subscription_state(
                    subscription.id,
                    status="error",
                    running=False,
                    next_run_at=_next_run_at(subscription.cron, failed_at) if subscription.enabled else "",
                    last_error_at=failed_at.isoformat(),
                    last_message=message,
                    last_discovered_count=current_count,
                    last_new_count=0,
                    last_enqueued_count=0,
                    last_deleted_count=0,
                    current_items=fallback_items,
                    tracked_items=fallback_items,
                )
                _append_subscription_log(
                    "sync_partial_rejected",
                    subscription_id=subscription.id,
                    url=subscription.url,
                    discovered=current_count,
                    tracked=previous_count,
                    reason=partial_context.get("reason"),
                    threshold=partial_context.get("threshold"),
                    expected_total=partial_context.get("expected_total"),
                    expected_total_source=partial_context.get("expected_total_source"),
                )
                return
            self._refresh_subscription_source_metadata(subscription, discovered, len(current_items))
            tracked_items = _merge_source_items(previous_state.get("tracked_items") or [], current_items)
            previous_tracked_keys = {item["task_key"] for item in _normalize_source_items(previous_state.get("tracked_items") or [])}
            source_new_items = [item for item in current_items if item["task_key"] not in previous_tracked_keys]
            deleted_items = _deleted_source_items(current_items, tracked_items)

            candidate_items = current_items if manual_requested_at else source_new_items
            pending_keys = self.archive_manager._queued_task_keys()
            archived_keys = self.archive_manager._archived_task_keys()
            new_items = []
            for item in candidate_items:
                task_key = item.get("task_key") or ""
                if not task_key or task_key in pending_keys or task_key in archived_keys:
                    continue
                new_items.append(item)

            enqueue_result = {
                "accepted": True,
                "queued_count": 0,
                "message": "没有新增模型。",
            }
            if new_items:
                enqueue_result = self.archive_manager.submit_discovered_batch(
                    source_url=subscription.url,
                    mode=subscription.mode,
                    discovered_items=[item["url"] for item in new_items],
                    expected_total=discovered.get("expected_total"),
                    pages_scanned=discovered.get("pages_scanned"),
                    scan_mode=f"subscription:{subscription.id}",
                    message_prefix=f"来自订阅同步：{subscription.name or subscription.url}",
                    meta={
                        "subscription_id": subscription.id,
                        "subscription_name": subscription.name,
                    },
                )

            finished_at = _now()
            finished_at_iso = finished_at.isoformat()
            next_run_at = _next_run_at(subscription.cron, finished_at)
            if not subscription.enabled:
                next_run_at = ""
            enqueued_count = int(enqueue_result.get("queued_count") or 0)
            message = (
                f"同步完成：扫描 {len(current_items)} 个，新增 {len(new_items)} 个，入队 {enqueued_count} 个，"
                f"源端删除标记 {len(deleted_items)} 个。"
            )
            if not enqueue_result.get("accepted", True) and str(enqueue_result.get("message") or "").strip():
                message = f"{message} {enqueue_result.get('message')}"

            self.task_store.patch_subscription_state(
                subscription.id,
                status="success",
                running=False,
                next_run_at=next_run_at,
                last_success_at=finished_at_iso,
                last_message=message,
                last_discovered_count=len(current_items),
                last_new_count=len(new_items),
                last_enqueued_count=enqueued_count,
                last_deleted_count=len(deleted_items),
                current_items=current_items,
                tracked_items=tracked_items,
            )
            _append_subscription_log(
                "sync_done",
                subscription_id=subscription.id,
                url=subscription.url,
                discovered=len(current_items),
                new=len(new_items),
                enqueued=enqueued_count,
                deleted=len(deleted_items),
            )
            self._refresh_source_preview_snapshots(subscription.id, subscription=subscription)
        except Exception as exc:
            failed_at = _now()
            self.task_store.patch_subscription_state(
                subscription.id,
                status="error",
                running=False,
                next_run_at=_next_run_at(subscription.cron, failed_at) if subscription.enabled else "",
                last_error_at=failed_at.isoformat(),
                last_message=str(exc),
            )
            _append_subscription_log("sync_error", subscription_id=subscription.id, url=subscription.url, error=str(exc))

    def _discover_subscription_items(self, subscription: SubscriptionRecord) -> dict:
        config = self.store.load()
        cookie = _select_cookie(subscription.url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")
        return run_discover_batch_urls_job(subscription.url, cookie, proxy_config=config.proxy)

    def _refresh_subscription_source_metadata(
        self,
        subscription: SubscriptionRecord,
        discovered: dict,
        current_count: int,
    ) -> None:
        try:
            expected_total = discovered.get("expected_total") if isinstance(discovered, dict) else None
            source_model_count = expected_total if isinstance(expected_total, int) and expected_total > 0 else current_count
            result = refresh_subscription_source_metadata(
                url=subscription.url,
                mode=subscription.mode,
                config=self.store.load(),
                source_model_count=source_model_count,
            )
            if result.get("refreshed"):
                _append_subscription_log(
                    "metadata_refreshed",
                    subscription_id=subscription.id,
                    url=subscription.url,
                    source_key=result.get("source_key"),
                    kind=result.get("kind"),
                    remote_model_count=result.get("remote_model_count"),
                )
        except Exception as exc:
            _append_subscription_log(
                "metadata_refresh_error",
                subscription_id=subscription.id,
                url=subscription.url,
                error=str(exc),
            )

    def _refresh_source_preview_snapshots(self, subscription_id: str = "", subscription: SubscriptionRecord | None = None) -> None:
        try:
            source_keys = set()
            if subscription is not None:
                source_key = source_identity_key(subscription.url, subscription.mode)
                if source_key:
                    source_keys.add(source_key)
            result = refresh_source_preview_snapshots(
                force=False,
                store=self.store,
                task_store=self.task_store,
                source_keys=source_keys or None,
            )
            _append_subscription_log(
                "preview_snapshots_refreshed",
                subscription_id=subscription_id,
                total=result.get("total"),
                generated=result.get("generated"),
                skipped=result.get("skipped"),
                failed=result.get("failed"),
            )
        except Exception as exc:
            _append_subscription_log(
                "preview_snapshot_refresh_error",
                subscription_id=subscription_id,
                error=str(exc),
            )

    def _enqueue_initialized_subscription_items(self, subscription: SubscriptionRecord, initialized: dict) -> dict:
        current_items = _normalize_source_items(initialized.get("current_items") or [])
        pending_keys = self.archive_manager._queued_task_keys()
        archived_keys = self.archive_manager._archived_task_keys()
        new_items = []
        for item in current_items:
            task_key = item.get("task_key") or ""
            if not task_key or task_key in pending_keys or task_key in archived_keys:
                continue
            new_items.append(item)

        enqueue_result = {
            "accepted": True,
            "queued_count": 0,
            "message": "没有新增模型。",
        }
        if new_items:
            enqueue_result = self.archive_manager.submit_discovered_batch(
                source_url=subscription.url,
                mode=subscription.mode,
                discovered_items=[item["url"] for item in new_items],
                expected_total=len(current_items),
                pages_scanned=initialized.get("pages_scanned"),
                scan_mode=f"subscription-initial:{subscription.id}",
                message_prefix=f"来自订阅首次扫描：{subscription.name or subscription.url}",
                meta={
                    "subscription_id": subscription.id,
                    "subscription_name": subscription.name,
                },
            )

        enqueued_count = int(enqueue_result.get("queued_count") or 0)
        message = f"订阅已初始化：扫描 {len(current_items)} 个，首次入队 {enqueued_count} 个。"
        if not new_items:
            message = f"订阅已初始化：扫描 {len(current_items)} 个，模型均已归档或已在队列中。"
        if not enqueue_result.get("accepted", True) and str(enqueue_result.get("message") or "").strip():
            message = f"{message} {enqueue_result.get('message')}"

        patched = self.task_store.patch_subscription_state(
            subscription.id,
            status="success",
            running=False,
            next_run_at=_next_run_at(subscription.cron) if subscription.enabled else "",
            manual_requested_at="",
            last_message=message,
            last_new_count=len(new_items),
            last_enqueued_count=enqueued_count,
            current_items=current_items,
            tracked_items=current_items,
        )
        _append_subscription_log(
            "initial_enqueue_done",
            subscription_id=subscription.id,
            url=subscription.url,
            scanned=len(current_items),
            new=len(new_items),
            enqueued=enqueued_count,
        )
        return patched

    def _initialize_subscription_state(self, subscription: SubscriptionRecord) -> dict:
        discovered = self._discover_subscription_items(subscription)
        current_items = _normalize_source_items(discovered.get("items") or [])
        self._refresh_subscription_source_metadata(subscription, discovered, len(current_items))
        now_iso = _now_iso()
        next_run_at = _next_run_at(subscription.cron) if subscription.enabled else ""
        expected_total = discovered.get("expected_total")
        pages_scanned = discovered.get("pages_scanned")
        scan_mode = discovered.get("mode")
        _append_subscription_log(
            "initialized",
            subscription_id=subscription.id,
            url=subscription.url,
            mode=subscription.mode,
            discovered=len(current_items),
            expected_total=expected_total,
            pages_scanned=pages_scanned,
            scan_mode=scan_mode,
            next_run_at=next_run_at,
        )
        return self.task_store.patch_subscription_state(
            subscription.id,
            status="success",
            running=False,
            next_run_at=next_run_at,
            manual_requested_at="",
            last_run_at=now_iso,
            last_success_at=now_iso,
            last_message=f"订阅已初始化，当前扫描到 {len(current_items)} 个模型。",
            last_discovered_count=len(current_items),
            last_new_count=0,
            last_enqueued_count=0,
            last_deleted_count=0,
            current_items=current_items,
            tracked_items=current_items,
        )

    def _merge_subscription_record(self, record: SubscriptionRecord, state: Optional[dict]) -> dict:
        state = state or {}
        current_items = _normalize_source_items(state.get("current_items") or [])
        tracked_items = _normalize_source_items(state.get("tracked_items") or [])
        deleted_items = _deleted_source_items(current_items, tracked_items)
        return {
            "id": record.id,
            "name": record.name or _default_subscription_name(record.url, record.mode),
            "url": record.url,
            "mode": record.mode,
            "cron": record.cron,
            "enabled": bool(record.enabled),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "status": str(state.get("status") or "idle"),
            "running": bool(state.get("running", False)),
            "next_run_at": str(state.get("next_run_at") or ""),
            "last_run_at": str(state.get("last_run_at") or ""),
            "last_success_at": str(state.get("last_success_at") or ""),
            "last_error_at": str(state.get("last_error_at") or ""),
            "last_message": str(state.get("last_message") or ""),
            "last_discovered_count": int(state.get("last_discovered_count") or 0),
            "last_new_count": int(state.get("last_new_count") or 0),
            "last_enqueued_count": int(state.get("last_enqueued_count") or 0),
            "last_deleted_count": int(state.get("last_deleted_count") or len(deleted_items)),
            "current_count": len(current_items),
            "tracked_count": len(tracked_items),
            "deleted_count": len(deleted_items),
            "deleted_items": deleted_items,
        }

    def _merge_subscription_record_light(self, record: SubscriptionRecord, state: Optional[dict]) -> dict:
        state = state or {}
        return {
            "id": record.id,
            "name": record.name or _default_subscription_name(record.url, record.mode),
            "url": record.url,
            "mode": record.mode,
            "cron": record.cron,
            "enabled": bool(record.enabled),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "status": str(state.get("status") or "idle"),
            "running": bool(state.get("running", False)),
            "next_run_at": str(state.get("next_run_at") or ""),
            "last_run_at": str(state.get("last_run_at") or ""),
            "last_success_at": str(state.get("last_success_at") or ""),
            "last_error_at": str(state.get("last_error_at") or ""),
            "last_message": str(state.get("last_message") or ""),
            "last_discovered_count": int(state.get("last_discovered_count") or 0),
            "last_new_count": int(state.get("last_new_count") or 0),
            "last_enqueued_count": int(state.get("last_enqueued_count") or 0),
            "last_deleted_count": int(state.get("last_deleted_count") or 0),
            "current_count": int(state.get("last_discovered_count") or 0),
            "tracked_count": int(state.get("last_discovered_count") or 0),
            "deleted_count": int(state.get("last_deleted_count") or 0),
            "deleted_items": [],
        }
