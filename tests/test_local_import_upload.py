import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import catalog, local_import_upload, local_organizer


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

    def _update_organize_tasks(self, updater):
        updated = updater(dict(self.payload))
        if updated is not None:
            self.payload = dict(updated)
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


def _queued_package_task(staging_dir: Path, title: str, source_dir: Path, archive_root: Path, package_source: str) -> dict:
    return {
        "id": local_import_upload._package_task_id(staging_dir),
        "title": title,
        "package_title": title,
        "source_dir": source_dir.as_posix(),
        "target_dir": archive_root.as_posix(),
        "source_path": package_source,
        "staging_dir": staging_dir.as_posix(),
        "package_source": package_source,
        "kind": "local_package_import",
        "status": "queued",
    }


def _queued_local_source_package_task(
    staging_dir: Path,
    title: str,
    source_dir: Path,
    archive_root: Path,
    source_path: Path,
) -> dict:
    task = _queued_package_task(staging_dir, title, source_dir, archive_root, source_path.as_posix())
    task["source_path"] = source_path.as_posix()
    task["package_source"] = source_path.name
    task["original_source_path"] = source_path.as_posix()
    task["move_files"] = True
    return task


def _stage_test_file(root: Path, relative_path: str, data: bytes) -> tuple[Path, dict]:
    staging_dir = root / f"staged-{len(list(root.glob('staged-*'))) + 1}"
    target_path = staging_dir / "uploads" / relative_path
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(data)
    return staging_dir, {"path": target_path.as_posix(), "relative_path": relative_path}


