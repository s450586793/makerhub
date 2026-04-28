import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.store import JsonStore
from app.services import remote_refresh
from app.services.remote_refresh import RemoteRefreshManager
import app.services.task_state as task_state_module
from app.services.task_state import TaskStateStore


class RemoteRefreshManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_remote_refresh_state_path = task_state_module.REMOTE_REFRESH_STATE_PATH
        self.original_append_remote_refresh_log = remote_refresh._append_remote_refresh_log
        self.original_append_business_log = remote_refresh.append_business_log

        task_state_module.REMOTE_REFRESH_STATE_PATH = self.temp_path / "remote_refresh_state.json"
        remote_refresh._append_remote_refresh_log = lambda *_args, **_kwargs: None
        remote_refresh.append_business_log = lambda *_args, **_kwargs: None

        self.store = JsonStore(self.temp_path / "config.json")
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 0 * * *"
        self.store.save(config)

        self.task_store = TaskStateStore()
        self.manager = RemoteRefreshManager(
            store=self.store,
            task_store=self.task_store,
            archive_manager=None,
        )

    def tearDown(self):
        self.manager._set_batch_running(False)
        task_state_module.REMOTE_REFRESH_STATE_PATH = self.original_remote_refresh_state_path
        remote_refresh._append_remote_refresh_log = self.original_append_remote_refresh_log
        remote_refresh.append_business_log = self.original_append_business_log
        self.temp_dir.cleanup()

    def test_manual_trigger_marks_state_running_when_accepted(self):
        self.manager._service_busy = lambda: False
        self.manager._start_batch_async = lambda _config: True

        result = self.manager.trigger_manual_refresh()

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(result["accepted"])
        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])
        self.assertIn("已手动触发一轮源端同步", state["last_message"])

    def test_manual_trigger_rejects_when_service_busy(self):
        self.manager._service_busy = lambda: True

        result = self.manager.trigger_manual_refresh()

        state = self.task_store.load_remote_refresh_state()
        self.assertFalse(result["accepted"])
        self.assertEqual(state["status"], "idle")
        self.assertFalse(state["running"])
        self.assertIn("请稍后再试手动同步", state["last_message"])

    def test_state_payload_keeps_running_status_for_manual_sync_when_schedule_disabled(self):
        config = self.store.load()
        config.remote_refresh.enabled = False
        self.store.save(config)
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            last_message="已手动触发一轮源端同步，正在启动。",
        )
        self.manager._set_batch_running(True)

        state = self.manager.state_payload()

        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])
        self.assertIn("已手动触发一轮源端同步", state["last_message"])

    def test_remote_refresh_state_preserves_parallel_items_and_metrics(self):
        state = self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            current_items=[
                {"id": "m1", "title": "模型 1", "progress": 20, "message": "运行中"},
                {"id": "m2", "title": "模型 2", "progress": 40, "message": "运行中"},
            ],
            last_batch_metrics={"comments": 12, "replies": 5},
            last_resource_waits={"disk_io": {"wait_count": 1}},
            last_slow_models=[{"model_dir": "m1", "total_duration_ms": 1200}],
        )

        self.assertEqual(len(state["current_items"]), 2)
        self.assertEqual(state["current_item"]["id"], "m1")
        self.assertEqual(state["last_batch_metrics"]["comments"], 12)
        self.assertEqual(state["last_resource_waits"]["disk_io"]["wait_count"], 1)
        self.assertEqual(state["last_slow_models"][0]["model_dir"], "m1")

    def test_remote_refresh_model_workers_uses_advanced_config(self):
        config = SimpleNamespace(
            advanced=SimpleNamespace(remote_refresh_model_workers=3)
        )
        high_config = SimpleNamespace(
            advanced=SimpleNamespace(remote_refresh_model_workers=99)
        )

        self.assertEqual(remote_refresh._remote_refresh_model_workers(config), 3)
        self.assertEqual(remote_refresh._remote_refresh_model_workers(high_config), 4)

    def test_run_batch_refreshes_models_concurrently(self):
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 2
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
            {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
            {"model_dir": "m3", "title": "模型 3", "origin_url": "https://makerworld.com.cn/model/3", "meta_path": str(self.temp_path / "m3" / "meta.json")},
        ]
        self.manager._pick_candidates = lambda: (
            items,
            {
                "eligible_total": 3,
                "selected_total": 3,
                "remaining_total": 0,
                "missing_cookie": 0,
                "local_or_invalid": 0,
            },
        )
        first_two_started = threading.Event()
        release = threading.Event()
        started: list[str] = []
        started_lock = threading.Lock()

        def fake_refresh_one(item, *, index, total, config):
            with started_lock:
                started.append(str(item.get("model_dir") or ""))
                if len(started) == 2:
                    first_two_started.set()
            first_two_started.wait(timeout=2)
            release.wait(timeout=2)
            time.sleep(0.01)
            return {
                "ok": True,
                "metrics": {
                    "model_dir": str(item.get("model_dir") or ""),
                    "title": str(item.get("title") or ""),
                    "comments": 1,
                    "total_duration_ms": 10,
                },
            }

        self.manager._refresh_one = fake_refresh_one
        try:
            runner = threading.Thread(target=lambda: self.manager._run_batch(config), daemon=True)
            runner.start()
            self.assertTrue(first_two_started.wait(timeout=2))
            with started_lock:
                self.assertEqual(len(started), 2)
            release.set()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive())
        finally:
            remote_refresh._remote_refresh_model_workers = original_workers
            release.set()

        state = self.task_store.load_remote_refresh_state()
        self.assertEqual(state["last_batch_succeeded"], 3)
        self.assertEqual(state["last_batch_failed"], 0)
        self.assertEqual(state["last_batch_metrics"]["comments"], 3)
        self.assertEqual(len(state["last_slow_models"]), 3)


if __name__ == "__main__":
    unittest.main()
