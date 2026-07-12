from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from app.services import cloakbrowser_session


class CloakBrowserSessionTest(unittest.TestCase):
    def test_cloakbrowser_configured_requires_internal_url_and_auth_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cloakbrowser_session.cloakbrowser_configured())
        with patch.dict(os.environ, {"MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080"}, clear=True):
            self.assertFalse(cloakbrowser_session.cloakbrowser_configured())
        with patch.dict(os.environ, {"MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token"}, clear=True):
            self.assertFalse(cloakbrowser_session.cloakbrowser_configured())
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ):
            self.assertTrue(cloakbrowser_session.cloakbrowser_configured())

    def test_request_rejects_missing_auth_token_before_network_io(self):
        with patch.dict(
            os.environ,
            {"MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080"},
            clear=True,
        ), patch.object(cloakbrowser_session.requests, "request") as request_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "AUTH_TOKEN"):
                cloakbrowser_session._request("GET", "/api/profiles")

        request_mock.assert_not_called()

    def test_request_sends_bearer_auth(self):
        response = Mock(status_code=200, content=b"{}")
        response.json.return_value = {}
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ), patch.object(cloakbrowser_session.requests, "request", return_value=response) as request_mock:
            cloakbrowser_session._request("GET", "/api/profiles")

        self.assertEqual(
            request_mock.call_args.kwargs["headers"],
            {"Authorization": "Bearer secret-token"},
        )

    def test_bridge_payload_requires_auth_token_before_subprocess_io(self):
        with patch.dict(
            os.environ,
            {"MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080"},
            clear=True,
        ), patch.object(cloakbrowser_session.subprocess, "run") as run_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "AUTH_TOKEN"):
                cloakbrowser_session._bridge_payload("profile-cn", action="snapshot")

        run_mock.assert_not_called()

    def test_run_bridge_rejects_missing_auth_token_before_subprocess_io(self):
        with patch.object(cloakbrowser_session.subprocess, "run") as run_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "AUTH_TOKEN"):
                cloakbrowser_session._run_bridge({"action": "snapshot"})

        run_mock.assert_not_called()

    def test_bridge_uses_bearer_auth_for_discovery_and_websocket(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('if (!token) throw new Error("auth_token is required")', source)
        self.assertIn("return { Authorization: `Bearer ${token}` }", source)
        self.assertRegex(source, r"fetch\([^;]+\{ headers \}\)")
        self.assertRegex(source, r"puppeteer\.connect\(\{[\s\S]+?headers,")

    def test_ensure_profile_reuses_saved_profile_id(self):
        with patch.object(
            cloakbrowser_session,
            "_request",
            return_value={"id": "profile-cn", "name": "MakerHub CN", "status": "stopped"},
        ) as request_mock:
            profile = cloakbrowser_session.ensure_profile("cn", "profile-cn")

        self.assertEqual(profile.id, "profile-cn")
        request_mock.assert_called_once_with("GET", "/api/profiles/profile-cn")

    def test_ensure_profile_reuses_managed_tag(self):
        responses = [
            [
                {
                    "id": "profile-global",
                    "name": "Custom name",
                    "status": "running",
                    "tags": [{"tag": "makerhub"}, {"tag": "global"}],
                }
            ]
        ]
        with patch.object(cloakbrowser_session, "_request", side_effect=responses) as request_mock:
            profile = cloakbrowser_session.ensure_profile("global")

        self.assertEqual(profile.id, "profile-global")
        self.assertEqual(profile.status, "running")
        request_mock.assert_called_once_with("GET", "/api/profiles")

    def test_ensure_profile_creates_stable_platform_profile(self):
        responses = [
            [],
            {"id": "created-cn", "name": "MakerHub CN", "status": "stopped"},
        ]
        with patch.object(cloakbrowser_session, "_request", side_effect=responses) as request_mock:
            profile = cloakbrowser_session.ensure_profile("cn")

        self.assertEqual(profile.id, "created-cn")
        create_payload = request_mock.call_args_list[1].kwargs["json_payload"]
        self.assertEqual(create_payload["name"], "MakerHub CN")
        self.assertTrue(create_payload["humanize"])
        self.assertFalse(create_payload["auto_launch"])
        self.assertEqual({item["tag"] for item in create_payload["tags"]}, {"makerhub", "cn"})

    def test_browser_cookie_items_preserve_structured_domain_and_expand_tokens(self):
        items = cloakbrowser_session.browser_cookie_items(
            "token=access; refreshToken=refresh",
            "cn",
            [{"name": "bbl_device_id", "value": "device", "domain": ".bambulab.cn", "path": "/"}],
        )

        keys = {(item["name"], item.get("domain")) for item in items}
        self.assertIn(("bbl_device_id", ".bambulab.cn"), keys)
        self.assertIn(("token", ".makerworld.com.cn"), keys)
        self.assertIn(("token", ".bambulab.cn"), keys)
        self.assertIn(("refreshToken", ".makerworld.com.cn"), keys)

    def test_browser_cookie_items_reject_domain_suffix_lookalike(self):
        items = cloakbrowser_session.browser_cookie_items(
            "",
            "global",
            [{"name": "token", "value": "attacker", "domain": ".notmakerworld.com"}],
        )

        self.assertEqual(items, [])

    def test_cookie_header_from_snapshot_reads_browser_and_storage_tokens(self):
        snapshot = {
            "cookies": [
                {"name": "cf_clearance", "value": "clear", "domain": ".makerworld.com"},
                {"name": "lookalike", "value": "x", "domain": ".notmakerworld.com"},
                {"name": "ignored", "value": "x", "domain": ".example.com"},
            ],
            "storage": [
                {
                    "origin": "https://makerworld.com",
                    "local": {"accessToken": "access", "refreshToken": "refresh"},
                    "session": {},
                },
                {
                    "origin": "https://makerworld.com.evil.example",
                    "local": {"accessToken": "attacker"},
                    "session": {},
                },
            ],
        }

        cookie = cloakbrowser_session._cookie_header_from_snapshot(snapshot, "global")

        self.assertIn("cf_clearance=clear", cookie)
        self.assertIn("token=access", cookie)
        self.assertIn("refreshToken=refresh", cookie)
        self.assertNotIn("ignored=x", cookie)
        self.assertNotIn("lookalike=x", cookie)
        self.assertNotIn("attacker", cookie)

    def test_makerworld_ticket_url_uses_bearer_token_and_platform_callback(self):
        response = Mock(status_code=200, text='{"ticket":"ticket-value"}')
        response.json.return_value = {"ticket": "ticket-value"}
        session = Mock()
        session.headers = {}
        session.cookies = Mock()
        session.get.return_value = response

        with patch.object(cloakbrowser_session.requests, "Session", return_value=session):
            url = cloakbrowser_session.makerworld_ticket_url(
                "global",
                "token=access; refreshToken=refresh",
            )

        self.assertIn("makerworld.com/api/sign-in/ticket", url)
        self.assertIn("ticket=ticket-value", url)
        self.assertEqual(session.headers["Authorization"], "Bearer access")
        session.close.assert_called_once()

    def test_synchronize_browser_session_seeds_snapshot_and_stops_newly_launched_profile(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
            cdp_url="/api/profiles/profile-cn/cdp",
        )
        snapshot = {
            "ok": True,
            "current_url": "https://makerworld.com.cn/zh",
            "cookies": [{"name": "token", "value": "browser-token", "domain": ".makerworld.com.cn"}],
            "storage": [],
        }
        with patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, True)), \
                patch.object(cloakbrowser_session, "makerworld_ticket_url", return_value="https://makerworld.com.cn/ticket"), \
                patch.object(cloakbrowser_session, "_run_bridge", return_value=snapshot) as bridge_mock, \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            result = cloakbrowser_session.synchronize_browser_session(
                "cn",
                "token=api-token",
                profile_id="profile-cn",
            )

        self.assertEqual(result.cookie, "token=browser-token")
        self.assertEqual(result.current_url, "https://makerworld.com.cn/zh")
        self.assertEqual(bridge_mock.call_args.args[0]["action"], "seed")
        stop_mock.assert_called_once_with("profile-cn")

    def test_synchronize_browser_session_requires_ticket_for_automatic_login(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN", status="running")
        with patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, True)), \
                patch.object(cloakbrowser_session, "makerworld_ticket_url", return_value=""), \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    return_value={
                        "ok": True,
                        "current_url": "https://makerworld.com.cn/zh",
                        "cookies": [],
                        "storage": [],
                    },
                ), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserError, "ticket"):
                cloakbrowser_session.synchronize_browser_session("cn", "token=api-token")

        stop_mock.assert_called_once_with("profile-cn")

    def test_prepare_browser_login_keeps_profile_running_for_user(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN", status="running")
        with patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, True)), \
                patch.object(cloakbrowser_session, "makerworld_ticket_url", return_value=""), \
                patch.object(cloakbrowser_session, "_run_bridge", return_value={"ok": True, "cookies": [], "storage": []}), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            result = cloakbrowser_session.prepare_browser_login("cn")

        self.assertEqual(result.profile_id, "profile-cn")
        stop_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
