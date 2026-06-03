import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from app.services import state_events
from app.services.task_state import TaskStateStore, _ORGANIZER_TERMINAL_LOG_CACHE, _normalize_organize_tasks


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
