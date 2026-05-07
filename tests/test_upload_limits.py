import unittest

from app.core import settings


class UploadLimitsTest(unittest.TestCase):
    def test_local_import_default_accepts_large_zip_without_expanding_attachment_limit(self):
        self.assertGreaterEqual(settings.MAX_LOCAL_IMPORT_UPLOAD_BYTES, 2 * 1024 * 1024 * 1024)
        self.assertEqual(settings.MAX_MANUAL_ATTACHMENT_BYTES, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
