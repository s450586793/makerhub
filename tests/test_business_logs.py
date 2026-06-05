import unittest
from unittest.mock import patch

from app.services import business_logs


class BusinessLogsTest(unittest.TestCase):
    def test_share_receive_sensitive_fields_are_masked(self):
        payload = business_logs._safe_value(
            {
                "share_code": "MH3.EXAMPLE",
                "access_code": "access-secret",
                "manifest_url": "https://example.test/api/public/share-access/access-secret/manifest",
                "share_url": "https://example.test/api/public/shares/share-id/files/file-id?access=access-secret",
                "baseUrl": "https://example.test",
                "public_base_url": "https://example.test",
                "nested": {"token": "legacy-token"},
                "safe_count": 2,
            }
        )

        self.assertEqual(payload["share_code"], "***")
        self.assertEqual(payload["access_code"], "***")
        self.assertEqual(payload["manifest_url"], "***")
        self.assertEqual(payload["share_url"], "***")
        self.assertEqual(payload["baseUrl"], "***")
        self.assertEqual(payload["public_base_url"], "***")
        self.assertEqual(payload["nested"]["token"], "***")
        self.assertEqual(payload["safe_count"], 2)

    def test_append_business_log_writes_database_only(self):
        captured = []

        with patch.object(
            business_logs,
            "append_database_log_entry",
            side_effect=lambda file_name, entry, raw="": captured.append((file_name, entry, raw)) or True,
        ), patch("builtins.print"):
            business_logs.append_business_log("sharing", "share_created", "ok", token="secret", safe=1)

        self.assertEqual(captured[0][0], "business.log")
        self.assertEqual(captured[0][1]["token"], "***")
        self.assertEqual(captured[0][1]["safe"], 1)
        self.assertIn("share_created", captured[0][2])

    def test_noisy_info_business_log_can_be_skipped(self):
        captured = []

        with patch.object(
            business_logs,
            "append_database_log_entry",
            side_effect=lambda file_name, entry, raw="": captured.append((file_name, entry, raw)) or True,
        ), patch("builtins.print") as printed:
            business_logs.append_business_log("scrapling", "fetch_trace", "trace detail", status_code=200)

        self.assertEqual(captured, [])
        printed.assert_not_called()

    def test_noisy_warning_business_log_is_preserved(self):
        captured = []

        with patch.object(
            business_logs,
            "append_database_log_entry",
            side_effect=lambda file_name, entry, raw="": captured.append((file_name, entry, raw)) or True,
        ), patch("builtins.print"):
            business_logs.append_business_log("scrapling", "fetch_trace", "failed trace", level="warning", status_code=403)

        self.assertEqual(captured[0][0], "business.log")
        self.assertEqual(captured[0][1]["level"], "warning")
        self.assertEqual(captured[0][1]["event"], "fetch_trace")

    def test_noisy_structured_success_log_can_be_skipped(self):
        captured = []

        with patch.object(
            business_logs,
            "append_database_log_entry",
            side_effect=lambda file_name, entry, raw="": captured.append((file_name, entry, raw)) or True,
        ):
            business_logs.append_structured_log(
                "subscriptions.log",
                "metadata_refreshed",
                category="subscription",
                total=35,
            )

        self.assertEqual(captured, [])

    def test_read_log_entries_prefers_database(self):
        with patch.object(
            business_logs,
            "_database_log_file_items",
            return_value={
                "business.log": {
                    "name": "business.log",
                    "size": 0,
                    "modified_at": "2026-05-22T10:00:00+08:00",
                    "exists": True,
                    "primary": True,
                    "database": True,
                    "count": 1,
                }
            },
        ), patch.object(
            business_logs,
            "_read_database_log_entries",
            return_value=(
                [
                    {
                        "time": "2026-05-22T10:00:00+08:00",
                        "level": "info",
                        "category": "settings",
                        "event": "saved",
                        "message": "ok",
                        "payload": {},
                        "raw": "{}",
                    }
                ],
                False,
                "",
            ),
        ), patch.object(
            business_logs,
            "_read_database_log_facets",
            return_value={"levels": [], "categories": [], "events": []},
        ), patch.object(business_logs, "_database_logs_enabled", return_value=True):
            payload = business_logs.read_log_entries("business.log")

        self.assertEqual(payload["source"], "database")
        self.assertEqual(payload["entries"][0]["event"], "saved")
        self.assertEqual(payload["files"][0]["database"], True)

    def test_read_log_entries_applies_database_filters_and_cursor(self):
        calls = []

        class FakeResult:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "FROM makerhub_logs" in sql and "GROUP BY" not in sql:
                    return FakeResult(
                        [
                            {
                                "id": 91,
                                "file_name": "business.log",
                                "time_text": "2026-06-05T10:00:00+08:00",
                                "level": "error",
                                "category": "archive",
                                "event": "download_failed",
                                "message": "first",
                                "payload": {"model_id": "1"},
                                "raw": "{}",
                                "created_at": "2026-06-05T10:00:00+08:00",
                            },
                            {
                                "id": 77,
                                "file_name": "business.log",
                                "time_text": "2026-06-05T09:00:00+08:00",
                                "level": "error",
                                "category": "archive",
                                "event": "download_failed",
                                "message": "second",
                                "payload": {"model_id": "2"},
                                "raw": "{}",
                                "created_at": "2026-06-05T09:00:00+08:00",
                            },
                            {
                                "id": 66,
                                "file_name": "business.log",
                                "time_text": "2026-06-05T08:00:00+08:00",
                                "level": "error",
                                "category": "archive",
                                "event": "download_failed",
                                "message": "third",
                                "payload": {"model_id": "3"},
                                "raw": "{}",
                                "created_at": "2026-06-05T08:00:00+08:00",
                            },
                        ]
                    )
                return FakeResult([])

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(business_logs, "_database_logs_enabled", return_value=True), \
                patch.object(business_logs, "_ensure_database_logs_ready", return_value=True), \
                patch.object(business_logs, "_database_log_file_items", return_value={}), \
                patch.object(business_logs, "_read_database_log_facets", return_value={"levels": [], "categories": [], "events": []}), \
                patch.object(business_logs, "database_connection", return_value=FakeContext()):
            payload = business_logs.read_log_entries(
                "business.log",
                limit=2,
                query="failed",
                level="error",
                category="archive",
                event="download_failed",
                since="2026-06-05T00:00:00+08:00",
                cursor=120,
            )

        query_sql, query_params = calls[0]
        self.assertIn("level IN", query_sql)
        self.assertIn("category IN", query_sql)
        self.assertIn("event IN", query_sql)
        self.assertIn("created_at >= %s::timestamptz", query_sql)
        self.assertIn("id < %s", query_sql)
        self.assertIn("failed", query_params[1])
        self.assertIn("error", query_params)
        self.assertIn("archive", query_params)
        self.assertIn("download_failed", query_params)
        self.assertIn(120, query_params)
        self.assertEqual(len(payload["entries"]), 2)
        self.assertTrue(payload["has_more"])
        self.assertEqual(payload["next_cursor"], "77")
        self.assertEqual(payload["filters"]["level"], "error")


if __name__ == "__main__":
    unittest.main()
