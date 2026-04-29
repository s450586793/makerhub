import json
import os
import re
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from app.core.settings import LOGS_DIR, STATE_DIR, ensure_app_dirs
from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime
from app.services.three_mf import describe_three_mf_failure, normalize_makerworld_source, normalize_three_mf_failure_state

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local dev only.
    fcntl = None


ARCHIVE_QUEUE_PATH = STATE_DIR / "archive_queue.json"
MISSING_3MF_PATH = STATE_DIR / "missing_3mf.json"
THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
ORGANIZE_TASKS_PATH = STATE_DIR / "organize_tasks.json"
MODEL_FLAGS_PATH = STATE_DIR / "model_flags.json"
SUBSCRIPTIONS_STATE_PATH = STATE_DIR / "subscriptions_state.json"
REMOTE_REFRESH_STATE_PATH = STATE_DIR / "remote_refresh_state.json"
ORGANIZER_LOG_PATH = LOGS_DIR / "organizer.log"
ORGANIZE_TASK_VISIBLE_LIMIT = 50
ORGANIZER_TERMINAL_EVENTS = {
    "organized",
    "duplicate_skipped",
    "deleted_model_skipped",
    "duplicate_skip_failed",
    "organize_failed",
    "worker_timeout",
}
METADATA_ONLY_MISSING_3MF_MESSAGE = "信息补全任务会整理打印配置详情、实例展示媒体和评论回复，不下载 3MF。"
_STATE_LOCK = threading.RLock()
_ORGANIZER_HISTORY_COUNT_CACHE = {
    "mtime_ns": 0,
    "size": 0,
    "count": 0,
}


def _looks_like_html_message(text: str) -> bool:
    head = str(text or "").strip().lower()[:1200]
    if not head:
        return False
    if head.startswith("<!doctype html") or "<html" in head:
        return True
    return bool(re.search(r"<(html|head|body|script|title|div|meta|style)\b", head))


