import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import config as config_api
from app.core.store import JsonStore
from app.schemas.models import CookiePair


class ConfigCookieApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_save_cookies_queues_source_sync_without_running_it_inline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = []
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            retry_result = {"queued_count": 1, "subscription_ids": ["sub-1"]}
            queued_result = {"queued_count": 1, "platforms": ["cn"]}

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value=retry_result) as retry_mock, \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value=queued_result) as queue_mock, \
                    patch.object(config_api.subscription_manager, "sync_cookie_sources") as sync_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.save_cookies(
                    [
                        CookiePair(platform="cn", cookie="token=ok"),
                        CookiePair(platform="global", cookie=""),
                    ],
                    request,
                )

            retry_mock.assert_called_once_with({"cn"})
            queue_mock.assert_called_once_with({"cn"}, reason="cookie_save")
            sync_mock.assert_not_called()
            self.assertEqual(payload["subscription_retry"], retry_result)
            self.assertEqual(payload["cookie_source_sync"], queued_result)
            self.assertEqual(store.load().cookies[0].cookie, "token=ok")


if __name__ == "__main__":
    unittest.main()
