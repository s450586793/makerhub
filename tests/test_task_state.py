import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

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
    def test_clear_recent_failures_preserves_active_and_queued_tasks(self):
        with TemporaryDirectory() as temp_dir:
            archive_queue_path = Path(temp_dir) / "archive_queue.json"
            store = TaskStateStore()
            with patch("app.services.task_state.ARCHIVE_QUEUE_PATH", archive_queue_path):
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


if __name__ == "__main__":
    unittest.main()
