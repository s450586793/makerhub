import os
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.services import local_import_cleanup, local_import_upload, local_organizer
from tests.test_local_import_upload import FakeTaskStore


def _staging(root: Path, suffix: str = "a1b2c3d4") -> Path:
    path = root / f"1700000000000-123-456-{suffix}"
    path.mkdir(parents=True)
    (path / "model.stl").write_bytes(b"source model")
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(path, (old, old))
    return path


def _tasks(*items):
    return SimpleNamespace(load_organize_tasks=lambda: {"items": list(items)})


def test_old_staging_for_queued_import_is_not_deleted(tmp_path):
    staging_root = tmp_path / "import_uploads"
    stage = _staging(staging_root)
    tasks = _tasks({"id": "queued", "status": "queued", "staging_dir": str(stage)})

    with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", staging_root), \
            patch.object(local_import_upload, "TaskStateStore", return_value=tasks):
        local_import_upload._cleanup_stale_staging_dirs()

    assert (stage / "model.stl").read_bytes() == b"source model"


def test_staging_cleanup_does_not_delete_unknown_directories(tmp_path):
    staging_root = tmp_path / "import_uploads"
    unknown = staging_root / "personal-backup"
    unknown.mkdir(parents=True)
    old = time.time() - 3 * 24 * 60 * 60
    os.utime(unknown, (old, old))

    with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", staging_root), \
            patch.object(local_import_upload, "TaskStateStore", return_value=_tasks()):
        local_import_upload._cleanup_stale_staging_dirs()

    assert unknown.is_dir()


@pytest.mark.parametrize("status", ["pending", "running", "retrying", "paused", "resuming", "interrupted"])
def test_recoverable_import_keeps_old_staging(tmp_path, status):
    stage = _staging(tmp_path)
    local_import_cleanup.cleanup_expired_import_staging(
        task_store=_tasks({"status": status, "staging_dir": str(stage)}), root=tmp_path,
        ttl_seconds=24 * 60 * 60,
    )
    assert stage.is_dir()


def test_failed_staging_expires_from_task_failure_time_and_retry_blocked_is_not_permanent(tmp_path):
    stage = _staging(tmp_path)
    failed_at = time.time()
    task = {
        "status": "failed", "staging_dir": str(stage), "meta": {"retry_blocked": True},
        "updated_at": datetime.fromtimestamp(failed_at, timezone.utc).isoformat(),
    }
    for age, exists in [(23 * 60 * 60, True), (25 * 60 * 60, False)]:
        local_import_cleanup.cleanup_expired_import_staging(
            task_store=_tasks(task), root=tmp_path, ttl_seconds=24 * 60 * 60, now=failed_at + age,
        )
        assert stage.exists() is exists


@pytest.mark.parametrize("meta", [{"retry_at": "2099-01-01T00:00:00Z"}, {"resumable": True}])
def test_explicit_retry_or_resume_keeps_failed_staging(tmp_path, meta):
    stage = _staging(tmp_path)
    local_import_cleanup.cleanup_expired_import_staging(
        task_store=_tasks({"status": "failed", "staging_dir": str(stage), "meta": meta}),
        root=tmp_path, ttl_seconds=24 * 60 * 60,
    )
    assert stage.exists()


def test_completed_staging_retries_cleanup_without_waiting_for_ttl(tmp_path):
    stage = _staging(tmp_path)
    os.utime(stage, None)
    result = local_import_cleanup.cleanup_expired_import_staging(
        task_store=_tasks({"status": "success", "staging_dir": str(stage)}),
        root=tmp_path, ttl_seconds=24 * 60 * 60,
    )
    assert result["removed"] == 1
    assert not stage.exists()


def test_cleanup_keeps_in_use_directory_when_task_history_was_cleared(tmp_path):
    stage = _staging(tmp_path)
    with local_import_cleanup.staging_in_use(stage):
        local_import_cleanup.cleanup_expired_import_staging(
            task_store=_tasks(), root=tmp_path, ttl_seconds=24 * 60 * 60,
        )
        assert stage.exists()


