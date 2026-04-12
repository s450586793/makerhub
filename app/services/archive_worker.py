import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
from app.services.batch_discovery import discover_batch_model_urls, extract_model_id, normalize_model_url, normalize_source_url
from app.services.catalog import load_archive_models
from app.services.legacy_archiver import archive_model as legacy_archive_model
from app.services.task_state import TaskStateStore


def _detect_mode(url: str) -> str:
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

    def submit(self, url: str, force: bool = False) -> dict:
        clean_url = normalize_source_url(url)
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = _detect_mode(clean_url)
        if mode == "single_model":
            return self._submit_single(clean_url, force=force)
        if mode in {"author_upload", "collection_models"}:
            return self._submit_batch(clean_url, mode)
        return {
            "accepted": False,
            "message": "无法识别该链接类型，请输入单模型、作者上传页或收藏夹页面。",
            "mode": mode,
            "url": clean_url,
        }

    def _queued_task_keys(self) -> set[str]:
        queue = self.task_store.load_archive_queue()
        items = (queue.get("active") or []) + (queue.get("queued") or [])
        return {_task_key(item.get("url") or item.get("title") or "") for item in items if item.get("url") or item.get("title")}

    def _archived_task_keys(self) -> set[str]:
        keys = set()
        for item in load_archive_models():
            model_id = str(item.get("id") or "").strip()
            if model_id:
                keys.add(f"model:{model_id}")
                continue
            origin_url = str(item.get("origin_url") or "").strip()
            if origin_url:
                keys.add(_task_key(origin_url))
        return keys

    def _enqueue_single_task(self, url: str, message: str = "等待归档") -> str:
        task_id = uuid.uuid4().hex
        self.task_store.enqueue_archive_task(
            {
                "id": task_id,
                "url": url,
                "title": url,
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
        task_id = self._enqueue_single_task(clean_url, message=queue_message)
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
                title=title,
                instance_id=instance_id,
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
                title=title,
                instance_id=instance_id,
                status="queued",
                message="已加入重新下载队列",
            )
            return result

        message = str(result.get("message") or "")
        if "已经在归档队列中" in message:
            self.task_store.update_missing_3mf_status(
                model_id=clean_model_id,
                title=title,
                instance_id=instance_id,
                status="queued",
                message="已存在于归档队列",
            )
        return result

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

    def _submit_batch(self, clean_url: str, mode: str) -> dict:
        config = self.store.load()
        cookie = _select_cookie(clean_url, config)
        if not cookie:
            return {
                "accepted": False,
                "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                "mode": mode,
                "url": clean_url,
            }

        with _temporary_proxy_env(config):
            discovered = discover_batch_model_urls(clean_url, cookie)

        pending_keys = self._queued_task_keys()
        archived_keys = self._archived_task_keys()
        queued_count = 0
        skipped_pending = 0
        skipped_archived = 0

        for model_url in discovered.get("items") or []:
            key = _task_key(model_url)
            if key in pending_keys:
                skipped_pending += 1
                continue
            if key in archived_keys:
                skipped_archived += 1
                continue
            self._enqueue_single_task(model_url, message=f"来自批量归档：{clean_url}")
            pending_keys.add(key)
            queued_count += 1

        if queued_count > 0:
            self._ensure_worker()

        return {
            "accepted": queued_count > 0,
            "mode": mode,
            "url": clean_url,
            "discovered_count": len(discovered.get("items") or []),
            "queued_count": queued_count,
            "skipped_pending": skipped_pending,
            "skipped_archived": skipped_archived,
            "pages_scanned": discovered.get("pages_scanned") or 0,
            "message": (
                f"批量扫描完成：发现 {len(discovered.get('items') or [])} 个模型，"
                f"新增入队 {queued_count} 个，已在队列 {skipped_pending} 个，已归档 {skipped_archived} 个。"
            ),
        }

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._run_loop, daemon=True)
            self._worker.start()

    def _run_loop(self) -> None:
        while True:
            queue = self.task_store.load_archive_queue()
            queued = queue.get("queued") or []
            if not queued:
                return

            task = queued[0]
            task_id = task["id"]
            self.task_store.start_archive_task(task_id)
            self.task_store.update_active_task(task_id, message="正在准备归档", progress=1)

            try:
                self._run_single_task(task_id, task["url"])
            except Exception as exc:
                model_id = extract_model_id(task.get("url") or "")
                if model_id:
                    self.task_store.update_missing_3mf_status(
                        model_id=model_id,
                        status="missing",
                        message=str(exc),
                    )
                self.task_store.fail_archive_task(task_id, str(exc))

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
        self.task_store.replace_missing_3mf_for_model(str(result.get("model_id") or ""), missing_items)

        self.task_store.update_active_task(
            task_id,
            progress=100,
            message=f"归档完成：{result.get('base_name') or result.get('work_dir') or ''}",
        )
        self.task_store.complete_archive_task(task_id)
