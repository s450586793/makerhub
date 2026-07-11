import threading
import time
import unittest
from unittest.mock import patch

from app.schemas.models import AdvancedRuntimeConfig
from app.services import resource_limiter


class ResourceLimiterConfigTest(unittest.TestCase):
    def setUp(self):
        self.original_limits = dict(resource_limiter.RESOURCE_LIMITS)
        self.original_gates = dict(resource_limiter._GATES)

    def tearDown(self):
        resource_limiter.RESOURCE_LIMITS.clear()
        resource_limiter.RESOURCE_LIMITS.update(self.original_limits)
        resource_limiter._GATES.clear()
        resource_limiter._GATES.update(self.original_gates)

    def test_configure_resource_limits_updates_existing_gates(self):
        resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 1,
            "comment_asset_download_limit": 4,
            "three_mf_download_limit": 1,
            "disk_io_limit": 1,
        })
        resource_limiter.resource_snapshot()

        changed = resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 3,
            "comment_asset_download_limit": 5,
            "three_mf_download_limit": 2,
            "disk_io_limit": 2,
        })
        snapshot = resource_limiter.resource_snapshot()

        self.assertEqual(changed["makerworld_page_api"], 3)
        self.assertEqual(snapshot["makerworld_page_api"]["capacity"], 3)
        self.assertEqual(snapshot["comment_assets"]["capacity"], 5)
        self.assertEqual(snapshot["three_mf_download"]["capacity"], 2)
        self.assertEqual(snapshot["disk_io"]["capacity"], 2)

    def test_configure_resource_limits_clamps_raw_values(self):
        changed = resource_limiter.configure_resource_limits({
            "makerworld_request_limit": 99,
            "comment_asset_download_limit": 99,
            "three_mf_download_limit": 99,
            "disk_io_limit": 0,
        })

        self.assertEqual(changed["makerworld_page_api"], 8)
        self.assertEqual(changed["comment_assets"], 16)
        self.assertEqual(changed["three_mf_download"], 4)
        self.assertEqual(changed["disk_io"], 1)

    def test_advanced_runtime_defaults_honor_existing_env_limits(self):
        with patch.dict("os.environ", {
            "MAKERHUB_REMOTE_REFRESH_MODEL_WORKERS": "3",
            "MAKERHUB_LIMIT_MAKERWORLD_REQUESTS": "4",
            "MAKERHUB_LIMIT_COMMENT_ASSETS": "5",
            "MAKERHUB_LIMIT_THREE_MF_DOWNLOADS": "2",
            "MAKERHUB_LIMIT_DISK_IO": "2",
        }):
            config = AdvancedRuntimeConfig()

        self.assertEqual(config.remote_refresh_model_workers, 3)
        self.assertEqual(config.makerworld_request_limit, 4)
        self.assertEqual(config.comment_asset_download_limit, 5)
        self.assertEqual(config.three_mf_download_limit, 2)
        self.assertEqual(config.disk_io_limit, 2)

    def test_advanced_runtime_defaults_enable_scrapling_first(self):
        config = AdvancedRuntimeConfig()

        self.assertEqual(config.scraping_engine, "scrapling_first")

    def test_resource_gate_reports_waiters_and_serves_them_fifo(self):
        resource_limiter.RESOURCE_LIMITS["fifo_test"] = 1
        order = []
        first_started = threading.Event()
        second_started = threading.Event()

        def wait_for_slot(label, started):
            started.set()
            with resource_limiter.resource_slot("fifo_test"):
                order.append(label)
                time.sleep(0.01)

        with resource_limiter.resource_slot("fifo_test"):
            first = threading.Thread(target=wait_for_slot, args=("first", first_started))
            second = threading.Thread(target=wait_for_slot, args=("second", second_started))
            first.start()
            first_started.wait(timeout=1)

            deadline = time.monotonic() + 1
            snapshot = resource_limiter.resource_snapshot()["fifo_test"]
            while snapshot.get("waiting") != 1 and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = resource_limiter.resource_snapshot()["fifo_test"]
            self.assertEqual(snapshot.get("waiting"), 1)

            second.start()
            second_started.wait(timeout=1)

            deadline = time.monotonic() + 1
            snapshot = resource_limiter.resource_snapshot()["fifo_test"]
            while snapshot.get("waiting") != 2 and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = resource_limiter.resource_snapshot()["fifo_test"]
            self.assertEqual(snapshot.get("waiting"), 2)

        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(order, ["first", "second"])

    def test_resource_gate_does_not_let_reacquisition_bypass_existing_waiter(self):
        resource_limiter.RESOURCE_LIMITS["shared_test"] = 1
        first_acquired = threading.Event()
        release_first = threading.Event()
        order = []

        def repeated_requester():
            with resource_limiter.resource_slot("shared_test"):
                first_acquired.set()
                release_first.wait(timeout=1)
            with resource_limiter.resource_slot("shared_test"):
                order.append("repeat")

        def queued_requester():
            with resource_limiter.resource_slot("shared_test"):
                order.append("queued")

        first = threading.Thread(target=repeated_requester)
        queued = threading.Thread(target=queued_requester)
        first.start()
        self.assertTrue(first_acquired.wait(timeout=1))
        queued.start()

        deadline = time.monotonic() + 1
        snapshot = resource_limiter.resource_snapshot()["shared_test"]
        while snapshot.get("waiting") != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = resource_limiter.resource_snapshot()["shared_test"]
        self.assertEqual(snapshot.get("waiting"), 1)

        release_first.set()
        first.join(timeout=1)
        queued.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(queued.is_alive())
        self.assertEqual(order, ["queued", "repeat"])

    def test_resource_gate_is_reentrant_for_same_thread(self):
        resource_limiter.RESOURCE_LIMITS["reentrant_test"] = 1
        completed = threading.Event()
        snapshots = []

        def acquire_nested():
            with resource_limiter.resource_slot("reentrant_test"):
                with resource_limiter.resource_slot("reentrant_test"):
                    snapshots.append(resource_limiter.resource_snapshot()["reentrant_test"])
            completed.set()

        thread = threading.Thread(target=acquire_nested, daemon=True)
        thread.start()
        thread.join(timeout=0.2)

        self.assertTrue(completed.is_set())
        self.assertEqual(snapshots[0]["active"], 1)
        self.assertEqual(resource_limiter.resource_snapshot()["reentrant_test"]["active"], 0)


if __name__ == "__main__":
    unittest.main()
