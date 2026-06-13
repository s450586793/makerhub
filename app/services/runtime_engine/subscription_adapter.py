from __future__ import annotations

from typing import Any

from app.api.dependencies import subscription_manager


class SubscriptionRuntimeAdapter:
    def __init__(self, *, manager=None) -> None:
        self.manager = manager or subscription_manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        picker = getattr(self.manager, "pick_runtime_subscriptions", None)
        if callable(picker):
            return list(picker(context) or [])
        payload = self.manager.list_payload() if hasattr(self.manager, "list_payload") else {}
        return [item for item in payload.get("items") or [] if isinstance(item, dict) and item.get("enabled", True)]

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 20))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        sync_one = getattr(self.manager, "sync_subscription_runtime", None)
        if callable(sync_one):
            return dict(sync_one(item, context) or {})
        sync_now = getattr(self.manager, "request_sync", None)
        if callable(sync_now):
            subscription_id = str(item.get("subscription_id") or item.get("id") or "")
            return dict(sync_now(subscription_id) or {})
        return {"success": False, "message": "subscription runtime sync hook is unavailable"}

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "subscription_sync",
            "status": "failed",
            "message": str(error_or_result or "订阅同步失败。")[:500],
            "retryable": True,
        }
