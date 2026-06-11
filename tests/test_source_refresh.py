import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.store import JsonStore
from app.services import remote_refresh
from app.services import source_refresh
from app.services import source_refresh_jobs
import app.services.task_state as task_state_module
from app.services.source_refresh import SourceRefreshTaskManager
from app.services.task_state import TaskStateStore


class SourceRefreshTaskManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_append_remote_refresh_log = remote_refresh._append_remote_refresh_log
        self.original_append_business_log = remote_refresh.append_business_log
        self.original_invalidate_archive_snapshot = remote_refresh.invalidate_archive_snapshot

        remote_refresh._append_remote_refresh_log = lambda *_args, **_kwargs: None
        remote_refresh.append_business_log = lambda *_args, **_kwargs: None
        remote_refresh.invalidate_archive_snapshot = lambda *_args, **_kwargs: None
        self.db_state = {}
        self.db_patches = [
            patch.object(
                task_state_module,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.db_state.get(key) or default),
            ),
            patch.object(
                task_state_module,
                "save_database_json_state",
                side_effect=lambda key, value: self.db_state.__setitem__(key, value) or value,
            ),
            patch.object(remote_refresh, "_read_three_mf_limit_guard", return_value={}),
        ]
        for item in self.db_patches:
            item.start()

        self.store = JsonStore(self.temp_path / "config.json")
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 0 * * *"
        self.store.save(config)
        self.task_store = TaskStateStore()
        self.manager = SourceRefreshTaskManager(
            store=self.store,
            task_store=self.task_store,
            archive_manager=None,
        )

    def tearDown(self):
        self.manager._set_batch_running(False)
        for item in reversed(self.db_patches):
            item.stop()
        remote_refresh._append_remote_refresh_log = self.original_append_remote_refresh_log
        remote_refresh.append_business_log = self.original_append_business_log
        remote_refresh.invalidate_archive_snapshot = self.original_invalidate_archive_snapshot
        self.temp_dir.cleanup()

    def test_manual_trigger_ignores_archive_queue_busy_and_records_source_run(self):
        self.task_store.save_archive_queue(
            {
                "active": [],
                "queued": [{"id": "archive-1", "url": "https://makerworld.com.cn/model/1", "status": "queued"}],
                "recent_failures": [],
            }
        )
        self.manager._start_batch_async = lambda _config, *, resume=False: True

        result = self.manager.trigger_manual_refresh()

        archive_queue = self.task_store.load_archive_queue()
        source_runs = self.task_store.load_source_refresh_runs()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["mode"], "new")
        self.assertEqual(archive_queue["queued_count"], 1)
        self.assertEqual(source_runs["active_run"]["status"], "running")
        self.assertTrue(source_runs["active_run"]["run_id"])

    def test_run_batch_writes_source_refresh_queue_and_completed_run(self):
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 1
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
            {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
        ]
        self.manager._pick_candidates = lambda: (
            items,
            {
                "eligible_total": 2,
                "selected_total": 2,
                "remaining_total": 0,
                "missing_cookie": 0,
                "local_or_invalid": 0,
            },
        )

        def fake_refresh_one(item, *, index, total, config):
            return {
                "ok": True,
                "metrics": {"model_dir": item["model_dir"], "title": item["title"], "total_duration_ms": 10},
                "record": remote_refresh._remote_refresh_result_record(
                    model_dir=item["model_dir"],
                    title=item["title"],
                    url=item["origin_url"],
                    status="success",
                    message="完成",
                    metrics={"total_duration_ms": 10},
                    change_labels=["已检查，无远端变化"],
                ),
            }

        self.manager._refresh_one = fake_refresh_one
        try:
            self.manager._run_batch(config)
        finally:
            remote_refresh._remote_refresh_model_workers = original_workers

        source_queue = self.task_store.load_source_refresh_queue()
        source_runs = self.task_store.load_source_refresh_runs()
        self.assertEqual(source_queue["running_count"], 0)
        self.assertEqual(source_queue["queued_count"], 0)
        self.assertEqual(source_runs["active_run"], {})
        self.assertEqual(source_runs["last_completed_run"]["status"], "completed")
        self.assertEqual(source_runs["last_completed_run"]["candidate_total"], 2)
        self.assertEqual(source_runs["last_completed_run"]["completed_total"], 2)
        self.assertEqual(source_runs["last_completed_run"]["succeeded_total"], 2)

    def test_source_refresh_batch_does_not_monkey_patch_candidate_picker_during_model_refresh(self):
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 1
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
        ]

        def original_picker():
            return (
                items,
                {
                    "eligible_total": 1,
                    "selected_total": 1,
                    "remaining_total": 0,
                    "missing_cookie": 0,
                    "local_or_invalid": 0,
                },
            )

        self.manager._pick_candidates = original_picker

        def fake_refresh_one(item, *, index, total, config):
            self.assertIs(self.manager._pick_candidates, original_picker)
            return {
                "ok": True,
                "metrics": {"model_dir": item["model_dir"], "title": item["title"], "total_duration_ms": 10},
                "record": remote_refresh._remote_refresh_result_record(
                    model_dir=item["model_dir"],
                    title=item["title"],
                    url=item["origin_url"],
                    status="success",
                    message="完成",
                    metrics={"total_duration_ms": 10},
                    change_labels=["已检查，无远端变化"],
                ),
            }

        self.manager._refresh_one = fake_refresh_one
        try:
            self.manager._run_batch(config)
        finally:
            remote_refresh._remote_refresh_model_workers = original_workers

        source_runs = self.task_store.load_source_refresh_runs()
        self.assertEqual(source_runs["last_completed_run"]["status"], "completed")
        self.assertEqual(source_runs["last_completed_run"]["failed_total"], 0)

    def test_run_batch_records_failed_source_run_when_all_models_fail(self):
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
        ]
        self.manager._pick_candidates = lambda: (
            items,
            {
                "eligible_total": 1,
                "selected_total": 1,
                "remaining_total": 0,
                "missing_cookie": 0,
                "local_or_invalid": 0,
            },
        )

        def fake_refresh_one(*_args, **_kwargs):
            raise RuntimeError("boom")

        self.manager._refresh_one = fake_refresh_one

        self.manager._run_batch(config)

        source_runs = self.task_store.load_source_refresh_runs()
        self.assertEqual(source_runs["active_run"], {})
        self.assertEqual(source_runs["last_completed_run"]["status"], "failed")
        self.assertEqual(source_runs["last_completed_run"]["failed_total"], 1)

    def test_source_refresh_job_uses_lightweight_archive_compatibility_flags(self):
        calls = []

        def fake_run_archive_model_job(**kwargs):
            calls.append(kwargs)
            return {"ok": True, "missing_3mf": []}

        with patch.object(source_refresh_jobs, "run_archive_model_job", side_effect=fake_run_archive_model_job):
            result = source_refresh.run_source_refresh_model_job(
                url="https://makerworld.com.cn/model/1",
                cookie="token=1",
                download_dir=str(self.temp_path),
                logs_dir=str(self.temp_path / "logs"),
                existing_root=str(self.temp_path),
                progress_callback=lambda _payload: None,
                three_mf_skip_message="源端刷新仅检测新增 3MF",
                three_mf_skip_state="pending_download",
                download_assets=False,
                download_comment_assets=False,
                proxy_config={},
            )

        self.assertEqual(result["ok"], True)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["skip_three_mf_fetch"])
        self.assertFalse(calls[0]["download_assets"])
        self.assertFalse(calls[0]["download_comment_assets"])
        self.assertFalse(calls[0]["rebuild_archive"])
        self.assertFalse(calls[0]["record_missing_3mf_log"])


if __name__ == "__main__":
    unittest.main()
