import unittest

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


if __name__ == "__main__":
    unittest.main()
