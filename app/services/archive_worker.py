import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.settings import ARCHIVE_DIR, LOGS_DIR, STATE_DIR, ensure_app_dirs
from app.core.store import JsonStore
from app.core.timezone import now as china_now, parse_datetime
from app.services.cookie_utils import sanitize_cookie_header
from app.services.batch_discovery import (
    discover_batch_model_urls,
    extract_model_id,
    normalize_model_url,
    normalize_source_url,
    resolve_batch_source_name,
)
from app.services.business_logs import append_business_log
from app.services.catalog import (
    get_archive_snapshot,
    invalidate_archive_snapshot,
    invalidate_model_detail_cache,
    upsert_archive_snapshot_model,
)
from app.services.legacy_archiver import archive_model as legacy_archive_model
from app.services.task_state import TaskStateStore
from app.services.three_mf import describe_three_mf_failure, normalize_makerworld_source

BATCH_TASK_MODES = {"author_upload", "collection_models"}
BATCH_QUEUE_LOG_PATH = LOGS_DIR / "batch_queue.log"
MAX_BATCH_CHILD_REQUEUE_ATTEMPTS = 3
ACTIVE_BATCH_IDLE_POLL_SECONDS = 2.0
COLLECTION_DETAIL_RE = re.compile(r"/(?:[a-z]{2}/)?collections/\d+(?:-[^/?#]+)?(?:[/?#]|$)", re.I)
THREE_MF_LIMIT_GUARD_PATH = STATE_DIR / "three_mf_limit_guard.json"
THREE_MF_LIMIT_DEFAULT_MESSAGE = "已达到 MakerWorld 每日下载上限，今日暂停自动重试。"


def detect_archive_mode(url: str) -> str:
    lowered = (url or "").lower()
    path = urlparse(url or "").path or ""
    if "/collections/models" in lowered or COLLECTION_DETAIL_RE.search(path):
        return "collection_models"
    if "/upload" in lowered and "/@" in lowered:
        return "author_upload"
    if "/models/" in lowered:
        return "single_model"
    return "unknown"


def _select_cookie(url: str, config) -> str:
    netloc = urlparse(url).netloc.lower()
    platform = "global" if "makerworld.com" in netloc and "makerworld.com.cn" not in netloc else "cn"
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return sanitize_cookie_header(cookie_map.get(platform) or "")


def _task_key(url: str) -> str:
    model_id = extract_model_id(url)
    if model_id:
        return f"model:{model_id}"
    return normalize_source_url(url)


def _resolve_archive_result_model_dir(result: dict[str, Any]) -> str:
    work_dir = str(result.get("work_dir") or "").strip()
    if not work_dir:
        return ""
    try:
        return Path(work_dir).resolve().relative_to(ARCHIVE_DIR.resolve()).as_posix()
    except ValueError:
        return ""


def _queue_item_key(item: dict) -> str:
    return _task_key(item.get("url") or item.get("title") or "")


def _append_batch_queue_log(event: str, **payload: Any) -> None:
    try:
        BATCH_QUEUE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BATCH_QUEUE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time": china_now().isoformat(),
                        "event": event,
                        **payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        return


def _log_archive(event: str, message: str = "", level: str = "info", **payload: Any) -> None:
    append_business_log("archive", event, message, level=level, **payload)


def _base_three_mf_limit_guard() -> dict[str, Any]:
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


def _three_mf_limit_now(reference: Optional[datetime] = None) -> datetime:
    return china_now()


def _parse_three_mf_limit_time(value: str) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    return parse_datetime(raw)


