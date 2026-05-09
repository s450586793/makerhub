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
