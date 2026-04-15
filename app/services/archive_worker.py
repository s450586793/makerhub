import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.services.batch_discovery import discover_batch_model_urls, extract_model_id, normalize_model_url, normalize_source_url
from app.services.catalog import load_archive_models
from app.services.legacy_archiver import archive_model as legacy_archive_model
from app.services.task_state import TaskStateStore

BATCH_TASK_MODES = {"author_upload", "collection_models"}
BATCH_QUEUE_LOG_PATH = LOGS_DIR / "batch_queue.log"
MAX_BATCH_CHILD_REQUEUE_ATTEMPTS = 3


def detect_archive_mode(url: str) -> str:
    lowered = (url or "").lower()
    if "/collections/models" in lowered:
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
    return str(cookie_map.get(platform) or "").strip()


def _task_key(url: str) -> str:
    model_id = extract_model_id(url)
    if model_id:
        return f"model:{model_id}"
    return normalize_source_url(url)


def _queue_item_key(item: dict) -> str:
    return _task_key(item.get("url") or item.get("title") or "")


def _append_batch_queue_log(event: str, **payload: Any) -> None:
    try:
        BATCH_QUEUE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BATCH_QUEUE_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "time": datetime.now().isoformat(),
                        "event": event,
                        **payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        return


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

    def submit(self, url: str, force: bool = False, preview_token: str = "") -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = detect_archive_mode(clean_url)
        if mode == "single_model":
            return self._submit_single(clean_url, force=force)
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
        keys = set()
        for item in load_archive_models(include_detail=False):
            model_id = str(item.get("id") or "").strip()
            if model_id:
                keys.add(f"model:{model_id}")
                continue
            origin_url = str(item.get("origin_url") or "").strip()
            if origin_url:
                keys.add(_task_key(origin_url))
        return keys

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
            if updated or str(batch_task.get("message") or "") != message:
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
                "updated_at": datetime.now().isoformat(),
            }
        )
        return task_id

    def _submit_single(self, clean_url: str, force: bool = False) -> dict:
        task_key = _task_key(clean_url)
        if task_key in self._queued_task_keys():
            return {
                "accepted": False,
                "message": "该模型已经在归档队列中。",
                "mode": "single_model",
                "url": clean_url,
            }
        if not force and task_key in self._archived_task_keys():
            return {
                "accepted": False,
                "message": "该模型已归档，无需重复加入。",
                "mode": "single_model",
                "url": clean_url,
            }

        queue_message = "等待归档" if not force else "等待重新下载缺失 3MF"
        task_id = self._enqueue_single_task(clean_url, message=queue_message, mode="single_model")
        self._ensure_worker()
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": "single_model",
            "url": clean_url,
            "message": "归档任务已加入队列。" if not force else "缺失 3MF 重新下载任务已加入队列。",
        }

    def retry_missing_3mf(self, model_url: str, model_id: str = "", title: str = "", instance_id: str = "") -> dict:
        clean_url = normalize_source_url(model_url)
        clean_model_id = str(model_id or "").strip()
        if not clean_url and clean_model_id:
            clean_url = normalize_model_url(f"/zh/models/{clean_model_id}")
        if not clean_url:
            return {
                "accepted": False,
                "message": "缺少可用模型链接，无法重新下载 3MF。",
            }

        config = self.store.load()
        if not _select_cookie(clean_url, config):
            message = "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。"
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

        result = self.submit(clean_url, force=True)
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
        return {
            "success": removed > 0,
            "removed_count": removed,
            "message": "已取消该缺失 3MF 任务。" if removed else "没有找到可取消的缺失 3MF 任务。",
        }

    def retry_all_missing_3mf(self) -> dict:
        missing_payload = self.task_store.load_missing_3mf()
        items = missing_payload.get("items") or []
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
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        with _temporary_proxy_env(config):
            discovered = discover_batch_model_urls(clean_url, cookie)

        discovered_items = list(discovered.get("items") or [])
        discovered_count = len(discovered_items)
        if discovered_count <= 0:
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
            "message": (
                f"本次扫描到 {discovered_count} 个模型{total_hint}。"
                f" 其中新增 {new_count} 个，已在队列 {queued_count} 个，已归档 {archived_count} 个。"
            ),
        }

    def _submit_batch(self, clean_url: str, mode: str, preview_token: str = "") -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        batch_task_key = _task_key(clean_url)
        if batch_task_key in self._queued_task_keys():
            return {
                "accepted": False,
                "mode": mode,
                "url": clean_url,
                "message": "该批量归档任务已经在队列中。",
            }

        preview = self._consume_batch_preview(preview_token, clean_url, mode)
        if preview_token and preview is None:
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
            return {
                "accepted": False,
                "message": "仅作者上传页或收藏夹页面支持批量归档。",
                "mode": clean_mode,
                "url": clean_url,
            }

        normalized_items = [normalize_source_url(item) for item in discovered_items or [] if normalize_source_url(item)]
        if not normalized_items:
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
            if not queued:
                if has_active_batch:
                    time.sleep(0.2)
                    continue
                return

            task = queued[0]
            task_id = task["id"]
            task_url = str(task.get("url") or "")
            self.task_store.start_archive_task(task_id)
            self.task_store.update_active_task(task_id, message="正在准备归档", progress=1)

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
            return

        preview_items = list(meta.get("discovered_items") or [])
        if preview_items:
            print(f"[makerhub] batch_discovery reuse_preview mode={mode} url={url} items={len(preview_items)}", flush=True)
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
        if queued_count <= 0:
            self.task_store.update_active_task(
                task_id,
                progress=100,
                message=summary_message,
                meta=meta,
            )
            self.task_store.complete_archive_task(task_id)
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
        if model_id:
            self.task_store.update_missing_3mf_status(
                model_id=model_id,
                status="running",
                message="正在尝试重新下载 3MF",
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
            )

        missing_items = []
        for item in result.get("missing_3mf") or []:
            missing_items.append(
                {
                    "model_id": str(result.get("model_id") or ""),
                    "model_url": normalize_source_url(url),
                    "title": str(item.get("title") or item.get("name") or result.get("base_name") or ""),
                    "instance_id": str(item.get("id") or item.get("profileId") or item.get("instanceId") or ""),
                    "status": "missing",
                    "message": "等待重新下载",
                    "updated_at": datetime.now().isoformat(),
                }
            )
        resolved_model_id = str(result.get("model_id") or "")
        self.task_store.replace_missing_3mf_for_model(resolved_model_id, missing_items)
        self.task_store.remove_recent_failures_for_model(
            resolved_model_id,
            url=normalize_source_url(url),
        )

        self.task_store.update_active_task(
            task_id,
            progress=100,
            message=f"归档完成：{result.get('base_name') or result.get('work_dir') or ''}",
        )
        self.task_store.complete_archive_task(task_id)
