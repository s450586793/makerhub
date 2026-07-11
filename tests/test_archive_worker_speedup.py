import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from app.services import archive_worker as archive_worker_module
from app.services.archive_worker import ArchiveTaskManager


class ArchiveWorkerSpeedupTest(unittest.TestCase):
    def test_three_mf_gate_for_url_uses_platform_snapshot(self):
        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "status": "ok",
                "detail": "账号正常。",
                "three_mf_gate": "cookie_invalid",
                "three_mf_detail": "国内站网页验证失效，请重新验证并更新 Cookie。",
            },
        ) as health_mock:
            result = archive_worker_module.three_mf_gate_for_url(
                "https://makerworld.com.cn/zh/models/1461337",
                {"source": "cn"},
            )

        health_mock.assert_called_once_with("cn")
        self.assertFalse(result["open"])
        self.assertEqual(result["platform"], "cn")
        self.assertEqual(result["state"], "cookie_invalid")
        self.assertEqual(result["message"], "国内站网页验证失效，请重新验证并更新 Cookie。")

    def test_three_mf_gate_for_url_opens_expired_daily_limit_gate(self):
        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "status": "ok",
                "detail": "",
                "three_mf_gate": "daily_limit",
                "three_mf_detail": "已达到 MakerWorld 每日下载上限。",
            },
        ), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "open_three_mf_gate") as open_gate:
            result = archive_worker_module.three_mf_gate_for_url(
                "https://makerworld.com.cn/zh/models/1461337",
                {"source": "cn"},
            )

        self.assertTrue(result["open"])
        self.assertEqual(result["state"], "open")
        open_gate.assert_called_once_with(
            "cn",
            source="three_mf_limit_guard",
            detail="MakerWorld 每日下载上限暂停已过期，恢复 3MF 下载。",
        )

    def test_three_mf_gate_for_url_keeps_active_daily_limit_gate(self):
        guard = {
            "active": True,
            "limited_until": "2026-06-25T00:00:00",
            "message": "今日额度用尽。",
            "model_url": "https://makerworld.com.cn/zh/models/1461337",
        }

        with patch.object(
            archive_worker_module,
            "get_account_health",
            return_value={
                "status": "ok",
                "detail": "",
                "three_mf_gate": "daily_limit",
                "three_mf_detail": "",
            },
        ), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value=guard), \
                patch.object(archive_worker_module, "open_three_mf_gate") as open_gate:
            result = archive_worker_module.three_mf_gate_for_url(
                "https://makerworld.com.cn/zh/models/1461337",
                {"source": "cn"},
            )

        self.assertFalse(result["open"])
        self.assertEqual(result["state"], "daily_limit")
        self.assertIn("今日额度用尽", result["message"])
        open_gate.assert_not_called()

    def test_submit_three_mf_download_respects_platform_gate_before_queueing(self):
        manager = ArchiveTaskManager(background_enabled=False)

        with patch.object(manager, "_deleted_task_lookup", return_value={}), \
                patch.object(manager, "_queue_state_snapshot", return_value={"active_by_key": {}, "queued_by_key": {}}), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(
                    archive_worker_module,
                    "three_mf_gate_for_url",
                    return_value={
                        "open": False,
                        "state": "cookie_invalid",
                        "message": "国内站网页验证失效，请重新验证并更新 Cookie。",
                    },
                ) as gate_mock, \
                patch.object(manager, "_enqueue_single_task") as enqueue_mock, \
                patch.object(manager, "_ensure_worker") as ensure_worker_mock:
            result = manager.submit_three_mf_download(
                "https://makerworld.com.cn/zh/models/1461337",
                model_id="1461337",
                title="CN Model",
                instance_ids=["profile-1"],
            )

        gate_mock.assert_called_once_with(
            "https://makerworld.com.cn/zh/models/1461337",
            {"source": "cn"},
        )
        enqueue_mock.assert_not_called()
        ensure_worker_mock.assert_not_called()
        self.assertFalse(result["accepted"])
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "cookie_invalid")
        self.assertEqual(result["message"], "国内站网页验证失效，请重新验证并更新 Cookie。")

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

    def test_ensure_worker_for_pending_keeps_live_threads_when_queue_has_no_active_tasks(self):
        manager = ArchiveTaskManager(background_enabled=True)
        started_threads = []

        class StaleThread:
            def is_alive(self):
                return True

        class FakeThread:
            def __init__(self, *, target, daemon=True, name=""):
                self.target = target
                self.daemon = daemon
                self.name = name

            def is_alive(self):
                return True

            def start(self):
                started_threads.append(self)

        live_threads = [StaleThread(), StaleThread()]
        manager._workers = live_threads
        manager.task_store = SimpleNamespace(
            resume_verification_paused_archive_tasks=lambda selector=None: {
                "running_count": 0,
                "queued_count": 3,
                "resumed_count": 0,
            },
            refresh_recent_active_archive_leases=lambda: {"running_count": 0, "queued_count": 3},
        )

        with patch.object(manager, "_repair_queue_before_worker_start", return_value={"running_count": 0, "queued_count": 3}), \
                patch.object(archive_worker_module, "_archive_worker_concurrency", return_value=2, create=True), \
                patch.object(archive_worker_module.threading, "Thread", side_effect=lambda **kwargs: FakeThread(**kwargs)):
            queue = manager.ensure_worker_for_pending()

        self.assertEqual(queue["queued_count"], 3)
        self.assertEqual(len(started_threads), 0)
        self.assertEqual(manager._workers, live_threads)
        self.assertIs(manager._worker, live_threads[0])

    def test_run_loop_retires_surplus_worker_after_concurrency_is_reduced(self):
        manager = ArchiveTaskManager(background_enabled=True)
        primary_worker = SimpleNamespace(is_alive=lambda: True)
        current_worker = threading.current_thread()
        manager._workers = [primary_worker, current_worker]
        queue_reads = []
        manager.task_store = SimpleNamespace(
            load_archive_queue=lambda: queue_reads.append(True) or {
                "active": [],
                "queued": [],
                "recent_failures": [],
            },
        )

        with patch.object(archive_worker_module, "_archive_worker_concurrency", return_value=1):
            manager._ensure_worker()

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}):
            manager._run_loop()

        self.assertEqual(queue_reads, [])
        self.assertEqual(manager._workers, [primary_worker])
        self.assertIs(manager._worker, primary_worker)

    def test_waiting_worker_rechecks_reduced_concurrency_before_leasing(self):
        manager = ArchiveTaskManager(background_enabled=True)
        primary_worker = SimpleNamespace(is_alive=lambda: True)
        current_worker = threading.current_thread()
        manager._workers = [primary_worker, current_worker]
        manager._worker_target_count = 2
        task = {
            "id": "queued-after-resize",
            "url": "https://makerworld.com.cn/zh/models/1",
            "status": "queued",
            "mode": "single_model",
        }
        lease_calls = []
        manager.task_store = SimpleNamespace(
            load_archive_queue=lambda: {
                "active": [],
                "queued": [task],
                "recent_failures": [],
            },
            lease_next_archive_task=lambda _selector: lease_calls.append(True),
        )

        @contextmanager
        def resize_while_waiting(_name, **_kwargs):
            manager._worker_target_count = 1
            yield 0.0

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(archive_worker_module, "resource_slot", side_effect=resize_while_waiting), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}):
            manager._run_loop()

        self.assertEqual(lease_calls, [])
        self.assertEqual(manager._workers, [primary_worker])

    def test_run_loop_acquires_makerworld_slot_before_leasing_task(self):
        manager = ArchiveTaskManager(background_enabled=False)
        task = {
            "id": "task-1",
            "url": "https://makerworld.com.cn/zh/models/1",
            "mode": "single_model",
            "meta": {},
        }
        events = []
        queue_reads = iter(
            [
                {"active": [], "queued": [task], "recent_failures": []},
                {"active": [], "queued": [], "recent_failures": []},
            ]
        )

        def lease_next(_selector):
            events.append("lease")
            return task

        manager.task_store = SimpleNamespace(
            load_archive_queue=lambda: next(queue_reads),
            lease_next_archive_task=lease_next,
            update_active_task=lambda *_args, **_kwargs: None,
        )

        @contextmanager
        def makerworld_slot(name, **_kwargs):
            self.assertEqual(name, "makerworld_page_api")
            events.append("resource_acquired")
            try:
                yield 0.0
            finally:
                events.append("resource_released")

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(manager, "_run_single_task", side_effect=lambda *_args, **_kwargs: events.append("run")), \
                patch.object(archive_worker_module, "resource_slot", side_effect=makerworld_slot, create=True), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}):
            manager._run_loop()

        self.assertEqual(events[:4], ["resource_acquired", "lease", "run", "resource_released"])

    def test_archive_progress_throttle_limits_same_stage_writes(self):
        self.assertTrue(hasattr(archive_worker_module, "_ArchiveProgressThrottle"))
        clock = iter([0.0, 0.1, 0.2, 2.1, 2.2, 2.3])
        throttle = archive_worker_module._ArchiveProgressThrottle(
            interval_seconds=2.0,
            clock=lambda: next(clock),
        )

        self.assertTrue(throttle.should_persist(percent=10, stage="metadata"))
        self.assertFalse(throttle.should_persist(percent=11, stage="metadata"))
        self.assertFalse(throttle.should_persist(percent=12, stage="metadata"))
        self.assertTrue(throttle.should_persist(percent=13, stage="metadata"))
        self.assertTrue(throttle.should_persist(percent=14, stage="media"))
        self.assertTrue(throttle.should_persist(percent=100, stage="finalize"))

    def test_run_single_task_uses_progress_throttle(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        updates = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda _task_id, **payload: updates.append(payload),
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        def fake_archive_job(**kwargs):
            callback = kwargs["progress_callback"]
            callback({"percent": 10, "message": "正在读取元数据", "archive_stage": "metadata"})
            callback({"percent": 11, "message": "仍在读取元数据", "archive_stage": "metadata"})
            callback({"percent": 40, "message": "正在整理图片", "archive_stage": "media"})
            callback({"percent": 90, "message": "正在整理目录", "archive_stage": "finalize"})
            callback({"percent": 100, "message": "归档完成", "archive_stage": "finalize"})
            return {"model_id": "123", "base_name": "Demo", "work_dir": "", "missing_3mf": []}

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"), \
                patch.object(archive_worker_module.time, "monotonic", side_effect=[0.0, 0.1, 0.2, 0.3, 0.4]):
            manager._run_single_task(
                "task-progress",
                "https://makerworld.com.cn/zh/models/123",
                {"missing_3mf_retry": True, "source": "cn", "model_id": "123"},
            )

        self.assertEqual([item["progress"] for item in updates], [10, 40, 90, 100])

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
                patch.object(archive_worker_module, "resource_slot", side_effect=lambda *_args, **_kwargs: nullcontext(0.0), create=True), \
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

    def test_run_loop_closes_three_mf_gate_when_page_fetch_needs_verification(self):
        manager = ArchiveTaskManager(background_enabled=False)
        task = {
            "id": "task-cloudflare",
            "url": "https://makerworld.com.cn/zh/models/1595694",
            "mode": "single_model",
            "status": "queued",
            "meta": {"missing_3mf_retry": True, "source": "cn"},
        }
        load_calls = []
        failed = []
        missing_updates = []

        def fake_load_archive_queue():
            load_calls.append(True)
            if len(load_calls) == 1:
                return {"active": [], "queued": [task], "recent_failures": []}
            return {"active": [], "queued": [], "recent_failures": []}

        manager.task_store = SimpleNamespace(
            load_archive_queue=fake_load_archive_queue,
            lease_next_archive_task=lambda _selector: task,
            update_active_task=lambda *_args, **_kwargs: None,
            update_missing_3mf_status=lambda **payload: missing_updates.append(payload),
            fail_archive_task=lambda task_id, message: failed.append((task_id, message)),
        )

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(manager, "_run_single_task", side_effect=RuntimeError("页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试")), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active", return_value=False), \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate, \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_loop()

        update_gate.assert_called_once_with(
            "cn",
            gate="cloudflare",
            reason="archive_task_failed",
            source="archive_task",
            detail="页面被 Cloudflare 验证拦截，请更新 cookie（含 cf_clearance）后重试",
            model_url="https://makerworld.com.cn/zh/models/1595694",
            model_id="1595694",
            instance_id="",
        )
        self.assertEqual(failed[0][0], "task-cloudflare")
        self.assertEqual(missing_updates[0]["model_id"], "1595694")

    def test_next_executable_task_skips_gated_missing_3mf_retry(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "retry-cn",
                    "url": "https://makerworld.com.cn/zh/models/1461337",
                    "mode": "single_model",
                    "meta": {
                        "missing_3mf_retry": True,
                        "source": "cn",
                        "model_id": "1461337",
                    },
                },
                {
                    "id": "normal-cn",
                    "url": "https://makerworld.com.cn/zh/models/2000000",
                    "mode": "single_model",
                    "meta": {},
                },
            ]
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            side_effect=[
                {
                    "open": False,
                    "state": "verification_required",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "platform": "cn",
                }
            ],
        ) as gate_mock:
            task = manager._next_executable_task(queue)

        self.assertEqual(task["id"], "normal-cn")
        gate_mock.assert_called_once_with(
            "https://makerworld.com.cn/zh/models/1461337",
            {"missing_3mf_retry": True, "source": "cn", "model_id": "1461337"},
        )

    def test_next_executable_task_skips_batch_parents_when_only_gated_three_mf_tasks_remain(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "batch-parent",
                    "url": "https://makerworld.com.cn/zh/@demo/upload",
                    "mode": "author_upload",
                    "meta": {
                        "batch_expected_items": [
                            {
                                "url": "https://makerworld.com.cn/zh/models/2000000",
                                "task_key": "model:2000000",
                                "status": "queued",
                            }
                        ]
                    },
                },
                {
                    "id": "retry-cn",
                    "url": "https://makerworld.com.cn/zh/models/1461337",
                    "mode": "single_model",
                    "meta": {"missing_3mf_retry": True, "source": "cn"},
                },
            ]
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={
                "open": False,
                "state": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "platform": "cn",
            },
        ):
            task = manager._next_executable_task(queue)

        self.assertIsNone(task)

    def test_next_executable_task_returns_none_when_only_gated_three_mf_tasks_remain(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "retry-cn",
                    "url": "https://makerworld.com.cn/zh/models/1461337",
                    "mode": "single_model",
                    "meta": {"missing_3mf_retry": True, "source": "cn"},
                },
                {
                    "id": "download-cn",
                    "url": "https://makerworld.com.cn/zh/models/1461338",
                    "mode": "single_model",
                    "meta": {"three_mf_download": True, "source": "cn"},
                },
            ]
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={
                "open": False,
                "state": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                "platform": "cn",
            },
        ):
            task = manager._next_executable_task(queue)

        self.assertIsNone(task)

    def test_next_executable_task_does_not_block_cn_three_mf_when_global_gate_is_closed(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "retry-global",
                    "url": "https://makerworld.com/zh/models/1461337",
                    "mode": "single_model",
                    "meta": {"missing_3mf_retry": True, "source": "global"},
                },
                {
                    "id": "download-cn",
                    "url": "https://makerworld.com.cn/zh/models/2000000",
                    "mode": "single_model",
                    "meta": {"three_mf_download": True, "source": "cn"},
                },
            ]
        }

        def fake_gate(url, meta):
            if meta.get("source") == "global":
                return {
                    "open": False,
                    "state": "verification_required",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "platform": "global",
                }
            return {"open": True, "state": "open", "message": "", "platform": "cn"}

        with patch.object(archive_worker_module, "three_mf_gate_for_url", side_effect=fake_gate) as gate_mock:
            task = manager._next_executable_task(queue)

        self.assertEqual(task["id"], "download-cn")
        self.assertEqual(gate_mock.call_count, 2)

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
            complete_archive_task=lambda task_id, **_payload: completed.append(task_id),
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
            complete_archive_task=lambda task_id, **_payload: completed.append(task_id),
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
        self.assertFalse(run_kwargs[0]["download_assets"])
        self.assertFalse(run_kwargs[0]["download_comment_assets"])
        self.assertFalse(run_kwargs[0].get("collect_comments_data", True))
        self.assertTrue(run_kwargs[0]["rebuild_archive"])
        self.assertEqual(run_kwargs[0]["instance_ids"], ["profile-1", "profile-2"])
        self.assertEqual(completed, ["task-3mf"])

    def test_three_mf_download_task_respects_platform_gate_without_failing_archive(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        run_kwargs = []
        replaced_missing = []
        completed = []

        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda model_id, items: replaced_missing.append((model_id, items)),
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda task_id, **_payload: completed.append(task_id),
        )

        def fake_archive_job(**kwargs):
            run_kwargs.append(kwargs)
            return {
                "model_id": "1461337",
                "base_name": "CN Model",
                "work_dir": "",
                "missing_3mf": [
                    {
                        "id": "profile-1",
                        "title": "0.2mm",
                        "downloadState": "cookie_invalid",
                        "downloadMessage": "国内站网页验证失效，请重新验证并更新 Cookie。",
                    }
                ],
            }

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": False, "state": "cookie_invalid", "message": "国内站网页验证失效，请重新验证并更新 Cookie。"}), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-3mf-gated",
                "https://makerworld.com.cn/zh/models/1461337",
                {"three_mf_download": True, "model_id": "1461337", "instance_ids": ["profile-1"]},
            )

        self.assertTrue(run_kwargs[0]["skip_three_mf_fetch"])
        self.assertEqual(run_kwargs[0]["three_mf_skip_state"], "cookie_invalid")
        self.assertEqual(replaced_missing[0][0], "1461337")
        self.assertEqual(replaced_missing[0][1][0]["status"], "cookie_invalid")
        self.assertEqual(completed, ["task-3mf-gated"])

    def test_missing_3mf_verification_failure_closes_gate_and_pauses_same_platform_retries(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        queue_state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "retry-cn",
                        "url": "https://makerworld.com.cn/zh/models/222",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {"missing_3mf_retry": True, "source": "cn", "model_id": "222"},
                    },
                    {
                        "id": "normal-cn",
                        "url": "https://makerworld.com.cn/zh/models/333",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {},
                    },
                    {
                        "id": "retry-global",
                        "url": "https://makerworld.com/zh/models/444",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {"missing_3mf_retry": True, "source": "global", "model_id": "444"},
                    },
                ],
                "recent_failures": [],
            },
            "missing_3mf": {
                "items": [
                    {
                        "model_id": "222",
                        "model_url": "https://makerworld.com.cn/zh/models/222",
                        "source": "cn",
                        "instance_id": "profile-222",
                        "title": "CN profile",
                        "status": "queued",
                        "message": "已存在于重新下载队列",
                    },
                    {
                        "model_id": "444",
                        "model_url": "https://makerworld.com/zh/models/444",
                        "source": "global",
                        "instance_id": "profile-444",
                        "title": "Global profile",
                        "status": "queued",
                        "message": "已存在于重新下载队列",
                    },
                ],
            }
        }

        def fake_load_state(key, default):
            return queue_state.get(key, default)

        def fake_save_state(key, value):
            queue_state[key] = value
            return value

        manager.task_store.replace_missing_3mf_for_model = lambda *_args, **_kwargs: None
        manager.task_store.remove_recent_failures_for_model = lambda *_args, **_kwargs: None
        manager.task_store.update_active_task = lambda *_args, **_kwargs: None
        manager.task_store.complete_archive_task = lambda *_args, **_kwargs: None

        def fake_archive_job(**_kwargs):
            return {
                "model_id": "111",
                "base_name": "CN Model",
                "work_dir": "",
                "missing_3mf": [
                    {
                        "id": "profile-1",
                        "title": "0.2mm",
                        "downloadState": "verification_required",
                        "downloadMessage": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    }
                ],
            }

        with patch("app.services.task_state.load_database_json_state", side_effect=fake_load_state), \
                patch("app.services.task_state.save_database_json_state", side_effect=fake_save_state), \
                patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-cn-retry",
                "https://makerworld.com.cn/zh/models/111",
                {"missing_3mf_retry": True, "source": "cn", "model_id": "111"},
            )

        update_gate.assert_called_once()
        self.assertEqual(update_gate.call_args.args[0], "cn")
        self.assertEqual(update_gate.call_args.kwargs["gate"], "verification_required")
        queued = {item["id"]: item for item in queue_state["archive_queue"]["queued"]}
        self.assertEqual(queued["retry-cn"]["status"], "paused")
        self.assertIn("MakerWorld 需要验证", queued["retry-cn"]["message"])
        self.assertEqual(queued["normal-cn"]["status"], "queued")
        self.assertEqual(queued["retry-global"]["status"], "queued")
        missing_by_model = {item["model_id"]: item for item in queue_state["missing_3mf"]["items"]}
        self.assertEqual(missing_by_model["222"]["status"], "verification_required")
        self.assertIn("MakerWorld 需要验证", missing_by_model["222"]["message"])
        self.assertEqual(missing_by_model["444"]["status"], "queued")

    def test_stale_cookie_failure_does_not_close_gate_or_pause_retries(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(
                cookies=[SimpleNamespace(platform="cn", cookie="token=new; cf_clearance=ok")],
                proxy=None,
                three_mf_limits=None,
            )
        )
        queue_state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "retry-cn",
                        "url": "https://makerworld.com.cn/zh/models/222",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {"missing_3mf_retry": True, "source": "cn", "model_id": "222"},
                    },
                ],
                "recent_failures": [],
            },
            "missing_3mf": {
                "items": [
                    {
                        "model_id": "222",
                        "model_url": "https://makerworld.com.cn/zh/models/222",
                        "source": "cn",
                        "instance_id": "profile-222",
                        "title": "CN profile",
                        "status": "queued",
                        "message": "已存在于重新下载队列",
                    },
                ],
            },
        }

        def fake_load_state(key, default):
            return queue_state.get(key, default)

        def fake_save_state(key, value):
            queue_state[key] = value
            return value

        manager.task_store.replace_missing_3mf_for_model = lambda *_args, **_kwargs: None
        manager.task_store.remove_recent_failures_for_model = lambda *_args, **_kwargs: None
        manager.task_store.update_active_task = lambda *_args, **_kwargs: None
        manager.task_store.complete_archive_task = lambda *_args, **_kwargs: None

        def fake_archive_job(**_kwargs):
            return {
                "model_id": "111",
                "base_name": "CN Model",
                "work_dir": "",
                "missing_3mf": [
                    {
                        "id": "profile-1",
                        "title": "0.2mm",
                        "downloadState": "auth_required",
                        "downloadMessage": "国区下载 3MF 需要有效登录态；请更新国内站 Cookie / token。",
                    }
                ],
            }

        log_events = []
        with patch("app.services.task_state.load_database_json_state", side_effect=fake_load_state), \
                patch("app.services.task_state.save_database_json_state", side_effect=fake_save_state), \
                patch.object(archive_worker_module, "_select_cookie", return_value="token=old"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_gate, \
                patch.object(archive_worker_module, "mark_account_ok") as mark_ok, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive", side_effect=lambda *args, **kwargs: log_events.append((args, kwargs))):
            manager._run_single_task(
                "task-cn-retry",
                "https://makerworld.com.cn/zh/models/111",
                {"missing_3mf_retry": True, "source": "cn", "model_id": "111"},
            )

        update_gate.assert_not_called()
        mark_ok.assert_not_called()
        queued = {item["id"]: item for item in queue_state["archive_queue"]["queued"]}
        self.assertEqual(queued["retry-cn"]["status"], "queued")
        self.assertTrue(any(args and args[0] == "stale_cookie_result_ignored" for args, _kwargs in log_events))

    def test_cn_three_mf_download_ignores_global_account_gate(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None))
        run_kwargs = []
        completed = []

        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda task_id, **_payload: completed.append(task_id),
        )

        def fake_archive_job(**kwargs):
            run_kwargs.append(kwargs)
            return {
                "model_id": "2000000",
                "base_name": "CN Model",
                "work_dir": "",
                "missing_3mf": [],
            }

        def fake_get_account_health(platform):
            if platform == "global":
                return {
                    "three_mf_gate": "verification_required",
                    "three_mf_detail": "国际站需要验证。",
                }
            return {"three_mf_gate": "open", "three_mf_detail": ""}

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(archive_worker_module, "get_account_health", side_effect=fake_get_account_health), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=fake_archive_job), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result"), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-cn-3mf",
                "https://makerworld.com.cn/zh/models/2000000",
                {"three_mf_download": True, "source": "cn", "model_id": "2000000"},
            )

        self.assertFalse(run_kwargs[0]["skip_three_mf_fetch"])
        self.assertEqual(run_kwargs[0]["three_mf_skip_state"], "")
        self.assertEqual(completed, ["task-cn-3mf"])


if __name__ == "__main__":
    unittest.main()
