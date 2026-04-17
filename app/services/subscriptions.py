import json
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from croniter import CroniterBadCronError, croniter

from app.core.settings import LOGS_DIR
from app.core.store import JsonStore
from app.schemas.models import SubscriptionRecord
from app.services.archive_worker import ArchiveTaskManager, detect_archive_mode
from app.services.batch_discovery import discover_batch_model_urls, extract_model_id, normalize_source_url
from app.services.business_logs import append_business_log
from app.services.task_state import TaskStateStore


DEFAULT_SUBSCRIPTION_CRON = "0 */6 * * *"
SUBSCRIPTION_MODES = {"author_upload", "collection_models"}
SUBSCRIPTION_LOG_PATH = LOGS_DIR / "subscriptions.log"
SUBSCRIPTION_POLL_SECONDS = 20


def _now() -> datetime:
    return datetime.now()


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


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
    if event in {"sync_start", "sync_done", "sync_error", "scheduler_error"}:
        level = "error" if event in {"sync_error", "scheduler_error"} else "info"
        message_map = {
            "sync_start": "订阅同步开始。",
            "sync_done": "订阅同步完成。",
            "sync_error": "订阅同步失败。",
            "scheduler_error": "订阅调度器异常。",
        }
        append_business_log("subscription", event, message_map.get(event, event), level=level, **payload)


def _validate_cron(cron_expr: str) -> str:
    clean = str(cron_expr or "").strip() or DEFAULT_SUBSCRIPTION_CRON
    try:
        croniter(clean, _now())
    except (CroniterBadCronError, ValueError) as exc:
        raise ValueError(f"Cron 表达式无效：{exc}") from exc
    return clean


def _next_run_at(cron_expr: str, base: Optional[datetime] = None) -> str:
    normalized = _validate_cron(cron_expr)
    return croniter(normalized, base or _now()).get_next(datetime).isoformat()


def _select_cookie(url: str, config) -> str:
    netloc = urlparse(url).netloc.lower()
    platform = "global" if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc else "cn"
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return str(cookie_map.get(platform) or "").strip()


@contextmanager
def _temporary_proxy_env(config):
    import os

    if not getattr(config.proxy, "enabled", False):
        yield
        return

    previous = {
        "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
        "NO_PROXY": os.environ.get("NO_PROXY"),
        "http_proxy": os.environ.get("http_proxy"),
        "https_proxy": os.environ.get("https_proxy"),
        "no_proxy": os.environ.get("no_proxy"),
    }

    if config.proxy.http_proxy:
        os.environ["HTTP_PROXY"] = config.proxy.http_proxy
        os.environ["http_proxy"] = config.proxy.http_proxy
    if config.proxy.https_proxy:
        os.environ["HTTPS_PROXY"] = config.proxy.https_proxy
        os.environ["https_proxy"] = config.proxy.https_proxy
    if config.proxy.no_proxy:
        os.environ["NO_PROXY"] = config.proxy.no_proxy
        os.environ["no_proxy"] = config.proxy.no_proxy

    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


