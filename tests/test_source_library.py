import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.schemas.models import AppConfig, SubscriptionRecord
from app.services.catalog import _apply_subscription_flags, _source_deleted_model_count, build_dashboard_payload
from tests.test_helpers import InMemoryDatabaseState
from app.services.source_library import (
    _SOURCE_LIBRARY_GROUP_CACHE,
    _SOURCE_LIBRARY_PAYLOAD_REFRESH_STATE,
    _base_group,
    _finalize_group,
    _group_models,
    _source_key,
    _render_source_preview_snapshot,
    _source_preview_snapshot_signature,
    build_source_library_payload,
    build_source_group_models_payload,
    build_state_group_models_payload,
    refresh_source_preview_snapshots,
)


def _model(model_dir: str, *, source: str = "cn") -> dict:
    return {
        "model_dir": model_dir,
        "title": model_dir,
        "author": {"name": "Ace"},
        "tags": [],
        "source": source,
        "stats": {"downloads": 0, "likes": 0, "prints": 0},
        "collect_ts": 1,
        "publish_ts": 1,
        "local_flags": {"favorite": False, "printed": False, "deleted": True},
        "subscription_flags": {"deleted_on_source": False},
    }


class SourceLibraryTest(unittest.TestCase):
    def test_local_deleted_state_group_includes_soft_deleted_models(self):
        groups = {
            "local_deleted": {
                "key": "local_deleted",
                "route_kind": "state",
                "model_dirs": ["deleted-cn", "deleted-local"],
            }
        }
        all_models = [
            _model("deleted-cn", source="cn"),
            _model("deleted-local", source="local"),
        ]

        with patch("app.services.source_library._group_models", return_value=(groups, all_models, [])):
            payload = build_state_group_models_payload("local_deleted")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["filtered_total"], 2)
        self.assertEqual(payload["source_counts"]["all"], 2)
        self.assertEqual(payload["source_counts"]["local"], 1)
        self.assertEqual([item["model_dir"] for item in payload["items"]], ["deleted-local", "deleted-cn"])

    def test_source_deleted_state_group_includes_soft_deleted_models(self):
        groups = {
            "source_deleted": {
                "key": "source_deleted",
                "route_kind": "state",
                "model_dirs": ["source-deleted-visible", "source-deleted-hidden"],
            }
        }
        visible = _model("source-deleted-visible", source="cn")
        visible["local_flags"]["deleted"] = False
        hidden = _model("source-deleted-hidden", source="cn")
        for item in (visible, hidden):
            item["subscription_flags"]["deleted_on_source"] = True
        all_models = [visible, hidden]

        with patch("app.services.source_library._group_models", return_value=(groups, all_models, [])):
            payload = build_state_group_models_payload("source_deleted")

        self.assertIsNotNone(payload)
        self.assertEqual(payload["filtered_total"], 2)
        self.assertEqual(payload["source_counts"]["all"], 2)
        self.assertEqual([item["model_dir"] for item in payload["items"]], ["source-deleted-visible", "source-deleted-hidden"])

    def test_dashboard_source_deleted_count_uses_archived_models(self):
        items = [
            {"model_dir": "marked", "subscription_flags": {"deleted_on_source": True}},
            {"model_dir": "visible", "subscription_flags": {"deleted_on_source": False}},
            {"model_dir": "plain"},
        ]

        self.assertEqual(_source_deleted_model_count(items), 1)

    def test_dashboard_reuses_loaded_archive_snapshot_for_decorated_models(self):
        snapshot = {
            "models": (),
            "total": 0,
            "archived_model_ids": frozenset(),
            "archived_urls": frozenset(),
        }

        with InMemoryDatabaseState(), \
                patch("app.services.catalog.get_archive_snapshot", return_value=snapshot) as snapshot_mock, \
                patch("app.services.catalog.build_source_health_cards", return_value=[]):
            payload = build_dashboard_payload(AppConfig())

        self.assertEqual(payload["stats"][0]["value"], 0)
        snapshot_mock.assert_called_once_with()

    def test_subscription_flags_ignore_collection_missing_items(self):
        item = {
            "id": "2014963",
            "origin_url": "https://makerworld.com.cn/zh/models/2014963",
            "remote_sync": {},
        }
        config = type(
            "ConfigStub",
            (),
            {
                "subscriptions": [
                    SubscriptionRecord(
                        id="sub-collection",
                        name="艾斯收藏夹",
                        url="https://makerworld.com.cn/zh/@ace/collections/models",
                        mode="collection_models",
                    )
                ]
            },
        )()
        state_payload = {
            "items": [
                {
                    "id": "sub-collection",
                    "current_items": [],
                    "tracked_items": [
                        {
                            "task_key": "model:2014963",
                            "model_id": "2014963",
                            "url": "https://makerworld.com.cn/zh/models/2014963",
                        }
                    ],
                }
            ]
        }

        _apply_subscription_flags([item], config=config, state_payload=state_payload)

        self.assertFalse(item["subscription_flags"]["deleted_on_source"])
        self.assertEqual(item["subscription_flags"]["deleted_sources"], [])

    def test_subscription_flags_keep_author_missing_items(self):
        item = {
            "id": "2014963",
            "origin_url": "https://makerworld.com.cn/zh/models/2014963",
            "remote_sync": {},
        }
        config = type(
            "ConfigStub",
            (),
            {
                "subscriptions": [
                    SubscriptionRecord(
                        id="sub-author",
                        name="Whitt Labs",
                        url="https://makerworld.com.cn/zh/@GLB_Whittlabs/upload",
                        mode="author_upload",
                    )
                ]
            },
        )()
        state_payload = {
            "items": [
                {
                    "id": "sub-author",
                    "current_items": [],
                    "tracked_items": [
                        {
                            "task_key": "model:2014963",
                            "model_id": "2014963",
                            "url": "https://makerworld.com.cn/zh/models/2014963",
                        }
                    ],
                }
            ]
        }

        _apply_subscription_flags([item], config=config, state_payload=state_payload)

        self.assertTrue(item["subscription_flags"]["deleted_on_source"])
        self.assertEqual(item["subscription_flags"]["deleted_sources"][0]["id"], "sub-author")

    def test_source_group_can_return_multiple_loaded_pages_in_one_payload(self):
        groups = {
            "local-organizer": {
                "key": "local-organizer",
                "kind": "local",
                "route_kind": "source",
                "model_dirs": ["local-1", "local-2", "local-3"],
            }
        }
        all_models = []
        for name in ("local-1", "local-2", "local-3"):
            item = _model(name, source="local")
            item["local_flags"]["deleted"] = False
            all_models.append(item)

        with patch("app.services.source_library._group_models", return_value=(groups, all_models, [])):
            payload = build_source_group_models_payload(
                "local",
                "local-organizer",
                page=1,
                page_size=16,
            )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["filtered_total"], 3)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(payload["items"]), 3)
        self.assertEqual({item["model_dir"] for item in payload["items"]}, {"local-1", "local-2", "local-3"})

    def test_favorite_source_group_preserves_subscription_source_order_by_default(self):
        groups = {
            "favorite-cn-test": {
                "key": "favorite-cn-test",
                "kind": "favorite",
                "route_kind": "source",
                "model_dirs": ["newer-favorite", "older-favorite"],
            }
        }
        newer = _model("newer-favorite", source="cn")
        newer["local_flags"]["deleted"] = False
        newer["collect_ts"] = 1
        older = _model("older-favorite", source="cn")
        older["local_flags"]["deleted"] = False
        older["collect_ts"] = 999

        with patch("app.services.source_library._group_models", return_value=(groups, [newer, older], [])):
            payload = build_source_group_models_payload(
                "favorite",
                "favorite-cn-test",
                sort_key="collectDate",
            )

        self.assertIsNotNone(payload)
        self.assertEqual([item["model_dir"] for item in payload["items"]], ["newer-favorite", "older-favorite"])
        self.assertEqual(payload["filters"]["sort"], "collectDate")

    def test_favorite_group_model_count_uses_current_source_total(self):
        subscription = SubscriptionRecord(
            id="sub-favorite",
            name="艾斯收藏夹",
            url="https://makerworld.com/zh/@s450586793/collections/models",
            mode="collection_models",
        )
        config = type("ConfigStub", (), {"subscriptions": [subscription]})()
        current_items = [
            {
                "task_key": f"model:{index}",
                "model_id": str(index),
                "url": f"https://makerworld.com/zh/models/{index}",
            }
            for index in range(17)
        ]
        visible_models = []
        for index in range(21):
            model = _model(f"model-{index}", source="global")
            model["id"] = str(index)
            model["origin_url"] = f"https://makerworld.com/zh/models/{index}"
            model["local_flags"]["deleted"] = False
            visible_models.append(model)

        task_store = type(
            "TaskStoreStub",
            (),
            {
                "load_subscriptions_state": lambda self: {
                    "items": [
                        {
                            "id": "sub-favorite",
                            "current_items": current_items,
                            "tracked_items": [
                                {
                                    "task_key": f"model:{index}",
                                    "model_id": str(index),
                                    "url": f"https://makerworld.com/zh/models/{index}",
                                }
                                for index in range(21)
                            ],
                            "last_discovered_count": 21,
                        }
                    ]
                }
            },
        )()
        store = type("StoreStub", (), {"load": lambda self: config})()

        with patch("app.services.source_library._load_models", return_value=(visible_models, visible_models)), \
                patch("app.services.source_library.load_source_metadata_cache", return_value={"items": {}}), \
                patch("app.services.source_library._group_cache_signature", return_value=("favorite-current-count",)):
            groups, _all_models, sections = _group_models(store=store, task_store=task_store)

        group = next(
            item
            for section in sections
            for item in section["items"]
            if item.get("subscription_id") == "sub-favorite"
        )
        self.assertEqual(group["remote_model_count"], 17)
        self.assertEqual(group["local_model_count"], 17)
        self.assertEqual(group["model_count"], 17)
        self.assertEqual(groups[group["key"]]["model_count"], 17)

    def test_subscription_source_count_overrides_stale_metadata_count(self):
        subscription = SubscriptionRecord(
            id="sub-favorite",
            name="艾斯收藏夹",
            url="https://makerworld.com.cn/zh/@s450586793/collections/models",
            mode="collection_models",
        )
        config = type("ConfigStub", (), {"subscriptions": [subscription]})()
        current_items = [
            {
                "task_key": f"model:{index}",
                "model_id": str(index),
                "url": f"https://makerworld.com.cn/zh/models/{index}",
            }
            for index in range(310)
        ]
        visible_models = []
        for index in range(4):
            model = _model(f"model-{index}", source="cn")
            model["id"] = str(index)
            model["origin_url"] = f"https://makerworld.com.cn/zh/models/{index}"
            model["local_flags"]["deleted"] = False
            visible_models.append(model)

        task_store = type(
            "TaskStoreStub",
            (),
            {
                "load_subscriptions_state": lambda self: {
                    "items": [
                        {
                            "id": "sub-favorite",
                            "current_items": current_items,
                            "tracked_items": current_items,
                            "last_discovered_count": 310,
                        }
                    ]
                }
            },
        )()
        store = type("StoreStub", (), {"load": lambda self: config})()
        source_key = _source_key("collection", "cn", subscription.url)

        with patch("app.services.source_library._load_models", return_value=(visible_models, visible_models)), \
                patch(
                    "app.services.source_library.load_source_metadata_cache",
                    return_value={"items": {source_key: {"remote_model_count": 302}}},
                ), \
                patch("app.services.source_library._group_cache_signature", return_value=("favorite-stale-metadata",)):
            groups, _all_models, sections = _group_models(store=store, task_store=task_store)

        group = next(
            item
            for section in sections
            for item in section["items"]
            if item.get("subscription_id") == "sub-favorite"
        )
        self.assertEqual(group["remote_model_count"], 310)
        self.assertEqual(group["model_count"], 310)
        self.assertEqual(groups[group["key"]]["model_count"], 310)

    def test_default_favorite_group_uses_account_avatar_metadata(self):
        _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})
        subscription = SubscriptionRecord(
            id="sub-favorite",
            name="国际 艾斯 所有模型收藏夹",
            url="https://makerworld.com/zh/@s450586793/collections/models",
            mode="collection_models",
        )
        config = type("ConfigStub", (), {"subscriptions": [subscription]})()
        source_key = _source_key("favorite", "global", subscription.url)
        task_store = type(
            "TaskStoreStub",
            (),
            {
                "load_subscriptions_state": lambda self: {
                    "items": [
                        {
                            "id": "sub-favorite",
                            "current_items": [],
                            "tracked_items": [],
                            "last_discovered_count": 17,
                        }
                    ]
                }
            },
        )()
        store = type("StoreStub", (), {"load": lambda self: config})()

        try:
            with patch("app.services.source_library._load_models", return_value=([], [])), \
                    patch(
                        "app.services.source_library.load_source_metadata_cache",
                        return_value={"items": {source_key: {"avatar_url": "https://example.test/account.jpg", "cover_url": "https://example.test/model.jpg"}}},
                    ), \
                    patch("app.services.source_library._group_cache_signature", return_value=("favorite-avatar",)):
                groups, _all_models, sections = _group_models(store=store, task_store=task_store)

            group = next(
                item
                for section in sections
                for item in section["items"]
                if item.get("subscription_id") == "sub-favorite"
            )
            self.assertEqual(group["kind"], "favorite")
            self.assertEqual(group["avatar_url"], "https://example.test/account.jpg")
            self.assertEqual(group["cover_url"], "https://example.test/model.jpg")
            self.assertEqual(groups[group["key"]]["avatar_url"], "https://example.test/account.jpg")
        finally:
            _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})

    def test_source_metadata_keeps_existing_avatar_when_refresh_payload_lacks_one(self):
        _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})
        state = {}
        with patch("app.services.source_library.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.source_library.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            from app.services import source_library

            source_library._save_source_metadata_item("favorite-global-test", {"avatar_url": "https://example.test/account.jpg"})
            source_library._save_source_metadata_item("favorite-global-test", {"avatar_url": "", "cover_url": "https://example.test/model.jpg"})
            item = source_library.load_source_metadata_cache()["items"]["favorite-global-test"]

        self.assertEqual(item["avatar_url"], "https://example.test/account.jpg")
        self.assertEqual(item["cover_url"], "https://example.test/model.jpg")

    def test_group_models_reuses_cached_payload_for_same_signature(self):
        _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})
        local_model = _model("local-1", source="local")
        local_model["local_flags"]["deleted"] = False
        try:
            with patch("app.services.source_library._group_cache_signature", return_value=("same",)), \
                    patch("app.services.source_library._load_models", return_value=([local_model], [local_model])) as load_models, \
                    patch("app.services.source_library._group_subscription_sources", return_value=([], [], [])), \
                    patch("app.services.source_library.load_source_metadata_cache", return_value={"items": {}}):
                first_groups, first_models, _ = _group_models()
                second_groups, second_models, _ = _group_models()

            self.assertEqual(load_models.call_count, 1)
            self.assertIn("local-organizer", first_groups)
            self.assertIn("local-organizer", second_groups)
            self.assertEqual(first_models[0]["model_dir"], second_models[0]["model_dir"])
        finally:
            _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})

    def test_source_library_payload_uses_cache_for_empty_query(self):
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "source_library_payload_cache.json"
            built_payload = {
                "sections": [],
                "count": 0,
                "filters": {"q": ""},
                "summary": {"card_count": 0, "model_count": 0},
                "cache": {"stale": False, "refreshing": False, "updated_at": "now"},
            }

            with patch("app.services.source_library.SOURCE_LIBRARY_PAYLOAD_CACHE_PATH", cache_path), \
                    patch("app.services.source_library._source_library_payload_signature", return_value=("same",)), \
                    patch("app.services.source_library._build_source_library_payload_uncached", return_value=built_payload) as build_uncached:
                first = build_source_library_payload()
                second = build_source_library_payload()

        self.assertEqual(build_uncached.call_count, 1)
        self.assertEqual(first["summary"]["model_count"], 0)
        self.assertEqual(second["summary"]["model_count"], 0)
        self.assertFalse(second["cache"]["stale"])

    def test_source_library_payload_returns_stale_cache_and_schedules_refresh(self):
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "source_library_payload_cache.json"
            old_payload = {
                "sections": [{"key": "favorites", "label": "收藏夹", "count": 1, "items": [{"title": "旧缓存"}]}],
                "count": 1,
                "filters": {"q": ""},
                "summary": {"card_count": 1, "model_count": 1},
            }

            try:
                _SOURCE_LIBRARY_PAYLOAD_REFRESH_STATE.update({"running": False, "last_started": 0.0})
                with patch("app.services.source_library.SOURCE_LIBRARY_PAYLOAD_CACHE_PATH", cache_path), \
                        patch("app.services.source_library._source_library_payload_signature", return_value=("new",)), \
                        patch("app.services.source_library._schedule_source_library_payload_refresh") as schedule_refresh:
                    from app.services import source_library

                    source_library._write_source_library_payload_cache(old_payload, ("old",))
                    payload = build_source_library_payload()
            finally:
                _SOURCE_LIBRARY_PAYLOAD_REFRESH_STATE.update({"running": False, "last_started": 0.0})

        self.assertEqual(payload["sections"][0]["items"][0]["title"], "旧缓存")
        self.assertTrue(payload["cache"]["stale"])
        schedule_refresh.assert_called_once()

    def test_group_models_exposes_state_preview_snapshot_metadata(self):
        _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})
        group = _base_group(
            key="local_favorite",
            kind="local_favorite",
            card_kind="collection",
            title="本地收藏",
            subtitle="已在 MakerHub 标记收藏",
            site="local",
            route_kind="state",
        )
        signature = _source_preview_snapshot_signature(group, [])

        try:
            with TemporaryDirectory() as temp_dir:
                snapshot_dir = Path(temp_dir)
                snapshot_path = snapshot_dir / f"local-favorite-{signature[:12]}.webp"
                Image.new("RGB", (8, 8), "white").save(snapshot_path, "WEBP")
                metadata = {
                    "items": {
                        "local_favorite": {
                            "preview_snapshot_signature": signature,
                            "preview_snapshot_filename": snapshot_path.name,
                            "preview_snapshot_had_image": False,
                        }
                    }
                }

                with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir), \
                        patch("app.services.source_library._group_cache_signature", return_value=("state-preview-metadata",)), \
                        patch("app.services.source_library._load_models", return_value=([], [])), \
                        patch("app.services.source_library._group_subscription_sources", return_value=([], [], [])), \
                        patch("app.services.source_library.load_source_metadata_cache", return_value=metadata):
                    groups, _all_models, sections = _group_models()

            state_group = groups["local_favorite"]
            section_group = next(
                item
                for section in sections
                if section.get("key") == "states"
                for item in section.get("items") or []
                if item.get("key") == "local_favorite"
            )
            self.assertIn("preview_snapshot_url", state_group)
            self.assertIn(snapshot_path.name, state_group["preview_snapshot_url"])
            self.assertEqual(section_group["preview_snapshot_url"], state_group["preview_snapshot_url"])
        finally:
            _SOURCE_LIBRARY_GROUP_CACHE.update({"signature": None, "groups": {}, "all_models": (), "sections": ()})

    def test_finalize_group_exposes_matching_preview_snapshot(self):
        group = _base_group(
            key="author-cn-test",
            kind="author",
            card_kind="author",
            title="Ace",
            subtitle="@ace",
            site="cn",
        )
        group["model_dirs"] = ["model-1"]
        model = _model("model-1", source="cn")
        model["local_flags"]["deleted"] = False
        model["cover_url"] = "/archive/model-1/cover.webp"
        previews = [{"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/cover.webp"}]
        signature = _source_preview_snapshot_signature(group, previews)

        with TemporaryDirectory() as temp_dir:
            snapshot_dir = Path(temp_dir)
            snapshot_path = snapshot_dir / f"author-cn-test-{signature[:12]}.webp"
            Image.new("RGB", (8, 8), "white").save(snapshot_path, "WEBP")
            with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir):
                finalized = _finalize_group(
                    group,
                    {"model-1": model},
                    {
                        "preview_snapshot_signature": signature,
                        "preview_snapshot_filename": snapshot_path.name,
                    },
                )

        self.assertIn("preview_snapshot_url", finalized)
        self.assertIn(snapshot_path.name, finalized["preview_snapshot_url"])

    def test_finalize_group_ignores_placeholder_only_preview_snapshot(self):
        group = _base_group(
            key="author-cn-test",
            kind="author",
            card_kind="author",
            title="Ace",
            subtitle="@ace",
            site="cn",
        )
        group["model_dirs"] = ["model-1"]
        model = _model("model-1", source="cn")
        model["local_flags"]["deleted"] = False
        model["cover_url"] = "/archive/model-1/missing.webp"
        previews = [{"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/missing.webp"}]
        signature = _source_preview_snapshot_signature(group, previews)

        with TemporaryDirectory() as temp_dir:
            snapshot_dir = Path(temp_dir)
            snapshot_path = snapshot_dir / f"author-cn-test-{signature[:12]}.webp"
            Image.new("RGB", (8, 8), "white").save(snapshot_path, "WEBP")
            with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir):
                finalized = _finalize_group(
                    group,
                    {"model-1": model},
                    {
                        "preview_snapshot_signature": signature,
                        "preview_snapshot_filename": snapshot_path.name,
                        "preview_snapshot_had_image": False,
                    },
                )

        self.assertNotIn("preview_snapshot_url", finalized)
        self.assertEqual(finalized["preview_models"][0]["cover_url"], "/archive/model-1/missing.webp")

    def test_finalize_state_group_exposes_placeholder_only_preview_snapshot(self):
        group = _base_group(
            key="local_favorite",
            kind="local_favorite",
            card_kind="collection",
            title="本地收藏",
            subtitle="已在 MakerHub 标记收藏",
            site="local",
            route_kind="state",
        )
        previews: list[dict] = []
        signature = _source_preview_snapshot_signature(group, previews)

        with TemporaryDirectory() as temp_dir:
            snapshot_dir = Path(temp_dir)
            snapshot_path = snapshot_dir / f"local-favorite-{signature[:12]}.webp"
            Image.new("RGB", (8, 8), "white").save(snapshot_path, "WEBP")
            with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir):
                finalized = _finalize_group(
                    group,
                    {},
                    {
                        "preview_snapshot_signature": signature,
                        "preview_snapshot_filename": snapshot_path.name,
                        "preview_snapshot_had_image": False,
                    },
                )

        self.assertIn("preview_snapshot_url", finalized)
        self.assertIn(snapshot_path.name, finalized["preview_snapshot_url"])

    def test_refresh_source_preview_snapshots_writes_metadata_and_image(self):
        group = _base_group(
            key="author-cn-test",
            kind="author",
            card_kind="author",
            title="Ace",
            subtitle="@ace",
            site="cn",
        )
        group["preview_models"] = [
            {"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/cover.webp"}
        ]

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_root = root / "archive"
            snapshot_dir = root / "snapshots"
            cover_path = archive_root / "model-1" / "cover.webp"
            cover_path.parent.mkdir(parents=True)
            Image.new("RGB", (24, 24), "red").save(cover_path, "WEBP")
            metadata: dict[str, dict] = {"items": {}}

            def save_metadata(source_key: str, payload: dict) -> None:
                metadata["items"].setdefault(source_key, {}).update(payload)

            with patch("app.services.source_library.ARCHIVE_DIR", archive_root), \
                    patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir), \
                    patch("app.services.source_library._group_models", return_value=({"author-cn-test": group}, [], [])), \
                    patch("app.services.source_library.load_source_metadata_cache", return_value=metadata), \
                    patch("app.services.source_library._save_source_snapshot_metadata", side_effect=save_metadata), \
                    patch("app.services.source_library.append_business_log"):
                result = refresh_source_preview_snapshots()

            item = metadata["items"].get("author-cn-test") or {}
            self.assertEqual(result["generated"], 1)
            self.assertTrue(item.get("preview_snapshot_filename", "").endswith(".webp"))
            self.assertTrue((snapshot_dir / item["preview_snapshot_filename"]).is_file())
            self.assertTrue(item.get("preview_snapshot_url", "").startswith("/api/source-library/snapshots/"))

    def test_refresh_source_preview_snapshots_can_limit_to_source_keys(self):
        first_group = _base_group(
            key="author-cn-first",
            kind="author",
            card_kind="author",
            title="First",
            subtitle="@first",
            site="cn",
        )
        first_group["preview_models"] = [
            {"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/cover.webp"}
        ]
        second_group = _base_group(
            key="author-cn-second",
            kind="author",
            card_kind="author",
            title="Second",
            subtitle="@second",
            site="cn",
        )
        second_group["preview_models"] = [
            {"model_dir": "model-2", "title": "model-2", "cover_url": "/archive/model-2/cover.webp"}
        ]

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_root = root / "archive"
            snapshot_dir = root / "snapshots"
            for model_dir in ("model-1", "model-2"):
                cover_path = archive_root / model_dir / "cover.webp"
                cover_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (24, 24), "red").save(cover_path, "WEBP")
            metadata: dict[str, dict] = {"items": {}}

            def save_metadata(source_key: str, payload: dict) -> None:
                metadata["items"].setdefault(source_key, {}).update(payload)

            with patch("app.services.source_library.ARCHIVE_DIR", archive_root), \
                    patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir), \
                    patch("app.services.source_library._group_models", return_value=({"author-cn-first": first_group, "author-cn-second": second_group}, [], [])), \
                    patch("app.services.source_library.load_source_metadata_cache", return_value=metadata), \
                    patch("app.services.source_library._save_source_snapshot_metadata", side_effect=save_metadata), \
                    patch("app.services.source_library.append_business_log"):
                result = refresh_source_preview_snapshots(source_keys={"author-cn-first"})

            self.assertEqual(result["total"], 1)
            self.assertEqual(result["generated"], 1)
            self.assertIn("author-cn-first", metadata["items"])
            self.assertNotIn("author-cn-second", metadata["items"])

    def test_refresh_source_preview_snapshots_includes_empty_state_groups(self):
        group = _base_group(
            key="local_favorite",
            kind="local_favorite",
            card_kind="collection",
            title="本地收藏",
            subtitle="已在 MakerHub 标记收藏",
            site="local",
            route_kind="state",
        )
        group["preview_models"] = []

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot_dir = root / "snapshots"
            metadata: dict[str, dict] = {"items": {}}

            def save_metadata(source_key: str, payload: dict) -> None:
                metadata["items"].setdefault(source_key, {}).update(payload)

            with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_DIR", snapshot_dir), \
                    patch("app.services.source_library._group_models", return_value=({"local_favorite": group}, [], [])), \
                    patch("app.services.source_library.load_source_metadata_cache", return_value=metadata), \
                    patch("app.services.source_library._save_source_snapshot_metadata", side_effect=save_metadata), \
                    patch("app.services.source_library.append_business_log"):
                result = refresh_source_preview_snapshots()

            item = metadata["items"].get("local_favorite") or {}
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["generated"], 1)
            self.assertFalse(item.get("preview_snapshot_had_image"))
            self.assertTrue(item.get("preview_snapshot_filename", "").endswith(".webp"))
            self.assertTrue((snapshot_dir / item["preview_snapshot_filename"]).is_file())

    def test_preview_snapshot_gap_is_transparent_for_dark_theme(self):
        group = _base_group(
            key="author-cn-test",
            kind="author",
            card_kind="author",
            title="Ace",
            subtitle="@ace",
            site="cn",
        )

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive_root = root / "archive"
            cover_path = archive_root / "model-1" / "cover.webp"
            snapshot_path = root / "snapshot.webp"
            cover_path.parent.mkdir(parents=True)
            Image.new("RGB", (24, 24), "red").save(cover_path, "WEBP")
            previews = [{"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/cover.webp"}]

            with patch("app.services.source_library.ARCHIVE_DIR", archive_root):
                self.assertTrue(_render_source_preview_snapshot(previews, snapshot_path))

            with Image.open(snapshot_path) as snapshot:
                alpha = snapshot.convert("RGBA").getpixel((240, 240))[3]

        self.assertLess(alpha, 16)

    def test_preview_snapshot_signature_includes_render_version(self):
        group = _base_group(
            key="author-cn-test",
            kind="author",
            card_kind="author",
            title="Ace",
            subtitle="@ace",
            site="cn",
        )
        previews = [{"model_dir": "model-1", "title": "model-1", "cover_url": "/archive/model-1/cover.webp"}]

        with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_RENDER_VERSION", 100):
            first = _source_preview_snapshot_signature(group, previews)
        with patch("app.services.source_library.SOURCE_LIBRARY_SNAPSHOT_RENDER_VERSION", 101):
            second = _source_preview_snapshot_signature(group, previews)

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
