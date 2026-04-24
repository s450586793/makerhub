import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
