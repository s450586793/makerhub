from __future__ import annotations

import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.services.archive_worker as archive_worker_module
from app.core.store import JsonStore
from app.schemas.models import CookiePair
from app.services.archive_worker import ArchiveTaskManager
from app.services.cloakbrowser_session import (
    CloakBrowserBridgeError,
    CloakBrowserSessionResult,
    CloakBrowserUnavailable,
)


class ArchiveWorkerBrowserRecoveryTest(unittest.TestCase):
    def _manager_with_cookie(self, cookie: str) -> tuple[ArchiveTaskManager, JsonStore]:
        store = JsonStore(Path(self.temp_dir.name) / "config.json")
        config = store.load()
        config.cookies = [
            CookiePair(
                platform="cn",
                cookie=cookie,
                browser_profile_id="profile-cn",
            )
        ]
        store.save(config)
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = store
        return manager, store

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_same_browser_session_marks_gate_as_browser_confirmation_without_retry(self):
        manager, store = self._manager_with_cookie("token=same; refreshToken=refresh; cf_clearance=clear")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=same; refreshToken=refresh; cf_clearance=clear",
            current_url="https://makerworld.com.cn/zh",
        )

        self.assertTrue(hasattr(manager, "_recover_browser_session_for_three_mf_gate"))
        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result), \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock, \
                patch.object(manager, "retry_missing_3mf") as retry_mock:
            result = manager._recover_browser_session_for_three_mf_gate(
                "cn",
                primary={"model_url": "https://makerworld.com.cn/zh/models/123"},
            )

        saved = store.load().cookies[0]
        self.assertEqual(result["outcome"], "unchanged")
        self.assertEqual(saved.browser_status, "synced")
        self.assertIn("仍被拒绝", saved.browser_message)
        update_gate_mock.assert_called_once_with(
            "cn",
            gate="verification_required",
            reason="browser_session_unchanged",
            source="cloakbrowser_auto_sync",
            detail="指纹浏览器登录态已同步，但 MakerWorld 仍拒绝 3MF 下载；请在官网完成验证后再继续归档。",
        )
        retry_mock.assert_not_called()

    def test_changed_same_account_browser_session_resumes_matching_paused_retry_before_submitting_new_one(self):
        manager, store = self._manager_with_cookie("token=same; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=same; refreshToken=fresh; cf_clearance=clear",
            current_url="https://makerworld.com.cn/zh",
        )
        primary = {
            "model_url": "https://makerworld.com.cn/zh/models/123",
            "model_id": "123",
            "title": "model",
            "instance_id": "instance-1",
            "source": "cn",
        }

        self.assertTrue(hasattr(manager, "_recover_browser_session_for_three_mf_gate"))
        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result), \
                patch.object(archive_worker_module, "open_three_mf_gate") as open_gate_mock, \
                patch.object(manager, "_resume_browser_session_recovery_task", return_value=True) as resume_mock, \
                patch.object(manager, "retry_missing_3mf", return_value={"accepted": True}) as retry_mock:
            result = manager._recover_browser_session_for_three_mf_gate("cn", primary=primary)

        saved = store.load().cookies[0]
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(saved.cookie, browser_result.cookie)
        self.assertEqual(saved.browser_status, "synced")
        open_gate_mock.assert_not_called()
        resume_mock.assert_called_once_with(
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            source="cn",
            title="model",
            instance_id="instance-1",
        )
        retry_mock.assert_not_called()

    def test_changed_browser_session_submits_one_retry_when_no_paused_task_matches(self):
        manager, _store = self._manager_with_cookie("token=same; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=same; refreshToken=fresh; cf_clearance=clear",
            current_url="https://makerworld.com.cn/zh",
        )
        primary = {
            "model_url": "https://makerworld.com.cn/zh/models/123",
            "model_id": "123",
            "title": "model",
            "instance_id": "instance-1",
            "source": "cn",
        }

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result), \
                patch.object(manager, "_resume_browser_session_recovery_task", return_value=False), \
                patch.object(manager, "retry_missing_3mf", return_value={"accepted": True}) as retry_mock:
            manager._recover_browser_session_for_three_mf_gate("cn", primary=primary)

        retry_mock.assert_called_once_with(
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            source="cn",
            title="model",
            instance_id="instance-1",
            browser_session_recovery=True,
        )

    def test_task_session_refresh_adopts_the_linked_browser_cookie(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=browser; refreshToken=fresh; cf_clearance=verified",
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result):
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        saved = store.load().cookies[0]
        self.assertEqual(saved.cookie, browser_result.cookie)
        self.assertEqual(saved.browser_status, "synced")

    def test_task_session_refresh_reuses_recent_browser_session_without_collecting_again(self):
        manager, store = self._manager_with_cookie("token=synced; refreshToken=fresh")
        config = store.load()
        config.cookies = [
            config.cookies[0].model_copy(
                update={
                    "browser_status": "synced",
                    "browser_synced_at": archive_worker_module.china_now().isoformat(),
                }
            )
        ]
        store.save(config)

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session") as collect_mock:
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        collect_mock.assert_not_called()
        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.cookie, "token=synced; refreshToken=fresh")

    def test_concurrent_task_session_refresh_collects_profile_once(self):
        manager, _store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=fresh; refreshToken=fresh",
        )
        first_collect_started = threading.Event()
        release_collect = threading.Event()
        second_thread_started = threading.Event()
        results = []

        def collect_once(*_args):
            first_collect_started.set()
            release_collect.wait(timeout=2)
            return browser_result

        def refresh(*, second: bool = False):
            if second:
                second_thread_started.set()
            results.append(manager._refresh_browser_session_for_task("cn"))

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", side_effect=collect_once) as collect_mock:
            first = threading.Thread(target=refresh)
            second = threading.Thread(target=refresh, kwargs={"second": True})
            first.start()
            self.assertTrue(first_collect_started.wait(timeout=1))
            second.start()
            self.assertTrue(second_thread_started.wait(timeout=1))
            time.sleep(0.05)
            release_collect.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(collect_mock.call_count, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(error == "" for _account, error in results))
        self.assertTrue(all(account.cookie == browser_result.cookie for account, _error in results))

    def test_stale_browser_sync_result_reports_newer_cookie_without_overwriting_it(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        config = store.load()
        config.cookies = [
            config.cookies[0].model_copy(
                update={
                    "cookie": "token=new; refreshToken=new",
                    "browser_message": "较新的登录态",
                }
            )
        ]
        store.save(config)

        saved = manager._persist_browser_recovery_session(
            "cn",
            expected_cookie="token=old; refreshToken=old",
            cookie="token=stale; refreshToken=stale",
            profile_id="profile-cn",
            status="synced",
            message="过期的同步结果",
        )

        self.assertEqual(saved.outcome, "stale_cookie")
        self.assertIsNone(saved.account)
        current = store.load().cookies[0]
        self.assertEqual(current.cookie, "token=new; refreshToken=new")
        self.assertEqual(current.browser_message, "较新的登录态")

    def test_profile_change_is_distinguished_from_a_newer_cookie(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        config = store.load()
        config.cookies = [
            config.cookies[0].model_copy(
                update={
                    "browser_profile_id": "profile-new",
                    "cookie": "token=new; refreshToken=new",
                }
            )
        ]
        store.save(config)

        saved = manager._persist_browser_recovery_session(
            "cn",
            expected_cookie="token=old; refreshToken=old",
            cookie="token=stale; refreshToken=stale",
            profile_id="profile-cn",
            status="synced",
            message="过期的同步结果",
        )

        self.assertEqual(saved.outcome, "profile_changed")
        self.assertIsNone(saved.account)

    def test_task_session_refresh_uses_newer_cookie_when_browser_sync_races_with_config_update(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=browser; refreshToken=browser",
        )

        def replace_cookie_during_collection(*_args):
            config = store.load()
            config.cookies = [
                config.cookies[0].model_copy(
                    update={
                        "cookie": "token=new; refreshToken=new",
                        "browser_status": "synced",
                    }
                )
            ]
            store.save(config)
            return browser_result

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    side_effect=replace_cookie_during_collection,
                ):
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.cookie, "token=new; refreshToken=new")

    def test_task_session_refresh_uses_newer_profile_when_profile_changes_during_sync(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=browser; refreshToken=browser",
        )

        def replace_profile_during_collection(*_args):
            config = store.load()
            config.cookies = [
                config.cookies[0].model_copy(
                    update={
                        "browser_profile_id": "profile-new",
                        "cookie": "token=new; refreshToken=new",
                        "browser_status": "synced",
                    }
                )
            ]
            store.save(config)
            return browser_result

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    side_effect=replace_profile_during_collection,
                ):
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.browser_profile_id, "profile-new")
        self.assertEqual(refreshed.cookie, "token=new; refreshToken=new")

    def test_logged_out_browser_snapshot_does_not_override_a_newer_cookie(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="cf_clearance=verified",
        )

        def replace_cookie_during_collection(*_args):
            config = store.load()
            config.cookies = [
                config.cookies[0].model_copy(
                    update={
                        "cookie": "token=new; refreshToken=new",
                        "browser_status": "synced",
                    }
                )
            ]
            store.save(config)
            return browser_result

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    side_effect=replace_cookie_during_collection,
                ):
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.cookie, "token=new; refreshToken=new")
        self.assertEqual(store.load().cookies[0].browser_status, "synced")

    def test_task_session_refresh_rejects_a_logged_out_browser_without_using_old_cookie(self):
        manager, store = self._manager_with_cookie("token=old; refreshToken=old")
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="cf_clearance=verified",
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result), \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertIsNone(refreshed)
        self.assertIn("尚未登录", error)
        saved = store.load().cookies[0]
        self.assertEqual(saved.cookie, "token=old; refreshToken=old")
        self.assertEqual(saved.browser_status, "action_required")
        update_gate_mock.assert_called_once_with(
            "cn",
            gate="cookie_invalid",
            reason="cloakbrowser_login_missing",
            source="cloakbrowser_auto_sync",
            detail="关联的指纹浏览器尚未登录 MakerWorld，请完成浏览器登录后重试。",
        )

    def test_action_required_browser_platform_is_skipped_without_failing_queued_tasks(self):
        manager, store = self._manager_with_cookie("token=cn")
        config = store.load()
        config.cookies = [
            config.cookies[0].model_copy(
                update={"browser_status": "synced", "browser_synced_at": "2026-08-08T09:00:00+08:00"}
            ),
            CookiePair(
                platform="global",
                cookie="token=global",
                browser_profile_id="profile-global",
                browser_status="action_required",
                browser_message="指纹浏览器尚未登录 MakerWorld。",
            ),
        ]
        store.save(config)
        queue = {
            "active": [],
            "queued": [
                {
                    "id": "global-task",
                    "status": "queued",
                    "url": "https://makerworld.com/zh/models/3021853",
                    "meta": {"source": "global"},
                },
                {
                    "id": "cn-task",
                    "status": "queued",
                    "url": "https://makerworld.com.cn/zh/models/123",
                    "meta": {"source": "cn"},
                },
            ],
        }

        selected = manager._next_executable_task(queue)

        self.assertEqual(selected["id"], "cn-task")

    def test_browser_session_change_wakes_a_browser_blocked_queue(self):
        manager, store = self._manager_with_cookie("token=old")
        config = store.load()
        config.cookies = [config.cookies[0].model_copy(update={"browser_status": "action_required"})]
        store.save(config)
        queue = {
            "queued_count": 1,
            "queued": [
                {
                    "id": "task-1",
                    "status": "queued",
                    "updated_at": "2026-08-08T09:00:00+08:00",
                    "url": "https://makerworld.com.cn/zh/models/123",
                }
            ],
        }
        blocked_signature = manager._queue_wakeup_signature(queue)

        config = store.load()
        config.cookies = [config.cookies[0].model_copy(update={"browser_status": "synced"})]
        store.save(config)

        self.assertNotEqual(manager._queue_wakeup_signature(queue), blocked_signature)

    def test_three_mf_gate_change_wakes_a_gate_blocked_queue(self):
        manager, _store = self._manager_with_cookie("token=old")
        queue = {
            "queued_count": 1,
            "queued": [
                {
                    "id": "task-1",
                    "status": "paused",
                    "updated_at": "2026-08-18T10:00:00+08:00",
                    "url": "https://makerworld.com.cn/zh/models/123",
                    "meta": {"missing_3mf_retry": True, "source": "cn"},
                }
            ],
        }
        health = {
            "cn": {"three_mf_gate": "verification_required"},
            "global": {"three_mf_gate": "open"},
        }

        with patch.object(
            archive_worker_module,
            "load_account_health",
            side_effect=lambda: health,
        ):
            blocked_signature = manager._queue_wakeup_signature(queue)
            health["cn"] = {"three_mf_gate": "open"}

            self.assertNotEqual(manager._queue_wakeup_signature(queue), blocked_signature)

    def test_task_session_refresh_uses_last_browser_cookie_during_temporary_outage(self):
        manager, store = self._manager_with_cookie("token=synced; refreshToken=fresh")
        current = store.load().cookies[0]
        config = store.load()
        config.cookies = [current.model_copy(update={"browser_status": "synced"})]
        store.save(config)

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    side_effect=CloakBrowserUnavailable("指纹浏览器返回 HTTP 502"),
                ), \
                patch.object(archive_worker_module, "_log_archive") as log_mock:
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        saved = store.load().cookies[0]
        self.assertEqual(saved.cookie, "token=synced; refreshToken=fresh")
        self.assertEqual(saved.browser_status, "synced")
        log_mock.assert_called_once()

    def test_task_session_refresh_uses_last_browser_cookie_during_bridge_protocol_timeout(self):
        manager, store = self._manager_with_cookie("token=synced; refreshToken=fresh")
        current = store.load().cookies[0]
        config = store.load()
        config.cookies = [current.model_copy(update={"browser_status": "synced"})]
        store.save(config)

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    side_effect=CloakBrowserBridgeError("Network.enable timed out."),
                ), \
                patch.object(archive_worker_module, "_log_archive") as log_mock:
            refreshed, error = manager._refresh_browser_session_for_task("cn")

        self.assertEqual(error, "")
        self.assertIsNotNone(refreshed)
        saved = store.load().cookies[0]
        self.assertEqual(saved.cookie, "token=synced; refreshToken=fresh")
        self.assertEqual(saved.browser_status, "synced")
        log_mock.assert_called_once()

    def test_temporary_browser_outage_marks_network_error_without_closing_gate(self):
        with patch.object(archive_worker_module, "mark_account_network_error") as network_error_mock, \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            archive_worker_module._sync_account_health_for_archive_exception(
                task_meta={"source": "cn"},
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                detail="指纹浏览器服务暂时不可用：指纹浏览器返回 HTTP 502",
            )

        network_error_mock.assert_called_once_with(
            "cn",
            reason="archive_task_network_error",
            source="archive_task",
            detail="指纹浏览器服务暂时不可用：指纹浏览器返回 HTTP 502",
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            instance_id="",
        )
        update_gate_mock.assert_not_called()

    def test_browser_session_sync_race_does_not_close_three_mf_gate(self):
        with patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            archive_worker_module._sync_account_health_for_archive_exception(
                task_meta={"source": "cn"},
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                detail="指纹浏览器会话同步发生并发更新，请稍后重试。",
            )

        update_gate_mock.assert_not_called()

    def test_browser_recovery_task_passes_saved_profile_to_3mf_authorization(self):
        manager, _store = self._manager_with_cookie("token=same; refreshToken=fresh")
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    return_value=CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=same; refreshToken=fresh"),
                ), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": False, "state": "verification_required"}), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={"model_id": "123", "base_name": "Demo", "work_dir": "", "missing_3mf": []},
                ) as run_mock, \
                patch.object(archive_worker_module, "mark_account_ok"), \
                patch.object(manager, "_resume_paused_missing_3mf_retry_tasks_for_platform", return_value=3) as resume_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-browser",
                "https://makerworld.com.cn/zh/models/123",
                {"missing_3mf_retry": True, "browser_session_recovery": True, "source": "cn"},
            )

        self.assertTrue(run_mock.call_args.kwargs["browser_three_mf_authorization"])
        self.assertEqual(run_mock.call_args.kwargs["browser_profile_id"], "profile-cn")
        resume_mock.assert_called_once_with("cn")

    def test_browser_recovery_3mf_stage_success_resumes_next_paused_task(self):
        manager, _store = self._manager_with_cookie("token=same; refreshToken=fresh")
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    return_value=CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=same; refreshToken=fresh"),
                ), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True, "state": "open"}), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={"model_id": "123", "base_name": "Demo", "work_dir": "", "missing_3mf": []},
                ), \
                patch.object(archive_worker_module, "mark_account_ok"), \
                patch.object(manager, "_resume_paused_missing_3mf_retry_tasks_for_platform", return_value=1) as resume_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-browser-stage",
                "https://makerworld.com.cn/zh/models/123",
                {
                    "three_mf_download": True,
                    "browser_session_recovery": True,
                    "source": "cn",
                    "instance_ids": ["profile-1"],
                },
            )

        resume_mock.assert_called_once_with("cn")

    def test_3mf_retry_prefers_saved_browser_profile_before_direct_authorization(self):
        manager, _store = self._manager_with_cookie("token=same; refreshToken=fresh")
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(
                    archive_worker_module,
                    "collect_browser_session",
                    return_value=CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=same; refreshToken=fresh"),
                ), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True, "state": "open"}), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={"model_id": "123", "base_name": "Demo", "work_dir": "", "missing_3mf": []},
                ) as run_mock, \
                patch.object(archive_worker_module, "mark_account_ok"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-browser-preferred",
                "https://makerworld.com.cn/zh/models/123",
                {"missing_3mf_retry": True, "source": "cn"},
            )

        self.assertTrue(run_mock.call_args.kwargs["browser_three_mf_authorization"])
        self.assertEqual(run_mock.call_args.kwargs["browser_profile_id"], "profile-cn")

    def test_unchanged_linked_browser_session_keeps_a_closed_three_mf_gate_closed(self):
        manager, _store = self._manager_with_cookie("token=same; refreshToken=fresh")
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )
        browser_result = CloakBrowserSessionResult(
            profile_id="profile-cn",
            cookie="token=same; refreshToken=fresh",
        )

        with patch.object(archive_worker_module, "cloakbrowser_configured", return_value=True), \
                patch.object(archive_worker_module, "collect_browser_session", return_value=browser_result), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": False, "state": "verification_required", "message": "需要浏览器确认"},
                ), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={"model_id": "123", "base_name": "Demo", "work_dir": "", "missing_3mf": []},
                ) as run_mock, \
                patch.object(archive_worker_module, "mark_account_ok"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-browser-gated",
                "https://makerworld.com.cn/zh/models/123",
                {"three_mf_download": True, "source": "cn", "instance_ids": ["profile-1"]},
            )

        self.assertTrue(run_mock.call_args.kwargs["skip_three_mf_fetch"])

    def test_browser_recovery_task_bypasses_closed_gate_without_reopening_platform(self):
        manager = ArchiveTaskManager(background_enabled=False)
        item = {
            "url": "https://makerworld.com.cn/zh/models/123",
            "meta": {
                "missing_3mf_retry": True,
                "browser_session_recovery": True,
                "source": "cn",
            },
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={"open": False, "state": "verification_required"},
        ):
            blocked = manager._is_three_mf_only_task_blocked_by_gate(item)

        self.assertFalse(blocked)

    def test_unknown_gate_promotes_one_queued_three_mf_task_to_probe(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "active": [],
            "queued": [
                {
                    "id": "probe-1",
                    "status": "queued",
                    "url": "https://makerworld.com.cn/zh/models/123",
                    "meta": {"three_mf_download": True, "source": "cn"},
                }
            ],
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={"open": False, "state": "unknown", "platform": "cn"},
        ):
            selected = manager._next_executable_task(queue)

        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], "probe-1")
        self.assertTrue(selected["meta"]["browser_session_recovery"])

    def test_unknown_gate_does_not_start_second_probe_for_same_platform(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "active": [
                {
                    "id": "active-probe",
                    "status": "running",
                    "url": "https://makerworld.com.cn/zh/models/100",
                    "meta": {
                        "three_mf_download": True,
                        "browser_session_recovery": True,
                        "source": "cn",
                    },
                }
            ],
            "queued": [
                {
                    "id": "probe-2",
                    "status": "queued",
                    "url": "https://makerworld.com.cn/zh/models/123",
                    "meta": {"three_mf_download": True, "source": "cn"},
                }
            ],
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={"open": False, "state": "unknown", "platform": "cn"},
        ):
            selected = manager._next_executable_task(queue)

        self.assertIsNone(selected)
        self.assertNotIn("browser_session_recovery", queue["queued"][0]["meta"])

    def test_successful_browser_probe_opens_gate_for_three_mf_download_task(self):
        with patch.object(archive_worker_module, "mark_account_ok") as mark_ok_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=[],
                missing_3mf_retry=False,
                browser_session_recovery=True,
            )

        self.assertIsNone(failure)
        mark_ok_mock.assert_called_once_with(
            "cn",
            source="browser_session_recovery",
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            instance_id="instance-1",
        )

    def test_browser_recovery_auth_failure_requires_browser_confirmation_not_relogin(self):
        missing_items = [
            {
                "status": "auth_required",
                "message": "国区下载 3MF 需要有效登录态；请更新国内站 Cookie / token。",
                "instance_id": "instance-1",
            }
        ]

        with patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=missing_items,
                missing_3mf_retry=True,
                browser_session_recovery=True,
            )

        self.assertEqual(failure["status"], "verification_required")
        self.assertEqual(update_gate_mock.call_args.kwargs["gate"], "verification_required")

    def test_first_isolated_verification_failure_keeps_platform_gate_open(self):
        missing_items = [
            {
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "instance_id": "instance-1",
            }
        ]

        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "status": "ok",
                "three_mf_gate": "open",
                "three_mf_reason": "",
                "model_id": "",
                "instance_id": "",
            },
        ), patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=missing_items,
                missing_3mf_retry=True,
            )

        self.assertIsNone(failure)
        update_gate_mock.assert_called_once_with(
            "cn",
            gate="open",
            reason="isolated_three_mf_verification",
            source="archive_download",
            detail="MakerWorld 需要验证，前往官网任意下载一个模型。",
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            instance_id="instance-1",
        )

    def test_second_distinct_verification_failure_closes_platform_gate(self):
        missing_items = [
            {
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "instance_id": "instance-2",
            }
        ]

        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "status": "ok",
                "three_mf_gate": "open",
                "three_mf_reason": "isolated_three_mf_verification",
                "model_id": "123",
                "instance_id": "instance-1",
            },
        ), patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/456",
                model_id="456",
                instance_id="instance-2",
                missing_items=missing_items,
                missing_3mf_retry=True,
            )

        self.assertEqual(failure["status"], "verification_required")
        self.assertEqual(update_gate_mock.call_args.kwargs["gate"], "verification_required")

    def test_successful_authorization_opens_gate_even_when_another_instance_is_missing(self):
        missing_items = [
            {
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "instance_id": "instance-2",
            }
        ]

        with patch.object(archive_worker_module, "mark_account_ok") as mark_ok_mock, \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=missing_items,
                missing_3mf_retry=True,
                three_mf_authorization_succeeded=True,
            )

        self.assertIsNone(failure)
        mark_ok_mock.assert_called_once_with(
            "cn",
            source="three_mf_authorization",
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            instance_id="instance-1",
        )
        update_gate_mock.assert_not_called()

    def test_browser_bridge_http_error_does_not_change_platform_health(self):
        missing_items = [
            {
                "status": "http_error",
                "message": "指纹浏览器暂时无法完成 3MF 授权，请稍后自动重试。",
                "instance_id": "instance-1",
            }
        ]

        with patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock, \
                patch.object(archive_worker_module, "mark_account_network_error") as network_error_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=missing_items,
                missing_3mf_retry=True,
            )

        self.assertIsNone(failure)
        update_gate_mock.assert_not_called()
        network_error_mock.assert_not_called()

    def test_unchanged_browser_confirmation_gate_is_not_overwritten_by_parallel_auth_failure(self):
        missing_items = [
            {
                "status": "auth_required",
                "message": "国区下载 3MF 需要有效登录态；请更新国内站 Cookie / token。",
                "instance_id": "instance-1",
            }
        ]

        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "three_mf_gate": "verification_required",
                "three_mf_reason": "browser_session_unchanged",
            },
        ), patch.object(archive_worker_module, "update_three_mf_gate") as update_gate_mock:
            failure = archive_worker_module._sync_account_health_for_archive_result(
                platform="cn",
                model_url="https://makerworld.com.cn/zh/models/123",
                model_id="123",
                instance_id="instance-1",
                missing_items=missing_items,
                missing_3mf_retry=False,
            )

        self.assertEqual(failure["status"], "verification_required")
        self.assertEqual(failure["detail"], archive_worker_module.CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE)
        self.assertEqual(update_gate_mock.call_args.kwargs["gate"], "verification_required")

    def test_auth_required_archive_failure_schedules_browser_recovery_for_current_instance(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )
        failure = {
            "status": "auth_required",
            "detail": "国区下载 3MF 需要有效登录态；请更新国内站 Cookie / token。",
            "instance_id": "instance-1",
        }

        with patch.object(archive_worker_module, "_select_cookie", return_value="token=current"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "123",
                        "base_name": "CN Model",
                        "work_dir": "",
                        "missing_3mf": [
                            {
                                "id": "instance-1",
                                "title": "0.2mm",
                                "downloadState": "auth_required",
                                "downloadMessage": failure["detail"],
                            }
                        ],
                    },
                ), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result", return_value=failure), \
                patch.object(manager, "_pause_three_mf_retry_tasks_for_gate", return_value=0), \
                patch.object(manager, "_schedule_browser_session_recovery_for_three_mf_gate") as schedule_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task("task-1", "https://makerworld.com.cn/zh/models/123")

        schedule_mock.assert_called_once_with(
            "cn",
            primary={
                "model_url": "https://makerworld.com.cn/zh/models/123",
                "model_id": "123",
                "title": "0.2mm",
                "instance_id": "instance-1",
                "source": "cn",
            },
        )

    def test_browser_recovery_scheduler_uses_platform_cooldown(self):
        manager = ArchiveTaskManager(background_enabled=True)
        thread = Mock()

        with patch.object(archive_worker_module.threading, "Thread", return_value=thread) as thread_mock, \
                patch.object(archive_worker_module.time, "monotonic", return_value=1000.0):
            first = manager._schedule_browser_session_recovery_for_three_mf_gate("cn")
            second = manager._schedule_browser_session_recovery_for_three_mf_gate("cn")

        self.assertTrue(first)
        self.assertFalse(second)
        thread_mock.assert_called_once()
        thread.start.assert_called_once()

    def test_ensure_worker_for_pending_recovers_legacy_cookie_invalid_queue(self):
        manager = ArchiveTaskManager(background_enabled=True)
        paused_item = {
            "id": "paused-cn",
            "status": "paused",
            "blocked_reason": "needs_verification",
            "url": "https://makerworld.com.cn/zh/models/123",
            "meta": {
                "missing_3mf_retry": True,
                "source": "cn",
                "title": "CN model",
                "instance_id": "profile-1",
            },
        }

        def resume_paused(selector=None):
            item = dict(paused_item)
            return {
                "active": [],
                "queued": [item],
                "recent_failures": [],
                "running_count": 0,
                "queued_count": 1,
                "resumed_count": int(bool(selector and selector(item))),
            }

        resume_mock = Mock(side_effect=resume_paused)
        manager.task_store = SimpleNamespace(
            resume_verification_paused_archive_tasks=resume_mock,
        )
        queue = {
            "active": [],
            "queued": [paused_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
        }

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker"), \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": False, "state": "cookie_invalid", "platform": "cn"},
                ), \
                patch.object(manager, "_schedule_browser_session_recovery_for_three_mf_gate") as schedule_mock:
            manager.ensure_worker_for_pending()

        schedule_mock.assert_called_once_with(
            "cn",
            primary={
                "model_url": "https://makerworld.com.cn/zh/models/123",
                "model_id": "123",
                "title": "CN model",
                "instance_id": "profile-1",
                "source": "cn",
            },
        )
        resume_mock.assert_not_called()

    def test_ensure_worker_for_pending_resumes_legacy_browser_session_task_when_gate_open(self):
        manager = ArchiveTaskManager(background_enabled=True)
        paused_item = {
            "id": "legacy-browser-session",
            "status": "paused",
            "url": "https://makerworld.com.cn/zh/models/123",
            "message": "登录态已更新，正在检测 3MF 下载权限。",
            "meta": {"missing_3mf_retry": True, "source": "cn"},
        }
        resumed_item = {
            **paused_item,
            "status": "queued",
            "message": "浏览器登录态已恢复，正在探测 3MF 下载权限",
            "meta": {**paused_item["meta"], "browser_session_recovery": True},
        }
        queue = {
            "active": [],
            "queued": [paused_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
        }
        resumed_queue = {
            "active": [],
            "queued": [resumed_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
            "resumed_count": 1,
            "resumed_items": [resumed_item],
        }
        resume_mock = Mock(return_value=resumed_queue)
        manager.task_store = SimpleNamespace(resume_verification_paused_archive_tasks=resume_mock)

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker") as ensure_worker_mock, \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": True, "state": "open", "platform": "cn"},
                ), \
                patch.object(manager, "_schedule_browser_session_recovery_for_three_mf_gate") as schedule_mock, \
                patch.object(archive_worker_module, "append_business_log") as log_mock:
            manager.ensure_worker_for_pending()

        kwargs = resume_mock.call_args.kwargs
        self.assertEqual(kwargs["limit"], 1)
        self.assertEqual(kwargs["message"], "浏览器登录态已恢复，正在探测 3MF 下载权限")
        self.assertTrue(kwargs["meta_updates"]["browser_session_recovery"])
        self.assertTrue(kwargs["selector"](paused_item))
        ensure_worker_mock.assert_called_once()
        schedule_mock.assert_not_called()
        log_mock.assert_called_once()

    def test_ensure_worker_for_pending_resumes_one_browser_confirmation_probe_when_gate_open(self):
        manager = ArchiveTaskManager(background_enabled=True)
        queued_items = [
            {
                "id": f"browser-confirmation-{index}",
                "status": "paused",
                "blocked_reason": "needs_verification",
                "url": f"https://makerworld.com.cn/zh/models/{index}",
                "message": archive_worker_module.CLOAKBROWSER_BROWSER_CONFIRMATION_MESSAGE,
                "meta": {"missing_3mf_retry": True, "source": "cn"},
            }
            for index in (1, 2)
        ]
        queue = {
            "active": [],
            "queued": queued_items,
            "recent_failures": [],
            "running_count": 0,
            "queued_count": len(queued_items),
        }

        def resume_paused(*, selector=None, limit=None, message="", meta_updates=None):
            resumed_items = []
            next_items = []
            for original in queued_items:
                item = {**original, "meta": dict(original.get("meta") or {})}
                if (
                    item.get("status") == "paused"
                    and len(resumed_items) < int(limit or 0)
                    and selector is not None
                    and selector(item)
                ):
                    item["status"] = "queued"
                    item["message"] = message
                    item["meta"].update(meta_updates or {})
                    item.pop("blocked_reason", None)
                    resumed_items.append(item)
                next_items.append(item)
            return {
                **queue,
                "queued": next_items,
                "resumed_count": len(resumed_items),
                "resumed_items": resumed_items,
            }

        resume_mock = Mock(side_effect=resume_paused)
        manager.task_store = SimpleNamespace(resume_verification_paused_archive_tasks=resume_mock)

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker") as ensure_worker_mock, \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": True, "state": "open", "platform": "cn"},
                ), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.ensure_worker_for_pending()

        self.assertEqual(resume_mock.call_count, 1)
        self.assertEqual(resume_mock.call_args.kwargs["limit"], 1)
        self.assertEqual(
            [item["id"] for item in result["queued"] if item.get("status") == "queued"],
            ["browser-confirmation-1"],
        )
        self.assertTrue(result["queued"][0]["meta"]["browser_session_recovery"])
        self.assertEqual(result["queued"][1]["status"], "paused")
        ensure_worker_mock.assert_called_once()

    def test_ensure_worker_for_pending_resumes_one_expired_daily_limit_probe_per_open_platform(self):
        manager = ArchiveTaskManager(background_enabled=True)
        daily_limit_message = (
            "返回了每日下载上限，今日暂停自动重试，"
            "自动重试暂停至 2026-08-05 00:00。"
        )
        queued_items = [
            {
                "id": f"daily-limit-{platform}-{index}",
                "status": "paused",
                "url": (
                    f"https://makerworld.com.cn/zh/models/{index}"
                    if platform == "cn"
                    else f"https://makerworld.com/en/models/{index}"
                ),
                "message": daily_limit_message,
                "meta": {"missing_3mf_retry": True, "source": platform},
            }
            for platform in ("cn", "global")
            for index in (1, 2)
        ]
        queued_items.append(
            {
                "id": "verification-required-cn",
                "status": "paused",
                "blocked_reason": "needs_verification",
                "url": "https://makerworld.com.cn/zh/models/999",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "meta": {"missing_3mf_retry": True, "source": "cn"},
            }
        )
        queue = {
            "active": [],
            "queued": queued_items,
            "recent_failures": [],
            "running_count": 0,
            "queued_count": len(queued_items),
        }
        current_queue = queue

        def resume_paused(*, selector=None, limit=None, include_daily_limit=False, message="", meta_updates=None):
            nonlocal current_queue
            self.assertTrue(include_daily_limit)
            resumed_items = []
            next_items = []
            for original in current_queue["queued"]:
                item = {**original, "meta": dict(original.get("meta") or {})}
                is_daily_limit = (
                    item.get("status") == "paused"
                    and "每日下载上限" in str(item.get("message") or "")
                    and "自动重试暂停至" in str(item.get("message") or "")
                )
                within_limit = limit is None or len(resumed_items) < limit
                if is_daily_limit and within_limit and (selector is None or selector(item)):
                    item["status"] = "queued"
                    item["message"] = message
                    item["meta"].update(meta_updates or {})
                    resumed_items.append(item)
                next_items.append(item)
            current_queue = {
                **current_queue,
                "queued": next_items,
                "resumed_count": len(resumed_items),
                "resumed_items": resumed_items,
            }
            return current_queue

        resume_mock = Mock(side_effect=resume_paused)
        manager.task_store = SimpleNamespace(resume_verification_paused_archive_tasks=resume_mock)

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker") as ensure_worker_mock, \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    side_effect=lambda url, meta: {
                        "open": True,
                        "state": "open",
                        "platform": meta.get("source"),
                    },
                ), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.ensure_worker_for_pending()

        self.assertEqual(resume_mock.call_count, 2)
        self.assertTrue(all(call.kwargs["include_daily_limit"] for call in resume_mock.call_args_list))
        self.assertTrue(all(call.kwargs["limit"] == 1 for call in resume_mock.call_args_list))
        self.assertEqual(
            [item["id"] for item in result["queued"] if item.get("status") == "queued"],
            ["daily-limit-cn-1", "daily-limit-global-1"],
        )
        self.assertEqual(result["queued"][-1]["status"], "paused")
        ensure_worker_mock.assert_called_once()

    def test_ensure_worker_for_pending_keeps_daily_limit_tasks_paused_while_gate_is_closed(self):
        manager = ArchiveTaskManager(background_enabled=True)
        paused_item = {
            "id": "daily-limit-cn",
            "status": "paused",
            "url": "https://makerworld.com.cn/zh/models/123",
            "message": (
                "国区返回了每日下载上限，今日暂停自动重试，"
                "自动重试暂停至 2099-08-05 00:00。"
            ),
            "meta": {"missing_3mf_retry": True, "source": "cn"},
        }
        queue = {
            "active": [],
            "queued": [paused_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
        }
        resume_mock = Mock()
        manager.task_store = SimpleNamespace(resume_verification_paused_archive_tasks=resume_mock)

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker"), \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": False, "state": "daily_limit", "platform": "cn"},
                ):
            result = manager.ensure_worker_for_pending()

        self.assertEqual(result, queue)
        resume_mock.assert_not_called()

    def test_resume_pending_tasks_resumes_legacy_browser_session_task_when_gate_open(self):
        manager = ArchiveTaskManager(background_enabled=True)
        paused_item = {
            "id": "legacy-browser-session",
            "status": "paused",
            "url": "https://makerworld.com.cn/zh/models/123",
            "message": "登录态已更新，正在检测 3MF 下载权限。",
            "meta": {"missing_3mf_retry": True, "source": "cn"},
        }
        resumed_item = {
            **paused_item,
            "status": "queued",
            "message": "浏览器登录态已恢复，正在探测 3MF 下载权限",
            "meta": {**paused_item["meta"], "browser_session_recovery": True},
        }
        queue = {
            "active": [],
            "queued": [paused_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
        }
        resumed_queue = {
            **queue,
            "queued": [resumed_item],
            "resumed_count": 1,
            "resumed_items": [resumed_item],
        }
        resume_mock = Mock(return_value=resumed_queue)
        manager.task_store = SimpleNamespace(
            requeue_active_tasks=Mock(return_value=queue),
            resume_verification_paused_archive_tasks=resume_mock,
        )

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker") as ensure_worker_mock, \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": True, "state": "open", "platform": "cn"},
                ), \
                patch.object(manager, "_schedule_browser_session_recovery_for_three_mf_gate") as schedule_mock, \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.resume_pending_tasks()

        self.assertEqual(result, resumed_queue)
        self.assertTrue(resume_mock.call_args.kwargs["selector"](paused_item))
        ensure_worker_mock.assert_called_once()
        schedule_mock.assert_not_called()

    def test_ensure_worker_for_pending_does_not_recover_browser_after_confirmation_required(self):
        manager = ArchiveTaskManager(background_enabled=True)
        paused_item = {
            "id": "paused-cn",
            "status": "paused",
            "blocked_reason": "needs_verification",
            "url": "https://makerworld.com.cn/zh/models/123",
            "meta": {"missing_3mf_retry": True, "source": "cn"},
        }
        manager.task_store = SimpleNamespace(
            resume_verification_paused_archive_tasks=lambda selector=None: {
                "active": [],
                "queued": [paused_item],
                "recent_failures": [],
                "running_count": 0,
                "queued_count": 1,
                "resumed_count": 0,
            },
        )
        queue = {
            "active": [],
            "queued": [paused_item],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 1,
        }

        with patch.object(manager, "_repair_queue_before_worker_start", return_value=queue), \
                patch.object(manager, "_ensure_worker"), \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={"open": False, "state": "verification_required", "platform": "cn"},
                ), \
                patch.object(manager, "_schedule_browser_session_recovery_for_three_mf_gate") as schedule_mock:
            manager.ensure_worker_for_pending()

        schedule_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
