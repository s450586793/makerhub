from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import config as config_api
from app.core.store import JsonStore
from app.schemas.models import CookiePair, OnlineAccountLoginRequest
from app.services.cloakbrowser_session import CloakBrowserSessionResult


def _request():
    return SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))


def _public_payload(config):
    return {"cookies": [item.model_dump() for item in config.cookies]}


class ConfigCloakBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_login_marks_browser_syncing_and_schedules_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            login_result = {
                "platform": "cn",
                "username": "13800138000",
                "cookie": "token=new",
                "display_name": "艾斯",
                "account_id": "2024907479",
                "handle": "ace",
                "avatar_url": "",
                "status": "checking",
                "message": "账号已保存。",
                "auth_payload": {},
                "cookie_items": [{"name": "device", "value": "1", "domain": ".bambulab.cn"}],
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_login", return_value=login_result), \
                    patch.object(config_api, "cloakbrowser_configured", return_value=True), \
                    patch.object(config_api, "_schedule_cloakbrowser_seed") as seed_mock, \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value={}), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value={}), \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "_public_config_payload", side_effect=_public_payload), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.login_config_online_account(
                    OnlineAccountLoginRequest(platform="cn", username="13800138000", verification_code="123456"),
                    _request(),
                )

            saved = store.load().cookies[0]
            self.assertEqual(saved.browser_status, "syncing")
            self.assertIn("正在同步", saved.browser_message)
            self.assertEqual(payload["cookies"][0]["browser_status"], "syncing")
            self.assertEqual(seed_mock.call_args.kwargs["cookie_items"][0]["name"], "device")

    async def test_store_browser_session_updates_cookie_and_queues_follow_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="cn",
                cookie="token=old",
                account_id="2024907479",
                browser_profile_id="profile-cn",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-cn",
                cookie="token=new; cf_clearance=clear",
                current_url="https://makerworld.com.cn/zh",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "online_account_metadata_from_cookie",
                        return_value={"account_id": "2024907479", "status": "ok", "message": "ok"},
                    ), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync") as source_mock, \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms") as three_mf_mock, \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as test_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertTrue(applied)
            self.assertEqual(current.cookie, "token=new; cf_clearance=clear")
            self.assertEqual(current.browser_status, "synced")
            self.assertTrue(current.browser_synced_at)
            retry_mock.assert_called_once_with({"cn"})
            source_mock.assert_called_once_with({"cn"}, reason="cloakbrowser_sync")
            three_mf_mock.assert_called_once_with({"cn"})
            test_mock.assert_called_once()

    async def test_store_browser_session_skips_network_identity_probe_when_auth_token_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="global",
                cookie="token=same-account; refreshToken=refresh",
                account_id="account-a",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-global",
                cookie="token=same-account; refreshToken=refresh; cf_clearance=verified",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "online_account_metadata_from_cookie",
                        side_effect=AssertionError("同一 token 不应等待账号网络探针"),
                    ), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms"), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync"), \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms"), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("global", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertTrue(applied)
            self.assertEqual(current.browser_profile_id, "profile-global")
            self.assertEqual(current.browser_status, "synced")
            self.assertIn("cf_clearance=verified", current.cookie)

    async def test_store_browser_session_blocks_different_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="global",
                cookie="token=old",
                account_id="account-a",
                browser_profile_id="profile-global",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(profile_id="profile-global", cookie="token=other")

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "online_account_metadata_from_cookie",
                        return_value={"account_id": "account-b", "status": "ok"},
                    ), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("global", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertFalse(applied)
            self.assertEqual(current.cookie, "token=old")
            self.assertEqual(current.browser_status, "account_mismatch")
            retry_mock.assert_not_called()

    async def test_store_browser_session_blocks_cookie_when_account_identity_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="cn",
                cookie="token=old",
                account_id="account-a",
                browser_profile_id="profile-cn",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=unknown")

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value={}), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertFalse(applied)
            self.assertEqual(current.cookie, "token=old")
            self.assertEqual(current.browser_status, "action_required")
            self.assertIn("无法确认", current.browser_message)
            retry_mock.assert_not_called()

    async def test_store_browser_session_ignores_stale_cookie_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(platform="cn", cookie="token=old", browser_profile_id="profile-cn")
            config.cookies = [CookiePair(platform="cn", cookie="token=new", browser_profile_id="profile-cn")]
            store.save(config)
            result = CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=browser")

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock:
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            self.assertFalse(applied)
            self.assertEqual(saved.cookies[0].cookie, "token=new")
            retry_mock.assert_not_called()

    async def test_open_browser_returns_public_url_and_starts_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=old")]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-cn",
                cookie="token=old",
                public_url="https://browser.example.test",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "cloakbrowser_configured", return_value=True), \
                    patch.object(config_api, "prepare_browser_login", return_value=result), \
                    patch.object(config_api, "_schedule_cloakbrowser_monitor") as monitor_mock, \
                    patch.object(config_api, "_public_config_payload", side_effect=_public_payload), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                payload = await config_api.open_config_online_account_browser("cn", _request())

            self.assertEqual(payload["browser_session"]["public_url"], "https://browser.example.test")
            self.assertEqual(store.load().cookies[0].browser_profile_id, "profile-cn")
            self.assertEqual(store.load().cookies[0].browser_status, "waiting")
            monitor_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
