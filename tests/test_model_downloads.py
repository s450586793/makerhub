import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.api import config as config_api


class ModelDownloadArchiveTest(unittest.TestCase):
    def test_download_all_archive_includes_model_assets_without_generated_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve() / "archive"
            model_root = archive_root / "LOCAL_Demo"
            (model_root / "instances").mkdir(parents=True)
            (model_root / "images").mkdir()
            (model_root / "attachments").mkdir()
            (model_root / "file" / "manual").mkdir(parents=True)
            (model_root / "packages").mkdir()
            (model_root / "instances" / "body.stl").write_bytes(b"solid body\nendsolid body\n")
            (model_root / "images" / "cover.jpg").write_bytes(b"fake-jpg")
            (model_root / "attachments" / "guide.pdf").write_bytes(b"%PDF-1.7\n")
            (model_root / "file" / "manual" / "extra.txt").write_text("notes", encoding="utf-8")
            (model_root / "packages" / "Batch1.zip").write_bytes(b"nested-zip")
            (model_root / "meta.json").write_text(json.dumps({"title": "Demo"}, ensure_ascii=False), encoding="utf-8")

            cache_root = Path(tmp).resolve() / "state" / "downloads"
            with patch.object(config_api, "ARCHIVE_DIR", archive_root), \
                patch.object(config_api, "MODEL_DOWNLOAD_ALL_CACHE_DIR", cache_root):
                zip_path, download_name = config_api._create_model_download_all_archive("LOCAL_Demo")

            self.assertEqual(download_name, "Demo_所有文件.zip")
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(
                    sorted(archive.namelist()),
                    [
                        "attachments/guide.pdf",
                        "file/manual/extra.txt",
                        "images/cover.jpg",
                        "instances/body.stl",
                    ],
                )


if __name__ == "__main__":
    unittest.main()
