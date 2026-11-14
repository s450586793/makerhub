from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from httpx2 import ASGITransport, AsyncClient

from app.api import config as config_api
from app.api import system as system_api
from app.core.store import JsonStore
from app.schemas.models import CookiePair, OnlineAccountLoginRequest
from app.services.cloakbrowser_session import (
    CloakBrowserBridgeError,
    CloakBrowserSessionResult,
    CloakBrowserUnavailable,
)


def _request():
    return SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))


def _public_payload(config):
    return {"cookies": [item.model_dump() for item in config.cookies]}


class ConfigCloakBrowserTest(unittest.IsolatedAsyncioTestCase):
    async def test_manual_login_stays_unlinked_and_does_not_seed_a_browser_profile(self):
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
            self.assertEqual(saved.browser_status, "not_linked")
            self.assertIn("手工 Cookie", saved.browser_message)
            self.assertEqual(payload["cookies"][0]["browser_status"], "not_linked")

    async def test_login_marks_browser_unavailable_without_auth_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            login_result = {
                "platform": "cn",
                "username": "13800138000",
                "cookie": "token=new",
                "status": "checking",
                "message": "账号已保存。",
                "cookie_items": [],
            }

            with patch.dict(
                os.environ,
                {"MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080"},
                clear=True,
            ), patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_login", return_value=login_result), \
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

            self.assertEqual(payload["cookies"][0]["browser_status"], "not_configured")

    async def test_manual_login_is_rejected_before_calling_makerworld_when_browser_is_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", browser_profile_id="profile-cn")]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "run_task_api") as login_mock:
                with self.assertRaises(config_api.HTTPException) as raised:
                    await config_api.login_config_online_account(
                        OnlineAccountLoginRequest(platform="cn", username="13800138000", verification_code="123456"),
                        _request(),
                    )

            self.assertEqual(raised.exception.status_code, 409)
            login_mock.assert_not_called()

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
                    patch.object(config_api, "_mark_online_account_checking") as checking_mock, \
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
            checking_mock.assert_called_once_with("cn", source="cloakbrowser_sync")

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

    async def test_store_browser_session_adopts_different_browser_account(self):
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
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync"), \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms"), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "_mark_online_account_checking"), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("global", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertTrue(applied)
            self.assertEqual(current.cookie, "token=other")
            self.assertEqual(current.account_id, "account-b")
            self.assertEqual(current.browser_status, "synced")
            retry_mock.assert_called_once_with({"global"})

    async def test_store_browser_session_adopts_browser_cookie_when_identity_lookup_is_unavailable(self):
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
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync"), \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms"), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "_mark_online_account_checking"), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertTrue(applied)
            self.assertEqual(current.cookie, "token=unknown")
            self.assertEqual(current.browser_status, "synced")
            retry_mock.assert_called_once_with({"cn"})

    async def test_store_browser_session_rejects_profile_without_auth_token_without_identity_probe(self):
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
            result = CloakBrowserSessionResult(
                profile_id="profile-cn",
                cookie="cf_clearance=browser-clearance; lang=zh",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "online_account_metadata_from_cookie",
                        side_effect=AssertionError("未登录 profile 不应发起身份探针"),
                    ) as metadata_mock, \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api, "update_three_mf_gate") as update_gate_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            current = saved.cookies[0]
            self.assertFalse(applied)
            self.assertEqual(current.cookie, "token=old")
            self.assertEqual(current.browser_status, "action_required")
            self.assertIn("尚未登录", current.browser_message)
            metadata_mock.assert_not_called()
            retry_mock.assert_not_called()
            update_gate_mock.assert_called_once_with(
                "cn",
                gate="cookie_invalid",
                reason="cloakbrowser_login_missing",
                source="cloakbrowser_sync",
                detail="指纹浏览器尚未登录 MakerWorld，请在浏览器内完成登录后再同步。",
            )

    async def test_store_browser_session_prefers_linked_profile_over_stale_saved_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(platform="cn", cookie="token=old", browser_profile_id="profile-cn")
            config.cookies = [CookiePair(platform="cn", cookie="token=new", browser_profile_id="profile-cn")]
            store.save(config)
            result = CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=browser")

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value={}), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync"), \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms"), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "_mark_online_account_checking"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result("cn", target, result, config.proxy)

            self.assertTrue(applied)
            self.assertEqual(saved.cookies[0].cookie, "token=browser")
            retry_mock.assert_called_once_with({"cn"})

    async def test_open_browser_returns_public_url_and_queues_background_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=old")]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "cloakbrowser_configured", return_value=True), \
                    patch.object(config_api, "cloakbrowser_public_url", return_value="https://browser.example.test"), \
                    patch.object(config_api, "_schedule_cloakbrowser_login", return_value=True) as schedule_mock, \
                    patch.object(config_api, "prepare_browser_login") as prepare_mock, \
                    patch.object(config_api, "_public_config_payload", side_effect=_public_payload), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                payload = await config_api.open_config_online_account_browser("cn", _request())

            self.assertEqual(payload["browser_session"]["public_url"], "https://browser.example.test")
            self.assertEqual(payload["browser_session"]["status"], "launching")
            self.assertEqual(store.load().cookies[0].browser_status, "launching")
            prepare_mock.assert_not_called()
            schedule_mock.assert_called_once()

    async def test_open_browser_creates_an_empty_browser_backed_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "cloakbrowser_configured", return_value=True), \
                    patch.object(config_api, "cloakbrowser_public_url", return_value="https://browser.example.test"), \
                    patch.object(config_api, "_schedule_cloakbrowser_login", return_value=True), \
                    patch.object(config_api, "_public_config_payload", side_effect=_public_payload), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                await config_api.open_config_online_account_browser("global", _request())

            saved = next(item for item in store.load().cookies if item.platform == "global")
            self.assertEqual(saved.platform, "global")
            self.assertEqual(saved.cookie, "")
            self.assertEqual(saved.browser_profile_id, "")
            self.assertEqual(saved.browser_status, "launching")

    async def test_open_browser_returns_json_service_error_when_background_thread_cannot_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=synced",
                    browser_profile_id="profile-cn",
                    browser_status="synced",
                    browser_message="指纹浏览器登录态已同步。",
                    browser_synced_at="2026-07-26T03:00:00+08:00",
                )
            ]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "cloakbrowser_configured", return_value=True), \
                    patch.object(
                        config_api,
                        "_schedule_cloakbrowser_login",
                        side_effect=RuntimeError("can't start new thread"),
                    ), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                with self.assertRaises(config_api.HTTPException) as raised:
                    await config_api.open_config_online_account_browser("cn", _request())

            self.assertEqual(raised.exception.status_code, 503)
            self.assertNotIn("can't start", str(raised.exception.detail))
            saved = store.load().cookies[0]
            self.assertEqual(saved.browser_status, "synced")
            self.assertEqual(saved.browser_synced_at, "2026-07-26T03:00:00+08:00")
            self.assertIn("后台启动任务", saved.browser_message)

    async def test_manual_browser_sync_keeps_health_responsive_during_account_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(
                platform="global",
                cookie="token=old",
                browser_profile_id="profile-global",
            )]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-global",
                cookie="token=new",
            )
            probe_started = threading.Event()
            release_probe = threading.Event()
            health_responded = threading.Event()
            responsive_during_probe = []

            def probe_metadata(**_kwargs):
                probe_started.set()
                release_probe.wait(timeout=5)
                return {"account_id": "account-global", "status": "ok", "message": "ok"}

            def guard_probe():
                # 即使事件循环被同步网络调用堵住，也能释放测试并记录失败。
                started = probe_started.wait(timeout=5)
                responsive_during_probe.append(started and health_responded.wait(timeout=2))
                release_probe.set()

            app = FastAPI()
            app.include_router(config_api.router)
            app.include_router(system_api.router)
            guard = threading.Thread(target=guard_probe, daemon=True)

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_require_session_auth"), \
                    patch.object(config_api, "collect_browser_session", return_value=result), \
                    patch.object(config_api, "online_account_metadata_from_cookie", side_effect=probe_metadata), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms"), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync"), \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms"), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "_mark_online_account_checking"), \
                    patch.object(config_api, "_public_config_payload", side_effect=_public_payload), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"), \
                    patch.object(system_api.database, "database_status", return_value={"available": True}):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://makerhub.test") as client:
                    guard.start()
                    sync_request = asyncio.create_task(
                        client.post("/api/config/online-accounts/global/browser/sync")
                    )
                    try:
                        self.assertTrue(await asyncio.to_thread(probe_started.wait, 5))
                        health = await asyncio.wait_for(client.get("/api/public/health/ready"), timeout=1)
                        self.assertEqual(health.status_code, 200)
                        self.assertTrue(health.json()["ready"])
                        health_responded.set()
                    finally:
                        release_probe.set()
                        response = await asyncio.wait_for(sync_request, timeout=5)
                        await asyncio.to_thread(guard.join, 5)

            self.assertEqual(responsive_during_probe, [True])
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["browser_session"]["applied"])
            self.assertEqual(store.load().cookies[0].cookie, "token=new")

    async def test_manual_browser_sync_preserves_synced_status_during_temporary_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=synced",
                    browser_profile_id="profile-cn",
                    browser_status="synced",
                    browser_message="指纹浏览器登录态已同步。",
                    browser_synced_at="2026-07-26T03:00:00+08:00",
                )
            ]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "collect_browser_session",
                        side_effect=CloakBrowserUnavailable("指纹浏览器返回 HTTP 502"),
                    ), \
                    patch.object(config_api, "publish_state_event"):
                with self.assertRaises(config_api.HTTPException) as raised:
                    await config_api.sync_config_online_account_browser("cn", _request())

            self.assertEqual(raised.exception.status_code, 502)
            saved = store.load().cookies[0]
            self.assertEqual(saved.browser_status, "synced")
            self.assertEqual(saved.browser_synced_at, "2026-07-26T03:00:00+08:00")
            self.assertIn("暂时不可用", saved.browser_message)

    async def test_background_browser_open_preserves_synced_status_during_bridge_protocol_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=synced",
                    browser_profile_id="profile-cn",
                    browser_status="synced",
                    browser_message="指纹浏览器登录态已同步。",
                    browser_synced_at="2026-07-26T03:00:00+08:00",
                )
            ]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "prepare_browser_login",
                        side_effect=CloakBrowserBridgeError("Network.enable timed out."),
                    ), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                config_api._run_cloakbrowser_login("cn", config.cookies[0], config.proxy)

            saved = store.load().cookies[0]
            self.assertEqual(saved.browser_status, "synced")
            self.assertEqual(saved.browser_synced_at, "2026-07-26T03:00:00+08:00")
            self.assertIn("暂时不可用", saved.browser_message)

    async def test_background_browser_open_starts_monitor_after_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(platform="cn", cookie="token=old", browser_profile_id="profile-cn")
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-cn",
                cookie="token=old",
                public_url="https://browser.example.test",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "prepare_browser_login", return_value=result) as prepare_mock, \
                    patch.object(config_api, "_schedule_cloakbrowser_monitor") as monitor_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                config_api._run_cloakbrowser_login("cn", target, config.proxy)

            saved = store.load().cookies[0]
            self.assertEqual(saved.browser_profile_id, "profile-cn")
            self.assertEqual(saved.browser_status, "waiting")
            self.assertEqual(prepare_mock.call_args.args[1], "")
            monitor_mock.assert_called_once()

    async def test_background_browser_open_is_deduplicated_per_platform(self):
        pending_threads = []

        class PendingThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target
                pending_threads.append(self)

            def start(self):
                return None

        target = CookiePair(platform="cn", browser_profile_id="profile-cn")
        try:
            with patch.object(config_api.threading, "Thread", side_effect=lambda **kwargs: PendingThread(**kwargs)):
                first = config_api._schedule_cloakbrowser_login("cn", target, config_api.ProxyConfig())
                second = config_api._schedule_cloakbrowser_login("cn", target, config_api.ProxyConfig())
        finally:
            config_api.CLOAKBROWSER_LOGIN_RUNNING.discard("cn")

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(pending_threads), 1)

    async def test_browser_monitor_stops_after_service_outage_without_retrying(self):
        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self._target = target

            def start(self):
                self._target()

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="cn",
                browser_profile_id="profile-cn",
                browser_status="waiting",
                browser_message="指纹浏览器已打开，完成登录后会自动同步回 MakerHub。",
            )
            config.cookies = [target]
            store.save(config)

            with patch.object(config_api, "store", store), \
                    patch.object(
                        config_api,
                        "collect_browser_session",
                        side_effect=CloakBrowserUnavailable("指纹浏览器返回 HTTP 500：Xvnc failed to start"),
                    ) as collect_mock, \
                    patch.object(config_api.threading, "Thread", side_effect=lambda **kwargs: ImmediateThread(**kwargs)), \
                    patch.object(config_api.time, "monotonic", side_effect=[0.0, 0.0, 601.0]), \
                    patch.object(config_api.time, "sleep") as sleep_mock:
                config_api._schedule_cloakbrowser_monitor("cn", target, config.proxy)

            saved = store.load().cookies[0]
            collect_mock.assert_called_once_with("cn", "profile-cn")
            sleep_mock.assert_not_called()
            self.assertEqual(saved.browser_status, "waiting")
            self.assertIn("服务暂时不可用", saved.browser_message)

    async def test_browser_monitor_clears_stale_status_when_authenticated_cookie_is_unchanged(self):
        class ImmediateThread:
            def __init__(self, *, target, **_kwargs):
                self._target = target

            def start(self):
                self._target()

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="global",
                cookie="token=same-account",
                browser_profile_id="profile-global",
                browser_status="action_required",
                browser_message="请先在关联的指纹浏览器中完成 MakerWorld 登录。",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-global",
                cookie="token=same-account",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "collect_browser_session", return_value=result), \
                    patch.object(config_api.threading, "Thread", side_effect=lambda **kwargs: ImmediateThread(**kwargs)), \
                    patch.object(config_api.time, "monotonic", side_effect=[0.0, 0.0, 601.0]), \
                    patch.object(config_api.time, "sleep") as sleep_mock, \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms") as retry_mock, \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync") as source_sync_mock, \
                    patch.object(config_api, "_retry_verification_missing_3mf_for_platforms") as retry_three_mf_mock, \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as test_mock, \
                    patch.object(config_api, "_mark_online_account_checking") as checking_mock, \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                config_api._schedule_cloakbrowser_monitor("global", target, config.proxy)

            saved = store.load().cookies[0]
            sleep_mock.assert_not_called()
            self.assertEqual(saved.browser_status, "synced")
            self.assertEqual(saved.browser_message, "指纹浏览器登录态已同步。")
            checking_mock.assert_called_once_with("global", source="cloakbrowser_sync")
            retry_mock.assert_called_once_with({"global"})
            source_sync_mock.assert_called_once_with({"global"}, reason="cloakbrowser_sync")
            retry_three_mf_mock.assert_called_once_with({"global"})
            test_mock.assert_called_once()

    async def test_unchanged_browser_cookie_without_auth_token_is_not_marked_synced(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(
                platform="cn",
                cookie="cf_clearance=browser-clearance",
                browser_profile_id="profile-cn",
                browser_status="waiting",
            )
            config.cookies = [target]
            store.save(config)
            result = CloakBrowserSessionResult(
                profile_id="profile-cn",
                cookie="cf_clearance=browser-clearance",
            )

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "append_business_log"), \
                    patch.object(config_api, "publish_state_event"):
                saved, applied = config_api._store_browser_session_result(
                    "cn",
                    target,
                    result,
                    config.proxy,
                )

            current = saved.cookies[0]
            self.assertFalse(applied)
            self.assertEqual(current.browser_status, "action_required")
            self.assertIn("尚未登录", current.browser_message)


if __name__ == "__main__":
    unittest.main()