def _sanitize_message_text(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    if _looks_like_html_message(text):
        lowered = text.lower()
        if any(token in lowered for token in ("cloudflare", "cf-browser-verification", "cf-chl", "__cf_bm", "cf_clearance")):
            return "返回了风控校验页，通常是 Cookie 失效、代理异常或站点触发了 Cloudflare 校验。"
        return "返回了 HTML 页面，通常是 Cookie 失效、代理错误或站点风控页。"
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def _normalize_source_refresh_text(value: Any) -> str:
    return str(value or "").replace("远端刷新", "源端刷新")


def _normalize_source_refresh_item(item: dict) -> dict:
    normalized = dict(item or {})
    normalized["message"] = _normalize_source_refresh_text(normalized.get("message") or "")
    return normalized


def _normalize_task_item(item: Any, default_status: str) -> dict:
    if isinstance(item, str):
        return {
            "id": "",
            "title": item,
            "status": default_status,
            "progress": 0,
            "message": "",
            "updated_at": "",
        }

    if not isinstance(item, dict):
        return {
            "id": "",
            "title": "",
            "status": default_status,
            "progress": 0,
            "message": "",
            "updated_at": "",
        }

    progress = item.get("progress")
    if progress is None:
        progress = item.get("percent") or item.get("percent_complete") or 0

    return {
        "id": str(item.get("id") or item.get("task_id") or ""),
        "title": str(item.get("title") or item.get("name") or item.get("url") or item.get("model_dir") or ""),
        "status": str(item.get("status") or default_status),
        "progress": int(progress or 0),
        "message": _sanitize_message_text(item.get("message") or item.get("detail") or ""),
        "updated_at": str(item.get("updated_at") or item.get("time") or item.get("created_at") or ""),
        "url": str(item.get("url") or ""),
        "mode": str(item.get("mode") or ""),
        "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
    }


def _normalize_archive_queue(payload: Any) -> dict:
    if isinstance(payload, list):
        queued = [_normalize_task_item(item, "queued") for item in payload]
        return {"active": [], "queued": queued, "recent_failures": []}

    if not isinstance(payload, dict):
        return {"active": [], "queued": [], "recent_failures": []}

    active_items = payload.get("active") or payload.get("running") or []
    queued_items = payload.get("queued") or payload.get("items") or payload.get("pending") or []
    failed_items = payload.get("recent_failures") or payload.get("failed") or payload.get("failures") or []

    return {
        "active": [_normalize_task_item(item, "running") for item in active_items],
        "queued": [_normalize_task_item(item, "queued") for item in queued_items],
        "recent_failures": [_normalize_task_item(item, "failed") for item in failed_items],
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _organize_count_needs_backfill(*, item_count: int, total_count: int) -> bool:
    return item_count >= ORGANIZE_TASK_VISIBLE_LIMIT and total_count <= item_count


def _read_active_three_mf_limit_guard() -> dict[str, Any]:
    if not THREE_MF_LIMIT_GUARD_PATH.exists():
        return {}
    try:
        payload = json.loads(THREE_MF_LIMIT_GUARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or not bool(payload.get("active")):
        return {}

    limited_until = str(payload.get("limited_until") or "").strip()
    parsed_until = parse_datetime(limited_until)
    if parsed_until is None or parsed_until <= china_now():
        return {}
    return payload


def _limit_guard_for_missing_item(item_url: str, guard: dict[str, Any]) -> dict[str, Any]:
    if not guard:
        return {}
    guard_source = normalize_makerworld_source(url=guard.get("model_url"))
    item_source = normalize_makerworld_source(url=item_url)
    if guard_source and item_source and guard_source != item_source:
        return {}
    return guard


def _format_three_mf_limit_guard_message(guard: dict[str, Any]) -> str:
    if not guard:
        return ""
    source = normalize_makerworld_source(url=guard.get("model_url"))
    base_message = str(guard.get("message") or "").strip() or describe_three_mf_failure(
        "download_limited",
        source=source,
    )
    if "自动重试暂停至" in base_message:
        base_message = base_message.split("自动重试暂停至", 1)[0].rstrip("，,。 ")

    limited_until = str(guard.get("limited_until") or "").strip()
    parsed_until = parse_datetime(limited_until)
    if parsed_until is None:
        return base_message
    until_text = parsed_until.strftime("%Y-%m-%d %H:%M")
    return f"{base_message.rstrip('。')}，自动重试暂停至 {until_text}。"


def _normalize_missing_3mf(payload: Any, fallback_items: Optional[list[dict]] = None) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("models") or []
    else:
        items = []

    if not items and fallback_items:
        items = fallback_items

    normalized = []
    limit_guard = _read_active_three_mf_limit_guard()
    for item in items:
        if isinstance(item, str):
            normalized.append({"model_id": "", "title": item, "status": "missing"})
            continue
        if not isinstance(item, dict):
            continue
        if is_metadata_only_missing_3mf_placeholder(item):
            continue
        message = _sanitize_message_text(item.get("message") or item.get("downloadMessage") or "")
        item_url = str(item.get("model_url") or item.get("url") or "")
        status = normalize_three_mf_failure_state(
            item.get("status") or item.get("downloadState") or "",
            message,
            url=item_url,
        )
        if status == "download_limited":
            limit_message = _format_three_mf_limit_guard_message(_limit_guard_for_missing_item(item_url, limit_guard))
            message = describe_three_mf_failure(
                status,
                "",
                url=item_url,
                limit_message=limit_message,
            )
        if status in {"verification_required", "cloudflare"}:
            message = describe_three_mf_failure(status, message, url=item_url)
        normalized.append(
            {
                "model_id": str(item.get("model_id") or item.get("id") or ""),
                "title": str(item.get("title") or item.get("name") or ""),
                "status": status,
                "model_url": item_url,
                "instance_id": str(item.get("instance_id") or item.get("profileId") or item.get("instanceId") or ""),
                "message": message,
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
            }
        )

    return {"items": normalized}


def is_metadata_only_missing_3mf_placeholder(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    message = str(item.get("message") or item.get("downloadMessage") or "").strip()
    return "信息补全任务" in message and "不下载 3MF" in message


def _missing_3mf_key(item: dict) -> tuple[str, str, str]:
    normalized = _normalize_missing_3mf([item]).get("items", [])
    if not normalized:
        return ("", "", "")
    payload = normalized[0]
    return (
        str(payload.get("model_id") or ""),
        str(payload.get("instance_id") or ""),
        str(payload.get("title") or ""),
    )


def _matches_missing_3mf_item(
    item: dict,
    *,
    model_id: str = "",
    title: str = "",
    instance_id: str = "",
    model_url: str = "",
) -> bool:
    item_model_id = str(item.get("model_id") or "").strip()
    item_title = str(item.get("title") or "").strip()
    item_instance_id = str(item.get("instance_id") or "").strip()
    item_model_url = str(item.get("model_url") or "").strip()

    target_model_id = str(model_id or "").strip()
    target_title = str(title or "").strip()
    target_instance_id = str(instance_id or "").strip()
    target_model_url = str(model_url or "").strip()

    if target_model_id and item_model_id != target_model_id:
        return False
    if target_instance_id and item_instance_id != target_instance_id:
        return False
    if target_title and item_title != target_title:
        return False
    if target_model_url and item_model_url != target_model_url:
        return False

    return any((target_model_id, target_title, target_instance_id, target_model_url))


def _normalize_organize_tasks(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
        raw_detected_total = 0
        raw_count = len(items)
        raw_count_trusted = False
        raw_source_dir = ""
        raw_updated_at = ""
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("tasks") or []
        raw_detected_total = _safe_int(
            payload.get("detected_total", payload.get("pending_total", payload.get("total", 0))),
            0,
        )
        raw_count = _safe_int(
            payload.get("count", payload.get("total_count", len(items))),
            len(items),
        )
        raw_count_trusted = bool(payload.get("count_trusted"))
        raw_source_dir = str(payload.get("source_dir") or "")
        raw_updated_at = str(payload.get("updated_at") or "")
    else:
        items = []
        raw_detected_total = 0
        raw_count = 0
        raw_count_trusted = False
        raw_source_dir = ""
        raw_updated_at = ""

    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append(
                {
                    "source_dir": item,
                    "target_dir": "",
                    "status": "pending",
                    "updated_at": "",
                    "move_files": True,
                }
            )
            continue
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "id": str(item.get("id") or item.get("task_id") or ""),
                "title": str(item.get("title") or item.get("file_name") or Path(str(item.get("source_path") or "")).name or ""),
                "file_name": str(item.get("file_name") or Path(str(item.get("source_path") or "")).name or ""),
                "source_dir": str(item.get("source_dir") or item.get("source") or ""),
                "target_dir": str(item.get("target_dir") or item.get("target") or ""),
                "source_path": str(item.get("source_path") or ""),
                "target_path": str(item.get("target_path") or ""),
                "model_dir": str(item.get("model_dir") or ""),
                "status": str(item.get("status") or "pending"),
                "message": str(item.get("message") or item.get("detail") or ""),
                "progress": int(item.get("progress") or item.get("percent") or 0),
                "updated_at": str(item.get("updated_at") or item.get("time") or ""),
                "move_files": bool(item.get("move_files", item.get("move", True))),
                "fingerprint": str(item.get("fingerprint") or ""),
            }
        )

    normalized.sort(key=_organize_task_sort_key, reverse=True)
    queued_count = 0
    running_count = 0
    for item in normalized:
        status = str(item.get("status") or "").strip().lower()
        if status in {"pending", "queued"}:
            queued_count += 1
        elif status == "running":
            running_count += 1

    detected_total = max(raw_detected_total, queued_count + running_count)
    total_count = max(raw_count, len(normalized))
    if _organize_count_needs_backfill(item_count=len(normalized), total_count=total_count):
        total_count = max(total_count, _organizer_history_count_from_log())
    return {
        "items": normalized,
        "count": total_count,
        "count_trusted": bool(raw_count_trusted or not _organize_count_needs_backfill(item_count=len(normalized), total_count=total_count)),
        "detected_total": detected_total,
        "queued_count": queued_count,
        "running_count": running_count,
        "source_dir": raw_source_dir,
        "updated_at": raw_updated_at,
    }


def _organizer_history_count_from_log() -> int:
    try:
        stat = ORGANIZER_LOG_PATH.stat()
    except OSError:
        return 0

    cache_mtime_ns = int(_ORGANIZER_HISTORY_COUNT_CACHE.get("mtime_ns") or 0)
    cache_size = int(_ORGANIZER_HISTORY_COUNT_CACHE.get("size") or 0)
    if cache_mtime_ns == int(stat.st_mtime_ns) and cache_size == int(stat.st_size):
        return int(_ORGANIZER_HISTORY_COUNT_CACHE.get("count") or 0)

    count = 0
    try:
        with ORGANIZER_LOG_PATH.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = str(raw_line or "").strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(payload.get("event") or "").strip() in ORGANIZER_TERMINAL_EVENTS:
                    count += 1
    except OSError:
        return 0

    _ORGANIZER_HISTORY_COUNT_CACHE.update(
        {
            "mtime_ns": int(stat.st_mtime_ns),
            "size": int(stat.st_size),
            "count": count,
        }
    )
    return count


def _organize_task_sort_key(item: dict) -> tuple[int, str, str]:
    raw = str(item.get("updated_at") or "").strip()
    if raw:
        parsed = parse_datetime(raw)
        if parsed is not None:
            return (int(parsed.timestamp()), raw, str(item.get("id") or ""))
    return (0, raw, str(item.get("id") or ""))


def _normalize_model_flags(payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {}

    def _normalize_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            clean = str(item or "").strip().strip("/")
            if not clean or clean in seen:
                continue
            seen.add(clean)
            result.append(clean)
        return result

    return {
        "favorites": _normalize_list(payload.get("favorites")),
        "printed": _normalize_list(payload.get("printed")),
        "deleted": _normalize_list(payload.get("deleted")),
    }


def _normalize_subscription_source_item(item: Any) -> Optional[dict]:
    if isinstance(item, str):
        clean = str(item).strip()
        if not clean:
            return None
        return {
            "model_id": "",
            "url": clean,
            "task_key": clean,
        }

    if not isinstance(item, dict):
        return None

    model_id = str(item.get("model_id") or "").strip()
    url = str(item.get("url") or "").strip()
    task_key = str(item.get("task_key") or item.get("key") or "").strip()
    if not any((model_id, url, task_key)):
        return None
    if not task_key:
        task_key = f"model:{model_id}" if model_id else url
    return {
        "model_id": model_id,
        "url": url,
        "task_key": task_key,
    }


def _normalize_subscription_state(payload: Any) -> dict:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("items") or payload.get("subscriptions") or []
    else:
        items = []

    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        subscription_id = str(item.get("id") or "").strip()
        if not subscription_id or subscription_id in seen_ids:
            continue
        seen_ids.add(subscription_id)

        current_items = []
        current_seen: set[str] = set()
        for raw_child in item.get("current_items") or []:
            child = _normalize_subscription_source_item(raw_child)
            if not child:
                continue
            key = child["task_key"]
            if key in current_seen:
                continue
            current_seen.add(key)
            current_items.append(child)

        tracked_items = []
        tracked_seen: set[str] = set()
        for raw_child in item.get("tracked_items") or []:
            child = _normalize_subscription_source_item(raw_child)
            if not child:
                continue
            key = child["task_key"]
            if key in tracked_seen:
                continue
            tracked_seen.add(key)
            tracked_items.append(child)

        normalized.append(
            {
                "id": subscription_id,
                "status": str(item.get("status") or "idle"),
                "running": bool(item.get("running", False)),
                "next_run_at": str(item.get("next_run_at") or ""),
                "manual_requested_at": str(item.get("manual_requested_at") or ""),
                "last_run_at": str(item.get("last_run_at") or ""),
                "last_success_at": str(item.get("last_success_at") or ""),
                "last_error_at": str(item.get("last_error_at") or ""),
                "last_message": str(item.get("last_message") or ""),
                "last_discovered_count": int(item.get("last_discovered_count") or 0),
                "last_new_count": int(item.get("last_new_count") or 0),
                "last_enqueued_count": int(item.get("last_enqueued_count") or 0),
                "last_deleted_count": int(item.get("last_deleted_count") or 0),
                "current_items": current_items,
                "tracked_items": tracked_items,
            }
        )

    return {"items": normalized}


def _normalize_remote_refresh_state(payload: Any) -> dict:
    if not isinstance(payload, dict):
        payload = {}

    current_item = payload.get("current_item") if isinstance(payload.get("current_item"), dict) else {}
    current_items = payload.get("current_items") if isinstance(payload.get("current_items"), list) else []
    recent_items = payload.get("recent_items") if isinstance(payload.get("recent_items"), list) else []
    normalized_recent = [
        _normalize_source_refresh_item(_normalize_task_item(item, "idle"))
        for item in recent_items
        if isinstance(item, (dict, str))
    ]
    normalized_current_items = [
        _normalize_source_refresh_item(_normalize_task_item(item, "running"))
        for item in current_items
        if isinstance(item, (dict, str))
    ]
    if current_item and not normalized_current_items:
        normalized_current_items = [_normalize_source_refresh_item(_normalize_task_item(current_item, "running"))]
    normalized_metrics = payload.get("last_batch_metrics") if isinstance(payload.get("last_batch_metrics"), dict) else {}
    normalized_resource_waits = payload.get("last_resource_waits") if isinstance(payload.get("last_resource_waits"), dict) else {}
    normalized_slow_models = payload.get("last_slow_models") if isinstance(payload.get("last_slow_models"), list) else []

    return {
        "status": str(payload.get("status") or "idle"),
        "running": bool(payload.get("running", False)),
        "next_run_at": str(payload.get("next_run_at") or ""),
        "last_run_at": str(payload.get("last_run_at") or ""),
        "last_success_at": str(payload.get("last_success_at") or ""),
        "last_error_at": str(payload.get("last_error_at") or ""),
        "manual_requested_at": str(payload.get("manual_requested_at") or ""),
        "last_message": _normalize_source_refresh_text(_sanitize_message_text(payload.get("last_message") or "")),
        "last_batch_total": _safe_int(payload.get("last_batch_total") or 0),
        "last_batch_succeeded": _safe_int(payload.get("last_batch_succeeded") or 0),
        "last_batch_failed": _safe_int(payload.get("last_batch_failed") or 0),
        "last_batch_skipped": _safe_int(payload.get("last_batch_skipped") or 0),
        "last_eligible_total": _safe_int(payload.get("last_eligible_total") or 0),
        "last_remaining_total": _safe_int(payload.get("last_remaining_total") or 0),
        "last_skipped_missing_cookie": _safe_int(payload.get("last_skipped_missing_cookie") or 0),
        "last_skipped_local_or_invalid": _safe_int(payload.get("last_skipped_local_or_invalid") or 0),
        "current_item": normalized_current_items[0] if normalized_current_items else {},
        "current_items": normalized_current_items[:8],
        "last_batch_metrics": normalized_metrics,
        "last_resource_waits": normalized_resource_waits,
        "last_slow_models": normalized_slow_models[:10],
        "recent_items": normalized_recent[:50],
    }


def compact_remote_refresh_state(payload: Any, *, include_current: bool = True) -> dict:
    state = _normalize_remote_refresh_state(payload)
    compact = {
        "status": state["status"],
        "running": state["running"],
        "next_run_at": state["next_run_at"],
        "last_run_at": state["last_run_at"],
        "last_success_at": state["last_success_at"],
        "last_error_at": state["last_error_at"],
        "manual_requested_at": state["manual_requested_at"],
        "last_message": state["last_message"],
        "last_batch_total": state["last_batch_total"],
        "last_batch_succeeded": state["last_batch_succeeded"],
        "last_batch_failed": state["last_batch_failed"],
        "last_batch_skipped": state["last_batch_skipped"],
        "last_eligible_total": state["last_eligible_total"],
        "last_remaining_total": state["last_remaining_total"],
        "last_skipped_missing_cookie": state["last_skipped_missing_cookie"],
        "last_skipped_local_or_invalid": state["last_skipped_local_or_invalid"],
    }
    if include_current:
        compact["current_item"] = state["current_item"]
        compact["current_items"] = state["current_items"][:2]
    return compact


class TaskStateStore:
    def __init__(self) -> None:
        ensure_app_dirs()

    def _read_json(self, path: Path, default: dict) -> dict:
        if not path.exists():
            self._write_json(path, default)
            return default

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self._write_json(path, default)
            return default

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

    @contextmanager
    def _state_file_lock(self, path: Path):
        lock_path = path.with_name(f"{path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _load_archive_queue_unlocked(self) -> dict:
        payload = self._read_json(
            ARCHIVE_QUEUE_PATH,
            {"active": [], "queued": [], "recent_failures": []},
        )
        queue = _normalize_archive_queue(payload)
        queue["running_count"] = len(queue["active"])
        queue["queued_count"] = len(queue["queued"])
        queue["failed_count"] = len(queue["recent_failures"])
        return queue

    def _save_archive_queue_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_archive_queue(payload)
        self._write_json(
            ARCHIVE_QUEUE_PATH,
            {
                "active": normalized["active"],
                "queued": normalized["queued"],
                "recent_failures": normalized["recent_failures"],
            },
        )
        return self._load_archive_queue_unlocked()

    def _update_archive_queue(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(ARCHIVE_QUEUE_PATH):
            payload = self._load_archive_queue_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            return self._save_archive_queue_unlocked(updated)

    def _load_missing_3mf_unlocked(self, fallback_items: Optional[list[dict]] = None) -> dict:
        payload = self._read_json(MISSING_3MF_PATH, {"items": []})
        missing = _normalize_missing_3mf(payload, fallback_items=fallback_items)
        missing["count"] = len(missing["items"])
        return missing

    def _save_missing_3mf_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_missing_3mf(payload)
        self._write_json(MISSING_3MF_PATH, normalized)
        return self._load_missing_3mf_unlocked()

    def _update_missing_3mf(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(MISSING_3MF_PATH):
            payload = self._load_missing_3mf_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            return self._save_missing_3mf_unlocked(updated)

    def _load_subscriptions_state_unlocked(self) -> dict:
        payload = self._read_json(SUBSCRIPTIONS_STATE_PATH, {"items": []})
        state = _normalize_subscription_state(payload)
        state["count"] = len(state["items"])
        return state

    def _load_remote_refresh_state_unlocked(self) -> dict:
        payload = self._read_json(
            REMOTE_REFRESH_STATE_PATH,
            {
                "status": "idle",
                "running": False,
                "next_run_at": "",
                "last_run_at": "",
                "last_success_at": "",
                "last_error_at": "",
                "manual_requested_at": "",
                "last_message": "",
                "last_batch_total": 0,
                "last_batch_succeeded": 0,
                "last_batch_failed": 0,
                "last_batch_skipped": 0,
                "last_eligible_total": 0,
                "last_remaining_total": 0,
                "last_skipped_missing_cookie": 0,
                "last_skipped_local_or_invalid": 0,
                "current_item": {},
                "current_items": [],
                "last_batch_metrics": {},
                "last_resource_waits": {},
                "last_slow_models": [],
                "recent_items": [],
            },
        )
        return _normalize_remote_refresh_state(payload)

    def _load_organize_tasks_unlocked(self) -> dict:
        payload = self._read_json(ORGANIZE_TASKS_PATH, {"items": []})
        tasks = _normalize_organize_tasks(payload)
        raw_payload = payload if isinstance(payload, dict) else {}
        raw_count = _safe_int(raw_payload.get("count"), 0)
        raw_count_trusted = bool(raw_payload.get("count_trusted"))
        if int(tasks.get("count") or 0) != raw_count or bool(tasks.get("count_trusted")) != raw_count_trusted:
            persisted = dict(tasks)
            self._write_json(ORGANIZE_TASKS_PATH, persisted)
        return tasks

    def _save_organize_tasks_unlocked(self, payload: dict) -> dict:
        base_payload = dict(payload or {}) if isinstance(payload, dict) else {"items": payload if isinstance(payload, list) else []}
        normalized = _normalize_organize_tasks({**base_payload, "count_trusted": True})
        normalized["count_trusted"] = True
        self._write_json(ORGANIZE_TASKS_PATH, normalized)
        return self._load_organize_tasks_unlocked()

    def _update_organize_tasks(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(ORGANIZE_TASKS_PATH):
            payload = self._load_organize_tasks_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            return self._save_organize_tasks_unlocked(updated)

    def _save_subscriptions_state_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_subscription_state(payload)
        self._write_json(SUBSCRIPTIONS_STATE_PATH, normalized)
        return self._load_subscriptions_state_unlocked()

    def _save_remote_refresh_state_unlocked(self, payload: dict) -> dict:
        normalized = _normalize_remote_refresh_state(payload)
        self._write_json(REMOTE_REFRESH_STATE_PATH, normalized)
        return self._load_remote_refresh_state_unlocked()

    def _update_subscriptions_state(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(SUBSCRIPTIONS_STATE_PATH):
            payload = self._load_subscriptions_state_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            return self._save_subscriptions_state_unlocked(updated)

    def _update_remote_refresh_state(self, updater) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            payload = self._load_remote_refresh_state_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            return self._save_remote_refresh_state_unlocked(updated)

    def save_archive_queue(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(ARCHIVE_QUEUE_PATH):
            return self._save_archive_queue_unlocked(payload)

    def save_missing_3mf(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(MISSING_3MF_PATH):
            return self._save_missing_3mf_unlocked(payload)

    def load_archive_queue(self) -> dict:
        with _STATE_LOCK:
            return self._load_archive_queue_unlocked()

    def load_missing_3mf(self, fallback_items: Optional[list[dict]] = None) -> dict:
        with _STATE_LOCK:
            return self._load_missing_3mf_unlocked(fallback_items=fallback_items)

    def load_organize_tasks(self) -> dict:
        with _STATE_LOCK:
            return self._load_organize_tasks_unlocked()

    def save_organize_tasks(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(ORGANIZE_TASKS_PATH):
            return self._save_organize_tasks_unlocked(payload)

    def save_subscriptions_state(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(SUBSCRIPTIONS_STATE_PATH):
            return self._save_subscriptions_state_unlocked(payload)

    def load_subscriptions_state(self) -> dict:
        with _STATE_LOCK:
            return self._load_subscriptions_state_unlocked()

    def save_remote_refresh_state(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            return self._save_remote_refresh_state_unlocked(payload)

    def load_remote_refresh_state(self) -> dict:
        with _STATE_LOCK:
            return self._load_remote_refresh_state_unlocked()

    def save_model_flags(self, payload: dict) -> dict:
        with _STATE_LOCK, self._state_file_lock(MODEL_FLAGS_PATH):
            normalized = _normalize_model_flags(payload)
            self._write_json(MODEL_FLAGS_PATH, normalized)
            return self.load_model_flags()

    def load_model_flags(self) -> dict:
        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            flags["favorite_count"] = len(flags["favorites"])
            flags["printed_count"] = len(flags["printed"])
            flags["deleted_count"] = len(flags["deleted"])
            return flags

    def update_model_flag(self, model_dir: str, flag_name: str, active: bool) -> dict:
        clean_model_dir = str(model_dir or "").strip().strip("/")
        if not clean_model_dir:
            return self.load_model_flags()

        if flag_name not in {"favorites", "printed", "deleted"}:
            return self.load_model_flags()

        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            items = list(flags.get(flag_name) or [])
            item_set = set(items)

            if active:
                if clean_model_dir not in item_set:
                    items.append(clean_model_dir)
            else:
                items = [item for item in items if item != clean_model_dir]

            flags[flag_name] = items
            return self.save_model_flags(flags)

    def remove_model_flags(self, model_dirs: list[str]) -> dict:
        clean_model_dirs = {
            str(item or "").strip().strip("/")
            for item in model_dirs or []
            if str(item or "").strip().strip("/")
        }
        if not clean_model_dirs:
            return self.load_model_flags()

        with _STATE_LOCK:
            payload = self._read_json(MODEL_FLAGS_PATH, {"favorites": [], "printed": [], "deleted": []})
            flags = _normalize_model_flags(payload)
            flags["favorites"] = [item for item in flags.get("favorites") or [] if item not in clean_model_dirs]
            flags["printed"] = [item for item in flags.get("printed") or [] if item not in clean_model_dirs]
            flags["deleted"] = [item for item in flags.get("deleted") or [] if item not in clean_model_dirs]
            return self.save_model_flags(flags)

    def enqueue_archive_task(self, item: dict) -> dict:
        def _mutate(payload: dict) -> dict:
            queued = list(payload.get("queued") or [])
            queued.append(_normalize_task_item(item, "queued"))
            payload["queued"] = queued
            return payload

        return self._update_archive_queue(_mutate)

    def start_archive_task(self, task_id: str) -> dict:
        def _mutate(payload: dict) -> dict:
            queued = list(payload.get("queued") or [])
            active = list(payload.get("active") or [])
            task = None
            remaining = []
            for item in queued:
                normalized = _normalize_task_item(item, "queued")
                if normalized["id"] == task_id and task is None:
                    normalized["status"] = "running"
                    normalized["updated_at"] = china_now_iso()
                    task = normalized
                    continue
                remaining.append(normalized)

            if task is None:
                current_active = []
                for item in active:
                    normalized = _normalize_task_item(item, "running")
                    if normalized["id"] == task_id:
                        task = normalized
                    else:
                        current_active.append(normalized)
                active = current_active
            else:
                active = [_normalize_task_item(item, "running") for item in active]

            if task is not None:
                active.append(task)

            payload["queued"] = remaining
            payload["active"] = active
            return payload

        return self._update_archive_queue(_mutate)

    def update_active_task(self, task_id: str, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            active = []
            for item in payload.get("active") or []:
                normalized = _normalize_task_item(item, "running")
                if normalized["id"] == task_id:
                    normalized.update({key: value for key, value in changes.items() if value is not None})
                    normalized["updated_at"] = china_now_iso()
                active.append(normalized)
            payload["active"] = active
            return payload

        return self._update_archive_queue(_mutate)

    def complete_archive_task(self, task_id: str) -> dict:
        def _mutate(payload: dict) -> dict:
            payload["active"] = [
                _normalize_task_item(item, "running")
                for item in (payload.get("active") or [])
                if _normalize_task_item(item, "running")["id"] != task_id
            ]
            return payload

        return self._update_archive_queue(_mutate)

    def fail_archive_task(self, task_id: str, message: str) -> dict:
        def _mutate(payload: dict) -> dict:
            failed_item = None
            active = []
            for item in payload.get("active") or []:
                normalized = _normalize_task_item(item, "running")
                if normalized["id"] == task_id:
                    normalized["status"] = "failed"
                    normalized["message"] = message
                    normalized["updated_at"] = china_now_iso()
                    failed_item = normalized
                else:
                    active.append(normalized)

            queued = []
            for item in payload.get("queued") or []:
                normalized = _normalize_task_item(item, "queued")
                if normalized["id"] == task_id and failed_item is None:
                    normalized["status"] = "failed"
                    normalized["message"] = message
                    normalized["updated_at"] = china_now_iso()
                    failed_item = normalized
                else:
                    queued.append(normalized)

            recent_failures = [_normalize_task_item(item, "failed") for item in (payload.get("recent_failures") or [])]
            if failed_item is not None:
                recent_failures.insert(0, failed_item)
                recent_failures = recent_failures[:20]

            payload["active"] = active
            payload["queued"] = queued
            payload["recent_failures"] = recent_failures
            return payload

        return self._update_archive_queue(_mutate)

    def requeue_active_tasks(self, message: str = "服务重启后自动恢复") -> dict:
        recovered_count = 0

        def _mutate(payload: dict) -> dict:
            nonlocal recovered_count
            active_items = [_normalize_task_item(item, "running") for item in (payload.get("active") or [])]
            queued_items = [_normalize_task_item(item, "queued") for item in (payload.get("queued") or [])]
            if not active_items:
                recovered_count = 0
                return payload

            recovered = []
            now = china_now_iso()
            for item in active_items:
                item["status"] = "queued"
                item["progress"] = 0
                item["message"] = message
                item["updated_at"] = now
                recovered.append(item)

            recovered_count = len(recovered)
            payload["active"] = []
            payload["queued"] = recovered + queued_items
            return payload

        queue = self._update_archive_queue(_mutate)
        queue["recovered_count"] = recovered_count
        return queue

    def remove_recent_failures_for_model(self, model_id: str, url: str = "") -> dict:
        model_key = str(model_id or "").strip()
        url_key = str(url or "").strip()
        if not model_key and not url_key:
            return self.load_archive_queue()

        def _mutate(payload: dict) -> dict:
            remaining = []
            for item in payload.get("recent_failures") or []:
                normalized = _normalize_task_item(item, "failed")
                haystack = " ".join(
                    [
                        str(normalized.get("url") or ""),
                        str(normalized.get("title") or ""),
                        str(normalized.get("id") or ""),
                    ]
                )
                if model_key and model_key in haystack:
                    continue
                if url_key and (normalized.get("url") == url_key or normalized.get("title") == url_key):
                    continue
                remaining.append(normalized)

            payload["recent_failures"] = remaining
            return payload

        return self._update_archive_queue(_mutate)

    def merge_missing_3mf_items(self, items: list[dict]) -> dict:
        def _mutate(payload: dict) -> dict:
            existing = list(payload.get("items") or [])
            merged: list[dict] = []
            seen = set()

            for item in existing + (items or []):
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def replace_missing_3mf_for_model(self, model_id: str, items: list[dict]) -> dict:
        model_key = str(model_id or "").strip()

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item for item in existing
                if str(item.get("model_id") or "").strip() != model_key or not model_key
            ]
            merged: list[dict] = []
            seen = set()
            for item in remaining + (items or []):
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)
            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def replace_missing_3mf_for_models(self, items_by_model: dict[str, list[dict]]) -> dict:
        normalized_groups: dict[str, list[dict]] = {}
        for raw_model_id, raw_items in (items_by_model or {}).items():
            model_key = str(raw_model_id or "").strip()
            if not model_key:
                continue

            merged: list[dict] = []
            seen = set()
            for item in raw_items or []:
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                if not str(normalized.get("model_id") or "").strip():
                    normalized["model_id"] = model_key
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            normalized_groups[model_key] = merged

        if not normalized_groups:
            return self.load_missing_3mf()

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item
                for item in existing
                if str(item.get("model_id") or "").strip() not in normalized_groups
            ]

            merged: list[dict] = []
            seen = set()
            for item in remaining:
                normalized_list = _normalize_missing_3mf([item]).get("items", [])
                if not normalized_list:
                    continue
                normalized = normalized_list[0]
                key = _missing_3mf_key(normalized)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(normalized)

            for model_key in normalized_groups:
                for item in normalized_groups.get(model_key) or []:
                    normalized_list = _normalize_missing_3mf([item]).get("items", [])
                    if not normalized_list:
                        continue
                    normalized = normalized_list[0]
                    if not str(normalized.get("model_id") or "").strip():
                        normalized["model_id"] = model_key
                    key = _missing_3mf_key(normalized)
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(normalized)

            return {"items": merged}

        return self._update_missing_3mf(_mutate)

    def remove_missing_3mf_for_model(self, model_id: str) -> dict:
        return self.replace_missing_3mf_for_model(model_id, [])

    def remove_missing_3mf_item(
        self,
        *,
        model_id: str = "",
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
    ) -> dict:
        def _mutate(payload: dict) -> dict:
            items = _normalize_missing_3mf(payload).get("items", [])
            remaining = [
                item for item in items
                if not _matches_missing_3mf_item(
                    item,
                    model_id=model_id,
                    title=title,
                    instance_id=instance_id,
                    model_url=model_url,
                )
            ]
            return {"items": remaining}

        return self._update_missing_3mf(_mutate)

    def upsert_organize_task(self, item: dict, limit: int = 50) -> dict:
        normalized_items = _normalize_organize_tasks({"items": [item]}).get("items", [])
        if not normalized_items:
            return self.load_organize_tasks()

        target = normalized_items[0]
        target_id = target.get("id") or target.get("fingerprint") or target.get("source_path")
        if not target_id:
            target_id = f"{target.get('source_dir')}::{target.get('target_dir')}::{target.get('title')}"
            target["id"] = target_id

        def _mutate(payload: dict) -> dict:
            raw_items = payload.get("items") or []
            total_count = _safe_int(payload.get("count"), len(raw_items))
            items = []
            replaced = False
            for existing in payload.get("items") or []:
                normalized = _normalize_organize_tasks({"items": [existing]}).get("items", [])
                if not normalized:
                    continue
                current = normalized[0]
                current_id = current.get("id") or current.get("fingerprint") or current.get("source_path")
                if current_id == target_id:
                    items.append(target)
                    replaced = True
                else:
                    items.append(current)

            if not replaced:
                items.insert(0, target)
                total_count += 1

            return {
                **payload,
                "items": items[: max(int(limit or 0), 1)],
                "count": total_count,
            }

        return self._update_organize_tasks(_mutate)

    def update_missing_3mf_status(
        self,
        model_id: str,
        title: str = "",
        instance_id: str = "",
        model_url: str = "",
        status: str = "",
        message: str = "",
    ) -> dict:
        def _mutate(payload: dict) -> dict:
            items = _normalize_missing_3mf(payload).get("items", [])
            now = china_now_iso()

            for item in items:
                if not _matches_missing_3mf_item(
                    item,
                    model_id=model_id,
                    title=title,
                    instance_id=instance_id,
                    model_url=model_url,
                ):
                    continue
                if status:
                    item["status"] = status
                if message:
                    item["message"] = message
                item["updated_at"] = now

            return {"items": items}

        return self._update_missing_3mf(_mutate)

    def mark_missing_3mf_retrying(
        self,
        items: list[dict],
        *,
        status: str = "queued",
        message: str = "等待重新下载 3MF",
    ) -> dict:
        targets = _normalize_missing_3mf({"items": items or []}).get("items", [])
        if not targets:
            return self.load_missing_3mf()

        target_keys = {_missing_3mf_key(item) for item in targets}
        target_urls = {
            str(item.get("model_url") or "").strip()
            for item in targets
            if str(item.get("model_url") or "").strip()
        }

        def _mutate(payload: dict) -> dict:
            existing = _normalize_missing_3mf(payload).get("items", [])
            now = china_now_iso()
            for item in existing:
                key = _missing_3mf_key(item)
                item_url = str(item.get("model_url") or "").strip()
                if key not in target_keys and item_url not in target_urls:
                    continue
                item["status"] = status
                item["message"] = message
                item["updated_at"] = now
            return {"items": existing}

        return self._update_missing_3mf(_mutate)

    def upsert_subscription_state(self, item: dict) -> dict:
        normalized_item = _normalize_subscription_state({"items": [item]}).get("items", [])
        if not normalized_item:
            return self.load_subscriptions_state()
        target = normalized_item[0]
        target_id = target["id"]

        def _mutate(payload: dict) -> dict:
            items = list(payload.get("items") or [])
            replaced = False
            merged: list[dict] = []
            for existing in items:
                if str(existing.get("id") or "") == target_id:
                    merged.append(target)
                    replaced = True
                else:
                    merged.append(existing)
            if not replaced:
                merged.append(target)
            return {"items": merged}

        return self._update_subscriptions_state(_mutate)

    def patch_subscription_state(self, subscription_id: str, **changes: Any) -> dict:
        target_id = str(subscription_id or "").strip()
        if not target_id:
            return self.load_subscriptions_state()

        def _mutate(payload: dict) -> dict:
            items = _normalize_subscription_state(payload).get("items", [])
            updated_items: list[dict] = []
            found = False
            for existing in items:
                if str(existing.get("id") or "") != target_id:
                    updated_items.append(existing)
                    continue
                merged = dict(existing)
                for key, value in changes.items():
                    if value is None:
                        continue
                    merged[key] = value
                updated_items.append(merged)
                found = True

            if not found:
                baseline = {
                    "id": target_id,
                    "status": "idle",
                    "running": False,
                    "next_run_at": "",
                    "manual_requested_at": "",
                    "last_run_at": "",
                    "last_success_at": "",
                    "last_error_at": "",
                    "last_message": "",
                    "last_discovered_count": 0,
                    "last_new_count": 0,
                    "last_enqueued_count": 0,
                    "last_deleted_count": 0,
                    "current_items": [],
                    "tracked_items": [],
                }
                for key, value in changes.items():
                    if value is None:
                        continue
                    baseline[key] = value
                updated_items.append(baseline)

            return {"items": updated_items}

        return self._update_subscriptions_state(_mutate)

    def remove_subscription_state(self, subscription_ids: list[str]) -> dict:
        clean_ids = {
            str(item or "").strip()
            for item in subscription_ids or []
            if str(item or "").strip()
        }
        if not clean_ids:
            return self.load_subscriptions_state()

        def _mutate(payload: dict) -> dict:
            items = _normalize_subscription_state(payload).get("items", [])
            remaining = [item for item in items if str(item.get("id") or "") not in clean_ids]
            return {"items": remaining}

        return self._update_subscriptions_state(_mutate)

    def patch_remote_refresh_state(self, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            merged = dict(_normalize_remote_refresh_state(payload))
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "current_item":
                    if isinstance(value, dict) and value:
                        merged[key] = _normalize_task_item(value, "running")
                    elif not value:
                        merged[key] = {}
                        if "current_items" not in changes:
                            merged["current_items"] = []
                    continue
                if key == "current_items":
                    if isinstance(value, list):
                        merged[key] = [
                            _normalize_task_item(item, "running")
                            for item in value
                            if isinstance(item, (dict, str))
                        ][:8]
                    elif not value:
                        merged[key] = []
                    continue
                merged[key] = value
            return merged

        return self._update_remote_refresh_state(_mutate)

    def append_remote_refresh_history(self, item: dict, limit: int = 50) -> dict:
        normalized_list = _normalize_remote_refresh_state({"recent_items": [item]}).get("recent_items", [])
        if not normalized_list:
            return self.load_remote_refresh_state()
        target = normalized_list[0]

        def _mutate(payload: dict) -> dict:
            recent_items = [target]
            for existing in payload.get("recent_items") or []:
                normalized = _normalize_task_item(existing, "idle")
                if normalized["id"] and normalized["id"] == target["id"]:
                    continue
                recent_items.append(normalized)
            normalized_payload = dict(payload)
            normalized_payload["recent_items"] = recent_items[: max(int(limit or 0), 1)]
            return normalized_payload

        return self._update_remote_refresh_state(_mutate)
