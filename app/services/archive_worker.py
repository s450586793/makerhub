import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from app.core.settings import ARCHIVE_DIR, LOGS_DIR
from app.core.store import JsonStore
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

    def submit(self, url: str) -> dict:
        clean_url = str(url or "").strip()
        if not clean_url:
            return {
                "accepted": False,
                "message": "请先输入归档链接。",
            }

        mode = _detect_mode(clean_url)
        if mode != "single_model":
            return {
                "accepted": False,
                "message": "当前版本仅支持单模型链接归档，作者页和收藏夹批量归档还未接入。",
                "mode": mode,
                "url": clean_url,
            }

        task_id = uuid.uuid4().hex
        self.task_store.enqueue_archive_task(
            {
                "id": task_id,
                "url": clean_url,
                "title": clean_url,
                "status": "queued",
                "progress": 0,
                "message": "等待归档",
                "updated_at": datetime.now().isoformat(),
            }
        )
        self._ensure_worker()
        return {
            "accepted": True,
            "task_id": task_id,
            "mode": mode,
            "url": clean_url,
            "message": "归档任务已加入队列。",
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
                self.task_store.fail_archive_task(task_id, str(exc))

    def _run_single_task(self, task_id: str, url: str) -> None:
        config = self.store.load()
        cookie = _select_cookie(url, config)
        if not cookie:
            raise RuntimeError("未找到可用 Cookie，请先到设置页配置对应站点 Cookie。")

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
                    "title": str(item.get("title") or item.get("name") or result.get("base_name") or ""),
                    "status": "missing",
                }
            )
        if missing_items:
            self.task_store.merge_missing_3mf_items(missing_items)

        self.task_store.update_active_task(
            task_id,
            progress=100,
            message=f"归档完成：{result.get('base_name') or result.get('work_dir') or ''}",
        )
        self.task_store.complete_archive_task(task_id)
