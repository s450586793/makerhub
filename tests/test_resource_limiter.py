import os
import threading
import time
import unittest
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.schemas.models import AdvancedRuntimeConfig
from app.services import resource_limiter


SPAWN_CONTEXT = get_context("spawn")


def _configure_process_resource(name, capacity, state_dir):
    resource_limiter.STATE_DIR = Path(state_dir)
    resource_limiter.RESOURCE_LIMITS[name] = capacity
    resource_limiter._GATES.clear()


def _counted_resource_worker(state_dir, name, capacity, ready_queue, start_event, active, max_active, counter_lock):
    _configure_process_resource(name, capacity, state_dir)
    ready_queue.put(os.getpid())
    start_event.wait(timeout=5)
    with resource_limiter.resource_slot(name):
        with counter_lock:
            active.value += 1
            max_active.value = max(max_active.value, active.value)
        time.sleep(0.3)
        with counter_lock:
            active.value -= 1


def _holding_resource_worker(state_dir, name, capacity, entered_queue, release_event):
    _configure_process_resource(name, capacity, state_dir)
    with resource_limiter.resource_slot(name):
        entered_queue.put(os.getpid())
        release_event.wait(timeout=10)


def _sleeping_resource_worker(state_dir, name, capacity, entered_queue):
    _configure_process_resource(name, capacity, state_dir)
    with resource_limiter.resource_slot(name):
        entered_queue.put(os.getpid())
        time.sleep(30)


def _single_resource_worker(state_dir, name, capacity, entered_queue, ready_queue=None):
    _configure_process_resource(name, capacity, state_dir)
    if ready_queue is not None:
        ready_queue.put(os.getpid())
    with resource_limiter.resource_slot(name):
        entered_queue.put(os.getpid())


def _delayed_resource_worker(state_dir, name, capacity, ready_queue, start_event, entered_queue):
    _configure_process_resource(name, capacity, state_dir)
    ready_queue.put(os.getpid())
    start_event.wait(timeout=10)
    with resource_limiter.resource_slot(name):
        entered_queue.put(os.getpid())