class LocalImportUploadTest(unittest.TestCase):
    def test_organizer_run_once_processes_queued_package_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            package = _zip_bytes({"Demo/body.stl": b"solid body\nendsolid body\n"})
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix(), move_files=False)
                )
            )
            task_store = FakeTaskStore()

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", state_root / "import_uploads"), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(local_organizer, "ARCHIVE_DIR", archive_root), \
                patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0), \
                patch.object(local_organizer, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_organizer, "append_business_log"), \
                patch.object(local_organizer, "_append_organizer_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                queued = local_import_upload.upload_local_import_files(
                    files=[_upload("Demo.zip", package)],
                    paths=["Demo.zip"],
                    store=store,
                    task_store=task_store,
                )
                service = local_organizer.LocalOrganizerService(store=store, task_store=task_store)
                service.run_once()
                detail = catalog.get_model_detail("LOCAL_Demo")

            self.assertTrue(queued["queued"])
            self.assertTrue(queued["trigger_organizer"])
            self.assertEqual(queued["mode"], "package")
            self.assertIsNotNone(detail)
            self.assertEqual(detail["title"], "Demo")
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "success")
            self.assertEqual(task_item["kind"], "local_package_import")
            self.assertTrue((source_root / "_skipped" / "Demo").exists())

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
            staging_dir, staged_file = _stage_test_file(root, "Mai.zip", package)
            queued_task = _queued_package_task(staging_dir, "Mai", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                self.assertFalse((archive_root / "LOCAL_Mai").exists())
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
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

    def test_folder_import_creates_top_level_group_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            package_dir = source_root / "扎古"
            (package_dir / "Batch1" / "foot").mkdir(parents=True)
            (package_dir / "Batch1" / "images").mkdir(parents=True)
            (package_dir / "Batch2" / "leg").mkdir(parents=True)
            (package_dir / "Batch2" / "docs").mkdir(parents=True)
            (package_dir / "Batch1" / "foot" / "foot.stl").write_bytes(b"solid foot\nendsolid foot\n")
            (package_dir / "Batch1" / "images" / "cover.jpg").write_bytes(b"fake-jpg")
            (package_dir / "Batch2" / "leg" / "leg.stl").write_bytes(b"solid leg\nendsolid leg\n")
            (package_dir / "Batch2" / "docs" / "manual.pdf").write_bytes(b"%PDF-1.7\n")
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix(), move_files=False)
                )
            )
            task_store = FakeTaskStore()

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", state_root / "import_uploads"), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(local_organizer, "ARCHIVE_DIR", archive_root), \
                patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0), \
                patch.object(local_organizer, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_organizer, "append_business_log"), \
                patch.object(local_organizer, "_append_organizer_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                service = local_organizer.LocalOrganizerService(store=store, task_store=task_store)
                service.run_once()
                detail = catalog.get_model_detail("LOCAL_扎古")

            self.assertIsNotNone(detail)
            self.assertEqual(detail["title"], "扎古")
            meta = json.loads((archive_root / "LOCAL_扎古" / "meta.json").read_text(encoding="utf-8"))
            groups = meta["localImport"]["groups"]
            self.assertEqual(meta["localImport"]["groupCount"], 2)
            self.assertEqual([item["title"] for item in groups], ["Batch1", "Batch2"])
            self.assertTrue((archive_root / "LOCAL_扎古" / "packages" / "Batch1.zip").exists())
            self.assertTrue((archive_root / "LOCAL_扎古" / "packages" / "Batch2.zip").exists())
            with zipfile.ZipFile(archive_root / "LOCAL_扎古" / "packages" / "Batch1.zip") as archive:
                self.assertEqual(sorted(archive.namelist()), ["Batch1/foot/foot.stl", "Batch1/images/cover.jpg"])
            with zipfile.ZipFile(archive_root / "LOCAL_扎古" / "packages" / "Batch2.zip") as archive:
                self.assertEqual(sorted(archive.namelist()), ["Batch2/docs/manual.pdf", "Batch2/leg/leg.stl"])
            detail_groups = detail["local_import"]["groups"]
            self.assertEqual([item["title"] for item in detail_groups], ["Batch1", "Batch2"])
            self.assertTrue(detail_groups[0]["download_url"].endswith("/packages/Batch1.zip"))
            self.assertTrue(package_dir.exists())

    def test_local_folder_with_only_3mf_uses_legacy_3mf_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            archive_root.mkdir()
            source_root.mkdir()

            package_dir = source_root / "配置合集"
            package_dir.mkdir()
            (package_dir / "A.3mf").write_bytes(b"3mf-a")
            (package_dir / "B.3mf").write_bytes(b"3mf-b")
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix(), move_files=True)
                )
            )
            task_store = FakeTaskStore()

            with patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0), \
                patch.object(local_organizer, "append_business_log"), \
                patch.object(local_organizer, "_append_organizer_log"), \
                patch.object(local_organizer.LocalOrganizerService, "_spawn_worker") as spawn_worker:
                service = local_organizer.LocalOrganizerService(store=store, task_store=task_store)
                service.run_once()

            self.assertEqual(spawn_worker.call_count, 1)
            first_source = spawn_worker.call_args.kwargs["source_path"]
            self.assertEqual(first_source, package_dir / "A.3mf")
            queued = task_store.payload["items"]
            self.assertEqual(len(queued), 2)
            self.assertTrue(all(item.get("kind") != "local_package_import" for item in queued))

    def test_zip_with_only_3mf_splits_to_legacy_3mf_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            package = _zip_bytes({"A.3mf": b"3mf-a", "B.3mf": b"3mf-b"})
            staging_dir, staged_file = _stage_test_file(root, "Configs.zip", package)
            queued_task = _queued_package_task(staging_dir, "Configs", source_root, archive_root, staged_file["path"])
            task_store = FakeTaskStore()
            task_store.upsert_organize_task(queued_task)
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )

            with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", state_root / "import_uploads"), \
                patch.object(local_import_upload, "append_business_log"):
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
                    store=store,
                    task_store=task_store,
                )

            self.assertEqual(result["mode"], "3mf_split")
            upload_dir = source_root / local_import_upload.LOCAL_IMPORT_UPLOAD_SUBDIR
            self.assertTrue((upload_dir / "A.3mf").exists())
            self.assertTrue((upload_dir / "B.3mf").exists())
            last_import = task_store.payload["last_import"]
            self.assertEqual(last_import["uploaded_count"], 2)
            self.assertEqual([item["file_name"] for item in last_import["files"]], ["A.3mf", "B.3mf"])
            self.assertTrue(all(item["status"] == "queued" for item in last_import["files"]))

    def test_folder_with_3mf_and_assets_stays_single_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            package_dir = source_root / "混合模型"
            package_dir.mkdir()
            (package_dir / "A.3mf").write_bytes(b"3mf-a")
            (package_dir / "part.stl").write_bytes(b"solid part\nendsolid part\n")
            (package_dir / "cover.jpg").write_bytes(b"fake-jpg")
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix(), move_files=False)
                )
            )
            task_store = FakeTaskStore()

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", state_root / "import_uploads"), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_organizer, "ARCHIVE_DIR", archive_root), \
                patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0), \
                patch.object(local_organizer, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_organizer, "append_business_log"), \
                patch.object(local_organizer, "_append_organizer_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                service = local_organizer.LocalOrganizerService(store=store, task_store=task_store)
                service.run_once()
                detail = catalog.get_model_detail("LOCAL_混合模型")

            self.assertIsNotNone(detail)
            meta = json.loads((archive_root / "LOCAL_混合模型" / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["title"], "混合模型")
            file_names = {item["fileName"] for item in meta["instances"]}
            self.assertEqual(file_names, {"A.3mf", "part.stl"})

    def test_stl_import_marks_three_preview_pending_when_no_images_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            archive_root = root / "archive"
            source_root = root / "source"
            state_root = root / "state"
            archive_root.mkdir()
            source_root.mkdir()
            state_root.mkdir()

            stl_data = (
                b"solid cube\n"
                b"facet normal 0 0 1\nouter loop\n"
                b"vertex 0 0 0\nvertex 20 0 0\nvertex 0 20 0\n"
                b"endloop\nendfacet\n"
                b"facet normal 0 1 0\nouter loop\n"
                b"vertex 0 0 0\nvertex 20 0 0\nvertex 0 0 20\n"
                b"endloop\nendfacet\n"
                b"endsolid cube\n"
            )
            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix())
                )
            )
            task_store = FakeTaskStore()
            staging_dir, staged_file = _stage_test_file(root, "cube.stl", stl_data)
            queued_task = _queued_package_task(staging_dir, "cube", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
                    store=store,
                    task_store=task_store,
                )
                detail = catalog.get_model_detail(result["model_dir"])

            meta_path = archive_root / result["model_dir"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self.assertEqual(meta["cover"], "")
            self.assertEqual(meta["designImages"], [])
            self.assertEqual(meta["instances"][0]["thumbnailLocal"], "")
            self.assertEqual(meta["instances"][0]["pictures"], [])
            self.assertEqual(meta["localImport"]["previewGenerator"], "three")
            self.assertEqual(meta["localImport"]["previewStatus"], "pending")
            self.assertTrue(meta["localImport"]["previewNeedsGeneration"])
            self.assertIsNotNone(detail)
            self.assertTrue(detail["local_preview"]["needs_generation"])
            self.assertEqual(detail["local_preview"]["candidate"]["file_name"], "cube.stl")

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
            staging_dir, staged_file = _stage_test_file(root, "Engines.zip", package)
            queued_task = _queued_package_task(staging_dir, "Engines", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)

            with patch.object(local_import_upload, "ARCHIVE_DIR", archive_root), \
                patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", state_root / "organizer_library_index.json"), \
                patch.object(local_import_upload, "append_business_log"), \
                patch.object(local_import_upload, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
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
            staging_dir, staged_file = _stage_test_file(root, "Luffy.rar", b"fake-rar")
            queued_task = _queued_package_task(staging_dir, "Luffy", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)
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
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
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
            staging_dir, staged_file = _stage_test_file(root, "Engines.zip", package)
            queued_task = _queued_package_task(staging_dir, "Engines", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)
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
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
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
            staging_dir, staged_file = _stage_test_file(root, "leg_table.stl", existing_stl)
            queued_task = _queued_package_task(staging_dir, "leg_table", source_root, archive_root, staged_file["path"])
            task_store.upsert_organize_task(queued_task)
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
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
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
            last_import = task_store.payload["last_import"]
            self.assertEqual(last_import["uploaded_count"], 1)
            self.assertEqual(last_import["files"][0]["status"], "skipped")
            self.assertIn("本地库已存在相同模型文件", last_import["files"][0]["message"])

    def test_local_source_duplicate_package_preserves_original_source_for_cleanup(self):
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
                    organizer=SimpleNamespace(source_dir=source_root.as_posix(), target_dir=archive_root.as_posix(), move_files=True)
                )
            )
            task_store = FakeTaskStore()

            existing_stl = b"solid sonic\nendsolid sonic\n"
            source_file = source_root / "索尼克托架.stl"
            source_file.write_bytes(existing_stl)
            staging_dir, staged_file = _stage_test_file(root, "索尼克托架.stl", existing_stl)
            queued_task = _queued_local_source_package_task(staging_dir, "索尼克托架", source_root, archive_root, source_file)
            task_store.upsert_organize_task(queued_task)

            digest_path = root / "digest.stl"
            digest_path.write_bytes(existing_stl)
            digest = local_import_upload._sha256_file(digest_path)
            existing_root = archive_root / "LOCAL_索尼克托架"
            (existing_root / "instances").mkdir(parents=True)
            (existing_root / "instances" / "索尼克托架.stl").write_bytes(existing_stl)
            (existing_root / "meta.json").write_text(
                json.dumps(
                    {
                        "title": "索尼克托架",
                        "source": "local",
                        "instances": [
                            {
                                "title": "索尼克托架",
                                "fileName": "索尼克托架.stl",
                                "localImport": {
                                    "fileHash": digest,
                                    "configFingerprint": f"sha256:{digest}",
                                },
                            }
                        ],
                        "localImport": {"modelFileCount": 1},
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
                result = local_import_upload.run_queued_package_import_task(
                    queued_task,
                    store=store,
                    task_store=task_store,
                )

            self.assertFalse(result["success"])
            self.assertTrue(result["duplicate"])
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "skipped")
            self.assertEqual(task_item["original_source_path"], source_file.as_posix())
            self.assertTrue(task_item["move_files"])

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
