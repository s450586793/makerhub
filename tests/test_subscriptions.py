import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.core.store import JsonStore
from app.schemas.models import SubscriptionRecord
from app.services import subscriptions
from app.services.subscriptions import SubscriptionManager
import app.services.task_state as task_state_module
from app.services.task_state import TaskStateStore


class ArchiveManagerStub:
    def _queued_task_keys(self):
        return set()

    def _archived_task_keys(self):
        return set()

    def submit_discovered_batch(self, **_kwargs):
        return {"accepted": True, "queued_count": 0}


class SubscriptionManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_subscriptions_state_path = task_state_module.SUBSCRIPTIONS_STATE_PATH
        self.original_append_subscription_log = subscriptions._append_subscription_log

        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.temp_path / "subscriptions_state.json"
        subscriptions._append_subscription_log = lambda *_args, **_kwargs: None

        self.store = JsonStore(self.temp_path / "config.json")
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-1",
                name="艾斯收藏夹",
                url="https://makerworld.com.cn/zh/@ace/collections/models",
                mode="collection_models",
                cron="0 * * * *",
                enabled=True,
            )
        ]
        self.store.save(config)
        self.task_store = TaskStateStore()
        self.manager = SubscriptionManager(
            ArchiveManagerStub(),
            store=self.store,
            task_store=self.task_store,
            background_enabled=False,
        )
        self.manager._refresh_subscription_source_metadata = lambda *_args, **_kwargs: None

    def tearDown(self):
        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.original_subscriptions_state_path
        subscriptions._append_subscription_log = self.original_append_subscription_log
        self.temp_dir.cleanup()

    def _subscription_state(self):
        state_items = self.task_store.load_subscriptions_state().get("items") or []
        return next(item for item in state_items if item["id"] == "sub-1")

    def test_recovers_orphan_running_state(self):
        last_run_at = datetime.now() - subscriptions.SUBSCRIPTION_RUNNING_STALE_AFTER - timedelta(minutes=1)
        self.task_store.patch_subscription_state(
            "sub-1",
            status="running",
            running=True,
            last_run_at=last_run_at.isoformat(),
            next_run_at=(datetime.now() - timedelta(hours=1)).isoformat(),
            last_message="正在扫描订阅源。",
        )

        self.manager._ensure_state_records()

        state = self._subscription_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["last_message"], "上次订阅同步中断，已恢复并重新加入调度。")
        self.assertTrue(state["manual_requested_at"])
        self.assertTrue(state["next_run_at"])

    def test_keeps_fresh_active_running_state(self):
        self.task_store.patch_subscription_state(
            "sub-1",
            status="running",
            running=True,
            last_run_at=datetime.now().isoformat(),
            next_run_at="",
            last_message="正在扫描订阅源。",
        )
        self.manager._running_id = "sub-1"

        self.manager._ensure_state_records()

        state = self._subscription_state()
        self.assertTrue(state["running"])
        self.assertEqual(state["status"], "running")

    def test_recovers_stale_active_running_state(self):
        last_run_at = datetime.now() - subscriptions.SUBSCRIPTION_RUNNING_STALE_AFTER - timedelta(minutes=1)
        self.task_store.patch_subscription_state(
            "sub-1",
            status="running",
            running=True,
            last_run_at=last_run_at.isoformat(),
            next_run_at="",
            last_message="正在扫描订阅源。",
        )
        self.manager._running_id = "sub-1"

        self.manager._ensure_state_records()

        state = self._subscription_state()
        self.assertFalse(state["running"])
        self.assertEqual(self.manager._running_id, "")

    def test_sync_uses_finish_time_for_next_run(self):
        self.task_store.patch_subscription_state(
            "sub-1",
            status="idle",
            running=False,
            tracked_items=[],
            current_items=[],
        )
        self.manager._discover_subscription_items = lambda _subscription: {"items": []}
        start_at = datetime(2026, 4, 21, 21, 59)
        finish_at = datetime(2026, 4, 21, 22, 1)
        now_values = iter([start_at, finish_at, finish_at])
        original_now = subscriptions._now
        subscriptions._now = lambda: next(now_values)

        try:
            self.manager._sync_subscription("sub-1")
        finally:
            subscriptions._now = original_now

        state = self._subscription_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["next_run_at"], "2026-04-21T23:00:00+08:00")

    def test_retry_error_subscriptions_for_platforms_only_queues_matching_errors(self):
        config = self.store.load()
        config.subscriptions.extend(
            [
                SubscriptionRecord(
                    id="sub-2",
                    name="国际作者",
                    url="https://makerworld.com/en/@global/upload",
                    mode="author_upload",
                    cron="0 * * * *",
                    enabled=True,
                ),
                SubscriptionRecord(
                    id="sub-3",
                    name="国内正常",
                    url="https://makerworld.com.cn/zh/@ok/upload",
                    mode="author_upload",
                    cron="0 * * * *",
                    enabled=True,
                ),
                SubscriptionRecord(
                    id="sub-4",
                    name="国内停用",
                    url="https://makerworld.com.cn/zh/@off/upload",
                    mode="author_upload",
                    cron="0 * * * *",
                    enabled=False,
                ),
            ]
        )
        self.store.save(config)
        for item_id, status in (
            ("sub-1", "error"),
            ("sub-2", "error"),
            ("sub-3", "success"),
            ("sub-4", "error"),
        ):
            self.task_store.patch_subscription_state(
                item_id,
                status=status,
                running=False,
                manual_requested_at="",
                next_run_at="",
                last_message="旧状态",
            )

        result = self.manager.retry_error_subscriptions_for_platforms({"cn"})
        states = {
            item["id"]: item
            for item in self.task_store.load_subscriptions_state().get("items") or []
        }

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["subscription_ids"], ["sub-1"])
        self.assertTrue(states["sub-1"]["manual_requested_at"])
        self.assertEqual(states["sub-1"]["next_run_at"], states["sub-1"]["manual_requested_at"])
        self.assertEqual(states["sub-1"]["last_message"], "Cookie 已更新，已自动安排失败订阅重试。")
        self.assertFalse(states["sub-2"]["manual_requested_at"])
        self.assertFalse(states["sub-3"]["manual_requested_at"])
        self.assertFalse(states["sub-4"]["manual_requested_at"])


if __name__ == "__main__":
    unittest.main()
