from __future__ import annotations

import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.services.archive_worker as archive_worker_module
from app.core.store import JsonStore
from app.schemas.models import CookiePair
from app.services.archive_worker import ArchiveTaskManager
from app.services.cloakbrowser_session import CloakBrowserSessionResult


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

    def test_changed_same_account_browser_session_updates_cookie_and_retries_primary_only(self):
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
                patch.object(manager, "retry_missing_3mf", return_value={"accepted": True}) as retry_mock:
            result = manager._recover_browser_session_for_three_mf_gate("cn", primary=primary)

        saved = store.load().cookies[0]
        self.assertEqual(result["outcome"], "updated")
        self.assertEqual(saved.cookie, browser_result.cookie)
        self.assertEqual(saved.browser_status, "synced")
        open_gate_mock.assert_not_called()
        retry_mock.assert_called_once_with(
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            source="cn",
            title="model",
            instance_id="instance-1",
            browser_session_recovery=True,
        )

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

        manager.task_store = SimpleNamespace(
            resume_verification_paused_archive_tasks=resume_paused,
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
