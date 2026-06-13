from __future__ import annotations

from typing import Any

from app.api.dependencies import crawler, store
from app.services.archive_worker import BATCH_TASK_MODES, detect_archive_mode
from app.services.batch_discovery import discover_batch_model_urls, normalize_source_url
from app.services.cookie_utils import sanitize_cookie_header


def _select_cookie(url: str) -> str:
    config = store.load()
    platform = "global" if "makerworld.com" in url and "makerworld.com.cn" not in url else "cn"
    cookie_map = {item.platform: item.cookie for item in config.cookies}
    return sanitize_cookie_header(cookie_map.get(platform) or "")


class ArchiveRuntimeAdapter:
    def __init__(self, manager=None) -> None:
        self.manager = manager or crawler.manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        source_url = normalize_source_url(str(context.get("source_url") or context.get("url") or ""))
        if not source_url:
            return []
        mode = detect_archive_mode(source_url)
        if mode == "single_model":
            return [{"model_url": source_url, "source_url": source_url, "mode": mode}]
        if mode not in BATCH_TASK_MODES:
            return []

        discovered = discover_batch_model_urls(source_url, _select_cookie(source_url))
        items = discovered.get("items") if isinstance(discovered, dict) else discovered
        candidates: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, dict):
                model_url = normalize_source_url(str(item.get("url") or item.get("model_url") or ""))
            else:
                model_url = normalize_source_url(str(item or ""))
            if model_url:
                candidates.append({"model_url": model_url, "source_url": source_url, "mode": mode})
        return candidates

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return dict(self.manager.submit(str(item.get("model_url") or "")) or {})

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        message = str(error_or_result or "归档失败。").replace("<", "").replace(">", "")[:500]
        return {
            "type": "archive",
            "status": "failed",
            "message": message,
            "retryable": True,
        }
