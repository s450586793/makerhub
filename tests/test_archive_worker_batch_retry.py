import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.archive_worker import ArchiveTaskManager


class _ArchiveBatchRefreshConfig:
    cookies = []
    proxy = None
    runtime = SimpleNamespace(worker_concurrency=2)
    advanced = SimpleNamespace(
        remote_refresh_model_workers=2,
        makerworld_request_limit=2,
        comment_asset_download_limit=4,
        three_mf_download_limit=1,
        disk_io_limit=1,
    )


class _ArchiveBatchRefreshStore:
    def load(self):
        return _ArchiveBatchRefreshConfig()


class ArchiveWorkerBatchRetryTest(unittest.TestCase):
    def test_enqueue_single_task_returns_existing_id_when_state_dedupes(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            enqueue_archive_task=lambda _item: {
                "enqueued": False,
                "existing_task_id": "existing-task",
            }
        )

        task_id = manager._enqueue_single_task(
            "https://makerworld.com.cn/zh/models/123",
            mode="single_model",
        )

        self.assertEqual(task_id, "existing-task")

    def test_ensure_worker_starts_configured_archive_workers(self):
        manager = ArchiveTaskManager(background_enabled=True)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(runtime=SimpleNamespace(worker_concurrency=3)))
        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=False, name=""):
                self.target = target
                self.daemon = daemon
                self.name = name
                self.started = False

            def is_alive(self):
                return self.started

            def start(self):
                self.started = True
                started.append(self.name)

        with patch("app.services.archive_worker.threading.Thread", side_effect=FakeThread):
            manager._ensure_worker()
            manager._ensure_worker()

        self.assertEqual(
            started,
            [
                "makerhub-archive-worker-1",
                "makerhub-archive-worker-2",
                "makerhub-archive-worker-3",
            ],
        )

    def test_ensure_worker_for_pending_repairs_duplicate_missing_3mf_retries_before_starting(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            manager.task_store.save_archive_queue(
                {
                    "active": [],
                    "queued": [
                        {
                            "id": "retry-profile-1",
                            "url": "https://makerworld.com/zh/models/2193050",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "missing_3mf_retry": True,
                                "model_id": "2193050",
                                "instance_id": "profile-1",
                                "instance_ids": ["profile-1"],
                            },
                        },
                        {
                            "id": "retry-profile-2",
                            "url": "https://makerworld.com/zh/models/2193050",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "missing_3mf_retry": True,
                                "model_id": "2193050",
                                "instance_id": "profile-2",
                            },
                        },
                    ],
                    "recent_failures": [],
                }
            )

            queue = manager.ensure_worker_for_pending()

        self.assertEqual(queue["queued_count"], 1)
        self.assertEqual(queue["queued"][0]["id"], "retry-profile-1")
        self.assertEqual(queue["queued"][0]["meta"]["instance_ids"], ["profile-1", "profile-2"])

    def test_ensure_worker_for_pending_does_not_requeue_expired_active_tasks_after_initial_repair(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "active-expired",
                            "url": "https://makerworld.com/zh/models/2193050",
                            "mode": "single_model",
                            "status": "running",
                            "lease_expires_at": "2026-06-04T09:00:00+08:00",
                            "attempt_count": 1,
                        }
                    ],
                    "queued": [],
                    "recent_failures": [],
                }
            )

            manager.ensure_worker_for_pending()
            queue = manager.ensure_worker_for_pending()

        self.assertEqual([item["id"] for item in queue["active"]], ["active-expired"])
        self.assertEqual(queue["queued"], [])

    def test_resume_keeps_batch_parents_tracking_but_prioritizes_single_model_child(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": f"batch-{index}",
                            "url": f"https://makerworld.com.cn/zh/@author{index}/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "message": "批量归档执行中：成功 0/1，运行中 0，排队中 1，失败 0。",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": f"https://makerworld.com.cn/zh/models/{900000 + index}",
                                        "task_key": f"model:{900000 + index}",
                                        "model_id": str(900000 + index),
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": f"child-{index}",
                                    }
                                ],
                                "batch_progress": {
                                    "total": 1,
                                    "completed": 0,
                                    "failed": 0,
                                    "running": 0,
                                    "queued": 1,
                                    "remaining": 1,
                                },
                            },
                        }
                        for index in range(10)
                    ],
                    "queued": [
                        {
                            "id": "child-0",
                            "url": "https://makerworld.com.cn/zh/models/900000",
                            "title": "https://makerworld.com.cn/zh/models/900000",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-0",
                                "batch_source_url": "https://makerworld.com.cn/zh/@author0/upload",
                            },
                        }
                    ],
                    "recent_failures": [],
                }
            )

            queue = manager.resume_pending_tasks()
            next_task = manager._next_executable_task(queue)

        self.assertEqual(next_task["id"], "child-0")

    def test_refresh_batch_restores_orphaned_parent_from_child_tasks(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "child-running",
                            "url": "https://makerworld.com.cn/zh/models/656269",
                            "title": "https://makerworld.com.cn/zh/models/656269",
                            "mode": "single_model",
                            "status": "running",
                            "meta": {
                                "batch_parent_id": "batch-missing",
                                "batch_source_url": "https://makerworld.com.cn/zh/@ace/upload",
                            },
                        }
                    ],
                    "queued": [
                        {
                            "id": "child-queued",
                            "url": "https://makerworld.com.cn/zh/models/656270",
                            "title": "https://makerworld.com.cn/zh/models/656270",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-missing",
                                "batch_source_url": "https://makerworld.com.cn/zh/@ace/upload",
                            },
                        }
                    ],
                    "recent_failures": [],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()
            refreshed_again = manager._refresh_batch_tasks()
            queue_after_second_refresh = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertTrue(refreshed_again)
        restored_batches = [
            item
            for item in queue["active"]
            if item["id"] == "batch-missing"
        ]
        self.assertEqual(len(restored_batches), 1)
        restored_batch = restored_batches[0]
        self.assertEqual(restored_batch["mode"], "author_upload")
        self.assertEqual(restored_batch["progress"], 60)
        self.assertEqual(
            restored_batch["message"],
            "批量归档执行中：成功 0/2，运行中 1，排队中 1，失败 0。",
        )
        self.assertEqual(restored_batch["meta"]["batch_progress"]["total"], 2)
        self.assertEqual(restored_batch["meta"]["batch_progress"]["running"], 1)
        self.assertEqual(restored_batch["meta"]["batch_progress"]["queued"], 1)
        expected_items = restored_batch["meta"]["batch_expected_items"]
        self.assertEqual(
            {item["task_key"] for item in expected_items},
            {"model:656269", "model:656270"},
        )
        self.assertEqual(
            len([item for item in queue_after_second_refresh["active"] if item["id"] == "batch-missing"]),
            1,
        )

    def test_refresh_batch_prefers_live_child_before_archived_key(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value={"model:973599"}):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-1",
                            "url": "https://makerworld.com/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com/zh/models/973599",
                                        "task_key": "model:973599",
                                        "model_id": "973599",
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": "missing-3mf-task",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [
                        {
                            "id": "missing-3mf-task",
                            "url": "https://makerworld.com/zh/models/973599",
                            "title": "https://makerworld.com/zh/models/973599",
                            "mode": "single_model",
                            "status": "queued",
                            "message": "等待重新下载缺失 3MF",
                            "meta": {"missing_3mf_retry": True},
                        }
                    ],
                    "recent_failures": [],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertEqual(queue["active"][0]["meta"]["batch_progress"]["queued"], 1)
        self.assertEqual(queue["queued_count"], 1)
        self.assertEqual(queue["active"][0]["meta"]["batch_expected_items"][0]["status"], "queued")

    def test_refresh_batch_counts_recent_three_mf_failure_before_archived_key(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value={"model:973599"}), \
                patch.object(manager.task_store, "complete_archive_task", wraps=manager.task_store.complete_archive_task) as complete_archive_task:
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-1",
                            "url": "https://makerworld.com/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com/zh/models/973599",
                                        "task_key": "model:973599",
                                        "model_id": "973599",
                                        "attempts": 1,
                                        "status": "archived",
                                        "last_task_id": "missing-3mf-task",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [],
                    "recent_failures": [
                        {
                            "id": "missing-3mf-task",
                            "url": "https://makerworld.com/zh/models/973599",
                            "title": "https://makerworld.com/zh/models/973599",
                            "mode": "single_model",
                            "status": "failed",
                            "message": "3MF 下载失败：需要完成验证。",
                            "meta": {"three_mf_download": True},
                        }
                    ],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertEqual(queue["active"], [])
        self.assertEqual(queue["running_count"], 0)
        self.assertEqual(queue["failed_count"], 1)
        self.assertEqual(
            complete_archive_task.call_args.kwargs["meta"]["batch_progress"]["completed"],
            0,
        )
        self.assertEqual(
            complete_archive_task.call_args.kwargs["meta"]["batch_progress"]["failed"],
            1,
        )
        self.assertEqual(complete_archive_task.call_args.kwargs["message"], "批量归档完成：成功 0 个，失败 1 个。")

    def test_refresh_batch_restores_parent_removed_from_recent_failures(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [],
                    "queued": [
                        {
                            "id": "child-queued",
                            "url": "https://makerworld.com.cn/zh/models/656270",
                            "title": "https://makerworld.com.cn/zh/models/656270",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-failed",
                                "batch_source_url": "https://makerworld.com.cn/zh/@ace/upload",
                            },
                        }
                    ],
                    "recent_failures": [
                        {
                            "id": "batch-failed",
                            "url": "https://makerworld.com.cn/zh/@ace/upload",
                            "title": "https://makerworld.com.cn/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "failed",
                            "message": "未找到可用 Cookie，请先到设置页配置对应站点 Cookie。",
                        }
                    ],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertEqual(
            len([item for item in queue["active"] if item["id"] == "batch-failed"]),
            1,
        )
        self.assertNotIn("batch-failed", {item["id"] for item in queue["recent_failures"]})

    def test_refresh_batch_merges_duplicate_parent_for_same_source_url(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)
        source_url = "https://makerworld.com.cn/zh/@ace/upload"

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-active",
                            "url": source_url,
                            "title": source_url,
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/1001",
                                        "task_key": "model:1001",
                                        "model_id": "1001",
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": "child-active",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [
                        {
                            "id": "batch-queued",
                            "url": source_url,
                            "title": source_url,
                            "mode": "author_upload",
                            "status": "queued",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/1002",
                                        "task_key": "model:1002",
                                        "model_id": "1002",
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": "child-queued",
                                    }
                                ]
                            },
                        },
                        {
                            "id": "child-active",
                            "url": "https://makerworld.com.cn/zh/models/1001",
                            "title": "https://makerworld.com.cn/zh/models/1001",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-active",
                                "batch_source_url": source_url,
                            },
                        },
                        {
                            "id": "child-queued",
                            "url": "https://makerworld.com.cn/zh/models/1002",
                            "title": "https://makerworld.com.cn/zh/models/1002",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-queued",
                                "batch_source_url": source_url,
                            },
                        },
                    ],
                    "recent_failures": [],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        source_parents = [
            item
            for item in queue["active"] + queue["queued"]
            if item.get("mode") == "author_upload" and item.get("url") == source_url
        ]
        self.assertEqual([item["id"] for item in source_parents], ["batch-active"])
        child_parent_ids = {
            (item.get("meta") or {}).get("batch_parent_id")
            for item in queue["queued"]
            if (item.get("meta") or {}).get("batch_source_url") == source_url
        }
        self.assertEqual(child_parent_ids, {"batch-active"})
        expected_items = source_parents[0]["meta"]["batch_expected_items"]
        self.assertEqual(
            {item["task_key"] for item in expected_items},
            {"model:1001", "model:1002"},
        )

    def test_refresh_batch_requeues_transient_failure_beyond_normal_limit(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-1",
                            "url": "https://makerworld.com.cn/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/656269",
                                        "task_key": "model:656269",
                                        "model_id": "656269",
                                        "attempts": 3,
                                        "status": "failed",
                                        "last_task_id": "failed-task",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [],
                    "recent_failures": [
                        {
                            "id": "failed-task",
                            "url": "https://makerworld.com.cn/zh/models/656269",
                            "title": "https://makerworld.com.cn/zh/models/656269",
                            "status": "failed",
                            "message": "curl 失败 default: code=6 stderr=curl: (6) Could not resolve host: makerworld.com.cn",
                        }
                    ],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertEqual(queue["failed_count"], 0)
        self.assertEqual(queue["queued_count"], 1)
        child = queue["active"][0]["meta"]["batch_expected_items"][0]
        self.assertEqual(child["status"], "queued")
        self.assertEqual(child["attempts"], 4)
        self.assertIn("Could not resolve host", child["last_failure_message"])

    def test_refresh_batch_keeps_non_transient_failure_at_normal_limit(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-1",
                            "url": "https://makerworld.com.cn/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/656269",
                                        "task_key": "model:656269",
                                        "model_id": "656269",
                                        "attempts": 3,
                                        "status": "failed",
                                        "last_task_id": "failed-task",
                                    },
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/656270",
                                        "task_key": "model:656270",
                                        "model_id": "656270",
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": "queued-task",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [
                        {
                            "id": "queued-task",
                            "url": "https://makerworld.com.cn/zh/models/656270",
                            "title": "https://makerworld.com.cn/zh/models/656270",
                            "status": "queued",
                        }
                    ],
                    "recent_failures": [
                        {
                            "id": "failed-task",
                            "url": "https://makerworld.com.cn/zh/models/656269",
                            "title": "https://makerworld.com.cn/zh/models/656269",
                            "status": "failed",
                            "message": "页面被 Cloudflare 验证拦截，请更新 cookie 后重试",
                        }
                    ],
                }
            )

            refreshed = manager._refresh_batch_tasks()
            queue = manager.task_store.load_archive_queue()

        self.assertTrue(refreshed)
        self.assertEqual(queue["failed_count"], 1)
        self.assertEqual(queue["queued_count"], 1)
        child = queue["active"][0]["meta"]["batch_expected_items"][0]
        self.assertEqual(child["status"], "failed")
        self.assertEqual(child["attempts"], 3)
        self.assertIn("Cloudflare", child["last_failure_message"])

    def test_refresh_batch_tasks_serializes_child_requeue(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)
        requeue_calls = []
        original_requeue = manager._requeue_batch_child

        def save_state(key, value):
            state[key] = value
            return value

        def slow_requeue(**kwargs):
            requeue_calls.append(kwargs["item"]["task_key"])
            time.sleep(0.05)
            return original_requeue(**kwargs)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=save_state), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-race",
                            "url": "https://makerworld.com.cn/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": "https://makerworld.com.cn/zh/models/656269",
                                        "task_key": "model:656269",
                                        "model_id": "656269",
                                        "attempts": 1,
                                        "status": "queued",
                                    }
                                ]
                            },
                        }
                    ],
                    "queued": [],
                    "recent_failures": [],
                }
            )

            with patch.object(manager, "_requeue_batch_child", side_effect=slow_requeue):
                threads = [threading.Thread(target=manager._refresh_batch_tasks) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

            queue = manager.task_store.load_archive_queue()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(requeue_calls, ["model:656269"])
        self.assertEqual(queue["queued_count"], 1)

    def test_run_batch_task_throttles_parent_progress_and_success_logs(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = _ArchiveBatchRefreshStore()
        discovered_items = [
            {"url": f"https://makerworld.com.cn/zh/models/{index}"}
            for index in range(1, 121)
        ]
        progress_updates = []
        structured_events = []

        def save_state(key, value):
            state[key] = value
            return value

        def enqueue_single(url, **_kwargs):
            return f"child-{url.rsplit('/', 1)[-1]}"

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=save_state), \
                patch("app.services.archive_worker._select_cookie", return_value="cookie"), \
                patch("app.services.archive_worker.run_discover_batch_urls_job", return_value={"items": discovered_items}), \
                patch.object(manager, "_queued_task_keys", return_value=set()), \
                patch.object(manager, "_archived_task_keys", return_value=set()), \
                patch.object(manager, "_enqueue_single_task", side_effect=enqueue_single), \
                patch.object(manager.task_store, "update_active_task", wraps=manager.task_store.update_active_task) as update_task, \
                patch("app.services.archive_worker._append_batch_queue_log", side_effect=lambda event, **payload: structured_events.append(event)):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "batch-large",
                            "url": "https://makerworld.com.cn/zh/@ace/upload",
                            "mode": "author_upload",
                            "status": "running",
                        }
                    ],
                    "queued": [],
                    "recent_failures": [],
                }
            )

            manager._run_batch_task(
                "batch-large",
                "https://makerworld.com.cn/zh/@ace/upload",
                "author_upload",
            )

        for call in update_task.call_args_list:
            kwargs = call.kwargs
            message = str(kwargs.get("message") or "")
            if message.startswith("正在加入归档队列："):
                progress_updates.append(message)

        self.assertLessEqual(len(progress_updates), 12)
        self.assertNotIn("child_enqueued", structured_events)
        self.assertEqual(structured_events.count("batch_enqueued"), 1)

    def test_run_single_task_records_current_archive_subtask_progress(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = _ArchiveBatchRefreshStore()
        progress_snapshots = []

        def fake_archive_job(**kwargs):
            kwargs["progress_callback"](
                {
                    "percent": 50,
                    "message": "正在下载附件（1/2）",
                }
            )
            progress_snapshots.append(manager.task_store.load_archive_queue())
            return {
                "base_name": "Demo",
                "work_dir": "/tmp/Demo",
                "missing_3mf": [],
                "action": "created",
                "model_id": "123",
            }

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.archive_worker._select_cookie", return_value="cookie"), \
                patch("app.services.archive_worker.run_archive_model_job", side_effect=fake_archive_job), \
                patch("app.services.archive_worker._read_three_mf_limit_guard", return_value={"active": False}), \
                patch("app.services.archive_worker._sync_account_health_for_archive_result"), \
                patch("app.services.archive_worker.invalidate_model_detail_cache"), \
                patch("app.services.archive_worker.invalidate_archive_snapshot"), \
                patch("app.services.archive_worker.upsert_archive_snapshot_model", return_value=True):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "single-1",
                            "url": "https://makerworld.com.cn/zh/models/123",
                            "mode": "single_model",
                            "status": "running",
                        }
                    ],
                    "queued": [],
                    "recent_failures": [],
                }
            )

            manager._run_single_task("single-1", "https://makerworld.com.cn/zh/models/123")

        subtasks = progress_snapshots[0]["active"][0]["subtasks"]
        by_type = {item["type"]: item for item in subtasks}

        self.assertEqual(by_type["attachments"]["status"], "running")
        self.assertEqual(by_type["attachments"]["progress"], 0)
        self.assertEqual(by_type["attachments"]["message"], "正在下载附件（1/2）")
        self.assertEqual(state["missing_3mf"]["items"], [])
        self.assertEqual(state["archive_queue"]["active"], [])


if __name__ == "__main__":
    unittest.main()
