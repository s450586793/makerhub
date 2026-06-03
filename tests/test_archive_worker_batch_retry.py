import unittest
from unittest.mock import patch

from app.services.archive_worker import ArchiveTaskManager


class _ArchiveBatchRefreshConfig:
    cookies = []
    proxy = None


class _ArchiveBatchRefreshStore:
    def load(self):
        return _ArchiveBatchRefreshConfig()


class ArchiveWorkerBatchRetryTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
