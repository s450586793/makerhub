import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import catalog, local_import_upload


class FakeTaskStore:
    def __init__(self):
        self.payload = {"items": []}

    def load_organize_tasks(self):
        return dict(self.payload)

    def save_organize_tasks(self, payload):
        self.payload = dict(payload)
        return self.payload

    def upsert_organize_task(self, item, limit=50):
        items = [item, *list(self.payload.get("items") or [])]
        self.payload = {**self.payload, "items": items[:limit], "count": len(items)}
        return self.payload


def _upload(filename: str, data: bytes):
    return SimpleNamespace(filename=filename, file=io.BytesIO(data))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class LocalImportUploadTest(unittest.TestCase):
    def test_zip_import_classifies_assets_and_dedupes_stl_by_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            duplicate_stl = b"solid same\nendsolid same\n"
            package = _zip_bytes(
                {
                    "Mai/body.stl": duplicate_stl,
                    "Mai/duplicates/body-copy.stl": duplicate_stl,
                    "Mai/hair.stl": b"solid hair\nendsolid hair\n",
                    "Mai/cover.jpg": b"fake-jpeg",
                    "Mai/readme.txt": "这是模型说明".encode("utf-8"),
                    "Mai/manual.pdf": b"%PDF-1.7\n",
                }
            )
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )
            task_store = FakeTaskStore()

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.upload_local_import_files(
                    files=[_upload("Mai.zip", package)],
                    paths=["Mai.zip"],
                    store=store,
                    task_store=task_store,
                )
                detail = catalog.get_model_detail(result["model_dir"])

            self.assertEqual(result["mode"], "package")
            self.assertEqual(result["model_file_count"], 2)
            self.assertEqual(result["duplicate_file_count"], 1)
            self.assertIsNotNone(detail)
            self.assertEqual(len(detail["instances"]), 2)
            self.assertEqual([item["file_kind"] for item in detail["instances"]], ["STL", "STL"])
            self.assertEqual(detail["summary_text"], "这是模型说明")
            self.assertEqual(len(detail["attachments"]), 1)
            self.assertEqual(detail["attachments"][0]["ext"], "pdf")
            self.assertTrue(detail["attachments"][0]["url"].endswith("/attachments/manual.pdf"))

            meta_path = archive_root / result["model_dir"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["duplicateFileCount"], 1)

    def test_direct_3mf_mixed_with_other_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "3MF 请单独导入"):
            local_import_upload.upload_local_import_files(
                files=[
                    _upload("profile.3mf", b"3mf"),
                    _upload("part.stl", b"solid part"),
                ],
                paths=["profile.3mf", "part.stl"],
                store=SimpleNamespace(),
                task_store=FakeTaskStore(),
            )


if __name__ == "__main__":
    unittest.main()
