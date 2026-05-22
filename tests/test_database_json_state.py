import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.security import hash_api_token
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig, CookiePair
from app.services import subscriptions
from app.services.task_state import TaskStateStore


class JsonStateDatabaseRoutingTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.patches = [
            patch("app.core.store.database_configured", return_value=True),
            patch("app.core.store.database_driver_available", return_value=True),
            patch("app.core.store.load_json_state", side_effect=lambda key: self.state.get(key)),
            patch("app.core.store.save_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.task_state.database_configured", return_value=True),
            patch("app.services.task_state.database_driver_available", return_value=True),
            patch("app.services.task_state.load_json_state", side_effect=lambda key: self.state.get(key)),
            patch("app.services.task_state.save_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.subscriptions.database_configured", return_value=True),
            patch("app.services.subscriptions.database_driver_available", return_value=True),
            patch("app.services.subscriptions.load_json_state", side_effect=lambda key: self.state.get(key)),
            patch("app.services.subscriptions.save_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_json_store_migrates_config_with_cookie_and_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            raw_token = "mht_database_token"
            config = AppConfig()
            config.cookies = [CookiePair(platform="cn", cookie="token=makerworld")]
            config.api_tokens = [
                ApiTokenRecord(
                    id="token-1",
                    name="iPhone",
                    token_prefix=raw_token[:12],
                    token_hash=hash_api_token(raw_token),
                    token_value=raw_token,
                    permissions=["mobile_import"],
                    created_at="2026-05-22T10:00:00+08:00",
                )
            ]
            with patch("app.core.store.CONFIG_PATH", config_path):
                store = JsonStore(config_path)
                store.save(config)

                loaded = store.load()

            self.assertEqual(self.state["app_config"]["cookies"][0]["cookie"], "token=makerworld")
            self.assertEqual(self.state["app_config"]["api_tokens"][0]["token_value"], raw_token)
            self.assertEqual(loaded.cookies[0].cookie, "token=makerworld")
            self.assertEqual(loaded.api_tokens[0].token_value, raw_token)

    def test_task_state_stores_subscription_model_lists_in_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            subscriptions_path = Path(tmp) / "subscriptions_state.json"
            payload = {
                "items": [
                    {
                        "id": "sub-1",
                        "current_items": [
                            {
                                "model_id": "MW_1",
                                "url": "https://makerworld.com.cn/model/MW_1",
                                "task_key": "model:MW_1",
                            }
                        ],
                        "tracked_items": [
                            {
                                "model_id": "MW_2",
                                "url": "https://makerworld.com.cn/model/MW_2",
                                "task_key": "model:MW_2",
                            }
                        ],
                    }
                ]
            }
            with patch("app.services.task_state.SUBSCRIPTIONS_STATE_PATH", subscriptions_path), \
                    patch.dict("app.services.task_state._JSON_STATE_KEYS", {subscriptions_path.resolve(): "subscriptions_state"}, clear=False):
                store = TaskStateStore()
                saved = store.save_subscriptions_state(payload)
                loaded = store.load_subscriptions_state()

            self.assertEqual(saved["count"], 1)
            self.assertEqual(self.state["subscriptions_state"]["items"][0]["current_items"][0]["model_id"], "MW_1")
            self.assertEqual(loaded["items"][0]["tracked_items"][0]["url"], "https://makerworld.com.cn/model/MW_2")

    def test_cookie_source_inventory_stores_discovered_source_lists_in_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_path = Path(tmp) / "cookie_source_inventory.json"
            with patch("app.services.subscriptions.COOKIE_SOURCE_INVENTORY_PATH", inventory_path):
                subscriptions._patch_cookie_source_inventory_state(
                    "global",
                    account={"uid": "2073587493", "handle": "s450586793"},
                    followed_authors=[{"title": "Author", "url": "https://makerworld.com/zh/@author/upload"}],
                    followed_collections=[{"title": "关注收藏夹", "url": "https://makerworld.com/zh/collections/1"}],
                    imported_sources=[
                        {
                            "subscription_id": "sub-1",
                            "url": "https://makerworld.com/zh/@s450586793/collections/models",
                            "mode": "collection_models",
                            "source_kind": "default_favorites",
                        }
                    ],
                    source_urls=["https://makerworld.com/zh/@s450586793/collections/models"],
                    last_status="success",
                )
                loaded = subscriptions._read_cookie_source_inventory_state()

            self.assertEqual(self.state["cookie_source_inventory"]["platforms"]["global"]["account"]["uid"], "2073587493")
            self.assertEqual(loaded["platforms"]["global"]["followed_authors"][0]["title"], "Author")
            self.assertEqual(loaded["platforms"]["global"]["imported_sources"][0]["subscription_id"], "sub-1")


if __name__ == "__main__":
    unittest.main()
