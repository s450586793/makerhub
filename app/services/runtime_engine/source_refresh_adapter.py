from __future__ import annotations

from typing import Any

from app.api.dependencies import remote_refresh_manager


class SourceRefreshRuntimeAdapter:
    def __init__(self, *, manager=None) -> None:
        self.manager = manager or remote_refresh_manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        picker = getattr(self.manager, "pick_runtime_candidates", None)
        if callable(picker):
            return list(picker(context) or [])
        state = self.manager.state_payload() if hasattr(self.manager, "state_payload") else {}
        candidates = state.get("candidates") if isinstance(state, dict) else []
        return [item for item in candidates or [] if isinstance(item, dict)]

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        refresh_one = getattr(self.manager, "refresh_runtime_item", None)
        if callable(refresh_one):
            return dict(refresh_one(item, context) or {})
        return {"success": False, "message": "source refresh runtime item hook is unavailable"}

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "source_refresh",
            "status": "failed",
            "message": str(error_or_result or "源端刷新失败。")[:500],
            "retryable": True,
        }
