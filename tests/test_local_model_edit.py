import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import catalog, local_model_edit


def _upload(filename: str, data: bytes, content_type: str = "application/octet-stream"):
    return SimpleNamespace(filename=filename, file=io.BytesIO(data), content_type=content_type)


class LocalModelEditTest(unittest.TestCase):
    def _write_local_model(self, root: Path) -> Path:
        model_root = root / "LOCAL_Test"
        (model_root / "instances").mkdir(parents=True)
        (model_root / "images").mkdir()
        (model_root / "instances" / "body.stl").write_bytes(b"solid body\nendsolid body\n")
        (model_root / "images" / "cover.jpg").write_bytes(b"fake-jpeg")
        meta = {
            "title": "Test",
            "source": "local",
            "cover": "images/cover.jpg",
            "designImages": [{"relPath": "images/cover.jpg"}],
            "summary": {"text": "old", "html": "<p>old</p>"},
            "stats": {"comments": 0},
            "comments": [],
            "attachments": [],
            "instances": [
                {
                    "id": "local-1",
                    "title": "body",
                    "fileName": "body.stl",
                    "fileKind": "STL",
                    "thumbnailLocal": "images/cover.jpg",
                    "pictures": [{"relPath": "images/cover.jpg"}],
                }
            ],
            "localImport": {
                "modelFileCount": 1,
            },
        }
        (model_root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return model_root

    def test_edit_description_and_add_delete_files_and_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = self._write_local_model(archive_root)
            with patch.object(local_model_edit, "ARCHIVE_DIR", archive_root), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                local_model_edit.update_local_model_description("LOCAL_Test", "1\n2\n\n3\n4")
                added_file = local_model_edit.add_local_model_file(
                    "LOCAL_Test",
                    _upload("extra.3mf", b"3mf-data"),
                )
                added_image = local_model_edit.add_local_model_image(
                    "LOCAL_Test",
                    _upload("side.png", b"png-data", content_type="image/png"),
                )
                detail = catalog.get_model_detail("LOCAL_Test")

                self.assertEqual(detail["summary_text"], "1\n2\n\n3\n4")
                self.assertIn("1<br", detail["summary_html"])
                self.assertIn("3<br", detail["summary_html"])
                self.assertEqual(len(detail["instances"]), 2)
                self.assertTrue((model_root / "instances" / "extra.3mf").exists())
                self.assertTrue((model_root / "images" / "side.png").exists())
                meta = json.loads((model_root / "meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["localImport"]["modelFileCount"], 2)

                local_model_edit.delete_local_model_file("LOCAL_Test", added_file["id"])
                local_model_edit.delete_local_model_image("LOCAL_Test", added_image["relPath"])
                detail = catalog.get_model_detail("LOCAL_Test")

            self.assertEqual(len(detail["instances"]), 1)
            self.assertEqual(len(detail["gallery"]), 1)
            self.assertFalse((model_root / "instances" / "extra.3mf").exists())
            self.assertFalse((model_root / "images" / "side.png").exists())

    def test_saved_description_html_newlines_render_after_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = self._write_local_model(archive_root)
            meta_path = model_root / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["summary"] = {
                "text": "1\n2\n3",
                "html": "<p>1\n2\n3</p>",
            }
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                detail = catalog.get_model_detail("LOCAL_Test")

            self.assertEqual(detail["summary_text"], "1\n2\n3")
            self.assertIn("1<br", detail["summary_html"])
            self.assertIn("2<br", detail["summary_html"])

    def test_update_metadata_changes_title_and_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = self._write_local_model(archive_root)

            with patch.object(local_model_edit, "ARCHIVE_DIR", archive_root), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_model_edit.update_local_model_metadata(
                    "LOCAL_Test",
                    title="新的标题",
                    description="一\n二",
                )
                detail = catalog.get_model_detail("LOCAL_Test")

            self.assertEqual(result["title"], "新的标题")
            self.assertEqual(detail["title"], "新的标题")
            self.assertEqual(detail["summary_text"], "一\n二")
            self.assertIn("一<br", detail["summary_html"])
            self.assertTrue(model_root.exists())
            meta = json.loads((model_root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["title"], "新的标题")

    def test_set_local_model_cover_image_reorders_gallery_and_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = self._write_local_model(archive_root)
            (model_root / "images" / "side.png").write_bytes(b"png-data")
            meta_path = model_root / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["designImages"].append({"relPath": "images/side.png"})
            meta["instances"][0]["pictures"].append({"relPath": "images/side.png"})
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            with patch.object(local_model_edit, "ARCHIVE_DIR", archive_root), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                updated = local_model_edit.set_local_model_cover_image("LOCAL_Test", "images/side.png")
                detail = catalog.get_model_detail("LOCAL_Test")

            self.assertEqual(updated["relPath"], "images/side.png")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["cover"], "images/side.png")
            self.assertEqual(meta["designImages"][0]["relPath"], "images/side.png")
            self.assertEqual(meta["instances"][0]["thumbnailLocal"], "images/side.png")
            self.assertEqual(meta["instances"][0]["pictures"][0]["relPath"], "images/side.png")
            self.assertTrue(detail["cover_url"].endswith("/LOCAL_Test/images/side.png"))
            self.assertTrue(detail["gallery"][0]["url"].endswith("/LOCAL_Test/images/side.png"))

    def test_rejects_non_local_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = archive_root / "MW_1_Test"
            model_root.mkdir()
            (model_root / "meta.json").write_text(
                json.dumps({"source": "mw_cn", "title": "MW"}),
                encoding="utf-8",
            )
            with patch.object(local_model_edit, "ARCHIVE_DIR", archive_root):
                with self.assertRaisesRegex(ValueError, "只有本地导入模型支持编辑"):
                    local_model_edit.update_local_model_description("MW_1_Test", "x")


if __name__ == "__main__":
    unittest.main()
