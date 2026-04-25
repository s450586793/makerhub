import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services import model_attachments


class ManualAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.original_archive_dir = model_attachments.ARCHIVE_DIR
        self.original_max_bytes = model_attachments.MAX_MANUAL_ATTACHMENT_BYTES

    def tearDown(self):
        model_attachments.ARCHIVE_DIR = self.original_archive_dir
        model_attachments.MAX_MANUAL_ATTACHMENT_BYTES = self.original_max_bytes

    def _prepare_model(self, root: Path) -> str:
        model_dir = "sample-model"
        model_root = root / model_dir
        model_root.mkdir(parents=True)
        (model_root / "meta.json").write_text(json.dumps({"title": "Sample"}), encoding="utf-8")
        return model_dir

    def test_create_manual_attachment_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_attachments.ARCHIVE_DIR = root
            model_attachments.MAX_MANUAL_ATTACHMENT_BYTES = 16
            model_dir = self._prepare_model(root)
            upload = SimpleNamespace(
                filename="guide.pdf",
                content_type="application/pdf",
                file=io.BytesIO(b"manual"),
            )

            item = model_attachments.create_manual_attachment(model_dir, upload, name="", category="guide")

            self.assertEqual(item["name"], "guide.pdf")
            self.assertEqual(item["size"], 6)
            self.assertTrue((root / model_dir / item["relPath"]).exists())

    def test_create_manual_attachment_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_attachments.ARCHIVE_DIR = root
            model_attachments.MAX_MANUAL_ATTACHMENT_BYTES = 3
            model_dir = self._prepare_model(root)
            upload = SimpleNamespace(
                filename="large.bin",
                content_type="application/octet-stream",
                file=io.BytesIO(b"1234"),
            )

            with self.assertRaisesRegex(ValueError, "上传文件过大"):
                model_attachments.create_manual_attachment(model_dir, upload, name="", category="other")

            manual_dir = root / model_dir / model_attachments.MANUAL_ATTACHMENTS_RELATIVE_DIR
            self.assertFalse(any(manual_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