class ResourceLimiterConfigTest(unittest.TestCase):
    def setUp(self):
        self.original_limits = dict(resource_limiter.RESOURCE_LIMITS)
        self.original_gates = dict(resource_limiter._GATES)
        self.original_published_limits = dict(resource_limiter._PUBLISHED_RESOURCE_LIMITS)
        self.original_state_dir = resource_limiter.STATE_DIR
        self.temp_dir = TemporaryDirectory()
        resource_limiter._GATES.clear()
        resource_limiter._PUBLISHED_RESOURCE_LIMITS.clear()
        resource_limiter.STATE_DIR = Path(self.temp_dir.name)

    def tearDown(self):
        resource_limiter.RESOURCE_LIMITS.clear()
        resource_limiter.RESOURCE_LIMITS.update(self.original_limits)
        resource_limiter._GATES.clear()
        resource_limiter._GATES.update(self.original_gates)
        resource_limiter._PUBLISHED_RESOURCE_LIMITS.clear()
        resource_limiter._PUBLISHED_RESOURCE_LIMITS.update(self.original_published_limits)
        resource_limiter.STATE_DIR = self.original_state_dir
        self.temp_dir.cleanup()

    @staticmethod
    def _stop_processes(processes):
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

    def test_resource_slots_limit_total_concurrency_across_spawned_processes(self):
        ready_queue = SPAWN_CONTEXT.Queue()
        start_event = SPAWN_CONTEXT.Event()
        active = SPAWN_CONTEXT.Value("i", 0)
        max_active = SPAWN_CONTEXT.Value("i", 0)
        counter_lock = SPAWN_CONTEXT.Lock()
        resource_limiter._publish_global_capacity("spawn_total", 2)
        processes = [
            SPAWN_CONTEXT.Process(
                target=_counted_resource_worker,
                args=(self.temp_dir.name, "spawn_total", 2, ready_queue, start_event, active, max_active, counter_lock),
            )
            for _index in range(6)
        ]

        try:
            for process in processes:
                process.start()
            for _process in processes:
                ready_queue.get(timeout=5)
            start_event.set()
            for process in processes:
                process.join(timeout=8)

            self.assertTrue(all(not process.is_alive() for process in processes))
            self.assertTrue(all(process.exitcode == 0 for process in processes))
            self.assertGreaterEqual(max_active.value, 2)
            self.assertLessEqual(max_active.value, 2)
        finally:
            self._stop_processes(processes)

    def test_killing_holder_releases_global_resource_slot(self):
        holder_entered = SPAWN_CONTEXT.Queue()
        waiter_entered = SPAWN_CONTEXT.Queue()
        waiter_ready = SPAWN_CONTEXT.Queue()
        holder = SPAWN_CONTEXT.Process(
            target=_sleeping_resource_worker,
            args=(self.temp_dir.name, "kill_release", 1, holder_entered),
        )
        waiter = SPAWN_CONTEXT.Process(
            target=_single_resource_worker,
            args=(self.temp_dir.name, "kill_release", 1, waiter_entered, waiter_ready),
        )
        processes = [holder, waiter]

        try:
            holder.start()
            holder_entered.get(timeout=5)
            waiter.start()
            waiter_ready.get(timeout=5)
            try:
                acquired_before_kill = waiter_entered.get(timeout=0.3)
            except Empty:
                acquired_before_kill = None

            holder.kill()
            holder.join(timeout=5)
            acquired_after_kill = acquired_before_kill or waiter_entered.get(timeout=5)
            waiter.join(timeout=5)

            self.assertIsNone(acquired_before_kill)
            self.assertTrue(acquired_after_kill)
            self.assertEqual(waiter.exitcode, 0)
        finally:
            self._stop_processes(processes)

    def test_resource_slot_shrink_drains_existing_holders_before_new_entry(self):
        holders_entered = SPAWN_CONTEXT.Queue()
        contender_entered = SPAWN_CONTEXT.Queue()
        contender_ready = SPAWN_CONTEXT.Queue()
        release_holders = [SPAWN_CONTEXT.Event() for _index in range(3)]
        resource_limiter._publish_global_capacity("shrink_drain", 3)
        holders = [
            SPAWN_CONTEXT.Process(
                target=_holding_resource_worker,
                args=(self.temp_dir.name, "shrink_drain", 3, holders_entered, release_holders[index]),
            )
            for index in range(3)
        ]
        contender = SPAWN_CONTEXT.Process(
            target=_single_resource_worker,
            args=(self.temp_dir.name, "shrink_drain", 1, contender_entered, contender_ready),
        )
        processes = [*holders, contender]

        try:
            for holder in holders:
                holder.start()
                holders_entered.get(timeout=5)

            resource_limiter._publish_global_capacity("shrink_drain", 1)
            contender.start()
            contender_ready.get(timeout=5)
            try:
                acquired_before_drain = contender_entered.get(timeout=0.3)
            except Empty:
                acquired_before_drain = None

            release_holders[0].set()
            try:
                acquired_before_high_slots_drained = contender_entered.get(timeout=0.3)
            except Empty:
                acquired_before_high_slots_drained = None

            for release_holder in release_holders[1:]:
                release_holder.set()
            acquired_after_drain = (
                acquired_before_drain
                or acquired_before_high_slots_drained
                or contender_entered.get(timeout=5)
            )
            for process in processes:
                process.join(timeout=5)

            self.assertIsNone(acquired_before_drain)
            self.assertIsNone(acquired_before_high_slots_drained)
            self.assertTrue(acquired_after_drain)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
        finally:
            for release_holder in release_holders:
                release_holder.set()
            self._stop_processes(processes)

    def test_stale_old_capacity_process_cannot_acquire_retired_slot_after_shrink(self):
        old_ready = SPAWN_CONTEXT.Queue()
        old_start = SPAWN_CONTEXT.Event()
        old_entered = SPAWN_CONTEXT.Queue()
        shrink_entered = SPAWN_CONTEXT.Queue()
        release_shrink = SPAWN_CONTEXT.Event()
        old_process = SPAWN_CONTEXT.Process(
            target=_delayed_resource_worker,
            args=(
                self.temp_dir.name,
                "shrink_epoch",
                3,
                old_ready,
                old_start,
                old_entered,
            ),
        )
        shrink_holder = SPAWN_CONTEXT.Process(
            target=_holding_resource_worker,
            args=(self.temp_dir.name, "shrink_epoch", 1, shrink_entered, release_shrink),
        )
        processes = [old_process, shrink_holder]

        try:
            old_process.start()
            old_ready.get(timeout=5)
            shrink_holder.start()
            shrink_entered.get(timeout=5)

            old_start.set()
            try:
                entered_before_release = old_entered.get(timeout=0.4)
            except Empty:
                entered_before_release = None

            release_shrink.set()
            entered_after_release = entered_before_release or old_entered.get(timeout=5)
            for process in processes:
                process.join(timeout=5)

            self.assertIsNone(entered_before_release)
            self.assertTrue(entered_after_release)
            self.assertTrue(all(process.exitcode == 0 for process in processes))
        finally:
            release_shrink.set()
            old_start.set()
            self._stop_processes(processes)

    def test_resource_slot_directories_do_not_collide_after_name_sanitizing(self):
        first = resource_limiter._resource_slot_directory("a/b")
        second = resource_limiter._resource_slot_directory("a?b")

        self.assertNotEqual(first, second)

    def test_stale_low_capacity_acquire_does_not_overwrite_authoritative_expansion(self):
        resource_limiter._publish_global_capacity("expand_authority", 3)

        handle = resource_limiter._try_acquire_global_slot("expand_authority", 1)
        resource_limiter._release_global_slot(handle)

        control_path = resource_limiter._resource_slot_directory("expand_authority") / "capacity.control"
        self.assertEqual(control_path.read_text(encoding="utf-8").strip(), "3")

    def test_child_fails_closed_when_capacity_control_is_corrupt(self):
        slot_dir = resource_limiter._resource_slot_directory("corrupt_control")
        slot_dir.mkdir(parents=True, exist_ok=True)
        control_path = slot_dir / "capacity.control"
        control_path.write_text("damaged", encoding="utf-8")

        first_handle = resource_limiter._try_acquire_global_slot("corrupt_control", 3)
        second_handle = resource_limiter._try_acquire_global_slot("corrupt_control", 3)
        try:
            self.assertIsNotNone(first_handle)
            self.assertIsNone(second_handle)
            self.assertEqual(control_path.read_text(encoding="utf-8"), "damaged")
        finally:
            resource_limiter._release_global_slot(first_handle)
            resource_limiter._release_global_slot(second_handle)

    def test_child_fails_closed_without_publishing_missing_capacity_control(self):
        slot_dir = resource_limiter._resource_slot_directory("missing_control")
        control_path = slot_dir / "capacity.control"

        first_handle = resource_limiter._try_acquire_global_slot("missing_control", 3)
        second_handle = resource_limiter._try_acquire_global_slot("missing_control", 3)
        try:
            self.assertIsNotNone(first_handle)
            self.assertIsNone(second_handle)
            self.assertFalse(control_path.exists())
        finally:
            resource_limiter._release_global_slot(first_handle)
            resource_limiter._release_global_slot(second_handle)

    def test_capacity_publish_failure_preserves_previous_control_file(self):
        resource_limiter._publish_global_capacity("atomic_publish", 2)
        control_path = resource_limiter._resource_slot_directory("atomic_publish") / "capacity.control"

        with patch("app.services.resource_limiter.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                resource_limiter._publish_global_capacity("atomic_publish", 3)

        self.assertEqual(control_path.read_text(encoding="utf-8"), "2")
        self.assertEqual(list(control_path.parent.glob("capacity.control.*.tmp")), [])

    def test_authoritative_config_refresh_repairs_corrupt_capacity_control(self):
        config = {"makerworld_request_limit": 3}
        resource_limiter.configure_resource_limits(config, publish_global=True)
        control_path = (
            resource_limiter._resource_slot_directory("makerworld_page_api") / "capacity.control"
        )
        control_path.write_text("damaged", encoding="utf-8")

        resource_limiter.configure_resource_limits(config, publish_global=True)

        self.assertEqual(control_path.read_text(encoding="utf-8"), "3")

    def test_global_resource_slots_are_isolated_by_resource_name(self):
        holder_entered = SPAWN_CONTEXT.Queue()
        other_entered = SPAWN_CONTEXT.Queue()
        release_holder = SPAWN_CONTEXT.Event()
        holder = SPAWN_CONTEXT.Process(
            target=_holding_resource_worker,
            args=(self.temp_dir.name, "isolated_a", 1, holder_entered, release_holder),
        )
        other = SPAWN_CONTEXT.Process(
            target=_single_resource_worker,
            args=(self.temp_dir.name, "isolated_b", 1, other_entered),
        )
        processes = [holder, other]

        try:
            holder.start()
            holder_entered.get(timeout=5)
            other.start()
            self.assertTrue(other_entered.get(timeout=5))
            other.join(timeout=5)
            self.assertEqual(other.exitcode, 0)
        finally:
            release_holder.set()
            self._stop_processes(processes)

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
