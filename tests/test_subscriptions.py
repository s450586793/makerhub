import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.core.store import JsonStore
from app.schemas.models import CookiePair, SubscriptionRecord
from app.services import subscriptions
from app.services.subscriptions import SubscriptionManager
import app.services.task_state as task_state_module
from app.services.task_state import TaskStateStore


class ArchiveManagerStub:
    def __init__(self):
        self.submitted_batches = []

    def _queued_task_keys(self):
        return set()

    def _archived_task_keys(self):
        return set()

    def submit_discovered_batch(self, **kwargs):
        self.submitted_batches.append(kwargs)
        return {"accepted": True, "queued_count": 0}


class SubscriptionManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_subscriptions_state_path = task_state_module.SUBSCRIPTIONS_STATE_PATH
        self.original_cookie_source_sync_state_path = subscriptions.COOKIE_SOURCE_SYNC_STATE_PATH
        self.original_append_subscription_log = subscriptions._append_subscription_log

        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.temp_path / "subscriptions_state.json"
        subscriptions.COOKIE_SOURCE_SYNC_STATE_PATH = self.temp_path / "cookie_source_sync_state.json"
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
        config.cookies = []
        self.store.save(config)
        self.task_store = TaskStateStore()
        self.archive_manager = ArchiveManagerStub()
        self.manager = SubscriptionManager(
            self.archive_manager,
            store=self.store,
            task_store=self.task_store,
            background_enabled=False,
        )
        self.manager._refresh_subscription_source_metadata = lambda *_args, **_kwargs: None

    def tearDown(self):
        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.original_subscriptions_state_path
        subscriptions.COOKIE_SOURCE_SYNC_STATE_PATH = self.original_cookie_source_sync_state_path
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

    def test_collection_sync_rejects_obvious_partial_scan_and_preserves_history(self):
        tracked_items = [
            {
                "task_key": f"model:{index}",
                "model_id": str(index),
                "url": f"https://makerworld.com.cn/zh/models/{index}",
            }
            for index in range(100)
        ]
        self.task_store.patch_subscription_state(
            "sub-1",
            status="success",
            running=False,
            tracked_items=tracked_items,
            current_items=tracked_items,
            last_deleted_count=0,
        )
        self.manager._discover_subscription_items = lambda _subscription: {
            "items": [
                f"https://makerworld.com.cn/zh/models/{index}"
                for index in range(10)
            ],
            "expected_total": 100,
        }

        self.manager._sync_subscription("sub-1")

        state = self._subscription_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "error")
        self.assertIn("订阅扫描结果异常", state["last_message"])
        self.assertEqual(state["last_discovered_count"], 10)
        self.assertEqual(state["last_deleted_count"], 0)
        self.assertEqual(len(state["current_items"]), 100)
        self.assertEqual(len(state["tracked_items"]), 100)
        self.assertEqual(self.archive_manager.submitted_batches, [])

    def test_collection_sync_rejects_when_discovered_count_misses_source_total(self):
        self.task_store.patch_subscription_state(
            "sub-1",
            status="idle",
            running=False,
            tracked_items=[],
            current_items=[],
            last_deleted_count=0,
        )
        self.manager._discover_subscription_items = lambda _subscription: {
            "items": [
                f"https://makerworld.com.cn/zh/models/{index}"
                for index in range(43)
            ],
            "expected_total": 308,
            "expected_total_source": "collection_page_all_models",
            "strict_expected_total": True,
        }

        self.manager._sync_subscription("sub-1")

        state = self._subscription_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "error")
        self.assertIn("源端显示 308 个模型，本次仅扫描到 43 个", state["last_message"])
        self.assertEqual(state["last_discovered_count"], 43)
        self.assertEqual(state["last_deleted_count"], 0)
        self.assertEqual(len(state["current_items"]), 43)
        self.assertEqual(len(state["tracked_items"]), 43)
        self.assertEqual(self.archive_manager.submitted_batches, [])

    def test_author_sync_rejects_when_discovered_count_misses_source_total(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-author",
                name="作者订阅",
                url="https://makerworld.com.cn/zh/@ace/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
        ]
        self.store.save(config)
        self.task_store.patch_subscription_state(
            "sub-author",
            status="idle",
            running=False,
            tracked_items=[],
            current_items=[],
            last_deleted_count=0,
        )
        self.manager._discover_subscription_items = lambda _subscription: {
            "items": [
                f"https://makerworld.com.cn/zh/models/{index}"
                for index in range(8)
            ],
            "expected_total": 20,
            "expected_total_source": "author_upload_api_total",
            "strict_expected_total": True,
        }

        self.manager._sync_subscription("sub-author")

        state_items = self.task_store.load_subscriptions_state().get("items") or []
        state = next(item for item in state_items if item["id"] == "sub-author")
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "error")
        self.assertIn("源端显示 20 个模型，本次仅扫描到 8 个", state["last_message"])
        self.assertIn("作者页接口返回不完整", state["last_message"])
        self.assertEqual(state["last_discovered_count"], 8)
        self.assertEqual(state["last_deleted_count"], 0)
        self.assertEqual(len(state["current_items"]), 8)
        self.assertEqual(len(state["tracked_items"]), 8)
        self.assertEqual(self.archive_manager.submitted_batches, [])

    def test_sync_cookie_sources_imports_default_favorites_followed_authors_and_collections(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        config.subscriptions = []
        self.store.save(config)

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={"uid": "2024907479", "handle": "s450586793", "name": "艾斯"},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={
                "count": 1,
                "items": [
                    {
                        "title": "Whitt Labs",
                        "handle": "GLB_Whittlabs",
                        "url": "https://makerworld.com.cn/zh/@GLB_Whittlabs/upload",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={
                "count": 1,
                "items": [
                    {
                        "title": "关注收藏夹",
                        "url": "https://makerworld.com.cn/zh/collections/518732-test",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "default_favorites_subscription_source",
            return_value={
                "title": "艾斯 所有模型收藏夹",
                "url": "https://makerworld.com.cn/zh/@s450586793/collections/models",
            },
        ), patch.object(subscriptions, "_patch_cookie_source_sync_state"):
            result = self.manager.sync_cookie_sources({"cn"}, reason="cookie_save")
            second = self.manager.sync_cookie_sources({"cn"}, reason="cookie_save")

        config = self.store.load()
        urls = {item.url: item for item in config.subscriptions}
        self.assertEqual(result["created_count"], 3)
        self.assertEqual(second["created_count"], 0)
        self.assertIn("https://makerworld.com.cn/zh/@s450586793/collections/models", urls)
        self.assertIn("https://makerworld.com.cn/zh/@GLB_Whittlabs/upload", urls)
        self.assertIn("https://makerworld.com.cn/zh/collections/518732-test", urls)
        self.assertEqual(urls["https://makerworld.com.cn/zh/@GLB_Whittlabs/upload"].mode, "author_upload")
        self.assertEqual(urls["https://makerworld.com.cn/zh/collections/518732-test"].mode, "collection_models")
        states = self.task_store.load_subscriptions_state().get("items") or []
        self.assertEqual(len(states), 3)
        self.assertTrue(all(item["manual_requested_at"] for item in states))

    def test_request_cookie_source_sync_marks_platforms_for_worker(self):
        result = self.manager.request_cookie_source_sync({"cn", "global"}, reason="cookie_save")
        state = subscriptions._read_cookie_source_sync_state()

        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["platforms"], ["cn", "global"])
        self.assertEqual(state["cn"]["last_status"], "pending")
        self.assertEqual(state["cn"]["requested_reason"], "cookie_save")
        self.assertTrue(state["cn"]["requested_at"])
        self.assertEqual(state["global"]["last_status"], "pending")

    def test_maybe_sync_cookie_sources_consumes_requested_platform(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        self.store.save(config)
        self.manager.request_cookie_source_sync({"cn"}, reason="cookie_save")
        calls = []
        self.manager.sync_cookie_sources = lambda platforms, *, reason="manual": calls.append((set(platforms), reason)) or {
            "created_count": 0,
            "updated_count": 0,
            "platforms": [],
        }

        self.manager._maybe_sync_cookie_sources()

        self.assertEqual(calls, [({"cn"}, "cookie_save")])


if __name__ == "__main__":
    unittest.main()
