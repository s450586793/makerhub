import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.api import remote_refresh_routes
from app.services.runtime_engine.source_refresh_adapter import SourceRefreshRuntimeAdapter


class SourceRefreshRuntimeAdapterTest(unittest.TestCase):
    def test_discover_uses_manager_candidates_when_available(self):
        manager = SimpleNamespace(
            pick_runtime_candidates=lambda context: [
                {"model_id": "1", "model_url": "https://makerworld.com/zh/models/1"},
                {"model_id": "2", "model_url": "https://makerworld.com/zh/models/2"},
            ]
        )
        adapter = SourceRefreshRuntimeAdapter(manager=manager)

        candidates = adapter.discover({"limit": 2})

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["model_id"], "1")

    def test_execute_item_calls_refresh_one_when_available(self):
        calls = []
        manager = SimpleNamespace(
            refresh_runtime_item=lambda item, context: calls.append((item, context)) or {"success": True}
        )
        adapter = SourceRefreshRuntimeAdapter(manager=manager)

        result = adapter.execute_item({"model_id": "1"}, {"run_id": "run-1"})

        self.assertTrue(result["success"])
        self.assertEqual(calls[0][0]["model_id"], "1")

    def test_remote_refresh_route_uses_runtime_engine_when_enabled(self):
        with patch.object(remote_refresh_routes, "_runtime_engine_enabled", return_value=True), \
                patch.object(
                    remote_refresh_routes,
                    "_submit_runtime_source_refresh",
                    return_value={"run_id": "run-1", "type": "source_refresh"},
                ) as submit, \
                patch.object(remote_refresh_routes.remote_refresh_manager, "trigger_manual_refresh") as legacy_trigger:
            payload = asyncio.run(remote_refresh_routes._trigger_source_refresh_run())

        submit.assert_called_once()
        legacy_trigger.assert_not_called()
        self.assertEqual(payload["run_id"], "run-1")


if __name__ == "__main__":
    unittest.main()