def _write_three_mf_limit_guard(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_app_dirs()
    current = _base_three_mf_limit_guard()
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


def _read_three_mf_limit_guard() -> dict[str, Any]:
    ensure_app_dirs()
    if not THREE_MF_LIMIT_GUARD_PATH.exists():
        return _base_three_mf_limit_guard()

    try:
        payload = json.loads(THREE_MF_LIMIT_GUARD_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _base_three_mf_limit_guard()

    state = _base_three_mf_limit_guard()
    if isinstance(payload, dict):
        state.update(payload)

    if bool(state.get("active")):
        limited_until = str(state.get("limited_until") or "").strip()
        parsed_until = _parse_three_mf_limit_time(limited_until)
        if parsed_until is None:
            return _write_three_mf_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
        if parsed_until <= _three_mf_limit_now(parsed_until):
            return _write_three_mf_limit_guard({"active": False, "limited_until": "", "message": "", "reason": ""})
    return state


def _is_three_mf_limit_guard_active(state: Optional[dict[str, Any]] = None) -> bool:
    current = state or _read_three_mf_limit_guard()
    return bool(current.get("active"))


def _is_three_mf_limit_guard_active_for_url(url: str, state: Optional[dict[str, Any]] = None) -> bool:
    current = state or _read_three_mf_limit_guard()
    if not _is_three_mf_limit_guard_active(current):
        return False
    guard_source = normalize_makerworld_source(url=current.get("model_url"))
    target_source = normalize_makerworld_source(url=url)
    if not guard_source or not target_source:
        return True
    return guard_source == target_source


def _three_mf_limit_until() -> str:
    now = _three_mf_limit_now()
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def _three_mf_limit_message(state: Optional[dict[str, Any]] = None) -> str:
    current = state or _read_three_mf_limit_guard()
    base_message = str(current.get("message") or "").strip() or THREE_MF_LIMIT_DEFAULT_MESSAGE
    limited_until = str(current.get("limited_until") or "").strip()
    if not limited_until:
        return base_message
    try:
        parsed_until = _parse_three_mf_limit_time(limited_until)
        if parsed_until is None:
            return base_message
        until_text = parsed_until.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return base_message
    return f"{base_message.rstrip('。')}，自动重试暂停至 {until_text}。"


def _activate_three_mf_limit_guard(
    *,
    message: str = "",
    model_id: str = "",
    model_url: str = "",
    instance_id: str = "",
) -> dict[str, Any]:
    return _write_three_mf_limit_guard(
        {
            "active": True,
            "limited_until": _three_mf_limit_until(),
            "last_hit_at": _three_mf_limit_now().isoformat(timespec="seconds"),
            "message": str(message or "").strip() or THREE_MF_LIMIT_DEFAULT_MESSAGE,
            "reason": "download_limited",
            "model_id": str(model_id or "").strip(),
            "model_url": str(model_url or "").strip(),
            "instance_id": str(instance_id or "").strip(),
        }
    )


def _missing_3mf_message_from_result(
    item: dict[str, Any],
    limit_guard: Optional[dict[str, Any]] = None,
    *,
    url: str = "",
) -> str:
    download_state = str(item.get("downloadState") or "").strip()
    download_message = str(item.get("downloadMessage") or "").strip()
    if download_state or download_message:
        return describe_three_mf_failure(
            download_state,
            download_message,
            url=url,
            limit_message=_three_mf_limit_message(limit_guard) if download_state == "download_limited" else "",
        )
    return "等待重新下载"


@contextmanager
def _temporary_proxy_env(config):
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


class ArchiveTaskManager:
    def __init__(self) -> None:
        self.store = JsonStore()
        self.task_store = TaskStateStore()
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._preview_lock = threading.Lock()
        self._batch_previews: dict[str, dict] = {}

    def submit(self, url: str, force: bool = False, preview_token: str = "", meta: Optional[dict] = None) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = detect_archive_mode(clean_url)
        if mode == "single_model":
            return self._submit_single(clean_url, force=force, meta=meta)
        if mode in {"author_upload", "collection_models"}:
            return self._submit_batch(clean_url, mode, preview_token=preview_token)
        return {
            "accepted": False,
            "message": "无法识别该链接类型，请输入单模型、作者上传页或收藏夹页面。",
            "mode": mode,
            "url": clean_url,
        }

    def preview(self, url: str) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = detect_archive_mode(clean_url)
        if mode == "single_model":
            return {
                "accepted": True,
                "mode": mode,
                "url": clean_url,
                "requires_confirmation": False,
                "message": "识别为单模型链接。",
            }
        if mode not in {"author_upload", "collection_models"}:
            return {
                "accepted": False,
                "message": "无法识别该链接类型，请输入单模型、作者上传页或收藏夹页面。",
                "mode": mode,
                "url": clean_url,
            }
        return self._preview_batch(clean_url, mode)

    def _queued_task_keys(self) -> set[str]:
        queue = self.task_store.load_archive_queue()
        items = (queue.get("active") or []) + (queue.get("queued") or [])
        return {_queue_item_key(item) for item in items if item.get("url") or item.get("title")}

    def _archived_task_keys(self) -> set[str]:
        snapshot = get_archive_snapshot()
        return set(snapshot.get("archived_keys") or [])

    def _deleted_task_lookup(self) -> dict[str, dict[str, str]]:
        deleted_dirs = set(self.task_store.load_model_flags().get("deleted") or [])
        if not deleted_dirs:
            return {}

        snapshot = get_archive_snapshot()
        lookup: dict[str, dict[str, str]] = {}
        for item in snapshot.get("models") or []:
            model_dir = str(item.get("model_dir") or "").strip().strip("/")
            if not model_dir or model_dir not in deleted_dirs:
                continue
            payload = {
                "model_dir": model_dir,
                "title": str(item.get("title") or model_dir),
            }
            model_id = str(item.get("id") or "").strip()
            if model_id:
                lookup[f"model:{model_id}"] = payload
            origin_url = normalize_source_url(str(item.get("origin_url") or ""))
            if origin_url:
                lookup[origin_url] = payload
        return lookup

    def _queue_state_snapshot(self) -> dict[str, Any]:
        queue = self.task_store.load_archive_queue()
        active = list(queue.get("active") or [])
        queued = list(queue.get("queued") or [])
        recent_failures = list(queue.get("recent_failures") or [])
        return {
            "queue": queue,
            "active": active,
            "queued": queued,
            "recent_failures": recent_failures,
            "active_by_key": {
                _queue_item_key(item): item
                for item in active
                if _queue_item_key(item)
            },
            "queued_by_key": {
                _queue_item_key(item): item
                for item in queued
                if _queue_item_key(item)
            },
            "failed_by_key": {
                _queue_item_key(item): item
                for item in recent_failures
                if _queue_item_key(item)
            },
        }

    def _normalize_batch_expected_items(self, items: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in items or []:
            if isinstance(raw, str):
                url = normalize_source_url(raw)
                key = _task_key(url)
                normalized.append(
                    {
                        "url": url,
                        "task_key": key,
                        "model_id": extract_model_id(url),
                        "attempts": 1,
                        "status": "",
                        "last_task_id": "",
                    }
                )
                continue

            if not isinstance(raw, dict):
                continue

            url = normalize_source_url(str(raw.get("url") or ""))
            key = str(raw.get("task_key") or "").strip() or _task_key(url)
            normalized.append(
                {
                    "url": url,
                    "task_key": key,
                    "model_id": str(raw.get("model_id") or extract_model_id(url) or "").strip(),
                    "attempts": max(int(raw.get("attempts") or 1), 1),
                    "status": str(raw.get("status") or "").strip(),
                    "last_task_id": str(raw.get("last_task_id") or raw.get("child_task_id") or "").strip(),
                }
            )
        return normalized

    def _refresh_batch_tasks(self) -> bool:
        snapshot = self._queue_state_snapshot()
        active_items = snapshot["active"]
        batch_tasks = [
            item
            for item in active_items
            if str(item.get("mode") or "") in BATCH_TASK_MODES
            and self._normalize_batch_expected_items((item.get("meta") or {}).get("batch_expected_items"))
        ]
        if not batch_tasks:
            return False

        active_batch_ids = {str(item.get("id") or "") for item in batch_tasks}
        active_child_by_key = {
            _queue_item_key(item): item
            for item in active_items
            if str(item.get("id") or "") not in active_batch_ids and _queue_item_key(item)
        }
        queued_by_key = dict(snapshot["queued_by_key"])
        failed_by_key = dict(snapshot["failed_by_key"])
        archived_keys = self._archived_task_keys()

        for batch_task in batch_tasks:
            batch_id = str(batch_task.get("id") or "")
            batch_url = str(batch_task.get("url") or "")
            meta = dict(batch_task.get("meta") or {})
            expected_items = self._normalize_batch_expected_items(meta.get("batch_expected_items"))
            if not expected_items:
                continue
            previous_batch_progress = (
                dict(meta.get("batch_progress") or {})
                if isinstance(meta.get("batch_progress"), dict)
                else {}
            )

            completed_count = 0
            running_count = 0
            queued_count = 0
            failed_count = 0
            updated = False

            for item in expected_items:
                key = str(item.get("task_key") or "").strip() or _task_key(item.get("url") or "")
                item["task_key"] = key
                status = str(item.get("status") or "").strip()

                if key in archived_keys:
                    if status != "archived":
                        item["status"] = "archived"
                        updated = True
                    completed_count += 1
                    continue

                if key in active_child_by_key:
                    if status != "running":
                        item["status"] = "running"
                        updated = True
                    running_count += 1
                    continue

                if key in queued_by_key:
                    if status != "queued":
                        item["status"] = "queued"
                        updated = True
                    queued_count += 1
                    continue

                if key in failed_by_key:
                    if status != "failed":
                        item["status"] = "failed"
                        updated = True
                    failed_count += 1
                    continue

                attempts = max(int(item.get("attempts") or 1), 1)
                if attempts < MAX_BATCH_CHILD_REQUEUE_ATTEMPTS:
                    child_task_id = self._enqueue_single_task(
                        item.get("url") or "",
                        message=f"批量任务补回：{batch_url}",
                        mode="single_model",
                        meta={
                            "batch_parent_id": batch_id,
                            "batch_source_url": batch_url,
                            "batch_requeued": True,
                        },
                    )
                    item["attempts"] = attempts + 1
                    item["last_task_id"] = child_task_id
                    item["status"] = "queued"
                    queued_count += 1
                    updated = True
                    _append_batch_queue_log(
                        "child_requeued",
                        batch_task_id=batch_id,
                        batch_url=batch_url,
                        child_task_id=child_task_id,
                        model_url=item.get("url") or "",
                        model_id=item.get("model_id") or "",
                        task_key=key,
                        attempts=item["attempts"],
                    )
                    continue

                if status != "failed":
                    item["status"] = "failed"
                    updated = True
                    _append_batch_queue_log(
                        "child_lost",
                        batch_task_id=batch_id,
                        batch_url=batch_url,
                        model_url=item.get("url") or "",
                        model_id=item.get("model_id") or "",
                        task_key=key,
                        attempts=attempts,
                    )
                failed_count += 1

            total_expected = len(expected_items)
            if total_expected <= 0:
                continue

            done_count = completed_count + failed_count
            remaining_count = max(total_expected - done_count, 0)
            progress = 60 + int((done_count / total_expected) * 40)

            meta["batch_expected_items"] = expected_items
            meta["batch_progress"] = {
                "total": total_expected,
                "completed": completed_count,
                "failed": failed_count,
                "running": running_count,
                "queued": queued_count,
                "remaining": remaining_count,
            }
            progress_changed = previous_batch_progress != meta["batch_progress"]

            if done_count >= total_expected and running_count == 0 and queued_count == 0:
                summary = (
                    f"批量归档完成：成功 {completed_count} 个，失败 {failed_count} 个。"
                )
                self.task_store.update_active_task(
                    batch_id,
                    progress=100,
                    message=summary,
                    meta=meta,
                )
                self.task_store.complete_archive_task(batch_id)
                _append_batch_queue_log(
                    "batch_completed",
                    batch_task_id=batch_id,
                    batch_url=batch_url,
                    completed=completed_count,
                    failed=failed_count,
                    total=total_expected,
                )
                continue

            message = (
                f"批量归档执行中：成功 {completed_count}/{total_expected}，"
                f"运行中 {running_count}，排队中 {queued_count}，失败 {failed_count}。"
            )
            if updated or progress_changed or str(batch_task.get("message") or "") != message:
                self.task_store.update_active_task(
                    batch_id,
                    progress=min(progress, 99),
                    message=message,
                    meta=meta,
                )

        return True

    def _prune_batch_previews(self) -> None:
        cutoff = time.time() - 15 * 60
        with self._preview_lock:
            expired_keys = [
                key
                for key, item in self._batch_previews.items()
                if float(item.get("created_at") or 0) < cutoff
            ]
            for key in expired_keys:
                self._batch_previews.pop(key, None)

    def _store_batch_preview(self, clean_url: str, mode: str, discovered: dict) -> str:
        self._prune_batch_previews()
        preview_token = uuid.uuid4().hex
        payload = {
            "created_at": time.time(),
            "url": clean_url,
            "mode": mode,
            "discovered_items": list(discovered.get("items") or []),
            "expected_total": discovered.get("expected_total"),
            "pages_scanned": discovered.get("pages_scanned"),
            "scan_mode": discovered.get("mode") or "",
            "source_name": str(discovered.get("source_name") or "").strip(),
        }
        with self._preview_lock:
            self._batch_previews[preview_token] = payload
        return preview_token

    def _consume_batch_preview(self, preview_token: str, clean_url: str, mode: str) -> Optional[dict]:
        if not preview_token:
            return None
        self._prune_batch_previews()
        with self._preview_lock:
            preview = self._batch_previews.pop(preview_token, None)
        if not preview:
            return None
        if preview.get("url") != clean_url or preview.get("mode") != mode:
            return None
        return preview

    def peek_batch_preview(self, preview_token: str, url: str, mode: Optional[str] = None) -> Optional[dict]:
        clean_url = normalize_source_url(url)
        clean_mode = mode or detect_archive_mode(clean_url)
        if not preview_token or clean_mode not in BATCH_TASK_MODES:
            return None
        self._prune_batch_previews()
        with self._preview_lock:
            preview = self._batch_previews.get(preview_token)
            if not preview:
                return None
            if preview.get("url") != clean_url or preview.get("mode") != clean_mode:
                return None
            return dict(preview)

    def _enqueue_single_task(self, url: str, message: str = "等待归档", mode: str = "", meta: Optional[dict] = None) -> str:
        task_id = uuid.uuid4().hex
        self.task_store.enqueue_archive_task(
            {
                "id": task_id,
                "url": url,
                "title": url,
                "mode": mode,
                "meta": meta or {},
                "status": "queued",
                "progress": 0,
                "message": message,
                "updated_at": china_now().isoformat(),
            }
        )
        return task_id

    def _submit_single(self, clean_url: str, force: bool = False, meta: Optional[dict] = None) -> dict:
        task_key = _task_key(clean_url)
        deleted_item = self._deleted_task_lookup().get(task_key)
        if deleted_item is not None:
            message = f"该模型已在 MakerHub 端删除，默认不会再次归档：{deleted_item.get('title') or clean_url}"
            _log_archive(
                "single_submit_skipped",
                message,
                url=clean_url,
                task_key=task_key,
                model_dir=deleted_item.get("model_dir") or "",
            )
            return {
                "accepted": False,
                "message": message,
                "mode": "single_model",
                "url": clean_url,
            }
        limit_guard = _read_three_mf_limit_guard()
        if force and _is_three_mf_limit_guard_active_for_url(clean_url, limit_guard):
            return {
                "accepted": False,
                "message": _three_mf_limit_message(limit_guard),
                "mode": "single_model",
                "url": clean_url,
            }
        if task_key in self._queued_task_keys():
            _log_archive("single_submit_skipped", "单模型已在归档队列中。", url=clean_url, task_key=task_key)
            return {
                "accepted": False,
                "message": "该模型已经在归档队列中。",
                "mode": "single_model",
                "url": clean_url,
            }
        if not force and task_key in self._archived_task_keys():
            _log_archive("single_submit_skipped", "单模型已归档，跳过重复提交。", url=clean_url, task_key=task_key)
            return {
                "accepted": False,
                "message": "该模型已归档，无需重复加入。",
                "mode": "single_model",
                "url": clean_url,
            }

        queue_message = "等待归档" if not force else "等待重新下载缺失 3MF"
        task_meta = dict(meta or {})
        if force:
            task_meta.setdefault("missing_3mf_retry", True)
        task_id = self._enqueue_single_task(clean_url, message=queue_message, mode="single_model", meta=task_meta)
        self._ensure_worker()
        _log_archive(
            "single_submitted",
            "单模型归档任务已入队。" if not force else "缺失 3MF 重新下载任务已入队。",
            url=clean_url,
            task_id=task_id,
            task_key=task_key,
            force=force,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": "single_model",
            "url": clean_url,
            "message": "归档任务已加入队列。" if not force else "缺失 3MF 重新下载任务已加入队列。",
        }

    def _is_missing_3mf_retry_task(self, item: dict) -> bool:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        if meta.get("missing_3mf_retry"):
            return True
        message = str(item.get("message") or "")
        return "缺失 3MF" in message

    def _pause_missing_3mf_retry_tasks_for_limit(self, state: Optional[dict[str, Any]] = None) -> int:
        guard_state = state or _read_three_mf_limit_guard()
        if not _is_three_mf_limit_guard_active(guard_state):
            return 0

        message = _three_mf_limit_message(guard_state)
        queue = self.task_store.load_archive_queue()
        queued_items = list(queue.get("queued") or [])
        kept_items: list[dict] = []
        paused_items: list[dict] = []

        for item in queued_items:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            model_url = normalize_source_url(
                str(meta.get("model_url") or item.get("url") or "")
            )
            if (
                self._is_missing_3mf_retry_task(item)
                and _is_three_mf_limit_guard_active_for_url(model_url, guard_state)
            ):
                paused_items.append(item)
            else:
                kept_items.append(item)

        if not paused_items:
            return 0

        queue["queued"] = kept_items
        self.task_store.save_archive_queue(queue)

        for item in paused_items:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            model_url = normalize_source_url(
                str(meta.get("model_url") or item.get("url") or "")
            )
            model_id = str(meta.get("model_id") or extract_model_id(model_url) or "").strip()
            self.task_store.update_missing_3mf_status(
                model_id=model_id,
                title=str(meta.get("title") or ""),
                instance_id=str(meta.get("instance_id") or ""),
                model_url=model_url,
                status="missing",
                message=message,
            )

        append_business_log(
            "missing_3mf",
            "retry_queue_paused_daily_limit",
            message,
            paused_count=len(paused_items),
            limited_until=guard_state.get("limited_until") or "",
        )
        return len(paused_items)

    def retry_missing_3mf(self, model_url: str, model_id: str = "", title: str = "", instance_id: str = "") -> dict:
        clean_url = normalize_source_url(model_url)
        clean_model_id = str(model_id or "").strip()
        if not clean_url and clean_model_id:
            clean_url = normalize_model_url(f"/zh/models/{clean_model_id}")
        if not clean_url:
            _log_archive("missing_retry_rejected", "缺少可用模型链接，无法重新下载 3MF。", level="warning")
            return {
                "accepted": False,
                "message": "缺少可用模型链接，无法重新下载 3MF。",
            }

        config = self.store.load()
        if not _select_cookie(clean_url, config):
            message = "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。"
            _log_archive(
                "missing_retry_rejected",
                message,
                level="warning",
                model_id=clean_model_id,
                model_url=clean_url,
                instance_id=instance_id,
            )
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="missing",
                message=message,
            )
            return {
                "accepted": False,
                "message": message,
            }

        limit_guard = _read_three_mf_limit_guard()
        if _is_three_mf_limit_guard_active_for_url(clean_url, limit_guard):
            message = _three_mf_limit_message(limit_guard)
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="missing",
                message=message,
            )
            return {
                "accepted": False,
                "message": message,
            }

        result = self.submit(
            clean_url,
            force=True,
            meta={
                "missing_3mf_retry": True,
                "model_id": clean_model_id or extract_model_id(clean_url),
                "model_url": clean_url,
                "title": title,
                "instance_id": instance_id,
            },
        )
        if result.get("accepted"):
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已加入重新下载队列",
            )
            return result

        message = str(result.get("message") or "")
        if "已经在归档队列中" in message:
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title="" if clean_model_id else title,
                instance_id="" if clean_model_id else instance_id,
                model_url="" if clean_model_id else clean_url,
                status="queued",
                message="已存在于归档队列",
            )
        return result

    def cancel_missing_3mf(self, model_id: str = "", model_url: str = "", title: str = "", instance_id: str = "") -> dict:
        clean_model_id = str(model_id or "").strip()
        clean_title = str(title or "").strip()
        clean_instance_id = str(instance_id or "").strip()
        clean_url = normalize_source_url(model_url)

        if not any((clean_model_id, clean_title, clean_instance_id, clean_url)):
            return {
                "success": False,
                "message": "缺少可识别的缺失 3MF 条目标识。",
            }

        before = self.task_store.load_missing_3mf()
        after = self.task_store.remove_missing_3mf_item(
            model_id=clean_model_id,
            title=clean_title,
            instance_id=clean_instance_id,
            model_url=clean_url,
        )
        removed = max(int(before.get("count") or 0) - int(after.get("count") or 0), 0)
        append_business_log(
            "missing_3mf",
            "cancelled",
            "缺失 3MF 任务已取消。" if removed else "没有找到可取消的缺失 3MF 任务。",
            model_id=clean_model_id,
            model_url=clean_url,
            instance_id=clean_instance_id,
            removed_count=removed,
        )
        return {
            "success": removed > 0,
            "removed_count": removed,
            "message": "已取消该缺失 3MF 任务。" if removed else "没有找到可取消的缺失 3MF 任务。",
        }

    def retry_all_missing_3mf(self) -> dict:
        missing_payload = self.task_store.load_missing_3mf()
        items = missing_payload.get("items") or []
        limit_guard = _read_three_mf_limit_guard()
        if _is_three_mf_limit_guard_active(limit_guard):
            message = _three_mf_limit_message(limit_guard)
            paused_count = self._pause_missing_3mf_retry_tasks_for_limit(limit_guard)
            append_business_log(
                "missing_3mf",
                "retry_all_paused_daily_limit",
                message,
                total=len(items),
                paused_queue_count=paused_count,
            )
            return {
                "accepted": False,
                "accepted_count": 0,
                "queued_count": 0,
                "failed_count": len(items),
                "message": message,
            }
        accepted = 0
        queued = 0
        failed = 0
        last_message = ""

        for item in items:
            result = self.retry_missing_3mf(
                model_url=str(item.get("model_url") or ""),
                model_id=str(item.get("model_id") or ""),
                title=str(item.get("title") or ""),
                instance_id=str(item.get("instance_id") or ""),
            )
            last_message = str(result.get("message") or "")
            if result.get("accepted"):
                accepted += 1
                continue
            if "已经在归档队列中" in last_message:
                queued += 1
            else:
                failed += 1

        append_business_log(
            "missing_3mf",
            "retry_all_completed",
            "缺失 3MF 全部重试处理完成。",
            total=len(items),
            accepted=accepted,
            queued=queued,
            failed=failed,
        )
        return {
            "accepted": accepted > 0 or queued > 0,
            "accepted_count": accepted,
            "queued_count": queued,
            "failed_count": failed,
            "message": (
                f"缺失 3MF 重试完成：新增入队 {accepted} 个，已在队列 {queued} 个，失败 {failed} 个。"
                if items
                else "当前没有缺失 3MF 任务。"
            ),
        }

    def _preview_batch(self, clean_url: str, mode: str) -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            _log_archive("batch_preview_rejected", "批量预扫描缺少 Cookie。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            _log_archive("batch_preview_skipped", "批量归档任务已在队列中。", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        with _temporary_proxy_env(config):
            _log_archive("batch_preview_started", "开始批量预扫描。", url=clean_url, mode=mode)
            discovered = discover_batch_model_urls(clean_url, cookie)
            discovered["source_name"] = resolve_batch_source_name(clean_url, cookie)

        discovered_items = list(discovered.get("items") or [])
        discovered_count = len(discovered_items)
        if discovered_count <= 0:
            _log_archive("batch_preview_empty", "批量预扫描没有发现可归档模型。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "没有扫描到可归档模型，请检查链接或 Cookie 是否有效。",
            }

        pending_keys = self._queued_task_keys()
        archived_keys = self._archived_task_keys()
        queued_count = 0
        archived_count = 0
        new_count = 0
        for model_url in discovered_items:
            key = _task_key(model_url)
            if key in pending_keys:
                queued_count += 1
            elif key in archived_keys:
                archived_count += 1
            else:
                new_count += 1

        preview_token = self._store_batch_preview(clean_url, mode, discovered)
        expected_total = discovered.get("expected_total")
        total_hint = ""
        if isinstance(expected_total, int) and expected_total > discovered_count:
            total_hint = f"，站点标记总数 {expected_total}"

        _log_archive(
            "batch_preview_done",
            f"批量预扫描完成，发现 {discovered_count} 个模型。",
            url=clean_url,
            mode=mode,
            discovered_count=discovered_count,
            expected_total=expected_total,
            new_count=new_count,
            queued_count=queued_count,
            archived_count=archived_count,
            pages_scanned=discovered.get("pages_scanned"),
            scan_mode=discovered.get("mode") or "",
        )
        return {
            "accepted": True,
            "mode": mode,
            "url": clean_url,
            "requires_confirmation": True,
            "preview_token": preview_token,
            "discovered_count": discovered_count,
            "expected_total": expected_total,
            "queued_count": queued_count,
            "archived_count": archived_count,
            "new_count": new_count,
            "subscription_supported": mode in BATCH_TASK_MODES,
            "subscription_name": str(discovered.get("source_name") or "").strip(),
            "message": (
                f"本次扫描到 {discovered_count} 个模型{total_hint}。"
                f" 其中新增 {new_count} 个，已在队列 {queued_count} 个，已归档 {archived_count} 个。"
            ),
        }

    def _submit_batch(self, clean_url: str, mode: str, preview_token: str = "") -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            _log_archive("batch_submit_rejected", "批量提交缺少 Cookie。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            _log_archive("batch_submit_skipped", "批量归档任务已在队列中。", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        preview = self._consume_batch_preview(preview_token, clean_url, mode)
        if preview_token and preview is None:
            _log_archive("batch_submit_rejected", "批量预扫描结果已失效。", level="warning", url=clean_url, mode=mode)
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "预扫描结果已失效，请重新扫描后再确认提交。",
            }

        preview_items = list((preview or {}).get("discovered_items") or [])
        preview_count = len(preview_items)
        task_message = "等待扫描模型链接"
        if preview_count > 0:
            task_message = f"已确认提交，等待批量入队（预扫描 {preview_count} 个模型）"

        task_id = self._enqueue_single_task(
            clean_url,
            message=task_message,
            mode=mode,
            meta={
                "discovered_items": preview_items,
                "expected_total": (preview or {}).get("expected_total"),
                "pages_scanned": (preview or {}).get("pages_scanned"),
                "scan_mode": (preview or {}).get("scan_mode") or "",
            },
        )
        self._ensure_worker()
        _log_archive(
            "batch_submitted",
            "批量归档任务已入队。",
            url=clean_url,
            mode=mode,
            task_id=task_id,
            preview_count=preview_count,
            expected_total=(preview or {}).get("expected_total"),
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": mode,
            "url": clean_url,
            "preview_count": preview_count,
            "message": (
                f"批量归档任务已加入队列，确认提交 {preview_count} 个预扫描模型。"
                if preview_count > 0
                else "批量归档任务已加入队列，后台正在扫描模型链接。"
            ),
        }

    def submit_discovered_batch(
        self,
        *,
        source_url: str,
        mode: str,
        discovered_items: list[str],
        expected_total: Any = None,
        pages_scanned: Any = None,
        scan_mode: str = "",
        message_prefix: str = "",
        meta: Optional[dict] = None,
    ) -> dict:
        clean_url = normalize_source_url(source_url)
        clean_mode = mode if mode in BATCH_TASK_MODES else detect_archive_mode(clean_url)
        if clean_mode not in BATCH_TASK_MODES:
            _log_archive("discovered_batch_rejected", "订阅批量入队只支持作者页或收藏夹。", level="warning", url=clean_url, mode=clean_mode)
            return {
                "accepted": False,
                "message": "仅作者上传页或收藏夹页面支持批量归档。",
                "mode": clean_mode,
                "url": clean_url,
            }

        normalized_items = [normalize_source_url(item) for item in discovered_items or [] if normalize_source_url(item)]
        if not normalized_items:
            _log_archive("discovered_batch_empty", "订阅同步没有新增模型需要入队。", url=clean_url, mode=clean_mode)
            return {
                "accepted": False,
                "message": "本次同步没有新增模型需要入队。",
                "mode": clean_mode,
                "url": clean_url,
                "queued_count": 0,
            }

        task_id = self._enqueue_single_task(
            clean_url,
            message=f"订阅同步发现 {len(normalized_items)} 个新增模型，等待批量入队",
            mode=clean_mode,
            meta={
                "discovered_items": normalized_items,
                "expected_total": expected_total,
                "pages_scanned": pages_scanned,
                "scan_mode": scan_mode or "subscription",
                "child_queue_message_prefix": message_prefix or f"来自批量归档：{clean_url}",
                **(meta or {}),
            },
        )
        self._ensure_worker()
        _log_archive(
            "discovered_batch_submitted",
            f"订阅同步发现 {len(normalized_items)} 个新增模型，已创建批量任务。",
            url=clean_url,
            mode=clean_mode,
            task_id=task_id,
            queued_count=len(normalized_items),
            expected_total=expected_total,
            pages_scanned=pages_scanned,
            scan_mode=scan_mode,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": clean_mode,
            "url": clean_url,
            "queued_count": len(normalized_items),
            "message": f"已把 {len(normalized_items)} 个新增模型加入归档队列。",
        }

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_loop, daemon=True)
            self._worker.start()

    def resume_pending_tasks(self) -> dict:
        queue = self.task_store.requeue_active_tasks()
        if (queue.get("queued_count") or 0) > 0:
            self._ensure_worker()
        return queue

    def _run_loop(self) -> None:
        while True:
            has_active_batch = self._refresh_batch_tasks()
            queue = self.task_store.load_archive_queue()
            queued = queue.get("queued") or []
            limit_guard = _read_three_mf_limit_guard()
            if _is_three_mf_limit_guard_active(limit_guard):
                paused_count = self._pause_missing_3mf_retry_tasks_for_limit(limit_guard)
                if paused_count:
                    queue = self.task_store.load_archive_queue()
                    queued = queue.get("queued") or []
            if not queued:
                if has_active_batch:
                    time.sleep(ACTIVE_BATCH_IDLE_POLL_SECONDS)
                    continue
                return

            task = queued[0]
            task_id = task["id"]
            task_url = str(task.get("url") or "")
            self.task_store.start_archive_task(task_id)
            self.task_store.update_active_task(task_id, message="正在准备归档", progress=1)
            _log_archive(
                "task_started",
                "归档任务开始执行。",
                task_id=task_id,
                url=task_url,
                mode=str(task.get("mode") or "") or detect_archive_mode(task_url),
            )

            try:
                task_mode = str(task.get("mode") or "") or detect_archive_mode(task_url)
                if task_mode in {"author_upload", "collection_models"}:
                    self._run_batch_task(task_id, task_url, task_mode, meta=task.get("meta") or {})
                else:
                    self._run_single_task(task_id, task_url)
            except Exception as exc:
                model_id = extract_model_id(task.get("url") or "")
                if model_id:
                    self.task_store.update_missing_3mf_status(
                        model_id=model_id,
                        status="missing",
                        message=str(exc),
                    )
                self.task_store.fail_archive_task(task_id, str(exc))
                _log_archive(
                    "task_failed",
                    str(exc),
                    level="error",
                    task_id=task_id,
                    url=task_url,
                    mode=str(task.get("mode") or "") or detect_archive_mode(task_url),
                )
            finally:
                self._refresh_batch_tasks()

    def _run_batch_task(self, task_id: str, url: str, mode: str, meta: Optional[dict] = None) -> None:
        config = self.store.load()
        cookie = _select_cookie(url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")

        meta = meta if isinstance(meta, dict) else {}
        existing_expected_items = self._normalize_batch_expected_items(meta.get("batch_expected_items"))
        if existing_expected_items:
            batch_progress = meta.get("batch_progress") if isinstance(meta.get("batch_progress"), dict) else {}
            total = max(int(batch_progress.get("total") or len(existing_expected_items)), 1)
            completed = max(int(batch_progress.get("completed") or 0), 0)
            failed = max(int(batch_progress.get("failed") or 0), 0)
            progress = 60 + int(((completed + failed) / total) * 40)
            self.task_store.update_active_task(
                task_id,
                progress=max(min(progress, 99), 1),
                message="批量任务已恢复，正在继续处理子任务",
                meta=meta,
            )
            _append_batch_queue_log(
                "batch_resumed",
                batch_task_id=task_id,
                batch_url=url,
                total=len(existing_expected_items),
            )
            _log_archive("batch_resumed", "批量任务已恢复，继续等待子任务完成。", task_id=task_id, url=url, total=len(existing_expected_items))
            return

        preview_items = list(meta.get("discovered_items") or [])
        if preview_items:
            print(f"[makerhub] batch_discovery reuse_preview mode={mode} url={url} items={len(preview_items)}", flush=True)
            _log_archive("batch_scan_reuse_preview", "使用预扫描结果执行批量归档。", task_id=task_id, url=url, mode=mode, items=len(preview_items))
            discovered = {
                "items": preview_items,
                "expected_total": meta.get("expected_total"),
                "pages_scanned": meta.get("pages_scanned"),
                "mode": meta.get("scan_mode") or "preview",
            }
            self.task_store.update_active_task(
                task_id,
                progress=55,
                message=f"已使用预扫描结果，发现 {len(preview_items)} 个模型，正在加入归档队列",
            )
        else:
            print(f"[makerhub] batch_discovery start mode={mode} url={url}", flush=True)
            _log_archive("batch_scan_started", "开始扫描批量归档链接。", task_id=task_id, url=url, mode=mode)
            self.task_store.update_active_task(
                task_id,
                progress=5,
                message="正在扫描模型链接",
            )

            with _temporary_proxy_env(config):
                discovered = discover_batch_model_urls(url, cookie)

        pending_keys = self._queued_task_keys()
        archived_keys = self._archived_task_keys()
        queued_count = 0
        skipped_pending = 0
        skipped_archived = 0
        expected_items: list[dict[str, Any]] = []
        discovered_items = discovered.get("items") or []
        total_items = len(discovered_items)

        if total_items:
            self.task_store.update_active_task(
                task_id,
                progress=55,
                message=f"扫描完成，发现 {total_items} 个模型，正在加入归档队列",
            )

        child_queue_message_prefix = str(meta.get("child_queue_message_prefix") or "").strip() or f"来自批量归档：{url}"

        for index, model_url in enumerate(discovered_items, start=1):
            key = _task_key(model_url)
            if key in pending_keys:
                skipped_pending += 1
                _append_batch_queue_log(
                    "child_skipped_pending",
                    batch_task_id=task_id,
                    batch_url=url,
                    model_url=model_url,
                    model_id=extract_model_id(model_url),
                    task_key=key,
                    index=index,
                    total=total_items,
                )
            elif key in archived_keys:
                skipped_archived += 1
                _append_batch_queue_log(
                    "child_skipped_archived",
                    batch_task_id=task_id,
                    batch_url=url,
                    model_url=model_url,
                    model_id=extract_model_id(model_url),
                    task_key=key,
                    index=index,
                    total=total_items,
                )
            else:
                child_task_id = self._enqueue_single_task(
                    model_url,
                    message=child_queue_message_prefix,
                    mode="single_model",
                    meta={
                        "batch_parent_id": task_id,
                        "batch_source_url": url,
                    },
                )
                expected_items.append(
                    {
                        "url": model_url,
                        "task_key": key,
                        "model_id": extract_model_id(model_url),
                        "attempts": 1,
                        "status": "queued",
                        "last_task_id": child_task_id,
                    }
                )
                pending_keys.add(key)
                queued_count += 1
                _append_batch_queue_log(
                    "child_enqueued",
                    batch_task_id=task_id,
                    batch_url=url,
                    child_task_id=child_task_id,
                    model_url=model_url,
                    model_id=extract_model_id(model_url),
                    task_key=key,
                    index=index,
                    total=total_items,
                )

            if total_items:
                progress = 55 + int((index / total_items) * 40)
                self.task_store.update_active_task(
                    task_id,
                    progress=min(progress, 95),
                    message=f"正在加入归档队列：{index}/{total_items}",
                )

        expected_total = discovered.get("expected_total")
        total_hint = ""
        if isinstance(expected_total, int) and expected_total > total_items:
            total_hint = f"（站点总数 {expected_total}）"

        print(
            "[makerhub] batch_discovery done "
            f"mode={mode} scan_mode={discovered.get('mode') or ''} pages={discovered.get('pages_scanned') or 0} "
            f"expected_total={expected_total} discovered={total_items} queued={queued_count} "
            f"skipped_pending={skipped_pending} skipped_archived={skipped_archived}",
            flush=True,
        )
        meta.pop("discovered_items", None)
        meta["batch_expected_items"] = expected_items
        meta["batch_progress"] = {
            "total": queued_count,
            "completed": 0,
            "failed": 0,
            "running": 0,
            "queued": queued_count,
            "remaining": queued_count,
        }
        meta["batch_summary"] = {
            "discovered": total_items,
            "expected_total": expected_total,
            "queued": queued_count,
            "skipped_pending": skipped_pending,
            "skipped_archived": skipped_archived,
            "scan_mode": discovered.get("mode") or "",
            "pages_scanned": discovered.get("pages_scanned") or 0,
        }
        summary_message = (
            f"批量扫描完成：发现 {total_items} 个模型{total_hint}，"
            f"新增入队 {queued_count} 个，已在队列 {skipped_pending} 个，已归档 {skipped_archived} 个。"
        )
        _append_batch_queue_log(
            "batch_enqueued",
            batch_task_id=task_id,
            batch_url=url,
            mode=mode,
            discovered=total_items,
            expected_total=expected_total,
            queued=queued_count,
            skipped_pending=skipped_pending,
            skipped_archived=skipped_archived,
        )
        _log_archive(
            "batch_enqueued",
            summary_message,
            task_id=task_id,
            url=url,
            mode=mode,
            discovered=total_items,
            expected_total=expected_total,
            queued=queued_count,
            skipped_pending=skipped_pending,
            skipped_archived=skipped_archived,
            pages_scanned=discovered.get("pages_scanned") or 0,
            scan_mode=discovered.get("mode") or "",
        )
        if queued_count <= 0:
            self.task_store.update_active_task(
                task_id,
                progress=100,
                message=summary_message,
                meta=meta,
            )
            self.task_store.complete_archive_task(task_id)
            _log_archive("batch_completed_no_new", summary_message, task_id=task_id, url=url, mode=mode)
            return

        self.task_store.update_active_task(
            task_id,
            progress=60,
            message=(
                f"{summary_message} 子任务已入队，正在等待批量完成确认。"
            ),
            meta=meta,
        )

    def _run_single_task(self, task_id: str, url: str) -> None:
        config = self.store.load()
        cookie = _select_cookie(url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")

        model_id = extract_model_id(url)
        limit_guard = _read_three_mf_limit_guard()
        skip_three_mf_fetch = _is_three_mf_limit_guard_active_for_url(url, limit_guard)
        skip_three_mf_message = _three_mf_limit_message(limit_guard) if skip_three_mf_fetch else ""
        if model_id:
            self.task_store.update_missing_3mf_status(
                model_id=model_id,
                status="running",
                message=(
                    "每日上限未恢复，正在刷新模型元数据并跳过 3MF 下载。"
                    if skip_three_mf_fetch
                    else "正在尝试重新下载 3MF"
                ),
            )

        def progress_callback(payload: dict) -> None:
            self.task_store.update_active_task(
                task_id,
                progress=int(payload.get("percent") or 0),
                message=str(payload.get("message") or ""),
            )

        with _temporary_proxy_env(config):
            result = legacy_archive_model(
                url=url,
                cookie=cookie,
                download_dir=ARCHIVE_DIR,
                logs_dir=LOGS_DIR,
                existing_root=ARCHIVE_DIR,
                progress_callback=progress_callback,
                skip_three_mf_fetch=skip_three_mf_fetch,
                three_mf_skip_message=skip_three_mf_message,
            )

        missing_items = []
        limit_guard_state: Optional[dict[str, Any]] = limit_guard if skip_three_mf_fetch else None
        for item in result.get("missing_3mf") or []:
            if str(item.get("downloadState") or "").strip() == "download_limited":
                if not _is_three_mf_limit_guard_active_for_url(url, limit_guard_state):
                    limit_guard_state = _activate_three_mf_limit_guard(
                        message=str(item.get("downloadMessage") or ""),
                        model_id=str(result.get("model_id") or ""),
                        model_url=normalize_source_url(url),
                        instance_id=str(item.get("id") or item.get("profileId") or item.get("instanceId") or ""),
                    )
            missing_items.append(
                {
                    "model_id": str(result.get("model_id") or ""),
                    "model_url": normalize_source_url(url),
                    "title": str(item.get("title") or item.get("name") or result.get("base_name") or ""),
                    "instance_id": str(item.get("id") or item.get("profileId") or item.get("instanceId") or ""),
                    "status": "missing",
                    "message": _missing_3mf_message_from_result(item, limit_guard_state, url=url),
                    "updated_at": china_now().isoformat(),
                }
            )
        resolved_model_id = str(result.get("model_id") or "")
        self.task_store.replace_missing_3mf_for_model(resolved_model_id, missing_items)
        if limit_guard_state is not None:
            self._pause_missing_3mf_retry_tasks_for_limit(limit_guard_state)
        self.task_store.remove_recent_failures_for_model(
            resolved_model_id,
            url=normalize_source_url(url),
        )
        archived_model_dir = _resolve_archive_result_model_dir(result)
        if archived_model_dir:
            invalidate_model_detail_cache(archived_model_dir)
        if not archived_model_dir or not upsert_archive_snapshot_model(
            archived_model_dir,
            reason="archive_worker_single_task_completed",
        ):
            invalidate_archive_snapshot("archive_worker_single_task_completed")

        self.task_store.update_active_task(
            task_id,
            progress=100,
            message=f"归档完成：{result.get('base_name') or result.get('work_dir') or ''}",
        )
        self.task_store.complete_archive_task(task_id)
        _log_archive(
            "single_completed",
            f"归档完成：{result.get('base_name') or result.get('work_dir') or ''}",
            task_id=task_id,
            url=url,
            model_id=resolved_model_id,
            base_name=result.get("base_name"),
            work_dir=result.get("work_dir"),
            missing_3mf_count=len(missing_items),
        )
