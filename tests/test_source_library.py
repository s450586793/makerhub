import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.services.catalog import _source_deleted_model_count
from app.services.source_library import (
    _SOURCE_LIBRARY_GROUP_CACHE,
    _base_group,
    _finalize_group,
    _group_models,
    _source_preview_snapshot_signature,
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


if __name__ == "__main__":
    unittest.main()
