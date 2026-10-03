import hashlib
import json
import os
import time
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from app.services import local_organizer


class FakeTaskStore:
    def __init__(self, payload=None):
        self.payload = dict(payload or {"items": []})

    def load_organize_tasks(self):
        return dict(self.payload)

    def load_model_flags(self):
        return {"deleted": []}

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


class LocalOrganizerTest(unittest.TestCase):
    def _write_makerworld_3mf(self, path, settings=b'{"scale": 1}', *, profile_id="456"):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "3D/3dmodel.model",
                '<model><metadata name="DesignModelId">123</metadata>'
                f'<metadata name="DesignProfileId">{profile_id}</metadata>'
                '<metadata name="Title">Model A</metadata>'
                '<metadata name="ProfileTitle">Original profile</metadata><resources /></model>',
            )
            archive.writestr("Metadata/project_settings.config", settings)

    @contextmanager
    def _makerworld_library(self, *, legacy_local=False, original_available=True):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            source_dir.mkdir()
            library_root = root / "archive"
            model_root = library_root / "Model_A"
            (model_root / "instances").mkdir(parents=True)
            original_path = model_root / "instances" / "original.3mf"
            self._write_makerworld_3mf(original_path)
            original_bytes = original_path.read_bytes()
            original_instance = {
                "id": "456", "profileId": "456", "title": "Original profile", "fileName": "original.3mf",
            }
            meta = {
                "id": "123", "title": "Model A", "source": "makerworld",
                "url": "https://makerworld.com/en/models/123",
                "author": {"name": "Original author"}, "summary": {"text": "Original description"},
                "cover": "images/cover.png", "instances": [original_instance],
            }
            if legacy_local:
                legacy_import = {"designProfileId": "456", "configFingerprint": "design_profile:456", "fileHash": ""}
                original_instance["localImport"] = dict(legacy_import)
                meta["localImport"] = {**legacy_import, "designModelId": "123"}
            if not original_available:
                original_path.unlink()
            (model_root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=task_store)
            with patch.object(local_organizer, "ARCHIVE_DIR", library_root), \
                    patch.object(local_organizer, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", root / "index.json"), \
                    patch.object(local_organizer, "_append_organizer_log"), \
                    patch.object(local_organizer, "invalidate_archive_snapshot"), \
                    patch.object(local_organizer, "upsert_archive_snapshot_model"), \
                    patch.object(local_organizer, "_confirm_model_visible_in_snapshot", return_value=True):
                yield service, task_store, source_dir, library_root, model_root, meta, original_bytes

    def test_modified_makerworld_3mf_is_appended_and_duplicate_upload_is_skipped(self):
        with self._makerworld_library() as (service, tasks, source, library, model, original_meta, original_bytes):
            for filename, settings in [("hole-enlarged.3mf", b'{"scale": 2}'), ("petg-optimized.3mf", b'{"material": "PETG"}')]:
                candidate = source / filename
                self._write_makerworld_3mf(candidate, settings)
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
                self.assertEqual(tasks.payload["items"][0]["status"], "success")

            meta = json.loads((model / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(len(meta["instances"]), 3)
            self.assertEqual(meta["instances"][0], original_meta["instances"][0])
            for field in ("title", "source", "url", "author", "summary", "cover"):
                self.assertEqual(meta[field], original_meta[field])
            self.assertEqual((model / "instances" / "original.3mf").read_bytes(), original_bytes)
            for instance, filename in zip(meta["instances"][1:], ("hole-enlarged.3mf", "petg-optimized.3mf")):
                self.assertEqual(instance["title"], Path(filename).stem)
                self.assertIn("本地", instance["summary"])
                self.assertEqual((model / "instances" / instance["fileName"]).read_bytes(), (source / filename).read_bytes())
                self.assertEqual(instance["localImport"]["designProfileId"], "456")
                digest = hashlib.sha256((source / filename).read_bytes()).hexdigest()
                self.assertEqual(instance["localImport"]["configFingerprint"], f"sha256:{digest}")

            service.release_idle_memory(force=True)
            duplicate = source / "renamed-copy.3mf"
            duplicate.write_bytes((source / "hole-enlarged.3mf").read_bytes())
            service.process_candidate(source_path=duplicate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual(tasks.payload["items"][0]["status"], "skipped")
            self.assertFalse(duplicate.exists())
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 3)

    def test_identical_makerworld_3mf_is_skipped_for_remote_and_legacy_local_archives(self):
        for legacy_local in (False, True):
            with self.subTest(legacy_local=legacy_local), self._makerworld_library(legacy_local=legacy_local) as fixture:
                service, tasks, source, library, model, _, original_bytes = fixture
                candidate = source / "renamed-original.3mf"
                candidate.write_bytes(original_bytes)
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
                self.assertEqual(tasks.payload["items"][0]["status"], "skipped")
                self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 1)

    def test_missing_archived_3mf_does_not_prove_duplicate(self):
        with self._makerworld_library(original_available=False) as fixture:
            service, tasks, source, library, model, _, original_bytes = fixture
            candidate = source / "recovered.3mf"
            candidate.write_bytes(original_bytes)
            service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
            self.assertEqual(tasks.payload["items"][0]["status"], "success")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 1)

    def test_modified_makerworld_3mf_merges_into_legacy_local_archive(self):
        with self._makerworld_library(legacy_local=True) as fixture:
            service, tasks, source, library, model, _, original_bytes = fixture
            candidate = source / "modified.3mf"
            self._write_makerworld_3mf(candidate, b'{"scale": 2}')
            service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
            self.assertEqual(tasks.payload["items"][0]["status"], "success")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 2)
            self.assertEqual((model / "instances" / "original.3mf").read_bytes(), original_bytes)

    def test_unreadable_archived_3mf_does_not_prove_duplicate(self):
        with self._makerworld_library() as fixture:
            service, tasks, source, library, model, _, original_bytes = fixture
            candidate = source / "recovered.3mf"
            candidate.write_bytes(original_bytes)
            original_sha256 = service._sha256_file

            def hash_readable_upload(path):
                if path.parent == model / "instances":
                    raise PermissionError("archive unavailable")
                return original_sha256(path)

            with patch.object(service, "_sha256_file", side_effect=hash_readable_upload):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
            self.assertEqual(tasks.payload["items"][0]["status"], "success")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 2)

    def test_modified_makerworld_3mf_respects_deleted_model_flag(self):
        with self._makerworld_library() as fixture:
            service, tasks, source, library, model, _, _ = fixture
            candidate = source / "modified.3mf"
            self._write_makerworld_3mf(candidate, b'{"scale": 2}')
            with patch.object(tasks, "load_model_flags", return_value={"deleted": ["Model_A"]}):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
            self.assertEqual(tasks.payload["items"][0]["status"], "skipped")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 1)

    def test_legacy_library_index_cache_is_rebuilt_before_content_comparison(self):
        with self._makerworld_library() as fixture:
            service, tasks, source, library, model, _, _ = fixture
            local_organizer.ORGANIZER_LIBRARY_INDEX_CACHE_PATH.write_text(json.dumps({
                "root": library.resolve().as_posix(), "cached_at": time.time(), "models": {},
                "configs": {"design_profile:456": {"model_dir": "Model_A", "model_root": model.as_posix()}},
            }), encoding="utf-8")
            candidate = source / "modified.3mf"
            self._write_makerworld_3mf(candidate, b'{"scale": 2}')
            service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=False)
            self.assertEqual(tasks.payload["items"][0]["status"], "success")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 2)

    def test_candidate_poll_only_saves_when_queue_changes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "demo.3mf"
            candidate.write_bytes(b"demo")
            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=task_store)
            options = dict(candidates=[candidate], source_dir=root, library_root=root / "archive", move_files=False)
            with patch.object(task_store, "save_organize_tasks", wraps=task_store.save_organize_tasks) as save, \
                    patch.object(local_organizer, "_now_iso", side_effect=["2026-09-14T01:00:00+08:00", "2026-09-14T01:00:05+08:00", "2026-09-14T01:00:10+08:00"]):
                self.assertEqual(service._sync_candidate_queue(**options), [candidate])
                self.assertEqual(service._sync_candidate_queue(**options), [candidate])
                self.assertEqual(save.call_count, 1)
                task_store.payload["items"][0]["status"] = "failed"
                self.assertEqual(service._sync_candidate_queue(**options), [candidate])
                self.assertEqual(save.call_count, 2)
                self.assertEqual(task_store.payload["items"][0]["status"], "queued")

    def test_terminal_poll_preserves_timestamp_and_detects_removed_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "demo.3mf"
            candidate.write_bytes(b"demo")
            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=task_store)
            options = dict(candidates=[candidate], source_dir=root, library_root=root / "archive", move_files=False)
            service._sync_candidate_queue(**options)
            task_store.payload["items"][0]["status"] = "success"
            with patch.object(task_store, "save_organize_tasks", wraps=task_store.save_organize_tasks) as save, \
                    patch.object(local_organizer, "_now_iso", return_value="2026-09-15T00:00:00+08:00"):
                self.assertEqual(service._sync_candidate_queue(**options), [])
                save.assert_not_called()
                service._sync_candidate_queue(**{**options, "candidates": []})
                self.assertEqual(save.call_count, 1)
                self.assertEqual(task_store.payload["detected_total"], 0)
                self.assertEqual(task_store.payload["items"][0]["status"], "success")

    def test_organizer_recycle_threshold_can_be_disabled(self):
        self.assertTrue(local_organizer.organizer_should_recycle(rss_mib=800, threshold_mib=768))
        self.assertFalse(local_organizer.organizer_should_recycle(rss_mib=800, threshold_mib=0))

    def test_daemon_loop_exits_after_high_memory_idle_release(self):
        service = local_organizer.LocalOrganizerService(
            store=SimpleNamespace(),
            task_store=FakeTaskStore(),
        )

        with patch.dict(os.environ, {local_organizer.ORGANIZER_DAEMON_ENV: "1"}), \
                patch.object(service, "run_once"), \
                patch.object(service, "release_idle_memory", return_value=True), \
                patch.object(local_organizer, "process_rss_mib", return_value=900), \
                patch.object(local_organizer, "organizer_recycle_rss_mib", return_value=768), \
                patch.object(local_organizer, "_append_organizer_log") as append_log:
            service._run_loop()

        append_log.assert_called_once_with(
            "daemon_memory_recycle",
            rss_mib=900,
            threshold_mib=768,
        )

    def test_parse_3mf_metadata_ignores_geometry_after_root_metadata(self):
        with TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "large.3mf"
            model_xml = (
                b'<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                b'<metadata name="DesignModelId">987654</metadata>'
                b'<resources><object id="1"><mesh><vertices>'
            )
            with zipfile.ZipFile(source_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/3dmodel.model", model_xml)

            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=FakeTaskStore(),
            )
            metadata = service._parse_3mf_metadata(source_path)

        self.assertEqual(metadata["DesignModelId"], "987654")

    def test_release_idle_memory_clears_library_index_cache(self):
        service = local_organizer.LocalOrganizerService(
            store=SimpleNamespace(),
            task_store=FakeTaskStore(),
        )
        service._library_index_cache = {"models": {"model-1": {}}, "configs": {}}
        service._library_index_cache_root = "/archive"
        service._library_index_cache_at = 123.0

        with patch.object(local_organizer, "release_catalog_memory") as release_catalog, \
                patch.object(local_organizer, "release_process_memory") as release_memory:
            released = service.release_idle_memory(force=True)

        self.assertTrue(released)
        self.assertIsNone(service._library_index_cache)
        self.assertEqual(service._library_index_cache_root, "")
        self.assertEqual(service._library_index_cache_at, 0.0)
        release_catalog.assert_called_once_with()
        release_memory.assert_called_once_with()

    def test_release_idle_memory_keeps_cache_while_worker_is_running(self):
        service = local_organizer.LocalOrganizerService(
            store=SimpleNamespace(),
            task_store=FakeTaskStore(),
        )
        service._library_index_cache = {"models": {"model-1": {}}, "configs": {}}
        service._worker_process = SimpleNamespace(poll=lambda: None)

        with patch.object(local_organizer, "release_catalog_memory") as release_catalog, \
                patch.object(local_organizer, "release_process_memory") as release_memory:
            released = service.release_idle_memory(force=True)

        self.assertFalse(released)
        self.assertIsNotNone(service._library_index_cache)
        release_catalog.assert_not_called()
        release_memory.assert_not_called()

    def test_sync_candidate_queue_moves_terminal_duplicate_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            library_root = root / "archive"
            source_dir.mkdir()
            library_root.mkdir()
            candidate = source_dir / "wechat-upload.3mf"
            candidate.write_bytes(b"demo-3mf")

            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=task_store,
            )
            fingerprint = service._fingerprint(candidate)
            task_store.payload = {
                "items": [
                    {
                        "id": local_organizer._task_id_from_fingerprint(fingerprint),
                        "file_name": candidate.name,
                        "source_path": candidate.as_posix(),
                        "status": "skipped",
                        "message": "该 3MF 与模型库现有配置重复，已跳过。",
                        "fingerprint": fingerprint,
                    }
                ]
            }

            with patch.object(local_organizer, "_append_organizer_log"):
                actionable = service._sync_candidate_queue(
                    candidates=[candidate],
                    source_dir=source_dir,
                    library_root=library_root,
                    move_files=True,
                )

            self.assertEqual(actionable, [])
            self.assertFalse(candidate.exists())
            moved_files = list((source_dir / "_duplicates").glob("wechat-upload*.3mf"))
            self.assertEqual(len(moved_files), 1)
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "skipped")
            self.assertEqual(task_item["target_path"], moved_files[0].as_posix())

    def test_sync_candidate_queue_does_not_reuse_stale_terminal_source_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            library_root = root / "archive"
            source_dir.mkdir()
            library_root.mkdir()
            candidate = source_dir / "wechat-upload.3mf"
            candidate.write_bytes(b"new-content")

            task_store = FakeTaskStore(
                {
                    "items": [
                        {
                            "id": "old-task",
                            "file_name": candidate.name,
                            "source_path": candidate.as_posix(),
                            "status": "skipped",
                            "message": "旧文件已跳过。",
                            "fingerprint": "old-fingerprint",
                        }
                    ]
                }
            )
            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=task_store,
            )

            with patch.object(local_organizer, "_append_organizer_log"):
                actionable = service._sync_candidate_queue(
                    candidates=[candidate],
                    source_dir=source_dir,
                    library_root=library_root,
                    move_files=True,
                )

            self.assertEqual(actionable, [candidate])
            self.assertTrue(candidate.exists())
            task_item = task_store.payload["items"][0]
            self.assertEqual(task_item["status"], "queued")
            self.assertEqual(task_item["source_path"], candidate.as_posix())
            self.assertNotEqual(task_item["fingerprint"], "old-fingerprint")

    def test_finished_duplicate_package_source_moves_to_duplicates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source"
            library_root = root / "archive"
            staging_dir = root / "state" / "staging"
            source_dir.mkdir()
            library_root.mkdir()
            staging_dir.mkdir(parents=True)
            source_file = source_dir / "索尼克托架.stl"
            source_file.write_bytes(b"solid sonic\nendsolid sonic\n")

            task_store = FakeTaskStore()
            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=task_store,
            )
            task = {
                "id": "duplicate-package",
                "title": "索尼克托架",
                "file_name": "索尼克托架",
                "source_dir": source_dir.as_posix(),
                "target_dir": library_root.as_posix(),
                "source_path": source_file.as_posix(),
                "status": "running",
                "progress": 55,
                "move_files": True,
                "kind": "local_package_import",
                "staging_dir": staging_dir.as_posix(),
                "package_source": source_file.name,
                "package_title": "索尼克托架",
                "original_source_path": source_file.as_posix(),
            }

            with patch.object(local_organizer, "_append_organizer_log"):
                service._cleanup_finished_package_source(task, duplicate=True)

            self.assertFalse(source_file.exists())
            moved_files = list((source_dir / "_duplicates").glob("索尼克托架*.stl"))
            self.assertEqual(len(moved_files), 1)
            self.assertEqual(moved_files[0].read_bytes(), b"solid sonic\nendsolid sonic\n")

    def test_shortcut_fallback_name_uses_3mf_metadata_title(self):
        with TemporaryDirectory() as tmp:
            library_root = Path(tmp) / "archive"
            library_root.mkdir()
            source_path = Path(tmp) / "wechat-upload.3mf"
            source_path.write_bytes(b"demo-3mf")

            service = local_organizer.LocalOrganizerService(
                store=SimpleNamespace(),
                task_store=FakeTaskStore(),
            )
            title = "EVA 新世纪福音战士 明日香四色半分件/单色打印/双色x2d多版本"
            analysis = {
                "source_title": "wechat-upload",
                "model_title": title,
                "profile_title": title,
            }

            self.assertEqual(service._display_title_for_analysis(source_path, analysis), title)
            self.assertEqual(
                service._target_filename_for_analysis(source_path, analysis),
                "EVA 新世纪福音战士 明日香四色半分件_单色打印_双色x2d多版本.3mf",
            )

            model_root = service._prepare_model_root(
                library_root,
                source_path,
                existing={},
                matched_model=None,
                title=service._display_title_for_analysis(source_path, analysis),
            )

            self.assertEqual(
                model_root.name,
                "LOCAL_EVA 新世纪福音战士 明日香四色半分件_单色打印_双色x2d多版本",
            )

    def test_shortcut_fallback_title_ignores_placeholder_metadata(self):
        service = local_organizer.LocalOrganizerService(
            store=SimpleNamespace(),
            task_store=FakeTaskStore(),
        )
        source_path = Path("/tmp/wechat-upload.3mf")

        self.assertEqual(service._display_title_for_analysis(source_path, {}), "移动端导入")
        self.assertEqual(service._target_filename_for_analysis(source_path, {}), "wechat-upload.3mf")

    def test_run_once_allows_local_source_inside_data_library_root(self):
        with TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "data"
            source_dir = data_root / "local"
            source_dir.mkdir(parents=True)
            candidate = source_dir / "demo.3mf"
            candidate.write_bytes(b"demo-3mf")

            store = SimpleNamespace(
                load=lambda: SimpleNamespace(
                    organizer=SimpleNamespace(
                        source_dir=source_dir.as_posix(),
                        target_dir=data_root.as_posix(),
                        move_files=False,
                    )
                )
            )
            service = local_organizer.LocalOrganizerService(
                store=store,
                task_store=FakeTaskStore(),
            )

            with patch.object(service, "_poll_worker"), \
                patch.object(service, "_queue_local_package_candidates"), \
                patch.object(service, "_iter_candidates", return_value=[candidate]), \
                patch.object(service, "_sync_candidate_queue", return_value=[]), \
                patch.object(service, "_spawn_worker") as spawn_worker, \
                patch.object(local_organizer, "_append_organizer_log") as append_log:
                service.run_once()

            invalid_events = [
                call
                for call in append_log.call_args_list
                if call.args and call.args[0] == "invalid_config"
            ]
            self.assertEqual(invalid_events, [])
            spawn_worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
