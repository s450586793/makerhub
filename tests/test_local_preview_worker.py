import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import catalog, local_preview_worker


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"preview"


def _write_model(root: Path, *, cover: str = "", legacy_cover: bool = False) -> Path:
    model_root = root / "LOCAL_Cube"
    (model_root / "instances").mkdir(parents=True)
    (model_root / "images").mkdir()
    (model_root / "instances" / "cube.stl").write_bytes(
        b"solid cube\n"
        b"facet normal 0 0 1\nouter loop\n"
        b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
        b"endloop\nendfacet\nendsolid cube\n"
    )
    design_images = []
    if legacy_cover:
        (model_root / "images" / "stl_preview_cube.svg").write_text("<svg></svg>", encoding="utf-8")
        cover = "images/stl_preview_cube.svg"
        design_images = [{"relPath": cover, "kind": "generated_stl_preview", "generated": True}]
    elif cover:
        (model_root / cover).parent.mkdir(parents=True, exist_ok=True)
        (model_root / cover).write_bytes(b"cover")
        design_images = [{"relPath": cover}]

    meta = {
        "title": "Cube",
        "source": "local",
        "cover": cover,
        "designImages": design_images,
        "summary": {"text": "", "html": ""},
        "stats": {"comments": 0},
        "comments": [],
        "attachments": [],
        "instances": [
            {
                "id": "local-1",
                "title": "cube",
                "fileName": "cube.stl",
                "fileKind": "STL",
                "thumbnailLocal": cover,
                "pictures": design_images,
            }
        ],
        "localImport": {
            "previewGenerator": "three",
            "previewVersion": 2,
            "previewStatus": "pending",
            "previewNeedsGeneration": True,
            "previewSourceFileName": "cube.stl",
            "modelFileCount": 1,
        },
    }
    (model_root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return model_root


class LocalPreviewWorkerTest(unittest.TestCase):
    def test_worker_generates_three_preview_and_replaces_legacy_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = _write_model(archive_root, legacy_cover=True)

            with patch.object(local_preview_worker, "ARCHIVE_DIR", archive_root), \
                patch.object(local_preview_worker, "LOCAL_PREVIEW_MAX_BYTES", 24 * 1024 * 1024), \
                patch.object(local_preview_worker, "_render_preview_png", return_value=PNG_BYTES), \
                patch.object(local_preview_worker, "append_business_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_preview_worker.run_local_preview_generation_once()
                detail = catalog.get_model_detail("LOCAL_Cube")

            self.assertTrue(result["processed"])
            self.assertEqual(result["status"], "success")
            meta = json.loads((model_root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["previewStatus"], "success")
            self.assertFalse(meta["localImport"]["previewNeedsGeneration"])
            self.assertTrue(meta["cover"].startswith("images/three_preview_cube"))
            self.assertFalse((model_root / "images" / "stl_preview_cube.svg").exists())
            self.assertTrue((model_root / meta["cover"]).exists())
            self.assertEqual(meta["designImages"][0]["kind"], "generated_three_preview")
            self.assertEqual(detail["local_preview"]["status"], "success")

    def test_worker_skips_too_large_pending_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = _write_model(archive_root)

            with patch.object(local_preview_worker, "ARCHIVE_DIR", archive_root), \
                patch.object(local_preview_worker, "LOCAL_PREVIEW_MAX_BYTES", 4), \
                patch.object(local_preview_worker, "append_business_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_preview_worker.run_local_preview_generation_once()

            self.assertEqual(result["status"], "too_large")
            meta = json.loads((model_root / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["previewStatus"], "too_large")
            self.assertFalse(meta["localImport"]["previewNeedsGeneration"])
            self.assertEqual(meta["cover"], "")

    def test_worker_does_not_scan_unqueued_history_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = _write_model(archive_root)
            meta_path = model_root / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["localImport"] = {"modelFileCount": 1}
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            with patch.object(local_preview_worker, "ARCHIVE_DIR", archive_root), \
                patch.object(local_preview_worker, "_render_preview_png") as render_mock:
                result = local_preview_worker.run_local_preview_generation_once()

            self.assertFalse(result["processed"])
            render_mock.assert_not_called()

    def test_opening_detail_marks_history_model_for_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            model_root = _write_model(archive_root)
            meta_path = model_root / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["localImport"] = {"modelFileCount": 1}
            meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root):
                detail = catalog.get_model_detail("LOCAL_Cube")

            self.assertIsNotNone(detail)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["previewStatus"], "pending")
            self.assertTrue(meta["localImport"]["previewNeedsGeneration"])
            self.assertTrue(detail["local_preview"]["needs_generation"])


if __name__ == "__main__":
    unittest.main()
