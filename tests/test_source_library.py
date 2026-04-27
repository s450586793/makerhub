import unittest
from unittest.mock import patch

from app.services.catalog import _source_deleted_model_count
from app.services.source_library import build_state_group_models_payload


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

    def test_dashboard_source_deleted_count_uses_archived_models(self):
        items = [
            {"model_dir": "marked", "subscription_flags": {"deleted_on_source": True}},
            {"model_dir": "visible", "subscription_flags": {"deleted_on_source": False}},
            {"model_dir": "plain"},
        ]

        self.assertEqual(_source_deleted_model_count(items), 1)


if __name__ == "__main__":
    unittest.main()
