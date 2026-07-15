import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from app.api import config as config_api
from app.core.store import JsonStore
from app.schemas.models import CookiePair, OnlineAccountLoginRequest, OnlineAccountSmsCodeRequest
from app.services import online_accounts


class ConfigCookieApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_config_rewrites_legacy_probe_count_message(self):
        config = SimpleNamespace(
            cookies=[
                CookiePair(
                    platform="cn",
                    cookie="token=ok",
                    message="国内 Cookie 部分成功，1/2 个接口可访问。",
                )
            ],
            proxy=SimpleNamespace(model_dump=lambda: {}),
            notifications=SimpleNamespace(model_dump=lambda: {}),
            sharing=SimpleNamespace(model_dump=lambda: {}),
            mobile_import=SimpleNamespace(enabled=False, token_prefix="", created_at="", last_used_at=""),
            user=SimpleNamespace(
                username="admin",
                display_name="Admin",
                password_hash=config_api.default_admin_password_hash(),
                password_hint="",
                theme_preference="auto",
                password_updated_at="",
            ),
            api_tokens=[],
            subscriptions=[],
            subscription_settings=SimpleNamespace(model_dump=lambda: {}),
            missing_3mf=[],
            organizer=SimpleNamespace(model_dump=lambda: {}),
            remote_refresh=SimpleNamespace(model_dump=lambda: {}),
            three_mf_limits=SimpleNamespace(model_dump=lambda: {}),
            advanced=SimpleNamespace(model_dump=lambda: {}),
            runtime=SimpleNamespace(model_dump=lambda: {}),
            paths=SimpleNamespace(model_dump=lambda: {}),
        )

        with patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}):
            payload = config_api._public_config_payload(config)

        message = payload["cookies"][0]["message"]
        self.assertIn("部分账号信息暂时读取失败", message)
        self.assertNotIn("1/2", message)
        self.assertNotIn("接口可访问", message)

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
                    patch.object(config_api, "_mark_online_account_checking") as checking_mock, \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as schedule_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
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
            checking_mock.assert_called_once_with("cn", source="cookie_save")
            schedule_mock.assert_called_once_with("cn", store.load().cookies[0], store.load().proxy)
            self.assertEqual(payload["subscription_retry"], retry_result)
            self.assertEqual(payload["cookie_source_sync"], queued_result)
            self.assertEqual(store.load().cookies[0].cookie, "token=ok")

    async def test_save_cookies_retries_verification_missing_3mf_for_updated_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = []
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            verification_retry = {
                "accepted": True,
                "accepted_count": 2,
                "queued_count": 0,
                "failed_count": 0,
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value={"queued_count": 0, "subscription_ids": []}), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value={"queued_count": 0, "platforms": []}), \
                    patch.object(config_api.crawler.manager, "retry_verification_missing_3mf", return_value=verification_retry) as retry_verification_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.save_cookies(
                    [
                        CookiePair(platform="cn", cookie="token=cn"),
                        CookiePair(platform="global", cookie="token=global"),
                    ],
                    request,
                )

            retry_verification_mock.assert_any_call(platform="cn")
            retry_verification_mock.assert_any_call(platform="global")
            self.assertEqual(retry_verification_mock.call_count, 2)
            self.assertEqual(payload["missing_3mf_verification_retry"]["cn"], verification_retry)
            self.assertEqual(payload["missing_3mf_verification_retry"]["global"], verification_retry)

    async def test_save_cookies_empty_cookie_does_not_delete_account_imported_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=old", account_id="2024907479")]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value={"queued_count": 0, "subscription_ids": []}), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value={"queued_count": 0, "platforms": []}), \
                    patch.object(config_api.subscription_manager, "remove_account_imported_subscriptions") as cleanup_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                await config_api.save_cookies(
                    [
                        CookiePair(platform="cn", cookie=""),
                        CookiePair(platform="global", cookie=""),
                    ],
                    request,
                )

            cleanup_mock.assert_not_called()
            saved_cookie = next(item for item in store.load().cookies if item.platform == "cn")
            self.assertEqual(saved_cookie.cookie, "")
            self.assertEqual(saved_cookie.account_id, "")

    async def test_online_account_login_saves_account_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="old=1", username="old")]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            retry_result = {"queued_count": 0, "subscription_ids": []}
            queued_result = {"queued_count": 1, "platforms": ["cn"]}
            login_result = {
                "platform": "cn",
                "username": "ace@example.com",
                "cookie": "token=new",
                "display_name": "艾斯",
                "account_id": "2024907479",
                "handle": "s450586793",
                "avatar_url": "https://example.com/avatar.jpg",
                "status": "ok",
                "message": "国区账号已登录，Cookie 已保存。",
                "auth_payload": {"ok": True},
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_login", return_value=login_result) as login_mock, \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value=retry_result) as retry_mock, \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value=queued_result) as queue_mock, \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as schedule_mock, \
                    patch.object(config_api, "_mark_online_account_checking") as checking_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.login_config_online_account(
                    OnlineAccountLoginRequest(platform="cn", username="13800138000", verification_code="123456"),
                    request,
                )

            login_payload = login_mock.call_args.args[0]
            self.assertEqual(login_payload.username, "13800138000")
            self.assertEqual(login_payload.verification_code, "123456")
            retry_mock.assert_called_once_with({"cn"})
            queue_mock.assert_called_once_with({"cn"}, reason="online_account_login")
            schedule_mock.assert_called_once()
            checking_mock.assert_called_once_with("cn", source="online_account_login")
            saved_cookie = store.load().cookies[0]
            self.assertEqual(saved_cookie.cookie, "token=new")
            self.assertEqual(saved_cookie.display_name, "艾斯")
            self.assertEqual(saved_cookie.handle, "s450586793")
            self.assertEqual(payload["cookie_source_sync"], queued_result)
            self.assertEqual(payload["test_result"]["state"], "checking")

    async def test_online_account_login_preserves_existing_profile_when_login_has_no_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="old=1",
                    username="13800138000",
                    display_name="艾斯",
                    account_id="2024907479",
                    handle="s450586793",
                    avatar_url="https://example.com/avatar.jpg",
                    status="ok",
                    message="旧状态",
                )
            ]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            login_result = {
                "platform": "cn",
                "username": "13800138000",
                "cookie": "token=new",
                "display_name": "13800138000",
                "account_id": "",
                "handle": "",
                "avatar_url": "",
                "status": "ok",
                "message": "国区账号已登录，Cookie 已保存。",
                "auth_payload": {"ok": True},
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_login", return_value=login_result), \
                    patch.object(config_api.subscription_manager, "retry_error_subscriptions_for_platforms", return_value={"queued_count": 0, "subscription_ids": []}), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value={"queued_count": 1, "platforms": ["cn"]}), \
                    patch.object(config_api, "_schedule_online_account_cookie_test"), \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                await config_api.login_config_online_account(
                    OnlineAccountLoginRequest(platform="cn", username="13800138000", verification_code="123456"),
                    request,
                )

            saved_cookie = store.load().cookies[0]
            self.assertEqual(saved_cookie.cookie, "token=new")
            self.assertEqual(saved_cookie.display_name, "艾斯")
            self.assertEqual(saved_cookie.account_id, "2024907479")
            self.assertEqual(saved_cookie.handle, "s450586793")
            self.assertEqual(saved_cookie.avatar_url, "https://example.com/avatar.jpg")

    async def test_online_account_delete_removes_account_imported_subscriptions_only_on_explicit_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=old", account_id="2024907479")]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            cleanup_result = {
                "success": True,
                "removed_subscription_count": 2,
                "removed_subscription_ids": ["sub-1", "sub-2"],
                "local_deleted_count": 5,
                "local_deleted_model_dirs": ["remote/model-1"],
                "message": "已移除 2 个账号关注来源订阅，并标记 5 个模型为本地删除。",
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.subscription_manager, "remove_account_imported_subscriptions", return_value=cleanup_result) as cleanup_mock, \
                    patch.object(config_api, "_get_github_version_status", return_value={}), \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {"cn": {"last_status": "deleted"}}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={"cn": {"last_status": "deleted"}}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.delete_config_online_account("cn", request)

            cleanup_mock.assert_called_once_with("cn")
            saved_cookie = next(item for item in store.load().cookies if item.platform == "cn")
            self.assertEqual(saved_cookie.cookie, "")
            self.assertEqual(saved_cookie.account_id, "")
            self.assertEqual(payload["online_account_cleanup"], cleanup_result)
            self.assertEqual(payload["cookie_source_inventory"]["platforms"]["cn"]["last_status"], "deleted")

    async def test_online_account_manual_sync_queues_worker_and_returns_source_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=ok", username="13800138000")]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            queued_result = {"queued_count": 1, "platforms": ["cn"]}

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.subscription_manager, "request_cookie_source_sync", return_value=queued_result) as queue_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {"cn": {"followed_authors": []}}, "updated_at": ""}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={"cn": {"last_status": "pending"}}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.sync_config_online_account("cn", request)

            queue_mock.assert_called_once_with({"cn"}, reason="manual")
            self.assertEqual(payload["cookie_source_sync"], queued_result)
            self.assertEqual(payload["cookie_source_inventory"]["platforms"]["cn"]["followed_authors"], [])
            self.assertEqual(payload["cookie_source_sync_state"]["cn"]["last_status"], "pending")

    async def test_online_account_sms_code_uses_proxy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.proxy.enabled = True
            config.proxy.http_proxy = "http://proxy.example"
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            result = {"ok": True, "platform": "cn", "phone": "13800138000", "message": "验证码已发送，请查看手机短信。"}

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_sms_code", return_value=result) as sms_mock, \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.send_config_online_account_sms_code(
                    OnlineAccountSmsCodeRequest(platform="cn", phone="13800138000"),
                    request,
                )

            sms_payload, proxy_config = sms_mock.call_args.args
            self.assertEqual(sms_payload.phone, "13800138000")
            self.assertTrue(proxy_config.enabled)
            self.assertEqual(payload["message"], "验证码已发送，请查看手机短信。")

    async def test_online_account_email_code_uses_email_for_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            result = {"ok": True, "platform": "global", "email": "ace@example.com", "message": "验证码已发送，请查看邮箱。"}

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_sms_code", return_value=result) as code_mock, \
                    patch.object(config_api, "append_business_log"):
                payload = await config_api.send_config_online_account_sms_code(
                    OnlineAccountSmsCodeRequest(platform="global", email="ace@example.com"),
                    request,
                )

            code_payload, _proxy_config = code_mock.call_args.args
            self.assertEqual(code_payload.email, "ace@example.com")
            self.assertEqual(code_payload.phone, "")
            self.assertEqual(payload["message"], "验证码已发送，请查看邮箱。")

    async def test_online_account_login_worker_skips_inline_cookie_probe(self):
        payload = OnlineAccountLoginRequest(
            platform="cn",
            username="13800138000",
            verification_code="123456",
        )
        result = online_accounts.OnlineAccountLoginResult(
            platform="cn",
            username="13800138000",
            cookie="token=ok",
        )

        with patch.object(config_api, "login_online_account", return_value=result) as login_mock:
            config_api._run_online_account_login(payload, SimpleNamespace())

        self.assertFalse(login_mock.call_args.kwargs["verify_cookie"])

    async def test_online_account_test_returns_checking_and_runs_probe_in_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=ok",
                    username="13800138000",
                    display_name="艾斯",
                    account_id="2024907479",
                    handle="s450586793",
                    avatar_url="https://example.com/avatar.jpg",
                    status="ok",
                    message="旧状态",
                )
            ]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_cookie_test", side_effect=AssertionError("manual account test should not block on probe")), \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as schedule_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}):
                payload = await config_api.test_config_online_account("cn", request)

            schedule_mock.assert_called_once()
            saved_cookie = store.load().cookies[0]
            self.assertEqual(saved_cookie.message, "旧状态")
            self.assertEqual(payload["test_result"]["state"], "checking")
            self.assertIn("后台检测", payload["test_result"]["message"])
            self.assertEqual(payload["cookies"][0]["message"], "旧状态")

    async def test_online_account_probe_result_preserves_existing_profile_when_probe_has_no_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=ok",
                    username="13800138000",
                    display_name="艾斯",
                    account_id="2024907479",
                    handle="s450586793",
                    avatar_url="https://example.com/avatar.jpg",
                    status="ok",
                    message="旧状态",
                )
            ]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
            test_result = {
                "ok": True,
                "message": "国内账号可用，Cookie 已保存。",
                "success_count": 1,
                "target_count": 2,
                "results": [],
            }
            metadata = {
                "platform": "cn",
                "username": "13800138000",
                "display_name": "13800138000",
                "account_id": "",
                "handle": "",
                "avatar_url": "",
                "status": "ok",
                "message": "",
                "last_tested_at": "2026-05-23T21:00:00+08:00",
                "updated_at": "2026-05-23T21:00:00+08:00",
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_cookie_test", return_value=test_result), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value=metadata), \
                    patch.object(config_api, "mark_account_ok") as mark_account_ok_mock, \
                    patch.object(config_api, "update_three_mf_gate") as update_gate_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                target = store.load().cookies[0]
                payload = config_api._run_and_store_online_account_cookie_test("cn", target, config.proxy)

            saved_cookie = store.load().cookies[0]
            self.assertEqual(saved_cookie.display_name, "艾斯")
            self.assertEqual(saved_cookie.account_id, "2024907479")
            self.assertEqual(saved_cookie.handle, "s450586793")
            self.assertEqual(saved_cookie.avatar_url, "https://example.com/avatar.jpg")
            self.assertEqual(saved_cookie.message, "国内账号可用，Cookie 已保存。")
            self.assertEqual(payload["test_result"], test_result)
            mark_account_ok_mock.assert_called_once_with(
                "cn",
                source="online_account_test",
                detail="国内账号可用，Cookie 已保存。",
            )
            update_gate_mock.assert_not_called()

    async def test_online_account_probe_failure_closes_three_mf_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="global", cookie="token=old", username="ace@example.com")]
            store.save(config)

            test_result = {
                "ok": False,
                "state": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "success_count": 0,
                "target_count": 2,
                "results": [],
            }
            metadata = {
                "platform": "global",
                "username": "ace@example.com",
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "last_tested_at": "2026-06-28T15:00:00+08:00",
                "updated_at": "2026-06-28T15:00:00+08:00",
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_cookie_test", return_value=test_result), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value=metadata), \
                    patch.object(config_api, "mark_account_ok") as mark_account_ok_mock, \
                    patch.object(config_api, "update_three_mf_gate") as update_gate_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                target = store.load().cookies[0]
                payload = config_api._run_and_store_online_account_cookie_test("global", target, config.proxy)

            self.assertEqual(payload["test_result"], test_result)
            mark_account_ok_mock.assert_not_called()
            update_gate_mock.assert_called_once_with(
                "global",
                gate="verification_required",
                reason="online_account_test",
                detail="MakerWorld 需要验证，前往官网任意下载一个模型。",
                source="online_account_test",
            )

    async def test_online_account_http_probe_failure_keeps_account_health_ok_with_source_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [
                CookiePair(
                    platform="global",
                    cookie="token=old",
                    username="ace@example.com",
                    display_name="艾斯",
                    account_id="2073587493",
                    handle="s450586793",
                    status="ok",
                )
            ]
            store.save(config)

            test_result = {
                "ok": False,
                "state": "http_error",
                "message": "国际账号测试失败，暂时无法确认 Cookie 是否可用。",
                "success_count": 0,
                "target_count": 2,
                "results": [],
            }
            metadata = {
                "platform": "global",
                "username": "ace@example.com",
                "display_name": "",
                "account_id": "",
                "handle": "",
                "avatar_url": "",
                "status": "http_error",
                "message": "国际账号测试失败，暂时无法确认 Cookie 是否可用。",
                "last_tested_at": "2026-07-04T08:56:00+08:00",
                "updated_at": "2026-07-04T08:56:00+08:00",
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_cookie_test", return_value=test_result), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value=metadata), \
                    patch.object(config_api, "mark_account_ok") as mark_account_ok_mock, \
                    patch.object(config_api, "update_three_mf_gate") as update_gate_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={
                        "global": {
                            "last_status": "success",
                            "account_uid": "2073587493",
                            "account_name": "艾斯",
                        }
                    }), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                target = store.load().cookies[0]
                payload = config_api._run_and_store_online_account_cookie_test("global", target, config.proxy)

            self.assertEqual(payload["test_result"], test_result)
            mark_account_ok_mock.assert_called_once_with(
                "global",
                source="online_account_test",
                detail="国际账号已保存，账号资料或来源同步可读取。",
            )
            update_gate_mock.assert_not_called()

    async def test_stale_online_account_probe_result_does_not_overwrite_new_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            stale_target = CookiePair(platform="cn", cookie="token=old", username="13800138000", message="旧状态")
            config.cookies = [CookiePair(platform="cn", cookie="token=new", username="13800138000", message="新状态")]
            store.save(config)

            test_result = {
                "ok": True,
                "message": "国内账号可用，Cookie 已保存。",
                "success_count": 1,
                "target_count": 2,
                "results": [],
            }

            with patch.object(config_api, "store", store), \
                    patch.object(config_api, "_run_online_account_cookie_test", return_value=test_result), \
                    patch.object(config_api, "online_account_metadata_from_cookie", return_value={"status": "ok"}), \
                    patch.object(config_api, "_schedule_online_account_cookie_test") as schedule_mock, \
                    patch.object(config_api, "mark_account_ok") as mark_account_ok_mock, \
                    patch.object(config_api, "update_three_mf_gate") as update_gate_mock, \
                    patch.object(config_api, "append_business_log"):
                payload = config_api._run_and_store_online_account_cookie_test("cn", stale_target, config.proxy)

            saved_cookie = store.load().cookies[0]
            self.assertEqual(saved_cookie.cookie, "token=new")
            self.assertEqual(saved_cookie.message, "新状态")
            self.assertTrue(payload["stale"])
            mark_account_ok_mock.assert_not_called()
            update_gate_mock.assert_not_called()
            schedule_mock.assert_called_once_with("cn", saved_cookie, config.proxy)

    async def test_online_account_test_deduplicates_running_background_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            config.cookies = [CookiePair(platform="cn", cookie="token=ok", username="13800138000")]
            store.save(config)

            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

            with patch.object(config_api, "store", store), \
                    patch.object(config_api.threading, "Thread") as thread_mock, \
                    patch.object(config_api, "cookie_source_inventory_payload", return_value={"platforms": {}}), \
                    patch.object(config_api, "cookie_source_sync_state_payload", return_value={}), \
                    patch.object(config_api, "compact_remote_refresh_state", return_value={}), \
                    patch.object(config_api.task_state_store, "load_remote_refresh_state", return_value={}), \
                    patch.object(config_api, "append_business_log"):
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()
                await config_api.test_config_online_account("cn", request)
                await config_api.test_config_online_account("cn", request)
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()

            self.assertEqual(thread_mock.call_count, 1)
            thread_mock.return_value.start.assert_called_once()

    async def test_online_account_test_rechecks_latest_cookie_after_running_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            old_target = CookiePair(platform="cn", cookie="token=old", username="13800138000")
            new_target = CookiePair(platform="cn", cookie="token=new", username="13800138000")
            deferred_threads = []

            class DeferredThread:
                def __init__(self, *, target, **_kwargs):
                    self.target = target

                def start(self):
                    return None

            def create_thread(*_args, **kwargs):
                thread = DeferredThread(**kwargs)
                deferred_threads.append(thread)
                return thread

            with patch.object(config_api.threading, "Thread", side_effect=create_thread), \
                    patch.object(config_api, "_run_and_store_online_account_cookie_test", return_value={}) as run_test, \
                    patch.object(config_api, "publish_state_event"):
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()
                config_api._schedule_online_account_cookie_test("cn", old_target, config.proxy)
                config_api._schedule_online_account_cookie_test("cn", new_target, config.proxy)

                self.assertEqual(len(deferred_threads), 1)
                deferred_threads[0].target()
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()

            self.assertEqual(
                [call.args[1].cookie for call in run_test.call_args_list],
                ["token=old", "token=new"],
            )

    async def test_online_account_test_does_not_recheck_unchanged_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            config = store.load()
            target = CookiePair(platform="cn", cookie="token=same", username="13800138000")
            deferred_threads = []

            class DeferredThread:
                def __init__(self, *, target, **_kwargs):
                    self.target = target

                def start(self):
                    return None

            def create_thread(*_args, **kwargs):
                thread = DeferredThread(**kwargs)
                deferred_threads.append(thread)
                return thread

            with patch.object(config_api.threading, "Thread", side_effect=create_thread), \
                    patch.object(config_api, "_run_and_store_online_account_cookie_test", return_value={}) as run_test, \
                    patch.object(config_api, "publish_state_event"):
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()
                config_api._schedule_online_account_cookie_test("cn", target, config.proxy)
                config_api._schedule_online_account_cookie_test("cn", target, config.proxy)

                self.assertEqual(len(deferred_threads), 1)
                deferred_threads[0].target()
                config_api.ONLINE_ACCOUNT_TEST_RUNNING.clear()
                config_api.ONLINE_ACCOUNT_TEST_PENDING.clear()
                config_api.ONLINE_ACCOUNT_TEST_ACTIVE_COOKIE.clear()

            self.assertEqual(run_test.call_count, 1)

