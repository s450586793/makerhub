import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main as main_app
from app.api import web as web_api


class BrowserVerificationWebRouteTest(unittest.TestCase):
    def test_authenticated_browser_verification_direct_url_serves_spa_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = Path(tmp) / "index.html"
            index_path.write_text("<!doctype html><div id=\"app\"></div>", encoding="utf-8")

            with patch.object(web_api, "FRONTEND_INDEX_PATH", index_path), patch.object(
                main_app.auth_manager,
                "resolve_request_auth",
                return_value={"kind": "session", "session_id": "test-session"},
            ):
                response = TestClient(main_app.app).get("/browser-verification/bv_test")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers.get("content-type", ""))
        self.assertIn("no-store", response.headers.get("cache-control", ""))
        self.assertIn("<div id=\"app\"></div>", response.text)


if __name__ == "__main__":
    unittest.main()
