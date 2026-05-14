import unittest
from unittest.mock import patch

from app.services.catalog import _source_deleted_model_count
from app.services.source_library import (
    _SOURCE_LIBRARY_GROUP_CACHE,
    _group_models,
    build_source_group_models_payload,
    build_state_group_models_payload,
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


if __name__ == "__main__":
    unittest.main()