class OnlineAccountServiceTest(unittest.TestCase):
    def _response(self, status_code=200, payload=None, text=None, headers=None):
        response = Mock()
        response.status_code = status_code
        response.headers = headers or {"Content-Type": "application/json"}
        response.text = text if text is not None else "{}"
        response.json.return_value = payload if payload is not None else {}
        return response

    def test_sms_code_posts_code_login_request(self):
        session = Mock()
        session.post.return_value = self._response(payload={"code": 0})
        session.close = Mock()
        proxy_config = SimpleNamespace(
            enabled=True,
            http_proxy="http://proxy.local:7890",
            https_proxy="http://proxy.local:7891",
        )

        with patch.object(online_accounts, "_session_from_platform", return_value=session):
            payload = online_accounts.send_online_account_sms_code(
                platform="cn",
                phone="13800138000",
                proxy_config=proxy_config,
            )

        self.assertTrue(payload["ok"])
        first_call = session.post.call_args
        self.assertIn("/user/sendsmscode", first_call.args[0])
        self.assertEqual(first_call.kwargs["json"], {"phone": "13800138000", "type": "codeLogin"})
        self.assertEqual(
            first_call.kwargs["proxies"],
            {"http": "http://proxy.local:7890", "https": "http://proxy.local:7891"},
        )

    def test_global_code_posts_email_code_login_request(self):
        session = Mock()
        session.post.return_value = self._response(payload={"code": 0})
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session):
            payload = online_accounts.send_online_account_sms_code(
                platform="global",
                phone="",
                email="ace@example.com",
            )

        self.assertTrue(payload["ok"])
        first_call = session.post.call_args
        self.assertIn("/user/sendemail/code", first_call.args[0])
        self.assertEqual(first_call.kwargs["json"], {"email": "ace@example.com", "type": "codeLogin"})
        self.assertEqual(payload["email"], "ace@example.com")

    def test_global_login_requires_email_address(self):
        with self.assertRaises(online_accounts.OnlineAccountLoginError) as cm:
            online_accounts.login_online_account(
                platform="global",
                username="not-an-email",
                password="",
                verification_code="123456",
            )

        self.assertIn("邮箱", str(cm.exception))

    def test_sms_code_html_login_page_message_does_not_suggest_verification(self):
        session = Mock()
        session.post.return_value = self._response(
            text="<html><body>Sign in</body></html>",
            headers={"Content-Type": "text/html"},
        )
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session):
            with self.assertRaises(online_accounts.OnlineAccountSmsCodeError) as cm:
                online_accounts.send_online_account_sms_code(
                    platform="cn",
                    phone="13800138000",
                )

        message = str(cm.exception)
        self.assertIn("接口未返回可用 JSON", message)
        self.assertIn("网页登录页或非 JSON 页面", message)
        self.assertNotIn("先在官网完成网页验证", message)

    def test_code_login_html_login_page_message_does_not_suggest_verification(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            text="<html><body>Sign in</body></html>",
            headers={"Content-Type": "text/html"},
        )
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session):
            with self.assertRaises(online_accounts.OnlineAccountLoginError) as cm:
                online_accounts.login_online_account(
                    platform="cn",
                    username="13800138000",
                    password="",
                    verification_code="123456",
                )

        message = str(cm.exception)
        self.assertIn("自动接口未返回可保存的 JSON/Cookie", message)
        self.assertIn("网页登录页或非 JSON 页面", message)
        self.assertNotIn("先在官网完成验证", message)

    def test_code_login_posts_official_consent_body_string(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            status_code=400,
            payload={"code": 1, "error": "Code does not exist or has expired; please request a new verification code."},
        )
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session):
            with self.assertRaises(online_accounts.OnlineAccountLoginError):
                online_accounts.login_online_account(
                    platform="cn",
                    username="13800138000",
                    password="",
                    verification_code="000000",
                )

        payload = session.post.call_args.kwargs["json"]
        self.assertIsInstance(payload["consentBody"], str)
        consent = json.loads(payload["consentBody"])
        self.assertEqual(consent["scene"], "register_or_login")
        self.assertEqual(
            consent["formList"],
            [
                {"formId": "TOU-CN", "op": "Opt-in", "key": "tou"},
                {"formId": "PrivacyPolicy-CN", "op": "Opt-in", "key": "privacy"},
            ],
        )
        self.assertEqual(session.post.call_count, 1)

    def test_code_login_reuses_recent_sms_code_device_cookies(self):
        sms_session = requests.Session()

        def sms_post(_url, **_kwargs):
            sms_session.cookies.set("bbl_device_id", "device-id", domain=".bambulab.cn", path="/")
            return self._response(payload={"code": 0})

        sms_post = Mock(side_effect=sms_post)
        sms_session.post = sms_post
        sms_session.close = Mock()

        login_session = requests.Session()
        login_session.cookies.clear()

        def login_post(_url, **_kwargs):
            self.assertEqual(login_session.cookies.get("bbl_device_id", domain=".bambulab.cn"), "device-id")
            return self._response(
                payload={
                    "code": 0,
                    "body": {
                        "token": "access-token",
                        "refreshToken": "refresh-token",
                        "uid": "2024907479",
                    },
                },
            )

        login_session.post = Mock(side_effect=login_post)
        login_session.get = Mock(return_value=self._response(payload={"ticket": "ticket-ok"}))
        login_session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", side_effect=[sms_session, login_session]), \
                patch.object(online_accounts, "probe_cookie_auth_status", return_value={"ok": True}), \
                patch.object(online_accounts, "discover_cookie_account_profile", return_value={}):
            online_accounts.send_online_account_sms_code(platform="cn", phone="13800138000")
            result = online_accounts.login_online_account(
                platform="cn",
                username="13800138000",
                password="",
                verification_code="123456",
            )

        self.assertEqual(result.username, "13800138000")
        self.assertEqual(login_session.post.call_count, 1)

    def test_code_login_keeps_cookie_when_auth_probe_fails_after_token_response(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            payload={
                "code": 0,
                "body": {
                    "token": "access-token",
                    "refreshToken": "refresh-token",
                    "uid": "2024907479",
                    "nickname": "艾斯",
                },
            },
        )
        session.get.return_value = self._response(payload={"ticket": "ticket-ok"})
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session), \
                patch.object(
                    online_accounts,
                    "probe_cookie_auth_status",
                    return_value={"ok": False, "state": "http_error", "message": "国内账号测试失败"},
                ), \
                patch.object(online_accounts, "discover_cookie_account_profile", return_value={}):
            result = online_accounts.login_online_account(
                platform="cn",
                username="13800138000",
                password="",
                verification_code="123456",
            )

        self.assertIn("token=access-token", result.cookie)
        self.assertEqual(result.status, "http_error")
        self.assertIn("暂时无法确认", result.message)

    def test_code_login_without_cookie_verification_skips_profile_network_calls(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            payload={
                "code": 0,
                "body": {
                    "token": "access-token",
                    "refreshToken": "refresh-token",
                    "uid": "2024907479",
                    "nickname": "艾斯",
                },
            },
        )
        session.get = Mock(return_value=self._response(payload={"ticket": "ticket-ok"}))
        session.close = Mock()
        auth_probe_mock = Mock(return_value={"ok": True})
        profile_mock = Mock(return_value={"handle": "s450586793"})

        with patch.object(online_accounts, "_session_from_platform", return_value=session), \
                patch.object(online_accounts, "probe_cookie_auth_status", auth_probe_mock), \
                patch.object(online_accounts, "discover_cookie_account_profile", profile_mock):
            result = online_accounts.login_online_account(
                platform="cn",
                username="13800138000",
                password="",
                verification_code="123456",
                verify_cookie=False,
            )

        self.assertIn("token=access-token", result.cookie)
        self.assertEqual(result.status, "checking")
        self.assertEqual(result.account_id, "2024907479")
        self.assertEqual(result.display_name, "艾斯")
        session.get.assert_not_called()
        auth_probe_mock.assert_not_called()
        profile_mock.assert_not_called()

    def test_global_code_login_uses_email_account(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            payload={
                "code": 0,
                "body": {
                    "token": "access-token",
                    "refreshToken": "refresh-token",
                    "uid": "2024907479",
                    "nickname": "Ace",
                },
            },
        )
        session.get.return_value = self._response(payload={"ticket": "ticket-ok"})
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session), \
                patch.object(online_accounts, "probe_cookie_auth_status", return_value={"ok": True}), \
                patch.object(online_accounts, "discover_cookie_account_profile", return_value={"handle": "ace"}):
            result = online_accounts.login_online_account(
                platform="global",
                username="ace@example.com",
                password="",
                verification_code="123456",
            )

        payload = session.post.call_args.kwargs["json"]
        self.assertEqual(payload["account"], "ace@example.com")
        self.assertEqual(payload["code"], "123456")
        self.assertIn("/user/signuporlogin", session.post.call_args.args[0])
        self.assertEqual(result.username, "ace@example.com")
        self.assertIn("国际邮箱登录成功", result.message)

    def test_code_login_extracts_token_cookie(self):
        session = Mock()
        session.cookies = []
        session.post.return_value = self._response(
            payload={
                "code": 0,
                "body": {
                    "token": "access-token",
                    "refreshToken": "refresh-token",
                    "uid": "2024907479",
                    "nickname": "艾斯",
                },
            },
        )
        session.get.return_value = self._response(payload={"ticket": "ticket-ok"})
        session.close = Mock()

        with patch.object(online_accounts, "_session_from_platform", return_value=session), \
                patch.object(online_accounts, "probe_cookie_auth_status", return_value={"ok": True}), \
                patch.object(online_accounts, "discover_cookie_account_profile", return_value={"handle": "s450586793"}):
            result = online_accounts.login_online_account(
                platform="cn",
                username="13800138000",
                password="",
                verification_code="123456",
            )

        self.assertIn("token=access-token", result.cookie)
        self.assertIn("refreshToken=refresh-token", result.cookie)
        self.assertEqual(result.handle, "s450586793")
        self.assertIn("/user/signuporlogin", session.post.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
