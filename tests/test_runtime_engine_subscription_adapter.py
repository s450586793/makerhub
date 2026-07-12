import unittest
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from app.api import subscriptions_routes
from app.services.runtime_engine.subscription_adapter import SubscriptionRuntimeAdapter


class SubscriptionRuntimeAdapterTest(unittest.TestCase):
    def test_discover_uses_manager_runtime_sources(self):
        manager = SimpleNamespace(
            pick_runtime_subscriptions=lambda context: [
                {"subscription_id": "sub-1", "url": "https://makerworld.com/zh/@demo/upload"}
            ]
        )
        adapter = SubscriptionRuntimeAdapter(manager=manager)

        candidates = adapter.discover({})

        self.assertEqual(candidates[0]["subscription_id"], "sub-1")

    def test_execute_item_calls_sync_subscription_runtime(self):
        calls = []
        manager = SimpleNamespace(
            sync_subscription_runtime=lambda item, context: calls.append((item, context)) or {"success": True, "queued": 3}
        )
        adapter = SubscriptionRuntimeAdapter(manager=manager)

        result = adapter.execute_item({"subscription_id": "sub-1"}, {"run_id": "run-1"})

        self.assertTrue(result["success"])
        self.assertEqual(result["queued"], 3)

    def test_subscription_sync_route_uses_legacy_manager_when_runtime_env_is_truthy(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        legacy_payload = {"accepted": True, "message": "legacy sync"}
        with patch.dict(os.environ, {"MAKERHUB_RUNTIME_ENGINE": "true"}), \
                patch.object(subscriptions_routes, "run_task_api", side_effect=lambda func, *args: func(*args)), \
                patch("app.api.runtime_routes.runtime_engine.submit_run") as submit, \
                patch.object(subscriptions_routes, "_require_session_auth"), \
                patch.object(subscriptions_routes, "append_business_log"), \
                patch.object(
                    subscriptions_routes.subscription_manager,
                    "request_sync",
                    return_value=legacy_payload,
                ) as legacy_request:
            payload = asyncio.run(subscriptions_routes.sync_subscription("sub-1", request))

        submit.assert_not_called()
        legacy_request.assert_called_once_with("sub-1")
        self.assertEqual(payload, legacy_payload)


if __name__ == "__main__":
    unittest.main()
