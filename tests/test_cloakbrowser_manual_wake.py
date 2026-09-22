from __future__ import annotations

from contextlib import contextmanager, nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services import cloakbrowser_session as cb


class CloakBrowserManualWakeTest(unittest.TestCase):
    def setUp(self):
        state_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(cb, "STATE_DIR", Path(state_dir)))
        self.enterContext(patch.object(cb, "cloakbrowser_configured", return_value=True))
        self.enterContext(patch.object(cb, "_configured_url", return_value="http://browser.test"))
        self.enterContext(patch.object(cb, "_auth_token", return_value="test-token"))
        self.enterContext(patch.object(cb, "_managed_profile_proxy", return_value=""))
        self.enterContext(patch.object(cb, "resource_slot", return_value=nullcontext()))
        self.profile = {
            "id": "profile-global", "name": "MakerHub Global", "status": "running",
            "launch_args": ["--disable-gpu"], "proxy": "",
        }
        self.requests = []

        def request(method, path, **kwargs):
            self.requests.append((method, path))
            if method == "POST" and path.endswith("/launch"):
                self.profile["status"] = "running"
            if path == "/api/profiles":
                return [dict(self.profile)]
            if path.startswith("/api/profiles/profile-global"):
                return dict(self.profile)
            raise AssertionError((method, path))

        self.enterContext(patch.object(cb, "_request", side_effect=request))

    def test_manual_window_survives_background_fetch_within_manual_timeout(self):
        cb.touch_profile_activity("global", now=19_000, detail="prepare-login")
        cb.touch_profile_activity("global", now=19_800, detail="fetch")
        with patch.object(cb, "stop_profile") as stop:
            result = cb.stop_idle_profiles(idle_seconds=1800, automation_idle_seconds=120, now=20_000)

        self.assertEqual(result["stopped_count"], 0)
        stop.assert_not_called()

    def test_manual_protection_expires_and_background_idle_reclaims_profile(self):
        cb.touch_profile_activity("global", now=18_000, detail="prepare-login")
        cb.touch_profile_activity("global", now=19_800, detail="fetch")
        with patch.object(cb, "stop_profile") as stop:
            result = cb.stop_idle_profiles(idle_seconds=1800, automation_idle_seconds=120, now=20_000)

        self.assertEqual(result["stopped_profiles"], ["profile-global"])
        self.assertEqual(cb.profile_activity_at("global"), 0)
        stop.assert_called_once_with("profile-global")

    def test_manual_protection_is_independent_between_platforms(self):
        cb.touch_profile_activity("cn", now=19_000, detail="prepare-login")
        cb.touch_profile_activity("global", now=19_800, detail="fetch")
        result = cb.stop_idle_profiles(idle_seconds=1800, automation_idle_seconds=120, now=20_000)
        self.assertEqual(result["stopped_profiles"], ["profile-global"])

    def test_disabled_manual_idle_timeout_survives_later_background_fetch(self):
        cb.touch_profile_activity("global", now=10_000, detail="prepare-login")
        cb.touch_profile_activity("global", now=19_800, detail="fetch")
        result = cb.stop_idle_profiles(idle_seconds=0, automation_idle_seconds=120, now=20_000)
        self.assertEqual(result["stopped_count"], 0)

    def test_idle_cleanup_rechecks_manual_protection_after_acquiring_slot(self):
        @contextmanager
        def occupied_slot(*args, **kwargs):
            cb.touch_profile_activity("global", now=19_700, detail="prepare-login")
            cb.touch_profile_activity("global", now=19_800, detail="fetch")
            yield

        cb.touch_profile_activity("global", now=19_000, detail="fetch")
        with patch.object(cb, "resource_slot", side_effect=occupied_slot):
            result = cb.stop_idle_profiles(idle_seconds=1800, automation_idle_seconds=120, now=20_000)
        self.assertEqual(result["stopped_count"], 0)

    def test_manual_open_can_launch_stopped_profile_during_automatic_recovery_cooldown(self):
        self.profile["status"] = "stopped"
        cb._mark_profile_recovery_attempt("profile-global")
        with patch.object(cb, "_run_bridge", return_value={"cookies": [], "storage": []}) as bridge:
            result = cb.prepare_browser_login("global", profile_id="profile-global")

        self.assertEqual(result.profile_id, "profile-global")
        self.assertTrue(result.launched_here)
        self.assertEqual(self.requests.count(("POST", "/api/profiles/profile-global/launch")), 1)
        bridge.assert_called_once()
        self.assertFalse(cb._profile_recovery_cooldown_active("profile-global"))

    def test_running_profile_can_sync_during_cooldown_without_restart(self):
        cb._mark_profile_recovery_attempt("profile-global")
        snapshot = {"cookies": [{"name": "token", "value": "current", "domain": ".makerworld.com"}], "storage": []}
        with patch.object(cb, "_run_bridge", return_value=snapshot) as bridge:
            result = cb.collect_browser_session("global", "profile-global")

        self.assertEqual(result.cookie, "token=current")
        self.assertFalse(any(method == "POST" for method, _ in self.requests))
        bridge.assert_called_once()
        self.assertFalse(cb._profile_recovery_cooldown_active("profile-global"))

    def test_manual_launch_failure_is_attempted_once_and_retains_cooldown(self):
        self.profile["status"] = "stopped"
        cb._mark_profile_recovery_attempt("profile-global")
        request = cb._request.side_effect

        def failed_launch(method, path, **kwargs):
            if method == "POST" and path.endswith("/launch"):
                self.requests.append((method, path))
                raise cb.CloakBrowserUnavailable("Xvnc startup failed")
            return request(method, path, **kwargs)

        with patch.object(cb, "_request", side_effect=failed_launch), \
                patch.object(cb, "_run_bridge") as bridge:
            with self.assertRaisesRegex(cb.CloakBrowserUnavailable, "启动失败"):
                cb.prepare_browser_login("global", profile_id="profile-global")

        self.assertEqual(self.requests.count(("POST", "/api/profiles/profile-global/launch")), 1)
        bridge.assert_not_called()
        self.assertTrue(cb._profile_recovery_cooldown_active("profile-global"))

    def test_failed_connection_during_cooldown_does_not_restart_profile(self):
        cb._mark_profile_recovery_attempt("profile-global")
        with patch.object(cb, "_run_bridge", side_effect=cb.CloakBrowserBridgeError("Network.enable timed out")) as bridge:
            with self.assertRaises(cb.CloakBrowserUnavailable):
                cb.collect_browser_session("global", "profile-global")

        bridge.assert_called_once()
        self.assertFalse(any(method == "POST" for method, _ in self.requests))
        self.assertTrue(cb._profile_recovery_cooldown_active("profile-global"))
