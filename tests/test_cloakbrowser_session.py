from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import contextmanager, nullcontext
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import Mock, patch

from app.services import cloakbrowser_session


SPAWN_CONTEXT = get_context("spawn")


def _hold_cloakbrowser_profile_slot(
    state_dir: str,
    ready_queue,
    start_event,
    active,
    max_active,
    counter_lock,
) -> None:
    from app.services import resource_limiter

    resource_limiter.STATE_DIR = Path(state_dir)
    slot_name = cloakbrowser_session._profile_resource_name("cn", "profile-cn")
    ready_queue.put(True)
    start_event.wait(5)
    with cloakbrowser_session.resource_slot(slot_name, detail="test-bridge"):
        with counter_lock:
            active.value += 1
            max_active.value = max(max_active.value, active.value)
        time.sleep(0.15)
        with counter_lock:
            active.value -= 1


class CloakBrowserSessionTest(unittest.TestCase):
    def test_stop_idle_profiles_only_stops_expired_running_profile(self):
        profiles = [
            {"id": "profile-cn", "name": "MakerHub CN", "status": "running"},
            {"id": "profile-global", "name": "MakerHub Global", "status": "running"},
            {"id": "profile-stopped", "name": "MakerHub CN", "status": "stopped"},
        ]
        now = 20_000.0
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir)), \
                patch.dict(os.environ, {
                    "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                    "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                }, clear=False), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=nullcontext()), \
                patch.object(cloakbrowser_session, "_request") as request_mock, \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock:
            cloakbrowser_session.touch_profile_activity("cn", now=now - 1900)
            cloakbrowser_session.touch_profile_activity("global", now=now - 60)
            request_mock.side_effect = [
                profiles,
                {"id": "profile-cn", "name": "MakerHub CN", "status": "running"},
            ]

            result = cloakbrowser_session.stop_idle_profiles(idle_seconds=1800, now=now)

            stopped_activity_at = cloakbrowser_session.profile_activity_at("cn")

        self.assertEqual(result["stopped_count"], 1)
        self.assertEqual(result["stopped_profiles"], ["profile-cn"])
        self.assertEqual(stopped_activity_at, 0.0)
        stop_mock.assert_called_once_with("profile-cn")

    def test_stop_idle_profiles_initializes_missing_activity_before_considering_stop(self):
        now = 20_000.0
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir)), \
                patch.dict(os.environ, {
                    "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                    "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                }, clear=False), \
                patch.object(
                    cloakbrowser_session,
                    "_request",
                    return_value=[{"id": "profile-cn", "name": "MakerHub CN", "status": "running"}],
                ), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock:
            result = cloakbrowser_session.stop_idle_profiles(idle_seconds=1800, now=now)

            activity_at = cloakbrowser_session.profile_activity_at("cn")

        self.assertEqual(result["initialized_count"], 1)
        self.assertEqual(activity_at, now)
        stop_mock.assert_not_called()

    def test_stop_idle_profiles_rechecks_activity_after_waiting_for_profile_slot(self):
        now = 20_000.0

        @contextmanager
        def busy_slot():
            cloakbrowser_session.touch_profile_activity("cn", now=now)
            yield

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir)), \
                patch.dict(os.environ, {
                    "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                    "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                }, clear=False), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=busy_slot()), \
                patch.object(
                    cloakbrowser_session,
                    "_request",
                    return_value=[{"id": "profile-cn", "name": "MakerHub CN", "status": "running"}],
                ), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock:
            cloakbrowser_session.touch_profile_activity("cn", now=now - 1900)

            result = cloakbrowser_session.stop_idle_profiles(idle_seconds=1800, now=now)

        self.assertEqual(result["stopped_count"], 0)
        stop_mock.assert_not_called()

    def test_stop_idle_profiles_uses_shorter_timeout_after_background_fetch(self):
        profiles = [
            {"id": "profile-cn", "name": "MakerHub CN", "status": "running"},
            {"id": "profile-global", "name": "MakerHub Global", "status": "running"},
        ]
        now = 20_000.0
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir)), \
                patch.dict(os.environ, {
                    "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                    "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                }, clear=False), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=nullcontext()), \
                patch.object(cloakbrowser_session, "_request") as request_mock, \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock:
            cloakbrowser_session.touch_profile_activity("cn", now=now - 121, detail="fetch")
            cloakbrowser_session.touch_profile_activity("global", now=now - 121, detail="prepare-login")
            request_mock.side_effect = [
                profiles,
                {"id": "profile-cn", "name": "MakerHub CN", "status": "running"},
            ]

            result = cloakbrowser_session.stop_idle_profiles(
                idle_seconds=1800,
                automation_idle_seconds=120,
                now=now,
            )

        self.assertEqual(result["automation_idle_seconds"], 120)
        self.assertEqual(result["stopped_profiles"], ["profile-cn"])
        stop_mock.assert_called_once_with("profile-cn")

    def test_profile_operation_records_latest_activity_detail(self):
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir)), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=nullcontext()):
            with cloakbrowser_session._profile_operation("global", detail="fetch"):
                pass

            detail = cloakbrowser_session.profile_activity_detail("global")

        self.assertEqual(detail, "fetch")

    def test_profile_resource_name_is_stable_before_and_after_profile_resolution(self):
        self.assertEqual(
            cloakbrowser_session._profile_resource_name("cn"),
            cloakbrowser_session._profile_resource_name("cn", "profile-cn"),
        )

    def test_spawned_bridge_operations_for_same_profile_do_not_overlap(self):
        with tempfile.TemporaryDirectory() as state_dir:
            ready_queue = SPAWN_CONTEXT.Queue()
            start_event = SPAWN_CONTEXT.Event()
            active = SPAWN_CONTEXT.Value("i", 0)
            max_active = SPAWN_CONTEXT.Value("i", 0)
            counter_lock = SPAWN_CONTEXT.Lock()
            processes = [
                SPAWN_CONTEXT.Process(
                    target=_hold_cloakbrowser_profile_slot,
                    args=(state_dir, ready_queue, start_event, active, max_active, counter_lock),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready_queue.get(timeout=5))
            start_event.set()
            for process in processes:
                process.join(timeout=10)

            self.assertEqual([process.exitcode for process in processes], [0, 0])
            self.assertEqual(max_active.value, 1)

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

    def test_request_classifies_server_error_as_temporarily_unavailable(self):
        response = Mock(status_code=502, content=b"")
        response.json.side_effect = ValueError
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ), patch.object(cloakbrowser_session.requests, "request", return_value=response):
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "HTTP 502"):
                cloakbrowser_session._request("GET", "/api/profiles/profile-cn")

    def test_bridge_payload_requires_auth_token_before_subprocess_io(self):
        with patch.dict(
            os.environ,
            {"MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080"},
            clear=True,
        ), patch.object(cloakbrowser_session.subprocess, "run") as run_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "AUTH_TOKEN"):
                cloakbrowser_session._bridge_payload("profile-cn", action="snapshot")

        run_mock.assert_not_called()

    def test_3mf_authorization_bridge_timeout_covers_default_navigation_budget(self):
        self.assertEqual(
            cloakbrowser_session._authorization_bridge_timeout_seconds(30_000),
            210,
        )

    def test_3mf_authorization_bridge_timeout_covers_maximum_navigation_budget(self):
        self.assertEqual(
            cloakbrowser_session._authorization_bridge_timeout_seconds(120_000),
            330,
        )

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
        self.assertIn('if (input.action === "click")', source)
        self.assertIn("await button.click({ delay: 20 })", source)
        self.assertIn("authorizationResponseMatches(response, instanceId)", source)
        self.assertIn('client.send("Target.closeTarget", { targetId })', source)
        self.assertNotIn("async function fetchAuthorization", source)

    def test_bridge_click_supports_makerworld_primary_download_span(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("button, a, [role='button'], .primaryButton", source)
        self.assertIn("threeMfDownloadActionScore(candidate)", source)
        self.assertIn("candidate.contextText", source)
        self.assertIn("candidate.ariaLabel", source)

    def test_bridge_click_continues_after_navigation_timeout(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('error.name !== "TimeoutError"', source)
        self.assertIn("navigation_timed_out: navigationTimedOut", source)
        self.assertLess(
            source.index('error.name !== "TimeoutError"'),
            source.index("const button = await findThreeMfDownloadButton"),
        )

    def test_bridge_completes_official_login_confirmation_after_navigation(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("completeBambuLoginConfirmation", source)
        self.assertIn("hasMakerWorldSessionCookie", source)
        self.assertIn('input.action === "login" || input.action === "sync"', source)
        self.assertLess(
            source.index("await page.goto(targetUrl"),
            source.index("await completeBambuLoginConfirmation"),
        )
        self.assertNotIn("tryDirectTicketLogin", source)

    def test_browser_fetch_rejects_non_makerworld_target(self):
        with patch.object(cloakbrowser_session, "ensure_profile") as ensure_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserError, "目标地址"):
                cloakbrowser_session.browser_fetch("cn", "https://example.com/private")

        ensure_mock.assert_not_called()

    def test_browser_fetch_uses_temporary_profile_page_and_returns_structured_result(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_result = {
            "status_code": 200,
            "url": "https://makerworld.com.cn/zh/models/1",
            "content_type": "text/html; charset=utf-8",
            "text": "<html>ok</html>",
        }
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ), patch.object(
            cloakbrowser_session,
            "ensure_profile",
            return_value=profile,
        ) as ensure_mock, patch.object(
            cloakbrowser_session,
            "launch_profile",
            return_value=(profile, False),
        ) as launch_mock, patch.object(
            cloakbrowser_session,
            "_run_bridge",
            return_value=bridge_result,
        ) as bridge_mock:
            result = cloakbrowser_session.browser_fetch(
                "cn",
                "https://makerworld.com.cn/zh/models/1",
                profile_id="profile-cn",
                headers={
                    "Accept": "text/html",
                    "Referer": "https://makerworld.com.cn/",
                    "User-Agent": "must-use-profile-fingerprint",
                    "X-BBL-App-Source": "makerworld",
                    "X-BBL-Client-Type": "web",
                    "x-bbl-captcha-result": "verified",
                    "Cookie": "must-not-cross-the-bridge",
                    "Host": "attacker.example",
                },
                cookie_items=[{"name": "token", "value": "legacy", "domain": ".makerworld.com.cn"}],
            )

        self.assertEqual(result.profile_id, "profile-cn")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "text/html; charset=utf-8")
        self.assertEqual(result.text, "<html>ok</html>")
        payload = bridge_mock.call_args.args[0]
        self.assertEqual(payload["action"], "fetch")
        self.assertEqual(payload["headers"], {
            "Accept": "text/html",
            "Referer": "https://makerworld.com.cn/",
            "X-BBL-App-Source": "makerworld",
            "X-BBL-Client-Type": "web",
            "x-bbl-captcha-result": "verified",
        })
        self.assertEqual(payload["cookies"][0]["name"], "token")
        ensure_mock.assert_not_called()
        launch_mock.assert_not_called()

    def test_browser_fetch_falls_back_to_profile_start_when_direct_cdp_connection_fails(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_result = {
            "status_code": 200,
            "url": "https://makerworld.com.cn/zh/models/1",
            "content_type": "text/html; charset=utf-8",
            "text": "<html>ok</html>",
        }
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ), patch.object(
            cloakbrowser_session,
            "ensure_profile",
            return_value=profile,
        ) as ensure_mock, patch.object(
            cloakbrowser_session,
            "launch_profile",
            return_value=(profile, True),
        ) as launch_mock, patch.object(
            cloakbrowser_session,
            "_run_bridge",
            side_effect=[
                cloakbrowser_session.CloakBrowserBridgeError("Unexpected server response: 503"),
                bridge_result,
            ],
        ) as bridge_mock:
            result = cloakbrowser_session.browser_fetch(
                "cn",
                "https://makerworld.com.cn/zh/models/1",
                profile_id="profile-cn",
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(bridge_mock.call_count, 2)
        ensure_mock.assert_called_once_with("cn", "profile-cn", browser_proxy=None)
        launch_mock.assert_called_once_with(profile)

    def test_browser_fetch_protocol_timeout_does_not_restart_profile(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(profile, False)), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out."),
                ), \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            with self.assertRaises(cloakbrowser_session.CloakBrowserBridgeError):
                cloakbrowser_session.browser_fetch(
                    "cn",
                    "https://makerworld.com.cn/zh/models/1",
                    profile_id="profile-cn",
                )

        stop_mock.assert_not_called()

    def test_browser_fetch_rejects_redirect_outside_platform_domains(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        with patch.dict(
            os.environ,
            {
                "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
            },
            clear=True,
        ), patch.object(
            cloakbrowser_session,
            "ensure_profile",
            return_value=profile,
        ), patch.object(
            cloakbrowser_session,
            "launch_profile",
            return_value=(profile, False),
        ), patch.object(
            cloakbrowser_session,
            "_run_bridge",
            return_value={
                "status_code": 302,
                "url": "https://attacker.example/login",
                "content_type": "text/html",
                "text": "",
            },
        ):
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserError, "目标地址"):
                cloakbrowser_session.browser_fetch(
                    "cn",
                    "https://makerworld.com.cn/zh/models/1",
                    profile_id="profile-cn",
                )

    def test_bridge_uses_hidden_cdp_fetch_and_closes_every_temporary_target_by_id(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        self.assertIn('if (input.action === "fetch")', source)
        self.assertIn("async function fetchBrowserResponse", source)
        helper_start = source.index("async function withTemporaryTarget")
        helper_end = source.index("async function fetchBrowserResponse")
        helper_source = source[helper_start:helper_end]
        self.assertIn('client.send("Target.createTarget"', helper_source)
        self.assertIn("background: true", helper_source)
        self.assertIn('client.send("Target.closeTarget", { targetId })', helper_source)
        self.assertIn("withTemporaryTarget(browser, context, { hidden: true }", helper_source)
        self.assertIn("const session = await target.createCDPSession()", helper_source)
        self.assertNotIn("await target.page()", helper_source[
            helper_source.index("async function withTemporaryCdpSession"):
            helper_source.index("async function withTemporaryPage")
        ])
        self.assertIn("withTemporaryTarget(browser, context, { hidden: false }", helper_source)
        fetch_start = source.index("async function fetchBrowserResponse")
        fetch_end = source.index("function isThreeMfAuthorizationUrl")
        fetch_source = source[fetch_start:fetch_end]
        self.assertIn("withTemporaryCdpSession(browser, context, async (session) =>", fetch_source)
        self.assertIn('session.send("Fetch.enable"', fetch_source)
        self.assertIn('session.send("Page.navigate"', fetch_source)
        self.assertIn('session.send("Network.getResponseBody"', fetch_source)
        self.assertIn("maxTotalBufferSize: BROWSER_FETCH_TOTAL_BUFFER_BYTES", fetch_source)
        self.assertIn("maxResourceBufferSize: BROWSER_FETCH_RESOURCE_BUFFER_BYTES", fetch_source)
        self.assertIn("enableDurableMessages: true", fetch_source)
        self.assertIn('event.resourceType !== "Document"', fetch_source)
        self.assertNotIn("withTemporaryPage", fetch_source)
        self.assertNotIn("context.newPage()", fetch_source)
        self.assertIn("const profileCookies = (await context.cookies()).filter", fetch_source)
        self.assertNotIn("page.setExtraHTTPHeaders", fetch_source)
        click_start = source.index("async function clickAuthorization")
        click_end = source.index("async function main")
        click_source = source[click_start:click_end]
        self.assertIn("withTemporaryPage(browser, context, async (page) =>", click_source)
        self.assertNotIn("context.newPage()", click_source)

    def test_bridge_closes_stale_makerhub_api_targets_before_automation(self):
        source = cloakbrowser_session.BRIDGE_SCRIPT.read_text(encoding="utf-8")

        cleanup_start = source.index("async function cleanupStaleAutomationTargets")
        cleanup_end = source.index("async function withTemporaryTarget")
        cleanup_source = source[cleanup_start:cleanup_end]
        self.assertIn('client.send("Target.getTargets")', cleanup_source)
        self.assertIn('targetInfo.type !== "page"', cleanup_source)
        self.assertIn("isMakerHubApiTargetUrl(targetInfo.url, platform)", cleanup_source)
        self.assertIn('client.send("Target.closeTarget", { targetId: targetInfo.targetId })', cleanup_source)
        self.assertIn("await cleanupStaleAutomationTargets(browser, context, input.platform)", source)

    def test_ensure_profile_reuses_saved_profile_id(self):
        responses = [
            {"id": "profile-cn", "name": "MakerHub CN", "status": "stopped", "launch_args": []},
            {
                "id": "profile-cn",
                "name": "MakerHub CN",
                "status": "stopped",
                "launch_args": ["--disable-gpu"],
            },
        ]
        with patch.object(cloakbrowser_session, "_request", side_effect=responses) as request_mock:
            profile = cloakbrowser_session.ensure_profile("cn", "profile-cn")

        self.assertEqual(profile.id, "profile-cn")
        self.assertEqual(profile.launch_args, ("--disable-gpu",))
        self.assertEqual(request_mock.call_count, 2)
        request_mock.assert_any_call("GET", "/api/profiles/profile-cn")
        request_mock.assert_any_call(
            "PUT",
            "/api/profiles/profile-cn",
            json_payload={"launch_args": ["--disable-gpu"]},
        )

    def test_ensure_profile_preserves_custom_launch_args_when_adding_low_cpu_default(self):
        responses = [
            {
                "id": "profile-cn",
                "name": "MakerHub CN",
                "status": "stopped",
                "launch_args": ["--window-size=1280,720"],
            },
            {
                "id": "profile-cn",
                "name": "MakerHub CN",
                "status": "stopped",
                "launch_args": ["--window-size=1280,720", "--disable-gpu"],
            },
        ]
        with patch.object(cloakbrowser_session, "_request", side_effect=responses) as request_mock:
            profile = cloakbrowser_session.ensure_profile("cn", "profile-cn")

        self.assertEqual(profile.launch_args, ("--window-size=1280,720", "--disable-gpu"))
        self.assertEqual(
            request_mock.call_args_list[1].kwargs["json_payload"]["launch_args"],
            ["--window-size=1280,720", "--disable-gpu"],
        )

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
        self.assertEqual(create_payload["launch_args"], ["--disable-gpu"])
        self.assertEqual({item["tag"] for item in create_payload["tags"]}, {"makerhub", "cn"})

    def test_ensure_profile_sets_managed_global_proxy_when_creating_profile(self):
        responses = [[], {"id": "created-global", "name": "MakerHub Global", "status": "stopped"}]
        with patch.object(cloakbrowser_session, "_request", side_effect=responses) as request_mock:
            profile = cloakbrowser_session.ensure_profile(
                "global",
                browser_proxy="http://proxy.example:7890",
            )

        self.assertEqual(profile.id, "created-global")
        self.assertEqual(
            request_mock.call_args_list[1].kwargs["json_payload"]["proxy"],
            "http://proxy.example:7890",
        )

    def test_global_profile_proxy_change_stops_updates_and_relaunches_profile(self):
        direct_profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-global",
            name="MakerHub Global",
            status="running",
        )
        stopped_profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-global",
            name="MakerHub Global",
            status="stopped",
        )
        proxied_profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-global",
            name="MakerHub Global",
            status="stopped",
            proxy="http://proxy.example:7891",
        )
        running_profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-global",
            name="MakerHub Global",
            status="running",
            proxy="http://proxy.example:7891",
        )
        proxy_config = {
            "enabled": True,
            "http_proxy": "http://proxy.example:7890",
            "https_proxy": "http://proxy.example:7891",
        }

        with patch.object(cloakbrowser_session, "ensure_profile", return_value=direct_profile), \
                patch.object(cloakbrowser_session, "_stop_profile_for_proxy_change", return_value=stopped_profile) as stop_mock, \
                patch.object(cloakbrowser_session, "_update_profile_proxy", return_value=proxied_profile) as update_mock, \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running_profile, True)) as launch_mock:
            _profile, running, launched_here = cloakbrowser_session._ensure_running_profile(
                "global",
                "profile-global",
                proxy_config=proxy_config,
            )

        self.assertEqual(running.proxy, "http://proxy.example:7891")
        self.assertTrue(launched_here)
        stop_mock.assert_called_once_with(direct_profile)
        update_mock.assert_called_once_with(stopped_profile, "http://proxy.example:7891")
        launch_mock.assert_called_once_with(proxied_profile)

    def test_cn_profile_keeps_its_browser_proxy_unmanaged(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
            proxy="http://user-controlled.example:7890",
        )
        with patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "_update_profile_proxy") as update_mock, \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(profile, False)):
            _profile, running, launched_here = cloakbrowser_session._ensure_running_profile(
                "cn",
                "profile-cn",
                proxy_config={"enabled": True, "https_proxy": "http://proxy.example:7891"},
            )

        self.assertEqual(running.proxy, "http://user-controlled.example:7890")
        self.assertFalse(launched_here)
        update_mock.assert_not_called()

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
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    return_value={"ok": True, "cookies": [], "storage": []},
                ) as bridge_mock, \
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
        bridge_payload = bridge_mock.call_args.args[0]
        self.assertEqual(bridge_payload["action"], "login")
        self.assertEqual(bridge_payload["platform"], "cn")
        stop_mock.assert_not_called()

    def test_collect_browser_session_navigates_blank_profile_to_platform_home(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN", status="running")
        snapshot = {
            "ok": True,
            "current_url": "https://makerworld.com.cn/zh",
            "cookies": [{"name": "token", "value": "browser-token", "domain": ".makerworld.com.cn"}],
            "storage": [],
        }

        with patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)), \
                patch.object(cloakbrowser_session, "_run_bridge", return_value=snapshot) as bridge_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            result = cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        self.assertEqual(result.cookie, "token=browser-token")
        bridge_payload = bridge_mock.call_args.args[0]
        self.assertEqual(bridge_payload["action"], "sync")
        self.assertEqual(bridge_payload["platform"], "cn")
        self.assertEqual(bridge_payload["target_url"], "https://makerworld.com.cn/zh")

    def test_collect_browser_session_restarts_stuck_profile_once_and_returns_retry_snapshot(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        snapshot = {
            "ok": True,
            "current_url": "https://makerworld.com.cn/zh",
            "cookies": [{"name": "token", "value": "recovered", "domain": ".makerworld.com.cn"}],
            "storage": [],
        }

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile) as ensure_mock, \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)) as launch_mock, \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=[
                        cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out."),
                        snapshot,
                    ],
                ) as bridge_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            result = cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        self.assertEqual(result.cookie, "token=recovered")
        self.assertEqual(bridge_mock.call_count, 2)
        self.assertEqual(ensure_mock.call_count, 2)
        self.assertEqual(launch_mock.call_count, 2)
        stop_mock.assert_called_once_with("profile-cn")

    def test_collect_browser_session_classifies_repeated_protocol_timeout_as_unavailable(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        timeout_error = cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out.")

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=[timeout_error, timeout_error],
                ) as bridge_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "自动重启"):
                cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        self.assertEqual(bridge_mock.call_count, 2)
        stop_mock.assert_called_once_with("profile-cn")

    def test_collect_browser_session_does_not_attempt_profile_launch_again_during_recovery_cooldown(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)) as launch_mock, \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=[
                        cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out."),
                        cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out."),
                    ],
                ) as bridge_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            for _ in range(2):
                with self.assertRaises(cloakbrowser_session.CloakBrowserUnavailable):
                    cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        self.assertEqual(bridge_mock.call_count, 2)
        self.assertEqual(launch_mock.call_count, 2)
        stop_mock.assert_called_once_with("profile-cn")

    def test_collect_browser_session_throttles_transient_profile_launch_failures(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        launch_error = cloakbrowser_session.CloakBrowserUnavailable(
            "指纹浏览器返回 HTTP 500：Xvnc failed to start"
        )

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(
                    cloakbrowser_session,
                    "launch_profile",
                    side_effect=launch_error,
                ) as launch_mock:
            for _ in range(2):
                with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserUnavailable, "指纹浏览器"):
                    cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        launch_mock.assert_called_once_with(profile)

    def test_collect_browser_session_does_not_restart_for_non_transient_bridge_error(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=cloakbrowser_session.CloakBrowserBridgeError(
                        "model page did not expose an enabled 3MF download action"
                    ),
                ), \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserBridgeError, "model page"):
                cloakbrowser_session.collect_browser_session("cn", "profile-cn")

        stop_mock.assert_not_called()

    def test_browser_3mf_authorization_uses_target_page_click_without_cookie_payload(self):
        profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="MakerHub CN")
        running = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_result = {
            "ok": True,
            "status_code": 200,
            "payload": {"name": "part.3mf", "url": "https://download.example.test/part.3mf"},
            "verification": {
                "attempted": True,
                "completed": True,
                "provider": "geetest4",
                "challenge_type": "slider",
                "attempts": 1,
                "reason": "completed",
                "confidence": 0.92,
                "token": "must-not-leak",
            },
        }

        with patch.object(cloakbrowser_session, "resource_slot", create=True) as resource_slot_mock, \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(running, False)), \
                patch.object(cloakbrowser_session, "_run_bridge", return_value=bridge_result) as bridge_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                        "MAKERHUB_CLOAKBROWSER_TIMEOUT": "30",
                    },
                    clear=True,
                ):
            result = cloakbrowser_session.browser_authorize_3mf_download(
                "cn",
                "https://api.bambulab.cn/v1/design-service/instance/123/f3mf?type=download&fileType=3mf",
                profile_id="profile-cn",
                model_url="https://makerworld.com.cn/zh/models/456",
                instance_id="123",
            )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["payload"]["name"], "part.3mf")
        self.assertEqual(
            result["verification"],
            {
                "attempted": True,
                "completed": True,
                "provider": "geetest4",
                "challenge_type": "slider",
                "attempts": 1,
                "reason": "completed",
                "confidence": 0.92,
            },
        )
        bridge_payload = bridge_mock.call_args.args[0]
        self.assertEqual(bridge_payload["action"], "click")
        self.assertEqual(bridge_payload["platform"], "cn")
        self.assertEqual(
            bridge_payload["model_url"],
            "https://makerworld.com.cn/zh/models/456#profileId-123",
        )
        self.assertEqual(bridge_payload["instance_id"], "123")
        self.assertEqual(bridge_payload["cookies"], [])
        self.assertNotIn("raw_cookie", bridge_payload)
        self.assertEqual(bridge_payload["navigation_timeout_ms"], 30000)
        self.assertEqual(bridge_payload["authorization_timeout_ms"], 90000)
        self.assertFalse(bridge_payload["auto_verify_3mf"])
        self.assertEqual(bridge_mock.call_args.kwargs["timeout_seconds"], 210)
        resource_slot_mock.assert_called_once_with(
            "cloakbrowser_platform_cn",
            detail="click",
            priority=100,
        )

    def test_browser_3mf_authorization_retries_one_transient_bridge_failure(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_result = {
            "ok": True,
            "status_code": 200,
            "payload": {
                "name": "part.3mf",
                "url": "https://download.example.test/part.3mf",
            },
        }

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=nullcontext()), \
                patch.object(
                    cloakbrowser_session,
                    "_ensure_running_profile",
                    return_value=(profile, profile, False),
                ) as ensure_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=[
                        cloakbrowser_session.CloakBrowserBridgeError(
                            "CDP endpoint returned HTTP 502"
                        ),
                        bridge_result,
                    ],
                ) as bridge_mock, \
                patch.object(cloakbrowser_session.time, "sleep") as sleep_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=True,
                ):
            result = cloakbrowser_session.browser_authorize_3mf_download(
                "cn",
                "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
                profile_id="profile-cn",
                model_url="https://makerworld.com.cn/zh/models/456",
                instance_id="123",
            )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(bridge_mock.call_count, 2)
        self.assertEqual(
            [call.kwargs["allow_recovery_restart"] for call in ensure_mock.call_args_list],
            [False, True],
        )
        sleep_mock.assert_called_once_with(
            cloakbrowser_session.AUTHORIZATION_TRANSIENT_RETRY_DELAY_SECONDS
        )

    def test_browser_3mf_authorization_reads_auto_verify_flag_per_operation(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_result = {
            "ok": True,
            "status_code": 418,
            "payload": {"captchaId": "captcha-123"},
        }
        values = (
            ("true", True),
            ("false", False),
            ("0", False),
            ("off", False),
            (None, False),
        )

        with patch.object(cloakbrowser_session, "resource_slot", create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(profile, False)), \
                patch.object(cloakbrowser_session, "_run_bridge", return_value=bridge_result) as bridge_mock:
            for value, expected in values:
                env = {
                    "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                    "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                }
                if value is not None:
                    env["MAKERHUB_AUTO_VERIFY_3MF"] = value
                with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                    cloakbrowser_session.browser_authorize_3mf_download(
                        "cn",
                        "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
                        profile_id="profile-cn",
                        model_url="https://makerworld.com.cn/zh/models/456",
                        instance_id="123",
                    )
                    self.assertIs(
                        bridge_mock.call_args.args[0]["auto_verify_3mf"],
                        expected,
                    )

    def test_browser_3mf_authorization_passes_dynamic_timeout_to_subprocess(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        bridge_process = Mock(
            returncode=0,
            stdout=(
                '{"ok":true,"status_code":200,'
                '"payload":{"name":"part.3mf","url":"https://download.example.test/part.3mf"}}'
            ),
            stderr="",
        )

        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "resource_slot", return_value=nullcontext()), \
                patch.object(cloakbrowser_session, "_managed_profile_proxy", return_value=None), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(profile, False)), \
                patch.object(
                    cloakbrowser_session.subprocess,
                    "run",
                    return_value=bridge_process,
                ) as run_mock, \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                        "MAKERHUB_CLOAKBROWSER_TIMEOUT": "120",
                    },
                    clear=True,
                ):
            result = cloakbrowser_session.browser_authorize_3mf_download(
                "cn",
                "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
                profile_id="profile-cn",
                model_url="https://makerworld.com.cn/zh/models/456",
                instance_id="123",
            )

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 330)

    def test_browser_3mf_authorization_protocol_timeout_does_not_restart_profile(self):
        profile = cloakbrowser_session.CloakBrowserProfile(
            id="profile-cn",
            name="MakerHub CN",
            status="running",
        )
        with tempfile.TemporaryDirectory() as state_dir, \
                patch.object(cloakbrowser_session, "STATE_DIR", Path(state_dir), create=True), \
                patch.object(cloakbrowser_session, "ensure_profile", return_value=profile), \
                patch.object(cloakbrowser_session, "launch_profile", return_value=(profile, False)), \
                patch.object(cloakbrowser_session, "stop_profile") as stop_mock, \
                patch.object(
                    cloakbrowser_session,
                    "_run_bridge",
                    side_effect=cloakbrowser_session.CloakBrowserBridgeError("Network.enable timed out."),
                ), \
                patch.dict(
                    os.environ,
                    {
                        "MAKERHUB_CLOAKBROWSER_URL": "http://cloakbrowser:8080",
                        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "secret-token",
                    },
                    clear=False,
                ):
            with self.assertRaises(cloakbrowser_session.CloakBrowserBridgeError):
                cloakbrowser_session.browser_authorize_3mf_download(
                    "cn",
                    "https://api.bambulab.cn/v1/design-service/instance/123/f3mf?type=download&fileType=3mf",
                    profile_id="profile-cn",
                    model_url="https://makerworld.com.cn/zh/models/456",
                    instance_id="123",
                )

        stop_mock.assert_not_called()

    def test_browser_3mf_authorization_rejects_model_page_from_another_platform(self):
        with patch.object(cloakbrowser_session, "_run_bridge") as bridge_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserError, "模型页面"):
                cloakbrowser_session.browser_authorize_3mf_download(
                    "cn",
                    "https://api.bambulab.cn/v1/design-service/instance/123/f3mf?type=download&fileType=3mf",
                    profile_id="profile-cn",
                    model_url="https://makerworld.com/zh/models/456",
                    instance_id="123",
                )

        bridge_mock.assert_not_called()

    def test_browser_3mf_authorization_rejects_non_makerworld_endpoint_before_bridge(self):
        with patch.object(cloakbrowser_session, "_run_bridge") as bridge_mock:
            with self.assertRaisesRegex(cloakbrowser_session.CloakBrowserError, "授权地址"):
                cloakbrowser_session.browser_authorize_3mf_download(
                    "cn",
                    "https://example.test/v1/design-service/instance/123/f3mf",
                    profile_id="profile-cn",
                )

        bridge_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
