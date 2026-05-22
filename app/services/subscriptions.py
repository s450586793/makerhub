import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from croniter import CroniterBadCronError, croniter

from app.core.database import database_configured, database_driver_available, load_json_state, save_json_state
from app.core.settings import BACKGROUND_TASKS_ENABLED, LOGS_DIR, STATE_DIR
from app.core.store import JsonStore
from app.core.timezone import ensure_timezone, now as china_now, now_iso as china_now_iso, parse_datetime
from app.schemas.models import SubscriptionRecord
from app.services.cookie_utils import sanitize_cookie_header
from app.services.archive_worker import ArchiveTaskManager, detect_archive_mode
from app.services.batch_discovery import (
    default_favorites_subscription_source,
    discover_cookie_account_profile,
    discover_cookie_followed_authors,
    discover_cookie_followed_authors_from_page,
    discover_cookie_followed_collections,
    extract_model_id,
    normalize_source_url,
)
from app.services.business_logs import append_business_log
from app.services.process_jobs import run_discover_batch_urls_job
from app.services.proxy_policy import temporary_proxy_env
from app.services.source_library import (
    build_subscription_overview_payload,
    refresh_source_preview_snapshots,
    refresh_subscription_source_metadata,
    source_identity_key,
    _default_favorites_identity_url,
    _save_source_metadata_item,
)
from app.services.task_state import TaskStateStore


DEFAULT_SUBSCRIPTION_CRON = "0 */6 * * *"
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


def _append_subscription_log(event: str, **payload: Any) -> None:
    try:
        SUBSCRIPTION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SUBSCRIPTION_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time": _now_iso(),
                        "event": event,
                        **payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        return
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
    if database_configured() and database_driver_available():
        try:
            payload = load_json_state(COOKIE_SOURCE_SYNC_STATE_KEY)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    try:
        if not COOKIE_SOURCE_SYNC_STATE_PATH.exists():
            return {}
        payload = json.loads(COOKIE_SOURCE_SYNC_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            try:
                save_json_state(COOKIE_SOURCE_SYNC_STATE_KEY, payload)
            except Exception:
                pass
            return payload
        return {}
    except Exception:
        return {}


def _write_cookie_source_sync_state(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        if database_configured() and database_driver_available():
            save_json_state(COOKIE_SOURCE_SYNC_STATE_KEY, payload)
    except Exception:
        pass
    try:
        COOKIE_SOURCE_SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_SOURCE_SYNC_STATE_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        return payload
    return payload


def _read_cookie_source_inventory_state() -> dict[str, Any]:
    if database_configured() and database_driver_available():
        try:
            payload = load_json_state(COOKIE_SOURCE_INVENTORY_KEY)
            if isinstance(payload, dict):
                platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
                return {
                    "platforms": {str(key): value for key, value in platforms.items() if isinstance(value, dict)},
                    "updated_at": str(payload.get("updated_at") or ""),
                }
        except Exception:
            pass
    try:
        if not COOKIE_SOURCE_INVENTORY_PATH.exists():
            return {"platforms": {}, "updated_at": ""}
        payload = json.loads(COOKIE_SOURCE_INVENTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return {"platforms": {}, "updated_at": ""}
        platforms = payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {}
        normalized = {
            "platforms": {str(key): value for key, value in platforms.items() if isinstance(value, dict)},
            "updated_at": str(payload.get("updated_at") or ""),
        }
        try:
            save_json_state(COOKIE_SOURCE_INVENTORY_KEY, normalized)
        except Exception:
            pass
        return normalized
    except Exception:
        return {"platforms": {}, "updated_at": ""}


def _write_cookie_source_inventory_state(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "platforms": payload.get("platforms") if isinstance(payload.get("platforms"), dict) else {},
        "updated_at": str(payload.get("updated_at") or _now_iso()),
    }
    try:
        if database_configured() and database_driver_available():
            save_json_state(COOKIE_SOURCE_INVENTORY_KEY, normalized)
    except Exception:
        pass
    try:
        COOKIE_SOURCE_INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_SOURCE_INVENTORY_PATH.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        return normalized
    return normalized


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


def _canonical_subscription_url(url: str, mode: str = "") -> str:
    clean_url = normalize_source_url(url)
    if (mode or _detect_subscription_mode(clean_url)) == "collection_models":
        return _default_favorites_identity_url(clean_url)
    return clean_url


def _subscription_identity_url(url: str, mode: str = "") -> str:
    return _canonical_subscription_url(url, mode)


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

    def list_payload(self) -> dict:
        self._ensure_state_records()
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        items = [self._merge_subscription_record(item, state_map.get(item.id)) for item in config.subscriptions]
        overview = build_subscription_overview_payload(store=self.store, task_store=self.task_store)
        return {
            "items": items,
            "count": len(items),
            "summary": {
                "enabled": len([item for item in items if item["enabled"]]),
                "running": len([item for item in items if item["running"]]),
                "deleted_marked": sum(int(item.get("deleted_count") or 0) for item in items),
            },
            "sections": overview.get("sections") or [],
            "settings": overview.get("settings") or config.subscription_settings.model_dump(),
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
                self._refresh_source_preview_snapshots(record.id)
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
            self._refresh_source_preview_snapshots(target.id)
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
        cookie_map = {
            str(item.platform or "").strip().lower(): sanitize_cookie_header(item.cookie)
            for item in getattr(config, "cookies", [])
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
                with temporary_proxy_env(config.proxy, "", platform=platform):
                    profile = discover_cookie_account_profile(platform, raw_cookie)
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
                        if name and not str(existing.name or "").strip():
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
                        "api_count": int((followed_authors or {}).get("count") or 0),
                    },
                    followed_collections=followed_collections.get("items") if isinstance(followed_collections, dict) else [],
                    followed_collection_count=int((followed_collections or {}).get("count") or 0),
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
                    default_favorites_found=bool(default_favorites.get("url")),
                    followed_author_count=len(followed_author_items),
                    followed_collection_count=int((followed_collections or {}).get("count") or 0),
                    account_uid=uid,
                    account_handle=str(profile.get("handle") or ""),
                )
                platform_results.append(
                    {
                        "platform": platform,
                        "status": "success",
                        "created_count": platform_created,
                        "updated_count": platform_updated,
                        "default_favorites_found": bool(default_favorites.get("url")),
                        "followed_author_count": len(followed_author_items),
                        "followed_collection_count": int((followed_collections or {}).get("count") or 0),
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
            if canonical_url and canonical_url != normalize_source_url(record.url):
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
            self._refresh_source_preview_snapshots(subscription.id)
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

    def _refresh_source_preview_snapshots(self, subscription_id: str = "") -> None:
        try:
            result = refresh_source_preview_snapshots(force=False, store=self.store, task_store=self.task_store)
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
