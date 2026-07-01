import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import settings


class SettingsLayoutTest(unittest.TestCase):
    def test_settings_page_does_not_show_legacy_profile_backfill_panel(self):
        source = (settings.ROOT_DIR / "frontend" / "src" / "pages" / "SettingsPage.vue").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("数据库索引与历史信息补全", source)
        self.assertNotIn("历史缺失信息补全入口", source)
        self.assertNotIn("/api/admin/archive/profile-backfill", source)

    def test_default_archive_dir_uses_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            model_root = data_root / "MW_Legacy"
            model_root.mkdir(parents=True)
            (model_root / "meta.json").write_text(
                json.dumps({"title": "Legacy"}, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", data_root)

        self.assertEqual(resolved, data_root)

    def test_legacy_container_archive_env_is_normalized_to_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = (Path(tmp) / "data").resolve()
            legacy_archive_root = data_root / "archive"

            with patch.object(settings, "DEFAULT_CONTAINER_DATA_DIR", data_root), \
                patch.object(settings, "LEGACY_CONTAINER_ARCHIVE_DIR", legacy_archive_root), \
                patch.dict("os.environ", {"MAKERHUB_ARCHIVE_DIR": legacy_archive_root.as_posix()}):
                resolved = settings._resolve_archive_dir("MAKERHUB_ARCHIVE_DIR", data_root)

        self.assertEqual(resolved, data_root)

    def test_default_archive_dir_can_still_use_custom_archive_child_for_local_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            archive_root = data_root / "archive"

            with patch.dict("os.environ", {}, clear=True):
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
