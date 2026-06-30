from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch


def iter_app_routes(app):
    for route in app.routes:
        if hasattr(route, "effective_route_contexts"):
            for route_context in route.effective_route_contexts():
                yield route_context
            continue
        yield route


class InMemoryDatabaseState:
    def __init__(self, initial: dict | None = None) -> None:
        self.state = deepcopy(initial or {})
        self._stack: ExitStack | None = None

    def load(self, key: str, default: dict | None = None) -> dict:
        value = self.state.get(str(key or ""))
        if isinstance(value, dict):
            return deepcopy(value)
        return deepcopy(default or {})

    def save(self, key: str, value: dict) -> dict:
        payload = deepcopy(value if isinstance(value, dict) else {})
        self.state[str(key or "")] = payload
        return deepcopy(payload)

    def signature(self, key: str, default: dict | None = None) -> tuple[str, str]:
        payload = self.load(key, default or {})
        version = ""
        for field in ("token", "version", "updated_at", "updatedAt", "last_updated_at"):
            value = str(payload.get(field) or "").strip()
            if value:
                version = value
                break
        return (version, repr(sorted(payload.items())))

    def __enter__(self) -> "InMemoryDatabaseState":
        stack = ExitStack()
        self._stack = stack
        module_names = (
            "app.core.store",
            "app.services.auth",
            "app.services.task_state",
            "app.services.subscriptions",
            "app.services.source_library",
            "app.services.catalog",
            "app.services.local_preview_worker",
            "app.services.archive_profile_backfill",
            "app.services.archive_repair",
            "app.services.archive_worker",
            "app.services.source_health",
            "app.services.three_mf_quota",
            "app.services.self_update",
            "app.api.config",
        )
        for module_name in module_names:
            stack.enter_context(patch(f"{module_name}.load_database_json_state", side_effect=self.load, create=True))
            stack.enter_context(patch(f"{module_name}.save_database_json_state", side_effect=self.save, create=True))

        for module_name in ("app.services.source_library", "app.services.catalog"):
            stack.enter_context(patch(f"{module_name}.database_json_state_signature", side_effect=self.signature, create=True))
            stack.enter_context(
                patch(
                    f"{module_name}.load_database_json_state_version",
                    side_effect=lambda key, _default=None: self.signature(key, {})[0],
                    create=True,
                )
            )

        stack.enter_context(patch("app.core.database_json_state.database_configured", return_value=True))
        stack.enter_context(patch("app.core.database_json_state.database_driver_available", return_value=True))
        stack.enter_context(patch("app.core.database_json_state.load_json_state", side_effect=lambda key: self.load(key, {})))
        stack.enter_context(patch("app.core.database_json_state.save_json_state", side_effect=self.save))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
