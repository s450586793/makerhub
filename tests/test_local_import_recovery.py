import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.services import local_import_upload, local_organizer, local_organizer_worker, task_state
from tests import test_local_import_upload as upload_tests, test_local_organizer as organizer_tests


class LocalImportRecoveryTest(unittest.TestCase):
    def test_folder_with_three_mf_and_readme_keeps_upload_task_identity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            library = root / "library"
            source.mkdir()
            library.mkdir()
            store = SimpleNamespace(load=lambda: SimpleNamespace(organizer=SimpleNamespace(
                source_dir=str(source), target_dir=str(library), move_files=False,
            )))
            tasks = organizer_tests.FakeTaskStore()
            with patch.object(local_import_upload, "append_business_log"), \
                    patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", root / "staging"), \
                    patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0):
                result = local_import_upload.upload_local_import_files(
                    files=[
                        SimpleNamespace(filename="part.3mf", file=io.BytesIO(b"model")),
                        SimpleNamespace(filename="README.txt", file=io.BytesIO(b"important description")),
                    ],
                    paths=["Demo/part.3mf", "Demo/README.txt"], store=store, task_store=tasks,
                )
                service = local_organizer.LocalOrganizerService(store=store, task_store=tasks)
                self.assertEqual(len(service._iter_package_candidates(source)), 1)
                service._queue_local_package_candidates(source_dir=source, library_root=library, move_files=False)
            package_task = next(item for item in tasks.payload["items"] if item["kind"] == "local_package_import")
            self.assertEqual(package_task["id"], result["task_id"])
            self.assertEqual(package_task["source_path"], package_task["original_source_path"])
            self.assertFalse(package_task["meta"]["staging_prepared"])
            self.assertTrue((Path(package_task["original_source_path"]) / "README.txt").exists())

    def test_metadata_failure_preserves_source_original_meta_and_instances(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, original_meta, original_bytes):
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')
            with patch.object(service, "_append_instance_to_existing_meta", side_effect=OSError("metadata unavailable")):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual(tasks.payload["items"][0]["status"], "failed")
            self.assertTrue(candidate.exists())
            self.assertEqual(json.loads((model / "meta.json").read_text()), original_meta)
            self.assertEqual((model / "instances" / "original.3mf").read_bytes(), original_bytes)
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 1)

    def test_partial_metadata_write_does_not_corrupt_original_model(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, original_meta, _):
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')
            original_write = Path.write_text

            def fail_metadata_write(path, content, *args, **kwargs):
                if path.parent == model and "meta.json" in path.name:
                    original_write(path, "partial", *args, **kwargs)
                    raise OSError("disk full")
                return original_write(path, content, *args, **kwargs)

            with patch.object(Path, "write_text", new=fail_metadata_write):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual(tasks.payload["items"][0]["status"], "failed")
            self.assertEqual(json.loads((model / "meta.json").read_text()), original_meta)
            self.assertTrue(candidate.exists())
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 1)

    def test_snapshot_failure_preserves_committed_file_and_retryable_source(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, _, _):
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')
            with patch.object(local_organizer, "_confirm_model_visible_in_snapshot", return_value=False):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            meta = json.loads((model / "meta.json").read_text())
            self.assertEqual(tasks.payload["items"][0]["status"], "failed")
            self.assertTrue(candidate.exists())
            self.assertEqual(len(meta["instances"]), 2)
            self.assertTrue(all((model / "instances" / item["fileName"]).exists() for item in meta["instances"]))
            service.release_idle_memory(force=True)
            service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual(tasks.payload["items"][0]["status"], "skipped")
            self.assertEqual(len(list((model / "instances").glob("*.3mf"))), 2)

    def test_source_is_only_removed_after_model_metadata_is_committed(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, _, _):
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')
            append = service._append_instance_to_existing_meta

            def verify_source(**kwargs):
                self.assertTrue(candidate.exists())
                return append(**kwargs)

            with patch.object(service, "_append_instance_to_existing_meta", side_effect=verify_source):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual(tasks.payload["items"][0]["status"], "success")
            self.assertFalse(candidate.exists())

    def test_partial_file_copy_is_removed_and_source_remains_retryable(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, original_meta, _):
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')

            def partial_copy(source_path, target_path):
                Path(target_path).write_bytes(b"partial")
                raise OSError("disk full")

            with patch.object(local_organizer.shutil, "copy2", side_effect=partial_copy):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertTrue(candidate.exists())
            self.assertEqual(tasks.payload["items"][0]["status"], "failed")
            self.assertEqual(json.loads((model / "meta.json").read_text()), original_meta)
            self.assertEqual([path.name for path in (model / "instances").iterdir()], ["original.3mf"])

    def test_reused_directory_without_metadata_does_not_lose_existing_files(self):
        fixture = organizer_tests.LocalOrganizerTest()
        with fixture._makerworld_library() as (service, tasks, source, library, model, _, original_bytes):
            (model / "meta.json").unlink()
            candidate = source / "modified.3mf"
            fixture._write_makerworld_3mf(candidate, b'{"scale": 2}')
            fingerprint = service._fingerprint(candidate)
            tasks.upsert_organize_task({
                "id": local_organizer._task_id_from_fingerprint(fingerprint), "fingerprint": fingerprint,
                "status": "failed", "model_dir": model.name, "source_path": str(candidate),
            })
            with patch.object(service, "_build_meta", side_effect=OSError("disk full")):
                service.process_candidate(source_path=candidate, source_dir=source, library_root=library, move_files=True)
            self.assertEqual((model / "instances" / "original.3mf").read_bytes(), original_bytes)
            self.assertTrue(candidate.exists())

    def test_package_queue_does_not_copy_source_before_supervised_worker(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Demo"
            source.mkdir()
            (source / "part.stl").write_bytes(b"model")
            tasks = organizer_tests.FakeTaskStore()
            with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", root / "staging"), \
                    patch.object(local_import_upload, "stage_local_package_path") as stage, \
                    patch.object(local_import_upload, "append_business_log"):
                result = local_import_upload.queue_local_path_package_import(
                    source_path=source, source_dir=root, library_root=root / "library",
                    move_files=False, task_store=tasks,
                )
            stage.assert_not_called()
            task = next(item for item in tasks.payload["items"] if item["id"] == result["task_id"])
            self.assertFalse(task["meta"]["staging_prepared"])

    def test_package_queue_starts_supervised_worker_instead_of_inline_import(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            library = Path(tmp) / "library"
            staging = Path(tmp) / "staging"
            source.mkdir()
            staging.mkdir()
            task = {"id": "package-1", "kind": "local_package_import", "status": "queued",
                    "source_path": str(source / "Demo"), "staging_dir": str(staging)}
            store = SimpleNamespace(load=lambda: SimpleNamespace(organizer=SimpleNamespace(
                source_dir=str(source), target_dir=str(library), move_files=False,
            )))
            service = local_organizer.LocalOrganizerService(store=store, task_store=organizer_tests.FakeTaskStore({"items": [task]}))
            with patch.object(local_organizer.subprocess, "Popen") as spawn, \
                    patch.object(service, "_process_package_import_task") as inline, \
                    patch.object(local_organizer, "_append_organizer_log"):
                service.run_once()
                inline.assert_not_called()
                self.assertIn("--package-task-id", spawn.call_args.args[0])
                self.assertEqual(service._worker_task_id, task["id"])

    def test_rar_extractor_has_bounded_timeout_and_clear_failure(self):
        with TemporaryDirectory() as tmp, \
                patch.object(local_import_upload.shutil, "which", return_value="/usr/bin/bsdtar"), \
                patch.object(local_import_upload.subprocess, "run", side_effect=subprocess.TimeoutExpired("bsdtar", 120)) as run:
            with self.assertRaisesRegex(RuntimeError, "超时"):
                local_import_upload._extract_rar_with_bsdtar(Path("Demo.rar"), Path(tmp) / "extracted")
            self.assertGreater(run.call_args.kwargs["timeout"], 0)

    def test_package_timeout_preserves_exact_task_and_allows_next_package(self):
        tasks = organizer_tests.FakeTaskStore({"items": [
            {"id": "package-1", "kind": "local_package_import", "status": "running", "source_path": "/source/Demo"},
            {"id": "package-2", "kind": "local_package_import", "status": "queued", "source_path": "/source/Next"},
        ]})
        service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=tasks)
        process = Mock()
        process.poll.return_value = None
        service._worker_process = process
        service._worker_source_path = "/source/Demo"
        service._worker_task_id = "package-1"
        service._worker_started_at = time.time() - local_organizer.ORGANIZER_WORKER_TIMEOUT_SECONDS - 1
        with patch.object(local_organizer, "_append_organizer_log"):
            service._poll_worker()
        timed_out = next(item for item in tasks.payload["items"] if item["id"] == "package-1")
        self.assertEqual(timed_out["status"], "failed")
        self.assertTrue(timed_out["meta"]["retry_blocked"])
        self.assertEqual(service._next_package_import_task()["id"], "package-2")

    def test_package_worker_heartbeat_preserves_latest_stage_progress(self):
        tasks = organizer_tests.FakeTaskStore({"items": [{
            "id": "package-1", "kind": "local_package_import", "status": "running",
            "source_path": "/source/Demo", "progress": 75, "message": "copying model files",
            "meta": {"staging_prepared": True},
        }]})
        service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=tasks)
        service._worker_process = Mock(poll=lambda: None)
        service._worker_task_id = "package-1"
        service._worker_started_at = time.time()
        service._poll_worker()
        task = tasks.payload["items"][0]
        self.assertEqual(task["progress"], 75)
        self.assertEqual(task["message"], "copying model files")
        self.assertTrue(task["meta"]["staging_prepared"])
        self.assertTrue(task["meta"]["worker_heartbeat_at"])

    def test_abnormal_package_exit_is_failed_without_hiding_or_requeueing_task(self):
        tasks = organizer_tests.FakeTaskStore({"items": [{
            "id": "package-1", "kind": "local_package_import", "status": "running",
            "source_path": "/source/Demo", "staging_dir": "/staging/Demo", "progress": 45,
        }]})
        service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=tasks)
        service._worker_process = Mock(poll=lambda: -9)
        service._worker_source_path = "/source/Demo"
        service._worker_task_id = "package-1"
        with patch.object(local_organizer, "_append_organizer_log"):
            service._poll_worker()
        task = tasks.payload["items"][0]
        self.assertEqual(task["id"], "package-1")
        self.assertEqual(task["status"], "failed")
        self.assertIn("-9", task["message"])
        self.assertTrue(task["meta"]["retry_blocked"])
        self.assertIsNone(service._next_package_import_task())

    @unittest.skipUnless(local_organizer.os.name == "posix", "POSIX process groups")
    def test_worker_termination_also_stops_descendant_processes(self):
        service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=organizer_tests.FakeTaskStore())
        service._worker_process = Mock(pid=12345)
        service._worker_process_group = True
        with patch.object(local_organizer.os, "killpg") as kill_group:
            service._terminate_worker()
        self.assertEqual(kill_group.call_args_list[0].args, (12345, local_organizer.signal.SIGTERM))
        self.assertEqual(kill_group.call_args_list[-1].args, (12345, local_organizer.signal.SIGKILL))

    def test_worker_entry_loads_the_exact_package_task(self):
        task = {"id": "package-1", "kind": "local_package_import", "status": "queued"}
        tasks = organizer_tests.FakeTaskStore({"items": [task]})
        service = Mock(task_store=tasks)
        service._process_package_import_task.side_effect = lambda item: tasks.upsert_organize_task({**item, "status": "success"})
        with patch.object(local_organizer_worker, "LocalOrganizerService", return_value=service), \
                patch.object(sys, "argv", ["worker", "--package-task-id", "package-1"]), \
                patch.object(local_organizer_worker.os, "nice", create=True):
            self.assertEqual(local_organizer_worker.main(), 0)
        service._process_package_import_task.assert_called_once_with(task)

    def test_three_mf_batch_started_at_precedes_first_source_publish(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SimpleNamespace(load=lambda: SimpleNamespace(organizer=SimpleNamespace(source_dir=str(root / "source"))))
            tasks = organizer_tests.FakeTaskStore()
            now = ["2026-10-17T14:00:00+08:00"]
            publish = local_import_upload._move_staged_file_to_target

            def publish_and_advance(staged, target):
                publish(staged, target)
                now[0] = "2026-10-17T14:01:00+08:00"

            with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", root / "staging"), \
                    patch.object(local_import_upload, "china_now_iso", side_effect=lambda: now[0]), \
                    patch.object(local_import_upload, "_move_staged_file_to_target", side_effect=publish_and_advance), \
                    patch.object(local_import_upload, "append_business_log"):
                local_import_upload.upload_local_import_files(
                    files=[SimpleNamespace(filename="part.3mf", file=io.BytesIO(b"model"))],
                    store=store, task_store=tasks,
                )
            self.assertEqual(tasks.payload["last_import"]["started_at"], "2026-10-17T14:00:00+08:00")
            self.assertEqual(tasks.payload["last_import"]["uploaded_at"], "2026-10-17T14:01:00+08:00")

    def test_last_import_uses_batch_start_for_completed_file_log_recovery(self):
        event = {"updated_at": "2026-10-17T14:01:00+08:00", "source": "/source/part.3mf", "status": "success", "message": "done"}
        with patch.object(task_state, "_recent_organizer_terminal_events", return_value=[event]):
            result = task_state._normalize_local_import_payload({
                "started_at": "2026-10-17T14:00:00+08:00", "uploaded_at": "2026-10-17T14:05:00+08:00",
                "files": [{"file_name": "part.3mf", "source_path": "/source/part.3mf"}],
            })
        self.assertEqual(result["started_at"], "2026-10-17T14:00:00+08:00")
        self.assertEqual(result["files"][0]["status"], "success")

    def test_split_package_publishes_child_batch_before_parent_success(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            library = root / "library"
            source.mkdir()
            store = SimpleNamespace(load=lambda: SimpleNamespace(organizer=SimpleNamespace(
                source_dir=str(source), target_dir=str(library), move_files=False,
            )))
            staging, item = upload_tests._stage_test_file(root, "Demo.zip", upload_tests._zip_bytes({"A.3mf": b"a", "B.3mf": b"b"}))
            task = upload_tests._queued_package_task(staging, "Demo", source, library, str(item["path"]))
            tasks = organizer_tests.FakeTaskStore({"items": [task]})
            upsert = tasks.upsert_organize_task

            def check_success_order(item, limit=50):
                if item.get("status") == "success":
                    children = tasks.payload.get("last_import", {}).get("files", [])
                    self.assertEqual([child["file_name"] for child in children], ["A.3mf", "B.3mf"])
                    self.assertTrue(all(child["status"] == "queued" for child in children))
                return upsert(item, limit=limit)

            with patch.object(tasks, "upsert_organize_task", side_effect=check_success_order), \
                    patch.object(local_import_upload, "append_business_log"):
                local_import_upload.run_queued_package_import_task(task, store=store, task_store=tasks)

    def test_failed_package_three_mf_cannot_bypass_package_watchdog(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "Demo"
            package.mkdir()
            (package / "part.3mf").write_bytes(b"model")
            (package / "README.txt").write_bytes(b"description")
            tasks = organizer_tests.FakeTaskStore({"items": [{
                "id": "failed-package", "kind": "local_package_import", "status": "failed",
                "source_path": str(package), "meta": {"retry_blocked": True},
            }]})
            service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=tasks)
            with patch.object(local_organizer, "ORGANIZER_MIN_FILE_AGE_SECONDS", 0):
                self.assertEqual(service._iter_candidates(root), [])
            self.assertTrue((package / "part.3mf").exists())


if __name__ == "__main__":
    unittest.main()
