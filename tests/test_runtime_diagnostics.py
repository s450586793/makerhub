import unittest
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api import system as system_api
from app.api import tasks_routes
from app.services import runtime_diagnostics


class _FakeConnection:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self.last_marker = ""

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        if "pg_stat_user_tables" in text:
            self.last_marker = "tables"
        elif "FROM makerhub_state_events" in text and "GROUP BY scope" in text:
            self.last_marker = "events"
        elif "FROM makerhub_logs" in text and "GROUP BY file_name" in text:
            self.last_marker = "logs"
        elif "FROM makerhub_json_state" in text:
            self.last_marker = "states"
        else:
            self.last_marker = "unknown"
        return self

    def fetchall(self):
        return self.rows_by_marker.get(self.last_marker, [])


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class RuntimeDiagnosticsTest(unittest.TestCase):
    def test_build_runtime_diagnostics_returns_database_aggregates(self):
        now = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)
        connection = _FakeConnection(
            {
                "tables": [
                    {"relname": "makerhub_logs", "total_size": "575 MB", "rows": 536482, "dead_rows": 2},
                ],
                "events": [
                    {"scope": "archive_queue", "rows": 71707, "newest": now},
                ],
                "logs": [
                    {
                        "file_name": "business.log",
                        "category": "archive",
                        "event": "single_completed",
                        "level": "info",
                        "rows": 9942,
                        "newest": now,
                    }
                ],
                "states": [
                    {"key": "archive_queue", "type": "object", "updated_at": now},
                ],
            }
        )

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": True, "schema_version": 2}), \
                patch.object(runtime_diagnostics, "database_connection", return_value=_ConnectionContext(connection)):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertTrue(payload["database"]["available"])
        self.assertEqual(payload["tables"][0]["name"], "makerhub_logs")
        self.assertEqual(payload["state_events_by_scope"][0]["scope"], "archive_queue")
        self.assertEqual(payload["recent_logs"][0]["event"], "single_completed")
        self.assertEqual(payload["json_states"][0]["key"], "archive_queue")

    def test_build_runtime_diagnostics_degrades_when_database_unavailable(self):
        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False, "error": "missing"}):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertFalse(payload["database"]["available"])
        self.assertEqual(payload["tables"], [])
        self.assertEqual(payload["state_events_by_scope"], [])

    def test_build_runtime_diagnostics_always_includes_account_health_snapshot(self):
        account_health = {
            "cn": {
                "platform": "cn",
                "status": "ok",
                "reason": "current_action_succeeded",
                "source": "archive_download",
                "updated_at": "2026-06-11T10:00:00+08:00",
            },
            "global": {
                "platform": "global",
                "status": "verification_required",
                "reason": "download_probe",
                "source": "diagnostic_probe",
                "updated_at": "2026-06-11T10:01:00+08:00",
            },
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False, "error": "missing"}), \
                patch.object(runtime_diagnostics, "load_account_health", return_value=account_health):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
        self.assertEqual(payload["account_health"]["cn"]["reason"], "current_action_succeeded")
        self.assertEqual(payload["account_health"]["cn"]["source"], "archive_download")
        self.assertEqual(payload["account_health"]["cn"]["updated_at"], "2026-06-11T10:00:00+08:00")
        self.assertEqual(payload["account_health"]["global"]["status"], "verification_required")
        self.assertEqual(payload["account_health"]["global"]["reason"], "download_probe")
        self.assertEqual(payload["account_health"]["global"]["source"], "diagnostic_probe")
        self.assertEqual(payload["account_health"]["global"]["updated_at"], "2026-06-11T10:01:00+08:00")
        self.assertNotIn("missing_3mf", payload["account_health"]["cn"])
        self.assertNotIn("missing_3mf", payload["account_health"]["global"])
        self.assertNotIn("model_url", payload["account_health"]["cn"])
        self.assertNotIn("message", payload["account_health"]["global"])

    def test_build_runtime_diagnostics_includes_archive_queue_summary(self):
        queue = {
            "active": [
                {
                    "id": "task-1",
                    "status": "running",
                    "title": "Running",
                    "lease_expires_at": "2026-06-04T09:00:00+08:00",
                },
                {"id": "batch-1", "status": "waiting_children", "title": "Batch"},
            ],
            "queued": [{"id": "task-2", "status": "queued", "title": "Queued"}],
            "recent_failures": [{"id": "task-3", "status": "failed", "title": "Failed"}],
            "running_count": 2,
            "queued_count": 1,
            "failed_count": 1,
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics.task_state_store, "load_archive_queue", return_value=queue), \
                patch.object(runtime_diagnostics, "is_lease_expired", return_value=True):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["archive_queue"]["running_count"], 2)
        self.assertEqual(payload["archive_queue"]["queued_count"], 1)
        self.assertEqual(payload["archive_queue"]["failed_count"], 1)
        self.assertEqual(payload["archive_queue"]["stale_candidates"][0]["id"], "task-1")

    def test_system_diagnostics_route_requires_session_and_returns_payload(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
        with patch.object(system_api.config_api, "_require_session_auth") as require_auth, \
                patch.object(system_api, "build_runtime_diagnostics", return_value={"database": {"available": True}}):
            payload = asyncio.run(system_api.get_system_diagnostics(request))

        require_auth.assert_called_once_with(request)
        self.assertTrue(payload["database"]["available"])

    def test_repair_archive_queue_route_requires_session_and_returns_summary(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        repair_payload = {
            "summary": {
                "examined": 1,
                "requeued": 1,
                "failed": 0,
                "finalized": 0,
                "skipped": 0,
                "errors": [],
            },
            "queue": {
                "running_count": 0,
                "queued_count": 1,
                "failed_count": 0,
                "active": [],
                "queued": [],
                "recent_failures": [],
            },
        }

        with patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch.object(tasks_routes.task_state_store, "repair_archive_queue", return_value=repair_payload), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(tasks_routes.repair_archive_queue(request))

        require_auth.assert_called_once_with(request)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["requeued"], 1)


if __name__ == "__main__":
    unittest.main()
