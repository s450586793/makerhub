import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.store import JsonStore
from app.schemas.models import AppConfig, CookiePair, SubscriptionRecord
from app.services import source_library
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
        self.original_cookie_source_inventory_path = subscriptions.COOKIE_SOURCE_INVENTORY_PATH
        self.original_append_subscription_log = subscriptions._append_subscription_log

        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.temp_path / "subscriptions_state.json"
        subscriptions.COOKIE_SOURCE_SYNC_STATE_PATH = self.temp_path / "cookie_source_sync_state.json"
        subscriptions.COOKIE_SOURCE_INVENTORY_PATH = self.temp_path / "cookie_source_inventory.json"
        subscriptions._append_subscription_log = lambda *_args, **_kwargs: None
        self.db_state = {}
        self.db_patches = [
            patch.object(
                task_state_module,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.db_state.get(key) or default),
            ),
            patch.object(
                task_state_module,
                "save_database_json_state",
                side_effect=lambda key, value: self.db_state.__setitem__(key, value) or value,
            ),
            patch.object(
                subscriptions,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.db_state.get(key) or default),
            ),
            patch.object(
                subscriptions,
                "save_database_json_state",
                side_effect=lambda key, value: self.db_state.__setitem__(key, value) or value,
            ),
            patch.object(
                source_library,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.db_state.get(key) or default),
            ),
            patch.object(
                source_library,
                "save_database_json_state",
                side_effect=lambda key, value: self.db_state.__setitem__(key, value) or value,
            ),
        ]
        for item in self.db_patches:
            item.start()

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
        for item in reversed(self.db_patches):
            item.stop()
        task_state_module.SUBSCRIPTIONS_STATE_PATH = self.original_subscriptions_state_path
        subscriptions.COOKIE_SOURCE_SYNC_STATE_PATH = self.original_cookie_source_sync_state_path
        subscriptions.COOKIE_SOURCE_INVENTORY_PATH = self.original_cookie_source_inventory_path
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

    def test_list_light_payload_does_not_build_full_source_library(self):
        with patch.object(subscriptions, "build_subscription_overview_payload", side_effect=AssertionError("full overview should not run")), \
                patch.object(self.manager.task_store, "load_subscriptions_state", side_effect=AssertionError("full subscription state should not load")):
            payload = self.manager.list_light_payload(page=1, page_size=8)

        self.assertTrue(payload["light"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["sections"][0]["key"], "subscription_sources")
        self.assertEqual(payload["sections"][0]["items"][0]["subscription_id"], "sub-1")
        self.assertFalse(payload["items"][0]["running"])
        self.assertEqual(payload["items"][0]["status"], "idle")

    def test_subscription_payload_limit_returns_source_cards_through_requested_page(self):
        manager = subscriptions.SubscriptionManager(
            archive_manager=SimpleNamespace(),
            store=JsonStore(self.temp_path / "limit_config.json"),
            task_store=TaskStateStore(),
            background_enabled=False,
        )
        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "items": [{"key": f"source-{index}"} for index in range(10)],
                }
            ],
            "settings": {},
        }
        with patch.object(manager, "_ensure_state_records"), \
                patch.object(manager.store, "load", return_value=AppConfig()), \
                patch.object(manager.task_store, "load_subscriptions_state", return_value={"items": []}), \
                patch("app.services.subscriptions.build_subscription_overview_payload", return_value=overview):
            payload = manager.list_payload(page=3, page_size=2, limit=6)

        section = next(item for item in payload["sections"] if item["key"] == "subscription_sources")
        self.assertEqual(section["page"], 3)
        self.assertEqual(section["page_size"], 2)
        self.assertEqual(section["count"], 6)
        self.assertTrue(section["has_more"])
        self.assertEqual(
            [item["key"] for item in section["items"]],
            ["source-0", "source-1", "source-2", "source-3", "source-4", "source-5"],
        )

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

    def test_ensure_state_records_dedupes_global_default_favorites_language_urls(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-en",
                name="国际 艾斯 所有模型收藏夹",
                url="https://makerworld.com/en/@s450586793/collections/models",
                mode="collection_models",
                cron="0 */6 * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-zh",
                name="艾斯国际区收藏夹订阅",
                url="https://makerworld.com/zh/@s450586793/collections/models",
                mode="collection_models",
                cron="0 * * * *",
                enabled=True,
            ),
        ]
        self.store.save(config)
        self.task_store.patch_subscription_state(
            "sub-en",
            status="success",
            running=False,
            last_success_at="2026-05-22T18:10:00+08:00",
            current_items=[{"task_key": f"model:{index}", "model_id": str(index), "url": f"https://makerworld.com/zh/models/{index}"} for index in range(18)],
            tracked_items=[{"task_key": f"model:{index}", "model_id": str(index), "url": f"https://makerworld.com/zh/models/{index}"} for index in range(18)],
        )
        self.task_store.patch_subscription_state(
            "sub-zh",
            status="success",
            running=False,
            last_success_at="2026-05-22T18:02:00+08:00",
            current_items=[{"task_key": f"model:{index}", "model_id": str(index), "url": f"https://makerworld.com/zh/models/{index}"} for index in range(18)],
            tracked_items=[{"task_key": f"model:{index}", "model_id": str(index), "url": f"https://makerworld.com/zh/models/{index}"} for index in range(25)],
        )

        self.manager._ensure_state_records()

        config = self.store.load()
        self.assertEqual(len(config.subscriptions), 1)
        self.assertEqual(config.subscriptions[0].url, "https://makerworld.com/zh/@s450586793/collections/models")
        self.assertEqual(config.subscriptions[0].name, "国际 艾斯 所有模型收藏夹")
        states = self.task_store.load_subscriptions_state().get("items") or []
        self.assertEqual(len(states), 1)
        self.assertEqual(len(states[0]["current_items"]), 18)
        self.assertEqual(len(states[0]["tracked_items"]), 18)

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
            return_value={
                "uid": "2024907479",
                "handle": "s450586793",
                "name": "艾斯",
                "follow_count": 27,
                "liked_collection_count": 1,
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={
                "uid": "2024907479",
                "handle": "s450586793",
                "name": "艾斯",
                "avatar_url": "https://example.test/avatar.jpg",
                "follow_count": 0,
                "liked_collection_count": 0,
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={
                "count": 1,
                "items": [
                    {
                        "title": "Whitt Labs",
                        "handle": "GLB_Whittlabs",
                        "uid": "31394486",
                        "url": "https://makerworld.com.cn/zh/@GLB_Whittlabs/upload",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={
                "count": 1,
                "total": 27,
                "items": [
                    {
                        "title": "Whitt Labs",
                        "handle": "GLB_Whittlabs",
                        "uid": "31394486",
                        "url": "https://makerworld.com.cn/zh/@GLB_Whittlabs/upload",
                    },
                    {
                        "title": "Second Author",
                        "handle": "user_123",
                        "uid": "123",
                        "url": "https://makerworld.com.cn/zh/@user_123/upload",
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
        ), patch.object(subscriptions, "_patch_cookie_source_sync_state"):
            result = self.manager.sync_cookie_sources({"cn"}, reason="cookie_save")
            second = self.manager.sync_cookie_sources({"cn"}, reason="cookie_save")

        config = self.store.load()
        urls = {item.url: item for item in config.subscriptions}
        self.assertEqual(result["created_count"], 4)
        self.assertEqual(second["created_count"], 0)
        self.assertIn("https://makerworld.com.cn/zh/@s450586793/collections/models", urls)
        self.assertEqual(urls["https://makerworld.com.cn/zh/@s450586793/collections/models"].name, "艾斯的收藏夹")
        self.assertIn("https://makerworld.com.cn/zh/@GLB_Whittlabs/upload", urls)
        self.assertIn("https://makerworld.com.cn/zh/@user_123/upload", urls)
        self.assertIn("https://makerworld.com.cn/zh/collections/518732-test", urls)
        self.assertEqual(urls["https://makerworld.com.cn/zh/@GLB_Whittlabs/upload"].mode, "author_upload")
        self.assertEqual(urls["https://makerworld.com.cn/zh/@user_123/upload"].mode, "author_upload")
        self.assertEqual(urls["https://makerworld.com.cn/zh/collections/518732-test"].mode, "collection_models")
        states = self.task_store.load_subscriptions_state().get("items") or []
        self.assertEqual(len(states), 4)
        self.assertTrue(all(item["manual_requested_at"] for item in states))
        inventory = subscriptions._read_cookie_source_inventory_state()
        cn_inventory = inventory["platforms"]["cn"]
        self.assertEqual(cn_inventory["account"]["uid"], "2024907479")
        self.assertEqual(cn_inventory["account"]["name"], "艾斯")
        self.assertEqual(cn_inventory["account"]["avatar_url"], "https://example.test/avatar.jpg")
        self.assertEqual(cn_inventory["followed_author_count"], 27)
        self.assertEqual(cn_inventory["followed_collection_count"], 1)
        self.assertEqual(len(cn_inventory["followed_authors"]), 2)
        self.assertEqual(cn_inventory["followed_collections"][0]["url"], "https://makerworld.com.cn/zh/collections/518732-test")
        self.assertIn("https://makerworld.com.cn/zh/@s450586793/collections/models", cn_inventory["source_urls"])
        self.assertIn("https://makerworld.com.cn/zh/@user_123/upload", cn_inventory["source_urls"])
        saved_cookie = next(item for item in config.cookies if item.platform == "cn")
        self.assertEqual(saved_cookie.display_name, "艾斯")
        self.assertEqual(saved_cookie.account_id, "2024907479")
        self.assertEqual(saved_cookie.handle, "s450586793")
        self.assertEqual(saved_cookie.avatar_url, "https://example.test/avatar.jpg")
        self.assertEqual(saved_cookie.message, "国内账号已同步，账号信息已更新。")

    def test_sync_cookie_sources_canonicalizes_imported_author_urls(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="global", cookie="token=ok")]
        config.subscriptions = []
        self.store.save(config)

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={
                "uid": "2073587493",
                "handle": "owner",
                "name": "Owner",
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={},
        ), patch.object(
            subscriptions,
            "default_favorites_subscription_source",
            return_value={},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={
                "count": 1,
                "total": 1,
                "items": [
                    {
                        "title": "Oierre",
                        "handle": "Oierre",
                        "uid": "1",
                        "url": "https://makerworld.com/en/@Oierre/upload",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={
                "count": 1,
                "total": 1,
                "items": [
                    {
                        "title": "Oierre",
                        "handle": "Oierre",
                        "uid": "1",
                        "url": "https://makerworld.com/@Oierre/upload?appSharePlatform=copy",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={"count": 0, "items": []},
        ), patch.object(subscriptions, "_patch_cookie_source_sync_state"):
            result = self.manager.sync_cookie_sources({"global"}, reason="cookie_save")

        config = self.store.load()
        inventory = subscriptions._read_cookie_source_inventory_state()["platforms"]["global"]

        self.assertEqual(result["created_count"], 1)
        self.assertEqual([item.url for item in config.subscriptions], ["https://makerworld.com/zh/@Oierre/upload"])
        self.assertEqual(inventory["followed_authors"][0]["url"], "https://makerworld.com/zh/@Oierre/upload")
        self.assertEqual(inventory["source_urls"], ["https://makerworld.com/zh/@Oierre/upload"])

    def test_sync_cookie_sources_imports_uid_verified_user_number_author_urls(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        config.subscriptions = []
        self.store.save(config)

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={
                "uid": "2024907479",
                "handle": "s450586793",
                "name": "艾斯",
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={},
        ), patch.object(
            subscriptions,
            "default_favorites_subscription_source",
            return_value={},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={"count": 0, "total": 0, "items": []},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={
                "count": 1,
                "total": 1,
                "items": [
                    {
                        "title": "樱桃的好奇心",
                        "handle": "user_1751098586",
                        "uid": "1751098586",
                        "url": "https://makerworld.com.cn/zh/@user_1751098586/upload",
                    }
                ],
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={"count": 0, "items": []},
        ), patch.object(subscriptions, "_patch_cookie_source_sync_state"):
            result = self.manager.sync_cookie_sources({"cn"}, reason="manual")

        config = self.store.load()
        inventory = subscriptions._read_cookie_source_inventory_state()["platforms"]["cn"]

        self.assertEqual(result["created_count"], 1)
        self.assertEqual(config.subscriptions[0].url, "https://makerworld.com.cn/zh/@user_1751098586/upload")
        self.assertEqual(config.subscriptions[0].name, "樱桃的好奇心 作者订阅")
        self.assertEqual(inventory["followed_authors"][0]["url"], "https://makerworld.com.cn/zh/@user_1751098586/upload")
        self.assertEqual(inventory["source_urls"], ["https://makerworld.com.cn/zh/@user_1751098586/upload"])

    def test_sync_cookie_sources_seeds_default_favorite_account_avatar_metadata(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="global", cookie="token=ok")]
        config.subscriptions = []
        self.store.save(config)
        saved_metadata = {}

        def fake_save_metadata(source_key, payload):
            saved_metadata[source_key] = payload

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={
                "uid": "2073587493",
                "handle": "s450586793",
                "name": "艾斯",
                "avatar_url": "https://example.test/account.jpg",
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={"count": 0, "items": []},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={"count": 0, "items": []},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={"count": 0, "items": []},
        ), patch.object(subscriptions, "_patch_cookie_source_sync_state"), \
                patch.object(subscriptions, "_save_source_metadata_item", side_effect=fake_save_metadata):
            result = self.manager.sync_cookie_sources({"global"}, reason="cookie_save")

        config = self.store.load()
        favorite = next(item for item in config.subscriptions if item.url.endswith("/@s450586793/collections/models"))
        metadata = saved_metadata[subscriptions.source_identity_key(favorite.url, favorite.mode)]
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(metadata["kind"], "favorite")
        self.assertEqual(metadata["avatar_url"], "https://example.test/account.jpg")
        self.assertEqual(metadata["title"], "艾斯的收藏夹")

    def test_sync_cookie_sources_uses_saved_account_metadata_when_profile_is_sparse(self):
        config = self.store.load()
        config.cookies = [
            CookiePair(
                platform="cn",
                cookie="token=ok",
                username="18651510352",
                display_name="艾斯",
                account_id="2024907479",
                handle="s450586793",
                avatar_url="https://example.test/account.jpg",
            )
        ]
        config.subscriptions = []
        self.store.save(config)

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={"platform": "cn", "uid": "", "handle": "", "name": "", "avatar_url": ""},
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={"count": 0, "items": []},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={"count": 0, "items": []},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={"count": 0, "items": []},
        ):
            result = self.manager.sync_cookie_sources({"cn"}, reason="manual")

        config = self.store.load()
        favorite = next(item for item in config.subscriptions if item.url.endswith("/@s450586793/collections/models"))
        inventory = subscriptions._read_cookie_source_inventory_state()["platforms"]["cn"]
        sync_state = subscriptions._read_cookie_source_sync_state()["cn"]
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(favorite.name, "艾斯的收藏夹")
        self.assertEqual(inventory["account"]["uid"], "2024907479")
        self.assertEqual(inventory["account"]["handle"], "s450586793")
        self.assertEqual(sync_state["default_favorites_count"], 1)

    def test_request_cookie_source_sync_marks_platforms_for_worker(self):
        result = self.manager.request_cookie_source_sync({"cn", "global"}, reason="cookie_save")
        state = subscriptions._read_cookie_source_sync_state()

        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["platforms"], ["cn", "global"])
        self.assertEqual(state["cn"]["last_status"], "pending")
        self.assertEqual(state["cn"]["requested_reason"], "cookie_save")
        self.assertTrue(state["cn"]["requested_at"])
        self.assertEqual(state["global"]["last_status"], "pending")

    def test_unrelated_config_save_preserves_concurrent_subscription_imports(self):
        stale_config = self.store.load()

        fresh_config = self.store.load()
        fresh_config.subscriptions.append(
            SubscriptionRecord(
                id="sub-imported",
                name="新导入作者",
                url="https://makerworld.com.cn/zh/@newmaker/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
        )
        self.store.save(fresh_config)

        stale_config.user.display_name = "Updated Admin"
        saved = self.store.save(stale_config)

        self.assertEqual(saved.user.display_name, "Updated Admin")
        self.assertIn("sub-imported", [item.id for item in saved.subscriptions])
        self.assertIn("sub-imported", [item.id for item in self.store.load().subscriptions])

    def test_list_payload_canonicalizes_existing_author_subscription_urls(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-old-author",
                name="Oierre 作者订阅",
                url="https://makerworld.com/en/@Oierre/upload?appSharePlatform=copy",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
        ]
        self.store.save(config)

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value={}):
            payload = self.manager.list_payload()

        self.assertEqual(payload["items"][0]["url"], "https://makerworld.com/zh/@Oierre/upload")
        self.assertEqual(self.store.load().subscriptions[0].url, "https://makerworld.com/zh/@Oierre/upload")

    def test_list_payload_paginates_subscription_source_cards_only(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id=f"sub-{index}",
                name=f"作者 {index}",
                url=f"https://makerworld.com.cn/zh/@author{index}/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
            for index in range(1, 6)
        ]
        self.store.save(config)
        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "label": "订阅来源",
                    "items": [{"key": f"source-{index}"} for index in range(1, 6)],
                },
                {
                    "key": "states",
                    "label": "状态",
                    "items": [{"key": "state-1"}],
                    "count": 1,
                },
            ],
            "settings": config.subscription_settings.model_dump(),
        }

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value=overview):
            payload = self.manager.list_payload(page=2, page_size=2)

        source_section = next(section for section in payload["sections"] if section["key"] == "subscription_sources")
        state_section = next(section for section in payload["sections"] if section["key"] == "states")
        self.assertEqual([item["key"] for item in source_section["items"]], ["source-3", "source-4"])
        self.assertEqual(source_section["count"], 2)
        self.assertEqual(source_section["total"], 5)
        self.assertEqual(source_section["page"], 2)
        self.assertEqual(source_section["page_size"], 2)
        self.assertTrue(source_section["has_more"])
        self.assertEqual(payload["count"], 5)
        self.assertEqual(payload["summary"]["enabled"], 5)
        self.assertEqual(state_section, overview["sections"][1])

    def test_list_payload_defaults_subscription_source_page_size_to_eight(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id=f"sub-{index}",
                name=f"作者 {index}",
                url=f"https://makerworld.com.cn/zh/@author{index}/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
            for index in range(1, 11)
        ]
        self.store.save(config)
        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "label": "订阅来源",
                    "items": [{"key": f"source-{index}"} for index in range(1, 11)],
                },
            ],
            "settings": config.subscription_settings.model_dump(),
        }

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value=overview):
            payload = self.manager.list_payload()

        source_section = next(section for section in payload["sections"] if section["key"] == "subscription_sources")
        self.assertEqual([item["key"] for item in source_section["items"]], [f"source-{index}" for index in range(1, 9)])
        self.assertEqual(source_section["count"], 8)
        self.assertEqual(source_section["total"], 10)
        self.assertEqual(source_section["page"], 1)
        self.assertEqual(source_section["page_size"], 8)
        self.assertTrue(source_section["has_more"])

    def test_list_payload_subscription_source_last_page_has_no_more(self):
        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "label": "订阅来源",
                    "items": [{"key": f"source-{index}"} for index in range(1, 4)],
                },
            ],
            "settings": self.store.load().subscription_settings.model_dump(),
        }

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value=overview):
            payload = self.manager.list_payload(page=2, page_size=2)

        source_section = next(section for section in payload["sections"] if section["key"] == "subscription_sources")
        self.assertEqual([item["key"] for item in source_section["items"]], ["source-3"])
        self.assertEqual(source_section["count"], 1)
        self.assertEqual(source_section["total"], 3)
        self.assertFalse(source_section["has_more"])

    def test_cookie_source_public_payloads_expose_source_state(self):
        subscriptions._write_cookie_source_inventory_state(
            {
                "platforms": {
                    "cn": {
                        "followed_authors": [{"url": "https://makerworld.com.cn/zh/@ace/upload"}],
                        "followed_collection_count": 2,
                    }
                },
                "updated_at": "2026-05-23T21:00:00+08:00",
            }
        )
        subscriptions._write_cookie_source_sync_state(
            {
                "cn": {"last_status": "success", "followed_author_count": 1},
                "other": {"last_status": "ignored"},
            }
        )

        inventory = subscriptions.cookie_source_inventory_payload()
        sync_state = subscriptions.cookie_source_sync_state_payload()

        self.assertEqual(inventory["platforms"]["cn"]["followed_collection_count"], 2)
        self.assertEqual(sync_state["cn"]["followed_author_count"], 1)
        self.assertNotIn("other", sync_state)

    def test_sync_cookie_sources_keeps_prior_non_error_user_number_author_imports(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-bad-account-author",
                name="坏作者订阅",
                url="https://makerworld.com.cn/zh/@user_123/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-good-account-author",
                name="真实作者订阅",
                url="https://makerworld.com.cn/zh/@RealMaker/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-manual-bad",
                name="手工坏作者",
                url="https://makerworld.com.cn/zh/@user_999/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
        ]
        self.store.save(config)
        for subscription_id in ("sub-bad-account-author", "sub-good-account-author", "sub-manual-bad"):
            self.task_store.patch_subscription_state(subscription_id, status="idle")
        subscriptions._write_cookie_source_inventory_state(
            {
                "platforms": {
                    "cn": {
                        "imported_sources": [
                            {
                                "subscription_id": "sub-bad-account-author",
                                "url": "https://makerworld.com.cn/zh/@user_123/upload",
                                "mode": "author_upload",
                                "source_kind": "followed_author",
                            },
                            {
                                "subscription_id": "sub-good-account-author",
                                "url": "https://makerworld.com.cn/zh/@RealMaker/upload",
                                "mode": "author_upload",
                                "source_kind": "followed_author",
                            },
                        ],
                        "source_urls": [
                            "https://makerworld.com.cn/zh/@user_123/upload",
                            "https://makerworld.com.cn/zh/@RealMaker/upload",
                        ],
                        "followed_authors": [
                            {"url": "https://makerworld.com.cn/zh/@user_123/upload"},
                            {"url": "https://makerworld.com.cn/zh/@RealMaker/upload"},
                        ],
                    }
                },
                "updated_at": "2026-05-26T01:00:00+08:00",
            }
        )

        with patch.object(subscriptions, "discover_cookie_account_profile", return_value={"uid": "1", "handle": "owner", "name": "Owner"}), \
                patch.object(subscriptions, "discover_cookie_account_home_summary", return_value={"uid": "1", "handle": "owner", "name": "Owner"}), \
                patch.object(subscriptions, "default_favorites_subscription_source", return_value={}), \
                patch.object(subscriptions, "discover_cookie_followed_authors_from_page", return_value={"items": [], "count": 0, "total": 0}), \
                patch.object(subscriptions, "discover_cookie_followed_authors", return_value={"items": [], "count": 0, "total": 0}), \
                patch.object(subscriptions, "discover_cookie_followed_collections", return_value={"items": [], "count": 0}):
            result = self.manager.sync_cookie_sources({"cn"}, reason="scheduled")

        config = self.store.load()
        state_ids = [item["id"] for item in self.task_store.load_subscriptions_state().get("items") or []]
        platform_result = result["platforms"][0]

        self.assertEqual(platform_result["removed_invalid_count"], 0)
        self.assertEqual(platform_result["removed_invalid_subscription_ids"], [])
        self.assertEqual(
            [item.id for item in config.subscriptions],
            ["sub-bad-account-author", "sub-good-account-author", "sub-manual-bad"],
        )
        self.assertEqual(state_ids, ["sub-bad-account-author", "sub-good-account-author", "sub-manual-bad"])

    def test_sync_cookie_sources_reports_missing_followed_collection_items(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        config.subscriptions = []
        self.store.save(config)

        with patch.object(
            subscriptions,
            "discover_cookie_account_profile",
            return_value={
                "uid": "2024907479",
                "handle": "s450586793",
                "name": "艾斯",
                "follow_count": 0,
                "liked_collection_count": 1,
            },
        ), patch.object(
            subscriptions,
            "discover_cookie_account_home_summary",
            return_value={},
        ), patch.object(
            subscriptions,
            "default_favorites_subscription_source",
            return_value={},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors_from_page",
            return_value={"items": [], "count": 0, "total": 0},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_authors",
            return_value={"items": [], "count": 0, "total": 0},
        ), patch.object(
            subscriptions,
            "discover_cookie_followed_collections",
            return_value={"items": [], "count": 0},
        ):
            result = self.manager.sync_cookie_sources({"cn"}, reason="scheduled")

        sync_state = subscriptions._read_cookie_source_sync_state()["cn"]
        inventory = subscriptions._read_cookie_source_inventory_state()["platforms"]["cn"]
        platform_result = result["platforms"][0]

        self.assertEqual(platform_result["followed_collection_count"], 1)
        self.assertEqual(platform_result["imported_followed_collection_count"], 0)
        self.assertEqual(platform_result["skipped_followed_collection_count"], 1)
        self.assertEqual(sync_state["last_status"], "warning")
        self.assertEqual(sync_state["followed_collection_count"], 1)
        self.assertEqual(sync_state["imported_followed_collection_count"], 0)
        self.assertEqual(sync_state["skipped_followed_collection_count"], 1)
        self.assertEqual(inventory["followed_collection_count"], 1)
        self.assertEqual(inventory["followed_collections"], [])

    def test_sync_cookie_sources_removes_legacy_error_synthetic_user_author_subscriptions(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="global", cookie="token=ok")]
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-legacy-bad",
                name="旧错误作者订阅",
                url="https://makerworld.com/zh/@user_2595475119/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-manual-bad",
                name="未失败的手工作者订阅",
                url="https://makerworld.com/zh/@user_999/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-good-author",
                name="真实作者订阅",
                url="https://makerworld.com/zh/@RealMaker/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
        ]
        self.store.save(config)
        self.task_store.patch_subscription_state(
            "sub-legacy-bad",
            status="error",
            last_message="未能在页面中识别模型链接，请确认链接与 Cookie 是否有效。",
        )
        self.task_store.patch_subscription_state("sub-manual-bad", status="idle")
        self.task_store.patch_subscription_state("sub-good-author", status="error")
        subscriptions._write_cookie_source_inventory_state(
            {
                "platforms": {
                    "global": {
                        "imported_sources": [],
                        "source_urls": [],
                        "followed_authors": [],
                    }
                },
                "updated_at": "2026-05-29T00:00:00+08:00",
            }
        )

        with patch.object(subscriptions, "discover_cookie_account_profile", return_value={"uid": "1", "handle": "owner", "name": "Owner"}), \
                patch.object(subscriptions, "discover_cookie_account_home_summary", return_value={"uid": "1", "handle": "owner", "name": "Owner"}), \
                patch.object(subscriptions, "default_favorites_subscription_source", return_value={}), \
                patch.object(subscriptions, "discover_cookie_followed_authors_from_page", return_value={"items": [], "count": 0, "total": 0}), \
                patch.object(subscriptions, "discover_cookie_followed_authors", return_value={"items": [], "count": 0, "total": 0}), \
                patch.object(subscriptions, "discover_cookie_followed_collections", return_value={"items": [], "count": 0}):
            result = self.manager.sync_cookie_sources({"global"}, reason="scheduled")

        config = self.store.load()
        state_ids = [item["id"] for item in self.task_store.load_subscriptions_state().get("items") or []]
        platform_result = result["platforms"][0]

        self.assertEqual(platform_result["removed_invalid_count"], 1)
        self.assertEqual(platform_result["removed_invalid_subscription_ids"], ["sub-legacy-bad"])
        self.assertEqual(
            [item.id for item in config.subscriptions],
            ["sub-manual-bad", "sub-good-author"],
        )
        self.assertEqual(state_ids, ["sub-manual-bad", "sub-good-author"])

    def test_sync_cookie_sources_does_not_resurrect_removed_legacy_error_subscription(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="global", cookie="token=ok")]
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-legacy-bad",
                name="旧错误作者订阅",
                url="https://makerworld.com/zh/@user_2595475119/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
        ]
        self.store.save(config)
        self.task_store.patch_subscription_state("sub-legacy-bad", status="error")
        subscriptions._write_cookie_source_inventory_state(
            {
                "platforms": {"global": {"imported_sources": [], "source_urls": [], "followed_authors": []}},
                "updated_at": "2026-05-29T00:00:00+08:00",
            }
        )

        with patch.object(subscriptions, "discover_cookie_account_profile", return_value={"uid": "1", "handle": "owner", "name": "Owner"}), \
                patch.object(subscriptions, "discover_cookie_account_home_summary", return_value={}), \
                patch.object(subscriptions, "default_favorites_subscription_source", return_value={}), \
                patch.object(subscriptions, "discover_cookie_followed_authors_from_page", return_value={"items": [], "count": 0, "total": 0}), \
                patch.object(
                    subscriptions,
                    "discover_cookie_followed_authors",
                    return_value={
                        "items": [
                            {
                                "title": "Oierre",
                                "handle": "Oierre",
                                "uid": "167015859",
                                "url": "https://makerworld.com/zh/@Oierre/upload",
                            }
                        ],
                        "count": 1,
                        "total": 1,
                    },
                ), \
                patch.object(subscriptions, "discover_cookie_followed_collections", return_value={"items": [], "count": 0}):
            result = self.manager.sync_cookie_sources({"global"}, reason="scheduled")

        config = self.store.load()
        state_ids = [item["id"] for item in self.task_store.load_subscriptions_state().get("items") or []]

        self.assertEqual(result["platforms"][0]["removed_invalid_count"], 1)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(len(config.subscriptions), 1)
        self.assertEqual(config.subscriptions[0].url, "https://makerworld.com/zh/@Oierre/upload")
        self.assertNotIn("sub-legacy-bad", [item.id for item in config.subscriptions])
        self.assertNotIn("sub-legacy-bad", state_ids)

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

    def test_remove_account_imported_subscriptions_removes_sources_and_marks_archived_models_deleted(self):
        config = self.store.load()
        config.cookies = [CookiePair(platform="cn", cookie="token=ok")]
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-account-author",
                name="关注作者",
                url="https://makerworld.com.cn/zh/@followed/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
            SubscriptionRecord(
                id="sub-manual",
                name="手工订阅",
                url="https://makerworld.com.cn/zh/@manual/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            ),
        ]
        self.store.save(config)
        self.task_store.patch_subscription_state(
            "sub-account-author",
            status="success",
            current_items=[
                {
                    "task_key": "model:1001",
                    "model_id": "1001",
                    "url": "https://makerworld.com.cn/zh/models/1001",
                }
            ],
            tracked_items=[
                {
                    "task_key": "model:1001",
                    "model_id": "1001",
                    "url": "https://makerworld.com.cn/zh/models/1001",
                },
                {
                    "task_key": "model:1002",
                    "model_id": "1002",
                    "url": "https://makerworld.com.cn/zh/models/1002",
                },
            ],
        )
        self.task_store.patch_subscription_state(
            "sub-manual",
            status="success",
            current_items=[
                {
                    "task_key": "model:2001",
                    "model_id": "2001",
                    "url": "https://makerworld.com.cn/zh/models/2001",
                }
            ],
            tracked_items=[
                {
                    "task_key": "model:2001",
                    "model_id": "2001",
                    "url": "https://makerworld.com.cn/zh/models/2001",
                }
            ],
        )
        subscriptions._write_cookie_source_inventory_state(
            {
                "platforms": {
                    "cn": {
                        "imported_sources": [
                            {
                                "subscription_id": "sub-account-author",
                                "url": "https://makerworld.com.cn/zh/@followed/upload",
                                "mode": "author_upload",
                                "source_kind": "followed_author",
                            }
                        ],
                        "source_urls": ["https://makerworld.com.cn/zh/@followed/upload"],
                        "followed_authors": [{"url": "https://makerworld.com.cn/zh/@followed/upload"}],
                    }
                },
                "updated_at": "2026-05-25T00:00:00+08:00",
            }
        )

        archive_snapshot = {
            "models": (
                {
                    "model_dir": "remote/model-1001",
                    "id": "1001",
                    "origin_url": "https://makerworld.com.cn/zh/models/1001",
                },
                {
                    "model_dir": "remote/model-1002",
                    "id": "1002",
                    "origin_url": "https://makerworld.com.cn/zh/models/1002",
                },
                {
                    "model_dir": "remote/model-2001",
                    "id": "2001",
                    "origin_url": "https://makerworld.com.cn/zh/models/2001",
                },
            )
        }

        with patch.object(subscriptions, "get_archive_snapshot", return_value=archive_snapshot), \
                patch.object(subscriptions, "invalidate_archive_snapshot") as invalidate_snapshot, \
                patch.object(subscriptions, "invalidate_model_detail_cache") as invalidate_detail:
            result = self.manager.remove_account_imported_subscriptions("cn")

        config = self.store.load()
        states = self.task_store.load_subscriptions_state().get("items") or []
        flags = self.task_store.load_model_flags()
        inventory = subscriptions._read_cookie_source_inventory_state()["platforms"]["cn"]

        self.assertEqual(result["removed_subscription_ids"], ["sub-account-author"])
        self.assertEqual(result["local_deleted_count"], 2)
        self.assertEqual([item.id for item in config.subscriptions], ["sub-manual"])
        self.assertEqual([item["id"] for item in states], ["sub-manual"])
        self.assertEqual(set(flags["deleted"]), {"remote/model-1001", "remote/model-1002"})
        self.assertEqual(inventory["last_status"], "deleted")
        self.assertEqual(inventory["imported_sources"], [])
        self.assertEqual(inventory["source_urls"], [])
        invalidate_snapshot.assert_called_once_with("online_account_deleted")
        self.assertEqual(invalidate_detail.call_count, 2)

    def test_remove_account_imported_subscriptions_ignores_untracked_cookie_loss(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(
                id="sub-manual",
                name="手工订阅",
                url="https://makerworld.com.cn/zh/@manual/upload",
                mode="author_upload",
                cron="0 * * * *",
                enabled=True,
            )
        ]
        self.store.save(config)

        result = self.manager.remove_account_imported_subscriptions("cn")

        self.assertEqual(result["removed_subscription_count"], 0)
        self.assertEqual([item.id for item in self.store.load().subscriptions], ["sub-manual"])


if __name__ == "__main__":
    unittest.main()