def test_cleanup_cannot_remove_staging_leased_by_another_process(tmp_path):
    stage = _staging(tmp_path)
    child = subprocess.Popen(
        [sys.executable, "-c", (
            "import fcntl, os, sys; "
            "fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY); "
            "fcntl.flock(fd, fcntl.LOCK_EX); "
            "print('ready', flush=True); sys.stdin.read(1); os.close(fd)"
        ), str(stage)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 5)[0], "child must acquire the staging lease"
        assert child.stdout.readline().strip() == "ready"
        local_import_cleanup.cleanup_expired_import_staging(
            task_store=_tasks(), root=tmp_path, ttl_seconds=24 * 60 * 60,
        )
        assert (stage / "model.stl").read_bytes() == b"source model"
    finally:
        child.communicate(input="x", timeout=5)
    local_import_cleanup.cleanup_expired_import_staging(
        task_store=_tasks(), root=tmp_path, ttl_seconds=24 * 60 * 60,
    )
    assert not stage.exists()


def test_cleanup_keeps_staging_if_task_state_cannot_be_read(tmp_path):
    stage = _staging(tmp_path)
    tasks = SimpleNamespace(load_organize_tasks=Mock(side_effect=OSError("database unavailable")))
    with patch.object(local_import_cleanup, "append_business_log") as logs:
        result = local_import_cleanup.cleanup_expired_import_staging(
            task_store=tasks, root=tmp_path, ttl_seconds=24 * 60 * 60,
        )
    assert stage.exists()
    assert result["errors"] == 1
    assert logs.call_args.kwargs["level"] == "warning"


def test_cleanup_refuses_symlink_and_nested_directory_symlink(tmp_path):
    root = tmp_path / "staging"
    outside = tmp_path / "personal"
    outside.mkdir()
    (outside / "keep.stl").write_bytes(b"keep")
    stage = _staging(root)
    (stage / "linked").symlink_to(outside, target_is_directory=True)
    (root / "1700000000000-123-456-deadbeef").symlink_to(outside, target_is_directory=True)
    with patch.object(local_import_cleanup, "append_business_log"):
        local_import_cleanup.cleanup_expired_import_staging(
            task_store=_tasks(), root=root, ttl_seconds=24 * 60 * 60,
        )
    assert stage.exists()
    assert (outside / "keep.stl").read_bytes() == b"keep"
    assert (root / "1700000000000-123-456-deadbeef").is_symlink()


def test_cleanup_failure_is_logged_and_next_cycle_retries(tmp_path):
    stage = _staging(tmp_path)
    with patch.object(local_import_cleanup.shutil, "rmtree", side_effect=PermissionError("busy")), \
            patch.object(local_import_cleanup, "append_business_log") as logs:
        result = local_import_cleanup.cleanup_expired_import_staging(
            task_store=_tasks(), root=tmp_path, ttl_seconds=24 * 60 * 60,
        )
    assert result == {"removed": 0, "errors": 1}
    assert stage.exists()
    assert logs.call_args.args[1] == "local_import_staging_cleanup_failed"
    result = local_import_cleanup.cleanup_expired_import_staging(
        task_store=_tasks(), root=tmp_path, ttl_seconds=24 * 60 * 60,
    )
    assert result["removed"] == 1


@pytest.mark.parametrize("duplicate,snapshot_ready", [(False, True), (True, True), (False, False)])
def test_import_cleanup_preserves_result_and_waits_for_snapshot(tmp_path, duplicate, snapshot_ready):
    staging_root = tmp_path / "state" / "import_uploads"
    stage = _staging(staging_root)
    (stage / "model.stl").unlink()
    (stage / "uploads").mkdir()
    (stage / "uploads" / "part.stl").write_bytes(b"solid part\nendsolid part\n")
    source = tmp_path / "source"
    library = tmp_path / "library"
    source.mkdir()
    library.mkdir()
    store = SimpleNamespace(load=lambda: SimpleNamespace(organizer=SimpleNamespace(
        source_dir=str(source), target_dir=str(library),
    )))
    tasks = FakeTaskStore()
    task = {"id": "import", "staging_dir": str(stage), "status": "queued", "kind": "local_package_import"}
    tasks.upsert_organize_task(task)
    matches = [{"model_dir": "LOCAL_existing", "model_title": "Existing", "file_name": "part.stl"}] if duplicate else []
    rmtree = local_import_cleanup.shutil.rmtree

    def remove_tree(*args, **kwargs):
        if snapshot_ready:
            raise PermissionError("busy")
        return rmtree(*args, **kwargs)

    with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", staging_root), \
            patch.object(local_import_upload, "ARCHIVE_DIR", library), \
            patch.object(local_import_upload, "ORGANIZER_LIBRARY_INDEX_CACHE_PATH", tmp_path / "index.json"), \
            patch.object(local_import_upload, "_find_existing_model_file_duplicates", return_value=matches), \
            patch.object(local_import_upload, "ensure_package_preview_images", return_value=[]), \
            patch.object(local_import_upload, "mark_local_preview_pending", return_value=False), \
            patch.object(local_import_upload, "invalidate_archive_snapshot"), \
            patch.object(local_import_upload, "upsert_archive_snapshot_model"), \
            patch.object(local_import_upload, "_confirm_model_visible_in_snapshot", return_value=snapshot_ready), \
            patch.object(local_import_upload, "append_business_log"), \
            patch.object(local_import_cleanup, "append_business_log") as logs, \
            patch.object(local_import_cleanup.shutil, "rmtree", side_effect=remove_tree):
        if not snapshot_ready:
            with pytest.raises(RuntimeError, match="快照刷新超时"):
                local_import_upload.run_queued_package_import_task(task, store=store, task_store=tasks)
            assert (stage / "uploads" / "part.stl").is_file()
            assert len(list(library.glob("*/meta.json"))) == 1
            logs.assert_not_called()
            return
        result = local_import_upload.run_queued_package_import_task(task, store=store, task_store=tasks)
    assert result.get("duplicate", False) is duplicate
    assert tasks.payload["items"][0]["status"] == ("skipped" if duplicate else "success")
    assert stage.exists()
    assert logs.called


def test_idle_organizer_runs_periodic_cleanup_without_new_uploads(tmp_path):
    service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=_tasks())
    with patch.object(service, "run_once"), \
            patch.object(service, "release_idle_memory", return_value=False), \
            patch.object(local_organizer, "_cleanup_stale_staging_dirs") as cleanup, \
            patch.object(local_organizer.time, "sleep", side_effect=InterruptedError("stop")):
        with pytest.raises(InterruptedError, match="stop"):
            service._run_loop()
    cleanup.assert_called_once_with(service.task_store)


def test_idle_periodic_cycle_removes_orphan_without_any_new_upload(tmp_path):
    stage = _staging(tmp_path)
    service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=_tasks())
    with patch.object(local_import_upload, "LOCAL_IMPORT_STAGING_DIR", tmp_path):
        service._run_import_cleanup_if_due()
    assert not stage.exists()


def test_import_maintenance_runs_at_most_once_every_ten_minutes():
    service = local_organizer.LocalOrganizerService(store=SimpleNamespace(), task_store=_tasks())
    with patch.object(local_organizer.time, "monotonic", side_effect=[100, 200, 701]), \
            patch.object(local_organizer, "_cleanup_stale_staging_dirs") as cleanup:
        for _ in range(3):
            service._run_import_cleanup_if_due()
    assert cleanup.call_count == 2
