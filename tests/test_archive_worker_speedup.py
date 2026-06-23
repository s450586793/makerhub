import threading
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from app.services import archive_worker as archive_worker_module
from app.services.archive_worker import ArchiveTaskManager


class ArchiveWorkerSpeedupTest(unittest.TestCase):
    def test_ensure_worker_starts_configured_archive_worker_threads(self):
        manager = ArchiveTaskManager(background_enabled=True)
        started_threads = []

        class FakeThread:
            def __init__(self, *, target, daemon=True, name=""):
                self.target = target
                self.daemon = daemon
                self.name = name
                self.started = False

            def is_alive(self):
                return self.started

            def start(self):
                self.started = True
                started_threads.append(self)

        with patch.object(archive_worker_module, "_archive_worker_concurrency", return_value=4, create=True), \
                patch.object(archive_worker_module.threading, "Thread", side_effect=lambda **kwargs: FakeThread(**kwargs)):
            manager._ensure_worker()
            manager._ensure_worker()

        self.assertEqual(len(started_threads), 4)
        self.assertEqual(len({thread.name for thread in started_threads}), 4)

    def test_run_loop_can_start_four_single_model_tasks_without_duplicate_leases(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        started = []
        release = threading.Event()
        lock = threading.Lock()

        def fake_run_single(task_id, _url, meta=None):
            with lock:
                started.append(task_id)
            release.wait(timeout=2)
            manager.task_store.complete_archive_task(task_id)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(manager, "_run_single_task", side_effect=fake_run_single), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}):
            manager.task_store.save_archive_queue(
                {
                    "active": [],
                    "queued": [
                        {
                            "id": f"task-{index}",
                            "url": f"https://makerworld.com.cn/zh/models/{index}",
                            "mode": "single_model",
                            "status": "queued",
                        }
                        for index in range(4)
                    ],
                    "recent_failures": [],
                }
            )

            threads = [
                threading.Thread(target=manager._run_loop)
                for index in range(4)
            ]
            for thread in threads:
                thread.start()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with lock:
                    if len(started) >= 4:
                        break
                time.sleep(0.01)

            release.set()
            for thread in threads:
                thread.join(timeout=2)

            queue = manager.task_store.load_archive_queue()

        self.assertEqual(sorted(started), [f"task-{index}" for index in range(4)])
        self.assertEqual(queue["queued"], [])
        self.assertEqual(queue["active"], [])

    def test_regular_archive_skips_3mf_and_queues_three_mf_stage(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        run_kwargs = []
        enqueued = []
        updates = []
        completed = []
        replaced_missing = []

        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda model_id, items: replaced_missing.append((model_id, items)),
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda task_id, **payload: updates.append((task_id, payload)),
            complete_archive_task=lambda task_id: completed.append(task_id),
            enqueue_archive_task=lambda item: enqueued.append(item),
        )

        def fake_archive_job(**kwargs):
            run_kwargs.append(kwargs)
            return {
                "model_id": "123",
                "base_name": "Demo",
                "work_dir": "",
                "missing_3mf": [],
                "instances": [
                    {"id": "profile-1", "downloadUrl": "https://example.test/demo.3mf"}
                ],
            }

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task("task-1", "https://makerworld.com.cn/zh/models/123")

        self.assertTrue(run_kwargs[0]["skip_three_mf_fetch"])
        self.assertEqual(replaced_missing, [("123", [])])
        self.assertEqual(completed, ["task-1"])
        self.assertEqual(len(enqueued), 1)
        self.assertTrue(enqueued[0]["meta"]["three_mf_download"])
        self.assertEqual(enqueued[0]["meta"]["model_id"], "123")
        self.assertEqual(enqueued[0]["message"], "等待下载 3MF")

    def test_three_mf_download_task_does_not_wrap_archive_job_in_resource_slot(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        resource_names = []
        run_kwargs = []
        completed = []

        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda task_id: completed.append(task_id),
        )

        def fake_resource_slot(name, **_kwargs):
            resource_names.append(name)
            return nullcontext()

        def fake_archive_job(**kwargs):
            run_kwargs.append(kwargs)
            return {
                "model_id": "123",
                "base_name": "Demo",
                "work_dir": "",
                "missing_3mf": [],
            }

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "resource_slot", side_effect=fake_resource_slot, create=True), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-3mf",
                "https://makerworld.com.cn/zh/models/123",
                {"three_mf_download": True, "model_id": "123", "instance_ids": ["profile-1", "", None, " profile-2 "]},
            )

        self.assertEqual(resource_names, [])
        self.assertFalse(run_kwargs[0]["skip_three_mf_fetch"])
        self.assertTrue(run_kwargs[0]["download_assets"])
        self.assertEqual(run_kwargs[0]["instance_ids"], ["profile-1", "profile-2"])
        self.assertEqual(completed, ["task-3mf"])


if __name__ == "__main__":
    unittest.main()
