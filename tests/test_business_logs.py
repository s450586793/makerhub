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
            return_value=[
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
        ), patch.object(business_logs, "_database_logs_enabled", return_value=True):
            payload = business_logs.read_log_entries("business.log")

        self.assertEqual(payload["source"], "database")
        self.assertEqual(payload["entries"][0]["event"], "saved")
        self.assertEqual(payload["files"][0]["database"], True)


if __name__ == "__main__":
    unittest.main()
