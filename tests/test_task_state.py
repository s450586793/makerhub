import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from app.services import state_events
from app.services.task_state import (
    TaskStateStore,
    _ORGANIZER_TERMINAL_LOG_CACHE,
    _normalize_archive_queue,
    _normalize_organize_tasks,
    _normalize_source_refresh_queue,
    _normalize_source_refresh_runs,
    compact_remote_refresh_state,
)


class OrganizeTaskStateTest(unittest.TestCase):
    def test_normalize_preserves_last_import_batch(self):
        payload = {
            "items": [
                {
                    "file_name": "demo.3mf",
                    "source_path": "/app/local/web_uploads/demo.3mf",
                    "status": "success",
                }
            ],
            "last_import": {
                "uploaded_at": "2026-05-03T16:06:55+08:00",
                "uploaded_count": 2,
                "source_dir": "/app/local",
                "upload_dir": "/app/local/web_uploads",
                "files": [
                    {
                        "file_name": "demo.3mf",
                        "source_path": "/app/local/web_uploads/demo.3mf",
                        "size": 123,
                    },
                    "legacy.3mf",
                ],
            },
        }

        normalized = _normalize_organize_tasks(payload)

        self.assertEqual(normalized["last_import"]["uploaded_count"], 2)
        self.assertEqual(normalized["last_import"]["upload_dir"], "/app/local/web_uploads")
        self.assertEqual(
            normalized["last_import"]["files"],
            [
                {
                    "file_name": "demo.3mf",
                    "source_path": "/app/local/web_uploads/demo.3mf",
                    "size": 123,
                },
                {
                    "file_name": "legacy.3mf",
                    "source_path": "",
                    "size": 0,
                },
            ],
        )

    def test_normalize_backfills_last_import_status_from_organizer_log(self):
        payload = {
            "items": [],
            "last_import": {
                "uploaded_at": "2026-05-03T16:06:55+08:00",
                "uploaded_count": 2,
                "files": [
                    {
                        "file_name": "done.3mf",
                        "source_path": "/app/local/web_uploads/done.3mf",
                        "size": 123,
                    },
                    {
                        "file_name": "same.3mf",
                        "source_path": "/app/local/web_uploads/same.3mf",
                        "size": 456,
                    },
                ],
            },
        }

        with TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "organizer.log"
            log_path.write_text(
                "\n".join(
                    [
                        '{"time": "2026-05-03T16:07:05+08:00", "event": "organized", "source": "/app/local/web_uploads/done.3mf"}',
                        '{"time": "2026-05-03T16:07:10+08:00", "event": "duplicate_skipped", "source": "/app/local/web_uploads/same.3mf"}',
                    ]
                ),
                encoding="utf-8",
            )

            with patch("app.services.task_state.ORGANIZER_LOG_PATH", log_path):
                _ORGANIZER_TERMINAL_LOG_CACHE.update({"mtime_ns": 0, "size": 0, "events": []})
                normalized = _normalize_organize_tasks(payload)

        files = normalized["last_import"]["files"]
        self.assertEqual(files[0]["status"], "success")
        self.assertEqual(files[1]["status"], "skipped")

    def test_normalize_preserves_snapshot_ready_and_backfills_legacy_success(self):
        payload = {
            "items": [
                {"id": "new", "status": "success", "snapshot_ready": False},
                {"id": "old", "status": "success"},
                {"id": "running", "status": "running"},
            ],
        }

        normalized = _normalize_organize_tasks(payload)
        items = {item["id"]: item for item in normalized["items"]}

        self.assertFalse(items["new"]["snapshot_ready"])
        self.assertTrue(items["old"]["snapshot_ready"])
        self.assertFalse(items["running"]["snapshot_ready"])

    def test_normalize_preserves_local_package_import_metadata(self):
        payload = {
            "items": [
                {
                    "id": "pkg-1",
                    "title": "Demo",
                    "status": "queued",
                    "kind": "local_package_import",
                    "staging_dir": "/app/state/import_uploads/demo",
                    "package_source": "Demo.zip",
                    "package_title": "Demo",
                    "meta": {"content_length": "1024", "received_bytes": 512},
                }
            ],
        }

        normalized = _normalize_organize_tasks(payload)
        item = normalized["items"][0]

        self.assertEqual(item["kind"], "local_package_import")
        self.assertEqual(item["staging_dir"], "/app/state/import_uploads/demo")
        self.assertEqual(item["package_source"], "Demo.zip")
        self.assertEqual(item["package_title"], "Demo")
        self.assertEqual(item["meta"]["content_length"], "1024")
        self.assertEqual(item["meta"]["received_bytes"], 512)

    def test_normalize_preserves_local_package_original_source_path(self):
        payload = {
            "items": [
                {
                    "id": "pkg-local",
                    "title": "索尼克托架",
                    "status": "queued",
                    "kind": "local_package_import",
                    "source_path": "/app/local/索尼克托架.stl",
                    "staging_dir": "/app/state/import_uploads/sonic",
                    "package_source": "索尼克托架.stl",
                    "package_title": "索尼克托架",
                    "original_source_path": "/app/local/索尼克托架.stl",
                }
            ],
        }

        normalized = _normalize_organize_tasks(payload)
        item = normalized["items"][0]

        self.assertEqual(item["original_source_path"], "/app/local/索尼克托架.stl")