class SubscriptionManager:
    def __init__(
        self,
        archive_manager: ArchiveTaskManager,
        store: Optional[JsonStore] = None,
        task_store: Optional[TaskStateStore] = None,
    ) -> None:
        self.archive_manager = archive_manager
        self.store = store or JsonStore()
        self.task_store = task_store or TaskStateStore()
        self._loop_lock = threading.Lock()
        self._running_id = ""
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._ensure_state_records()
        with self._loop_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run_loop, name="makerhub-subscriptions", daemon=True)
            self._thread.start()

    def list_payload(self) -> dict:
        config = self.store.load()
        state_payload = self.task_store.load_subscriptions_state()
        state_map = {str(item.get("id") or ""): item for item in state_payload.get("items") or []}
        items = [self._merge_subscription_record(item, state_map.get(item.id)) for item in config.subscriptions]
        return {
            "items": items,
            "count": len(items),
            "summary": {
                "enabled": len([item for item in items if item["enabled"]]),
                "running": len([item for item in items if item["running"]]),
                "deleted_marked": sum(int(item.get("deleted_count") or 0) for item in items),
            },
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
        clean_url = normalize_source_url(url)
        mode = _detect_subscription_mode(clean_url)
        if mode not in SUBSCRIPTION_MODES:
            raise ValueError("仅支持作者上传页或收藏夹模型页订阅。")

        normalized_cron = _validate_cron(cron)
        config = self.store.load()
        existing = next((item for item in config.subscriptions if normalize_source_url(item.url) == clean_url), None)
        now_iso = _now_iso()
        created_new = False

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
            if initialize_from_source:
                initialized = self._initialize_subscription_state(record)
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
            "message": "订阅已创建。" if not existing else "订阅已更新。",
            "initialized": initialized,
        }

    def update_subscription(self, subscription_id: str, *, url: str, name: str, cron: str, enabled: bool) -> dict:
        clean_id = str(subscription_id or "").strip()
        if not clean_id:
            raise ValueError("缺少订阅 ID。")

        clean_url = normalize_source_url(url)
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
                if item.id != clean_id and normalize_source_url(item.url) == clean_url
            ),
            None,
        )
        if duplicate is not None:
            raise ValueError("该链接已存在订阅。")

        previous = target.model_copy(deep=True)
        url_changed = normalize_source_url(target.url) != clean_url

        target.url = clean_url
        target.mode = mode
        target.name = str(name or target.name or _default_subscription_name(target.url, target.mode)).strip()
        target.cron = normalized_cron
        target.enabled = bool(enabled)
        target.updated_at = _now_iso()
        self.store.save(config)

        if url_changed:
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
        self._maybe_launch_due_sync()
        return {
            "success": True,
            "message": "订阅同步已触发。",
            "subscription": next((item for item in self.list_payload()["items"] if item["id"] == clean_id), None),
        }

    def upsert_from_archive(
        self,
        *,
        url: str,
        mode: str,
        discovered_items: list[str],
        cron: str = DEFAULT_SUBSCRIPTION_CRON,
        name: str = "",
    ) -> dict:
        clean_url = normalize_source_url(url)
        clean_mode = mode if mode in SUBSCRIPTION_MODES else _detect_subscription_mode(clean_url)
        if clean_mode not in SUBSCRIPTION_MODES:
            raise ValueError("仅作者上传页或收藏夹模型页支持创建订阅。")

        normalized_cron = _validate_cron(cron)
        config = self.store.load()
        existing = next((item for item in config.subscriptions if normalize_source_url(item.url) == clean_url), None)
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
            next_run_at=_next_run_at(record.cron),
            manual_requested_at="",
            last_run_at=now_iso,
            last_success_at=now_iso,
            last_message="归档时已同步订阅基线。",
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
                self._maybe_launch_due_sync()
            except Exception as exc:
                _append_subscription_log("scheduler_error", error=str(exc))
            time.sleep(SUBSCRIPTION_POLL_SECONDS)

    def _ensure_state_records(self) -> None:
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

            next_run_at = _next_run_at(subscription.cron, started_at)
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
                last_success_at=_now_iso(),
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
        except Exception as exc:
            self.task_store.patch_subscription_state(
                subscription.id,
                status="error",
                running=False,
                next_run_at=_next_run_at(subscription.cron, started_at) if subscription.enabled else "",
                last_error_at=_now_iso(),
                last_message=str(exc),
            )
            _append_subscription_log("sync_error", subscription_id=subscription.id, url=subscription.url, error=str(exc))

    def _discover_subscription_items(self, subscription: SubscriptionRecord) -> dict:
        config = self.store.load()
        cookie = _select_cookie(subscription.url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")
        with _temporary_proxy_env(config):
            return discover_batch_model_urls(subscription.url, cookie)

    def _initialize_subscription_state(self, subscription: SubscriptionRecord) -> dict:
        discovered = self._discover_subscription_items(subscription)
        current_items = _normalize_source_items(discovered.get("items") or [])
        now_iso = _now_iso()
        return self.task_store.patch_subscription_state(
            subscription.id,
            status="success",
            running=False,
            next_run_at=_next_run_at(subscription.cron) if subscription.enabled else "",
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
