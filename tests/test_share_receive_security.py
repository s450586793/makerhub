import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import config as config_api


class ShareReceiveSecurityTest(unittest.TestCase):
    def test_decode_share_code_requires_makerhub_ping_before_manifest_request(self):
        share_code = config_api._encode_share_code(
            base_url="https://share.example.com:1234",
            access_code="access-secret",
        )
        with patch.object(config_api, "_host_is_private_or_local", return_value=False), \
                patch.object(config_api, "_run_public_base_url_test", side_effect=ValueError("boom")), \
                patch.object(config_api.requests, "get") as get_mock:
            with self.assertRaises(ValueError) as raised:
                config_api._fetch_share_manifest(share_code)

        message = str(raised.exception)
        self.assertIn("验证分享端失败", message)
        self.assertNotIn("private.example.com", message)
        self.assertNotIn("access-secret", message)
        get_mock.assert_not_called()

    def test_share_receive_base_url_rejects_private_hosts_before_ping(self):
        with patch.object(config_api, "_run_public_base_url_test") as ping_mock:
            with self.assertRaises(ValueError) as raised:
                config_api._validate_share_receive_base_url("http://127.0.0.1:8000")

        self.assertIn("公开访问地址不能指向内网", str(raised.exception))
        ping_mock.assert_not_called()

    def test_public_base_url_test_does_not_follow_redirects(self):
        class Response:
            status_code = 200
            headers = {"Content-Type": "application/json"}
            url = "https://share.example.com/api/public/makerhub/ping"

            def json(self):
                return {"makerhub": True, "app_version": "0.9.62"}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

            def close(self):
                pass

        session = Session()
        with patch.object(config_api, "_make_test_session", return_value=session):
            result = config_api._run_public_base_url_test("https://share.example.com")

        self.assertTrue(result["ok"])
        self.assertEqual(session.calls[0][1]["allow_redirects"], False)

    def test_manifest_limits_reject_too_many_files_and_total_size(self):
        with patch.object(config_api, "SHARE_RECEIVE_MAX_FILES", 2), \
                patch.object(config_api, "SHARE_RECEIVE_MAX_TOTAL_BYTES", 10):
            with self.assertRaises(ValueError) as too_many:
                config_api._validate_share_manifest_limits(
                    {"files": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}
                )
            self.assertIn("文件数量超过", str(too_many.exception))

            with self.assertRaises(ValueError) as too_large:
                config_api._validate_share_manifest_limits(
                    {"files": [{"id": "1", "size": 6}, {"id": "2", "size": 6}]}
                )
            self.assertIn("文件总体积过大", str(too_large.exception))

    def test_download_share_file_stops_when_stream_exceeds_limit(self):
        class Response:
            status_code = 200
            headers = {}

            def iter_content(self, chunk_size=1):
                yield b"12345"
                yield b"67890"
                yield b"x"

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shared.stl"
            with patch.object(config_api.requests, "get", return_value=Response()), \
                    patch.object(config_api, "_host_is_private_or_local", return_value=False), \
                    patch.object(config_api, "SHARE_RECEIVE_MAX_FILE_BYTES", 10):
                with self.assertRaises(ValueError) as raised:
                    config_api._download_share_file(
                        "https://share.example.com",
                        {"url": "/api/public/shares/1/files/1", "size": 0},
                        target,
                    )

            self.assertIn("单个文件过大", str(raised.exception))
            self.assertFalse(target.exists())
            self.assertFalse(list(Path(tmp).glob("*.tmp")))

    def test_download_share_file_rejects_absolute_manifest_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shared.stl"
            with patch.object(config_api.requests, "get") as get_mock:
                with self.assertRaises(ValueError) as raised:
                    config_api._download_share_file(
                        "https://share.example.com",
                        {"url": "https://169.254.169.254/latest/meta-data/", "size": 0},
                        target,
                    )

            self.assertIn("下载地址无效", str(raised.exception))
            get_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
