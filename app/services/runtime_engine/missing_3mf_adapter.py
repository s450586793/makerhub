from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.api.dependencies import crawler, task_state_store
from app.services.three_mf import normalize_makerworld_source


RETRYABLE_MISSING_3MF_STATUSES = {
    "missing",
    "queued",
    "failed",
    "verification_required",
    "cloudflare",
    "auth_required",
    "cookie_invalid",
    "download_limited",
}


def _normalize_requested_statuses(context: dict[str, Any]) -> set[str]:
    statuses: set[str] = set()
    raw_statuses = context.get("statuses")
    if isinstance(raw_statuses, str):
        raw_iterable: Iterable[Any] = raw_statuses.split(",")
    elif isinstance(raw_statuses, Iterable):
        raw_iterable = raw_statuses
    else:
        raw_iterable = ()

    for item in raw_iterable:
        status = str(item or "").strip().lower()
        if status:
            statuses.add(status)

    single_status = str(context.get("status") or "").strip().lower()
    if single_status:
        statuses.add(single_status)
    return statuses


class Missing3mfRuntimeAdapter:
    def __init__(self, *, manager=None, task_store=None) -> None:
        self.manager = manager or crawler.manager
        self.task_store = task_store or task_state_store

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        direct_model_id = str(context.get("model_id") or "").strip()
        direct_model_url = str(context.get("model_url") or "").strip()
        direct_instance_id = str(context.get("instance_id") or "").strip()
        if direct_model_id or direct_model_url or direct_instance_id:
            return [
                {
                    "model_url": direct_model_url,
                    "model_id": direct_model_id,
                    "source": context.get("source") or context.get("platform") or "",
                    "title": context.get("title") or "",
                    "instance_id": direct_instance_id,
                    "status": context.get("status") or "queued",
                }
            ]

        platform = normalize_makerworld_source(context.get("platform")) or ""
        requested_statuses = _normalize_requested_statuses(context)
        payload = self.task_store.load_missing_3mf()
        candidates: list[dict[str, Any]] = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_platform = normalize_makerworld_source(item.get("source"), item.get("model_url")) or ""
            status = str(item.get("status") or "").strip().lower()
            if platform and item_platform and item_platform != platform:
                continue
            if requested_statuses and status not in requested_statuses:
                continue
            if status not in RETRYABLE_MISSING_3MF_STATUSES:
                continue
            candidates.append(dict(item))
        return candidates

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self.manager.retry_missing_3mf(
                model_url=item.get("model_url") or "",
                model_id=item.get("model_id") or "",
                source=item.get("source") or "",
                title=item.get("title") or "",
                instance_id=item.get("instance_id") or "",
            )
            or {}
        )

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "missing_3mf_retry",
            "status": "missing_3mf",
            "message": str(error_or_result or "缺失 3MF 重试失败。")[:500],
            "retryable": True,
        }