class ArchiveQueueStateTest(unittest.TestCase):
    def test_archive_queue_backfills_ordered_subtasks_for_legacy_items(self):
        normalized = _normalize_archive_queue(
            {
                "active": [
                    {
                        "id": "task-1",
                        "url": "https://makerworld.com.cn/zh/models/123",
                        "mode": "single_model",
                        "status": "running",
                        "progress": 42,
                        "message": "正在整理摘要与设计图片",
                    }
                ]
            }
        )

        subtasks = normalized["active"][0]["subtasks"]

        self.assertEqual(
            [item["type"] for item in subtasks],
            ["metadata", "media", "attachments", "comments", "three_mf", "finalize"],
        )
        self.assertEqual(subtasks[0]["status"], "done")
        self.assertEqual(subtasks[1]["status"], "running")
        self.assertEqual(subtasks[1]["progress"], 20)
        self.assertEqual(subtasks[-1]["label"], "落盘与索引")

    def test_update_active_task_marks_subtask_progress_from_archive_stage(self):
        state = {}
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": "task-1",
                            "url": "https://makerworld.com.cn/zh/models/123",
                            "mode": "single_model",
                            "status": "running",
                            "progress": 30,
                        }
                    ],
                    "queued": [],
                    "recent_failures": [],
                }
            )

            queue = store.update_active_task(
                "task-1",
                progress=52,
                message="正在下载附件（1/2）",
                archive_stage="attachments",
                archive_stage_progress=50,
            )

        subtasks = queue["active"][0]["subtasks"]
        by_type = {item["type"]: item for item in subtasks}

        self.assertEqual(by_type["metadata"]["status"], "done")
        self.assertEqual(by_type["media"]["status"], "done")
        self.assertEqual(by_type["attachments"]["status"], "running")
        self.assertEqual(by_type["attachments"]["progress"], 50)
        self.assertEqual(by_type["attachments"]["message"], "正在下载附件（1/2）")
        self.assertEqual(by_type["comments"]["status"], "pending")

    def test_normalize_source_refresh_queue_counts_active_queued_and_failures(self):
        normalized = _normalize_source_refresh_queue(
            {
                "active": [
                    {
                        "id": "task-1",
                        "run_id": "run-1",
                        "model_dir": "MW_1",
                        "title": "模型 1",
                        "url": "https://makerworld.com.cn/model/1",
                        "status": " RUNNING ",
                        "attempts": "2",
                        "message": "<html>bad</html>",
                    }
                ],
                "queued": [
                    {
                        "id": "task-2",
                        "run_id": "run-1",
                        "model_dir": "MW_2",
                        "url": "https://makerworld.com/model/2",
                    }
                ],
                "recent_failures": [{"id": "task-3", "status": "timed_out", "message": "timeout"}],
            }
        )

        self.assertEqual(normalized["running_count"], 1)
        self.assertEqual(normalized["queued_count"], 1)
        self.assertEqual(normalized["failed_count"], 1)
        self.assertEqual(normalized["active"][0]["status"], "running")
        self.assertEqual(normalized["active"][0]["attempts"], 2)
        self.assertNotIn("<html>", normalized["active"][0]["message"])
        self.assertIn("HTML 页面", normalized["active"][0]["message"])
        self.assertEqual(normalized["queued"][0]["status"], "queued")

    def test_normalize_source_refresh_runs_preserves_active_and_completed_summary(self):
        normalized = _normalize_source_refresh_runs(
            {
                "active_run": {
                    "run_id": "run-1",
                    "status": "running",
                    "candidate_total": "10",
                    "completed_total": "4",
                    "succeeded_total": "3",
                    "failed_total": "1",
                    "current_items": [{"id": "MW_1", "title": "模型 1"}],
                    "message": "运行中",
                },
                "last_completed_run": {
                    "run_id": "run-0",
                    "status": "completed",
                    "candidate_total": 8,
                    "completed_total": 8,
                },
                "last_defer_reason": "archive_queue_busy",
            }
        )

        self.assertEqual(normalized["active_run"]["run_id"], "run-1")
        self.assertEqual(normalized["active_run"]["remaining_total"], 6)
        self.assertEqual(normalized["active_run"]["current_items"][0]["title"], "模型 1")
        self.assertEqual(normalized["last_completed_run"]["run_id"], "run-0")
        self.assertEqual(normalized["last_defer_reason"], "archive_queue_busy")

    def test_task_state_store_persists_source_refresh_queue_and_runs(self):
        rows = []
        state = {}
        store = TaskStateStore()

        def capture(scope, event_type, payload):
            rows.append((scope, event_type, payload))

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=capture):
            queue = store.save_source_refresh_queue(
                {
                    "queued": [
                        {
                            "id": "task-1",
                            "run_id": "run-1",
                            "model_dir": "MW_1",
                            "url": "https://makerworld.com.cn/model/1",
                        }
                    ]
                }
            )
            runs = store.save_source_refresh_runs(
                {
                    "active_run": {
                        "run_id": "run-1",
                        "status": "queued",
                        "candidate_total": 1,
                    }
                }
            )

        self.assertIn("source_refresh_queue", state)
        self.assertIn("source_refresh_runs", state)
        self.assertEqual(queue["queued_count"], 1)
        self.assertEqual(runs["active_run"]["run_id"], "run-1")
        self.assertIn(("source_refresh_queue", "state.changed"), [(row[0], row[1]) for row in rows])
        self.assertIn(("source_refresh_runs", "state.changed"), [(row[0], row[1]) for row in rows])

    def test_update_archive_queue_skips_save_and_event_when_payload_unchanged(self):
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "task-1",
                        "url": "https://makerworld.com.cn/zh/models/1",
                        "title": "https://makerworld.com.cn/zh/models/1",
                        "mode": "single_model",
                        "status": "queued",
                        "progress": 0,
                        "message": "等待归档",
                        "updated_at": "2026-06-03T20:00:00+08:00",
                    }
                ],
                "recent_failures": [],
            }
        }
        saves = []
        events = []
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: saves.append((key, value)) or state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store._update_archive_queue(lambda payload: payload)

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(saves, [])
        self.assertEqual(events, [])

    def test_update_archive_queue_saves_and_publishes_when_payload_changes(self):
        state = {"archive_queue": {"active": [], "queued": [], "recent_failures": []}}
        saves = []
        events = []
        store = TaskStateStore()

        def mutate(payload):
            payload["queued"] = [
                {
                    "id": "task-1",
                    "url": "https://makerworld.com.cn/zh/models/1",
                    "mode": "single_model",
                    "status": "queued",
                }
            ]
            return payload

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: saves.append((key, value)) or state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store._update_archive_queue(mutate)

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(len(saves), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "archive_queue")

    def test_clear_recent_failures_preserves_active_and_queued_tasks(self):
        state = {}
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            store.save_archive_queue(
                {
                    "active": [{"id": "active-1", "title": "正在跑", "status": "running"}],
                    "queued": [{"id": "queued-1", "title": "等一下", "status": "queued"}],
                    "recent_failures": [
                        {"id": "failed-1", "title": "失败 A", "status": "failed"},
                        {"id": "failed-2", "title": "失败 B", "status": "failed"},
                    ],
                }
            )

            queue = store.clear_archive_recent_failures()

        self.assertEqual(queue["cleared_count"], 2)
        self.assertEqual(queue["failed_count"], 0)
        self.assertEqual(queue["recent_failures"], [])
        self.assertEqual(queue["active"][0]["id"], "active-1")
        self.assertEqual(queue["queued"][0]["id"], "queued-1")

    def test_state_update_publishes_event_after_save(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            queue = store.enqueue_archive_task({"id": "task-1", "title": "Demo"})

        self.assertEqual(queue["queued_count"], 1)
        self.assertEqual(state["archive_queue"]["queued"][0]["id"], "task-1")
        self.assertEqual(events[-1][0], "archive_queue")
        self.assertEqual(events[-1][1], "state.changed")
        self.assertEqual(events[-1][2]["queued_count"], 1)

    def test_start_archive_task_assigns_runtime_lease_fields(self):
        state = {"archive_queue": {"active": [], "queued": [{"id": "task-1", "title": "Demo"}], "recent_failures": []}}
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"), \
                patch("app.services.task_state.lease_expiry_from_now", return_value="2026-06-04T10:30:00+08:00"):
            queue = store.start_archive_task("task-1")

        task = queue["active"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["started_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["heartbeat_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["last_progress_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["lease_expires_at"], "2026-06-04T10:30:00+08:00")
        self.assertGreaterEqual(task["attempt_count"], 1)

    def test_lease_next_archive_task_skips_batch_parent_waiting_for_children(self):
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "batch-1",
                        "title": "Batch",
                        "mode": "author_upload",
                        "status": "queued",
                        "meta": {
                            "batch_expected_items": [
                                {"url": "https://makerworld.com.cn/zh/models/1", "status": "queued"}
                            ]
                        },
                    },
                    {
                        "id": "child-1",
                        "url": "https://makerworld.com.cn/zh/models/1",
                        "mode": "single_model",
                        "status": "queued",
                    },
                ],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        def select_child(queue):
            for item in queue.get("queued") or []:
                meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                if not meta.get("batch_expected_items"):
                    return item
            return None

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"), \
                patch("app.services.task_state.lease_expiry_from_now", return_value="2026-06-04T10:30:00+08:00"):
            claimed_task = store.lease_next_archive_task(select_child)
            queue = store.load_archive_queue()

        self.assertEqual(claimed_task["id"], "child-1")
        self.assertEqual(queue["active"][0]["id"], "child-1")
        self.assertEqual(queue["active"][0]["status"], "running")
        self.assertEqual(queue["active"][0]["lease_expires_at"], "2026-06-04T10:30:00+08:00")
        self.assertEqual([item["id"] for item in queue["queued"]], ["batch-1"])

    def test_lease_next_archive_task_claims_distinct_tasks_across_calls(self):
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {"id": "task-1", "url": "https://makerworld.com.cn/zh/models/1", "mode": "single_model"},
                    {"id": "task-2", "url": "https://makerworld.com.cn/zh/models/2", "mode": "single_model"},
                ],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            first = store.lease_next_archive_task()
            second = store.lease_next_archive_task()
            queue = store.load_archive_queue()

        self.assertEqual(first["id"], "task-1")
        self.assertEqual(second["id"], "task-2")
        self.assertEqual([item["id"] for item in queue["active"]], ["task-1", "task-2"])
        self.assertEqual(queue["queued_count"], 0)

    def test_batch_parent_normalizes_to_waiting_children(self):
        store = TaskStateStore()
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "batch-1",
                        "title": "Batch",
                        "mode": "author_upload",
                        "status": "running",
                        "meta": {
                            "batch_expected_items": [
                                {"url": "https://makerworld.com.cn/zh/models/1", "status": "queued"}
                            ]
                        },
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)):
            queue = store.load_archive_queue()

        self.assertEqual(queue["active"][0]["status"], "waiting_children")

    def test_update_active_task_preserves_runtime_lease_fields(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-1",
                        "title": "Demo",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T10:30:00+08:00",
                        "heartbeat_at": "2026-06-04T10:00:00+08:00",
                        "started_at": "2026-06-04T10:00:00+08:00",
                        "last_progress_at": "2026-06-04T10:00:00+08:00",
                        "attempt_count": 2,
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:05:00+08:00"):
            queue = store.update_active_task("task-1", progress=50, message="处理中")

        task = queue["active"][0]
        self.assertEqual(task["progress"], 50)
        self.assertEqual(task["lease_expires_at"], "2026-06-04T10:30:00+08:00")
        self.assertEqual(task["heartbeat_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["started_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["last_progress_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["attempt_count"], 2)

    def test_repair_archive_queue_requeues_expired_running_task(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-expired",
                        "title": "Expired",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                        "attempt_count": 1,
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["examined"], 1)
        self.assertEqual(result["summary"]["requeued"], 1)
        self.assertEqual(result["queue"]["queued"][0]["id"], "task-expired")
        self.assertEqual(result["queue"]["queued"][0]["status"], "queued")

    def test_repair_archive_queue_fails_expired_task_without_attempts(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-dead",
                        "title": "Dead",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                        "attempt_count": 3,
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["queue"]["recent_failures"][0]["id"], "task-dead")

    def test_repair_archive_queue_skips_paused_task(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "paused-1",
                        "title": "Paused",
                        "status": "paused",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertEqual(result["queue"]["active"][0]["status"], "paused")

    def test_completed_archive_task_publishes_semantic_event(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            store.save_archive_queue(
                {
                    "active": [{"id": "task-1", "title": "Demo", "url": "https://example.com/models/1", "mode": "single_model"}],
                    "queued": [],
                    "recent_failures": [],
                }
            )
            queue = store.complete_archive_task("task-1")

        self.assertEqual(queue["running_count"], 0)
        self.assertEqual(events[-1][1], "archive.completed")
        self.assertEqual(events[-1][2]["id"], "task-1")
        self.assertEqual(events[-1][2]["url"], "https://example.com/models/1")

    def test_load_archive_queue_discards_completed_active_snapshot(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-done",
                        "title": "Done",
                        "status": "completed",
                        "progress": 100,
                        "message": "归档完成：Done",
                    },
                    {
                        "id": "task-running",
                        "title": "Running",
                        "status": "running",
                    },
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            queue = store.load_archive_queue()

        self.assertEqual(queue["running_count"], 1)
        self.assertEqual([item["id"] for item in queue["active"]], ["task-running"])

    def test_remote_refresh_patch_can_skip_state_event_publish(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message="批次运行中。",
                publish_event=False,
            )

        self.assertEqual(result["status"], "running")
        self.assertTrue(result["running"])
        self.assertEqual(state["remote_refresh_state"]["status"], "running")
        self.assertEqual(events, [])

    def test_remote_refresh_patch_publishes_state_event_by_default(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message="批次开始。",
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "remote_refresh_state")
        self.assertEqual(events[0][1], "state.changed")
        self.assertNotIn("scope", events[0][2])
        self.assertEqual(events[0][2]["status"], "running")
        self.assertTrue(events[0][2]["running"])

    def test_remote_refresh_state_normalizes_active_run_and_scheduler_fields(self):
        state = {}
        store = TaskStateStore()
        active_run = {
            "batch_id": "batch-1",
            "status": "running",
            "started_at": "2026-06-06T10:00:00+08:00",
            "scheduled_cron": "0 2 * * *",
            "manual": True,
            "candidate_total": "3",
            "completed_total": "1",
            "remaining_total": "2",
            "manifest_path": "state/remote_refresh_batches/batch-1.manifest.json",
            "result_path": "state/remote_refresh_batches/batch-1.ndjson",
        }

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            saved = store.patch_remote_refresh_state(
                status="resuming",
                running=True,
                active_run=active_run,
                last_attempt_at="2026-06-06T10:01:00+08:00",
                last_deferred_at="2026-06-06T09:59:00+08:00",
                last_defer_reason="archive_queue_busy",
                last_interrupted_at="2026-06-06T09:58:00+08:00",
                last_interrupted_reason="worker_stopped",
                last_completed_at="2026-06-05T05:32:26+08:00",
                stale_archive_queue_detected=True,
            )

        self.assertEqual(saved["status"], "resuming")
        self.assertEqual(saved["active_run"]["batch_id"], "batch-1")
        self.assertEqual(saved["active_run"]["candidate_total"], 3)
        self.assertEqual(saved["active_run"]["completed_total"], 1)
        self.assertEqual(saved["active_run"]["remaining_total"], 2)
        self.assertTrue(saved["active_run"]["manual"])
        self.assertEqual(saved["last_attempt_at"], "2026-06-06T10:01:00+08:00")
        self.assertEqual(saved["last_defer_reason"], "archive_queue_busy")
        self.assertTrue(saved["stale_archive_queue_detected"])

    def test_compact_remote_refresh_state_includes_scheduler_and_active_run(self):
        compact = compact_remote_refresh_state(
            {
                "status": "interrupted",
                "running": False,
                "last_run_at": "2026-06-06T10:00:00+08:00",
                "last_completed_at": "2026-06-05T05:32:26+08:00",
                "last_attempt_at": "2026-06-06T10:10:00+08:00",
                "last_deferred_at": "2026-06-06T10:09:00+08:00",
                "last_defer_reason": "stale_runtime_state",
                "last_interrupted_at": "2026-06-06T10:08:00+08:00",
                "last_interrupted_reason": "manifest_missing",
                "stale_archive_queue_detected": True,
                "active_run": {
                    "batch_id": "batch-2",
                    "status": "interrupted",
                    "candidate_total": 8,
                    "completed_total": 5,
                    "remaining_total": 3,
                },
            },
            include_current=False,
        )

        self.assertEqual(compact["status"], "interrupted")
        self.assertEqual(compact["last_completed_at"], "2026-06-05T05:32:26+08:00")
        self.assertEqual(compact["last_attempt_at"], "2026-06-06T10:10:00+08:00")
        self.assertEqual(compact["last_defer_reason"], "stale_runtime_state")
        self.assertEqual(compact["active_run"]["remaining_total"], 3)
        self.assertTrue(compact["stale_archive_queue_detected"])

    def test_compact_remote_refresh_state_counts_skipped_batch_results(self):
        compact = compact_remote_refresh_state(
            {
                "status": "idle",
                "last_batch_succeeded": 2,
                "last_batch_failed": 1,
                "last_batch_skipped": 4,
            },
            include_current=False,
        )

        self.assertEqual(compact["last_batch_succeeded"], 2)
        self.assertEqual(compact["last_batch_failed"], 1)
        self.assertEqual(compact["last_batch_skipped"], 4)

    def test_repeated_equivalent_state_changed_events_are_coalesced_by_scope(self):
        rows = []
        wakes = []

        def append_event(event_type, scope, payload):
            row = {
                "id": len(rows) + 1,
                "type": event_type,
                "scope": scope,
                "payload": payload,
                "created_at": "",
            }
            rows.append(row)
            return row

        with patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_AT", {}), \
                patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_SIGNATURE", {}), \
                patch.object(state_events, "append_state_event", side_effect=append_event), \
                patch.object(state_events, "wake_state_event_subscribers", side_effect=lambda: wakes.append(True)), \
                patch.object(state_events.time, "monotonic", side_effect=[100.0, 100.2, 101.9]):
            state_events.publish_state_event("archive_queue", "state.changed", {"queued_count": 1})
            state_events.publish_state_event("archive_queue", "state.changed", {"queued_count": 1})
            state_events.publish_state_event("archive_queue", "state.changed", {"queued_count": 1})

        self.assertEqual([row["payload"]["queued_count"] for row in rows], [1, 1])
        self.assertEqual(len(wakes), 3)

    def test_state_changed_events_with_changed_summary_bypass_coalescing(self):
        rows = []

        def append_event(event_type, scope, payload):
            row = {
                "id": len(rows) + 1,
                "type": event_type,
                "scope": scope,
                "payload": payload,
                "created_at": "",
            }
            rows.append(row)
            return row

        with patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_AT", {}), \
                patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_SIGNATURE", {}), \
                patch.object(state_events, "append_state_event", side_effect=append_event), \
                patch.object(state_events, "wake_state_event_subscribers"), \
                patch.object(state_events.time, "monotonic", side_effect=[100.0, 100.1, 100.2]):
            state_events.publish_state_event("archive_queue", "state.changed", {"queued_count": 1})
            state_events.publish_state_event("archive_queue", "state.changed", {"queued_count": 2})
            state_events.publish_state_event("remote_refresh_state", "state.changed", {"status": "running", "running": True})

        self.assertEqual([row["payload"].get("queued_count") for row in rows[:2]], [1, 2])
        self.assertEqual(rows[2]["payload"]["status"], "running")

    def test_archive_queue_progress_message_only_changes_are_coalesced(self):
        rows = []

        def append_event(event_type, scope, payload):
            row = {
                "id": len(rows) + 1,
                "type": event_type,
                "scope": scope,
                "payload": payload,
                "created_at": "",
            }
            rows.append(row)
            return row

        with patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_AT", {}), \
                patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_SIGNATURE", {}), \
                patch.object(state_events, "append_state_event", side_effect=append_event), \
                patch.object(state_events, "wake_state_event_subscribers"), \
                patch.object(state_events.time, "monotonic", side_effect=[100.0, 100.1, 100.2]):
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 777, "failed_count": 0, "last_message": "正在下载设计图片（1/6）"},
            )
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 777, "failed_count": 0, "last_message": "正在下载设计图片（2/6）"},
            )
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 776, "failed_count": 0, "last_message": "下一个任务"},
            )

        self.assertEqual([row["payload"]["queued_count"] for row in rows], [777, 776])

    def test_semantic_state_events_bypass_coalescing(self):
        rows = []

        def append_event(event_type, scope, payload):
            row = {
                "id": len(rows) + 1,
                "type": event_type,
                "scope": scope,
                "payload": payload,
                "created_at": "",
            }
            rows.append(row)
            return row

        with patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_AT", {}), \
                patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_SIGNATURE", {}), \
                patch.object(state_events, "append_state_event", side_effect=append_event), \
                patch.object(state_events, "wake_state_event_subscribers"), \
                patch.object(state_events.time, "monotonic", side_effect=[100.0, 100.1, 100.2]):
            state_events.publish_state_event("archive_queue", "archive.completed", {"id": "a"})
            state_events.publish_state_event("archive_queue", "archive.completed", {"id": "b"})
            state_events.publish_state_event("archive_queue", "archive.failed", {"id": "c"})

        self.assertEqual([row["type"] for row in rows], ["archive.completed", "archive.completed", "archive.failed"])


if __name__ == "__main__":
    unittest.main()
