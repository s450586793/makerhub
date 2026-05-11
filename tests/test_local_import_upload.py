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
        target_id = item.get("id") or item.get("fingerprint") or item.get("source_path")
        items = []
        replaced = False
        for existing in list(self.payload.get("items") or []):
            current_id = existing.get("id") or existing.get("fingerprint") or existing.get("source_path")
            if current_id == target_id:
                items.append(item)
                replaced = True
            else:
                items.append(existing)
        if not replaced:
            items.insert(0, item)
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


def _fake_rar_extractor(files_by_archive: dict[str, dict[str, bytes]]):
    def _extract(rar_path: Path, destination: Path) -> None:
        files = files_by_archive.get(Path(rar_path).name)
        if files is None:
            raise RuntimeError("fake rar unreadable")
        destination.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    return _extract


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
                    "Mai/bom.xlsm": b"macro workbook",
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
            self.assertEqual(len(detail["attachments"]), 2)
            attachment_by_ext = {item["ext"]: item for item in detail["attachments"]}
            self.assertTrue(attachment_by_ext["pdf"]["url"].endswith("/attachments/manual.pdf"))
            self.assertTrue(attachment_by_ext["xlsm"]["url"].endswith("/attachments/bom.xlsm"))

            meta_path = archive_root / result["model_dir"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["duplicateFileCount"], 1)
            self.assertEqual(len(task_store.payload["items"]), 1)
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["title"], "Mai")
            self.assertEqual(task_item["status"], "success")
            self.assertEqual(task_item["progress"], 100)
            self.assertEqual(task_item["message"], "本地模型包已导入。")

    def test_nested_zip_import_skips_unreadable_child_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            good_child = _zip_bytes({"engine/part.stl": b"solid engine\nendsolid engine\n"})
            package = _zip_bytes(
                {
                    "readme.txt": "发动机模型合集".encode("utf-8"),
                    "good.zip": good_child,
                    "bad.zip": b"not a zip",
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
                    files=[_upload("Engines.zip", package)],
                    paths=["Engines.zip"],
                    store=store,
                    task_store=task_store,
                )
                detail = catalog.get_model_detail(result["model_dir"])

            self.assertEqual(result["mode"], "package")
            self.assertEqual(result["model_file_count"], 1)
            self.assertEqual(result["skipped_zip_count"], 1)
            self.assertIsNotNone(detail)
            self.assertEqual(len(detail["instances"]), 1)

            meta_path = archive_root / result["model_dir"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["skippedArchiveCount"], 1)
            self.assertEqual(meta["localImport"]["skippedArchives"][0]["file_name"], "bad.zip")
            self.assertIn("ZIP 文件无法读取", meta["localImport"]["skippedArchives"][0]["reason"])

    def test_rar_import_classifies_extracted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )
            task_store = FakeTaskStore()
            fake_extract = _fake_rar_extractor(
                {
                    "Luffy.rar": {
                        "Luffy/body.stl": b"solid body\nendsolid body\n",
                        "Luffy/cover.png": b"fake-png",
                        "Luffy/readme.txt": "路飞模型".encode("utf-8"),
                    }
                }
            )

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "_extract_rar_with_bsdtar", fake_extract), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.upload_local_import_files(
                    files=[_upload("Luffy.rar", b"fake-rar")],
                    paths=["Luffy.rar"],
                    store=store,
                    task_store=task_store,
                )
                detail = catalog.get_model_detail(result["model_dir"])

            self.assertEqual(result["mode"], "package")
            self.assertEqual(result["model_file_count"], 1)
            self.assertEqual(result["image_count"], 1)
            self.assertIsNotNone(detail)
            self.assertEqual(detail["title"], "Luffy")
            self.assertEqual(detail["summary_text"], "路飞模型")
            self.assertEqual(detail["instances"][0]["file_kind"], "STL")

    def test_nested_rar_import_skips_unreadable_child_rar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            package = _zip_bytes(
                {
                    "readme.txt": "合集".encode("utf-8"),
                    "good.rar": b"fake good rar",
                    "bad.rar": b"fake bad rar",
                }
            )
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )
            task_store = FakeTaskStore()
            fake_extract = _fake_rar_extractor(
                {
                    "good.rar": {
                        "engine/part.stl": b"solid engine\nendsolid engine\n",
                    }
                }
            )

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "_extract_rar_with_bsdtar", fake_extract), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.upload_local_import_files(
                    files=[_upload("Engines.zip", package)],
                    paths=["Engines.zip"],
                    store=store,
                    task_store=task_store,
                )

            self.assertEqual(result["mode"], "package")
            self.assertEqual(result["model_file_count"], 1)
            self.assertEqual(result["skipped_zip_count"], 1)
            meta_path = archive_root / result["model_dir"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["localImport"]["skippedArchiveCount"], 1)
            self.assertEqual(meta["localImport"]["skippedArchives"][0]["file_name"], "bad.rar")
            self.assertIn("RAR 文件无法读取", meta["localImport"]["skippedArchives"][0]["reason"])

    def test_package_import_skips_when_model_file_already_exists_in_local_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )
            task_store = FakeTaskStore()

            existing_stl = b"solid leg\nendsolid leg\n"
            digest_path = root / "digest.stl"
            digest_path.write_bytes(existing_stl)
            digest = local_import_upload._sha256_file(digest_path)
            existing_root = archive_root / "LOCAL_leg_table"
            (existing_root / "instances").mkdir(parents=True)
            (existing_root / "instances" / "leg_table.stl").write_bytes(existing_stl)
            (existing_root / "meta.json").write_text(
                json.dumps(
                    {
                        "title": "leg_table",
                        "source": "local",
                        "instances": [
                            {
                                "title": "leg_table",
                                "fileName": "leg_table.stl",
                                "localImport": {
                                    "fileHash": digest,
                                    "configFingerprint": f"sha256:{digest}",
                                },
                            }
                        ],
                        "localImport": {
                            "modelFileCount": 1,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.upload_local_import_files(
                    files=[_upload("leg_table.stl", existing_stl)],
                    paths=["leg_table.stl"],
                    store=store,
                    task_store=task_store,
                )

            self.assertFalse(result["success"])
            self.assertTrue(result["duplicate"])
            self.assertEqual(result["duplicate_count"], 1)
            self.assertIn("本地库已存在相同模型文件", result["message"])
            self.assertFalse((archive_root / "LOCAL_leg_table_2").exists())
            self.assertEqual(len(list(archive_root.glob("LOCAL_leg_table*"))), 1)
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "skipped")
            self.assertEqual(task_item["model_dir"], "LOCAL_leg_table")

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

    def test_empty_upload_reports_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"):
                with self.assertRaisesRegex(ValueError, "上传文件为空：empty.stl"):
                    local_import_upload.upload_local_import_files(
                        files=[_upload("empty.stl", b"")],
                        paths=["empty.stl"],
                        store=store,
                        task_store=FakeTaskStore(),
                    )


if __name__ == "__main__":
    unittest.main()
