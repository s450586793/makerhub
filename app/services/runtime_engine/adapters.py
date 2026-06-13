from __future__ import annotations

from typing import Any, Protocol


class RuntimeAdapter(Protocol):
    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        ...

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        ...

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        ...
