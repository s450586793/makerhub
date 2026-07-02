import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.services import flaresolverr_client


class _FakeFlareSolverrResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FlareSolverrClientTest(unittest.TestCase):
    def test_request_get_returns_solution_response_and_updates_session(self):
        session = requests.Session()
        posted_payloads = []

        def fake_post(url, json=None, timeout=None):
            posted_payloads.append({"url": url, "json": json, "timeout": timeout})
            return _FakeFlareSolverrResponse(
                {
                    "status": "ok",
                    "message": "ok",
                    "solution": {
                        "url": "https://makerworld.com.cn/zh/models/1",
                        "status": 200,
                        "response": "<html>ok</html>",
                        "userAgent": "Mozilla/5.0 FlareSolverr",
                        "cookies": [
                            {
                                "name": "cf_clearance",
                                "value": "token",
                                "domain": ".makerworld.com.cn",
                                "path": "/",
                            }
                        ],
                    },
                }
            )

        with patch.dict(
            "os.environ",
            {
                "MAKERHUB_FLARESOLVERR_URL": "http://flaresolverr:8191/v1",
                "MAKERHUB_FLARESOLVERR_TIMEOUT": "45",
            },
        ), patch.object(flaresolverr_client.requests, "post", side_effect=fake_post):
            solution = flaresolverr_client.flaresolverr_get(
                "https://makerworld.com.cn/zh/models/1",
                raw_cookie="session=abc",
                session=session,
                headers={"Referer": "https://makerworld.com.cn/"},
            )

        self.assertEqual(solution.text, "<html>ok</html>")
        self.assertEqual(solution.status_code, 200)
        self.assertEqual(session.headers["User-Agent"], "Mozilla/5.0 FlareSolverr")
        self.assertEqual(session.cookies.get("cf_clearance", domain=".makerworld.com.cn"), "token")
        self.assertEqual(posted_payloads[0]["url"], "http://flaresolverr:8191/v1")
        self.assertEqual(posted_payloads[0]["timeout"], 45)
        self.assertEqual(posted_payloads[0]["json"]["cmd"], "request.get")
        self.assertEqual(posted_payloads[0]["json"]["cookies"][0]["name"], "session")

    def test_get_json_parses_solution_response(self):
        with patch.object(
            flaresolverr_client,
            "flaresolverr_get",
            return_value=SimpleNamespace(text='{"hits":[{"id":1}]}', status_code=200, url="https://api.example.test"),
        ):
            payload = flaresolverr_client.flaresolverr_get_json("https://api.example.test")

        self.assertEqual(payload, {"hits": [{"id": 1}]})

    def test_non_ok_status_raises_clear_error(self):
        def fake_post(_url, json=None, timeout=None):
            return _FakeFlareSolverrResponse(
                {
                    "status": "error",
                    "message": "Cloudflare solver unavailable",
                    "solution": {},
                }
            )

        with patch.dict("os.environ", {"MAKERHUB_FLARESOLVERR_URL": "http://flaresolverr:8191/v1"}), patch.object(
            flaresolverr_client.requests,
            "post",
            side_effect=fake_post,
        ):
            with self.assertRaisesRegex(flaresolverr_client.FlareSolverrError, "Cloudflare solver unavailable"):
                flaresolverr_client.flaresolverr_get("https://makerworld.com.cn/zh/models/1")

    def test_missing_url_raises_clear_error(self):
        with patch.dict("os.environ", {"MAKERHUB_FLARESOLVERR_URL": ""}):
            with self.assertRaisesRegex(flaresolverr_client.FlareSolverrError, "MAKERHUB_FLARESOLVERR_URL"):
                flaresolverr_client.flaresolverr_get("https://makerworld.com.cn/zh/models/1")


if __name__ == "__main__":
    unittest.main()
