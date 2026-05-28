import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.responses import Response

from app.api import config as config_api
from app.schemas.models import BrowserVerificationInputRequest, BrowserVerificationSessionRequest


def _session_request():
    return SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))


class BrowserVerificationApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_list_browser_verification_sessions_require_session_and_redact(self):
        created_session = {
            "id": "bv_1",
            "status": "queued",
            "platform": "cn",
            "target": {
                "model_id": "1063416",
                "model_url": "https://makerworld.com.cn/zh/models/1063416",
                "api_url": "https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf",
            },
            "captcha_id": "geetest-id",
        }
        fake_store = SimpleNamespace(
            create_session=lambda item: created_session,
            list_sessions=lambda: [created_session],
        )

        with patch.object(config_api, "browser_verification_store", fake_store):
            payload = await config_api.create_browser_verification_session(
                BrowserVerificationSessionRequest(
                    model_id="1063416",
                    model_url="https://makerworld.com.cn/zh/models/1063416",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf",
                    captcha_id="geetest-id",
                    source="cn",
                    proof="must-not-persist",
                ),
                _session_request(),
            )
            listed = await config_api.list_browser_verification_sessions(_session_request())

        self.assertEqual(payload["id"], "bv_1")
        self.assertEqual(listed["items"][0]["id"], "bv_1")
        self.assertNotIn("must-not-persist", str(payload))
        self.assertNotIn("must-not-persist", str(listed))

        with self.assertRaises(HTTPException) as ctx:
            await config_api.list_browser_verification_sessions(SimpleNamespace(state=SimpleNamespace(auth_identity={})))
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_input_cancel_and_screenshot_routes(self):
        fake_store = SimpleNamespace(
            get_session=lambda session_id: {"id": session_id, "status": "running"} if session_id == "bv_1" else {},
            enqueue_input=lambda session_id, command: {"id": "cmd_1", "type": command["type"], "x": command.get("x", 0), "y": command.get("y", 0)},
            cancel_session=lambda session_id: {"id": session_id, "status": "cancelled"},
            read_screenshot=lambda session_id: (b"jpg-bytes", "image/jpeg") if session_id == "bv_1" else (b"", "image/jpeg"),
        )

        with patch.object(config_api, "browser_verification_store", fake_store):
            detail = await config_api.get_browser_verification_session("bv_1", _session_request())
            command = await config_api.enqueue_browser_verification_input(
                "bv_1",
                BrowserVerificationInputRequest(type="click", x=12, y=34, text="unused-secret"),
                _session_request(),
            )
            screenshot = await config_api.get_browser_verification_screenshot("bv_1", _session_request())
            missing_screenshot = await config_api.get_browser_verification_screenshot("missing", _session_request())
            cancelled = await config_api.cancel_browser_verification_session("bv_1", _session_request())
            with self.assertRaises(HTTPException) as ctx:
                await config_api.get_browser_verification_session("missing", _session_request())

        self.assertEqual(detail["id"], "bv_1")
        self.assertEqual(command["command"]["type"], "click")
        self.assertNotIn("unused-secret", str(command))
        self.assertIsInstance(screenshot, Response)
        self.assertEqual(screenshot.media_type, "image/jpeg")
        self.assertEqual(screenshot.body, b"jpg-bytes")
        self.assertEqual(missing_screenshot.status_code, 204)
        self.assertEqual(cancelled["status"], "cancelled")

        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
