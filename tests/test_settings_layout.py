import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import settings


class SettingsLayoutTest(unittest.TestCase):
    def test_default_archive_dir_falls_back_to_parent_when_legacy_models_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            archive_root = data_root / "archive"
            model_root = data_root / "MW_Legacy"
            model_root.mkdir(parents=True)
            (model_root / "meta.json").write_text(
                json.dumps({"title": "Legacy"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True), \
                patch.object(settings, "DEFAULT_CONTAINER_ARCHIVE_DIR", archive_root):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", archive_root)

        self.assertEqual(resolved, data_root)

    def test_default_archive_dir_falls_back_to_parent_for_legacy_shared_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            archive_root = data_root / "archive"
            model_root = data_root / "local" / "shared" / "Shared_Model"
            model_root.mkdir(parents=True)
            (model_root / "meta.json").write_text(
                json.dumps({"title": "Shared"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True), \
                patch.object(settings, "DEFAULT_CONTAINER_ARCHIVE_DIR", archive_root):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", archive_root)

        self.assertEqual(resolved, data_root)

    def test_default_archive_dir_falls_back_to_parent_for_legacy_local_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            archive_root = data_root / "archive"
            local_model_root = data_root / "local" / "LOCAL_Pending_Model"
            local_model_root.mkdir(parents=True)
            (local_model_root / "meta.json").write_text(
                json.dumps({"title": "Pending", "source": "local"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True), \
                patch.object(settings, "DEFAULT_CONTAINER_ARCHIVE_DIR", archive_root):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", archive_root)

        self.assertEqual(resolved, data_root)

    def test_default_archive_dir_prefers_archive_child_when_new_layout_has_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            legacy_model_root = data_root / "MW_Legacy"
            archive_model_root = data_root / "archive" / "MW_New"
            legacy_model_root.mkdir(parents=True)
            archive_model_root.mkdir(parents=True)
            (legacy_model_root / "meta.json").write_text(
                json.dumps({"title": "Legacy"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (archive_model_root / "meta.json").write_text(
                json.dumps({"title": "New"}, ensure_ascii=False),
                encoding="utf-8",
            )
            archive_root = data_root / "archive"

            with patch.dict("os.environ", {}, clear=True), \
                patch.object(settings, "DEFAULT_CONTAINER_ARCHIVE_DIR", archive_root):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", archive_root)

        self.assertEqual(resolved, archive_root)

    def test_custom_archive_dir_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom_root = Path(tmp) / "custom"
            fallback_root = Path(tmp) / "data" / "archive"
            with patch.dict("os.environ", {"MAKERHUB_ARCHIVE_DIR": str(custom_root)}):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", fallback_root)

        self.assertEqual(resolved, custom_root)


if __name__ == "__main__":
    unittest.main()
