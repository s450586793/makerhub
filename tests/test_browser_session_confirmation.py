from __future__ import annotations

import copy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.schemas.models import CookiePair
from app.services import browser_session_confirmation as confirmation
from app.services.cloakbrowser_session import CloakBrowserSessionResult


class _CopyingStore:
    def __init__(self, accounts):
        self.config = SimpleNamespace(cookies=copy.deepcopy(accounts), unrelated={"preserve": True})
        self.before_update = None
        self.update_error = None
        self.update_calls = 0

    def load(self):
        return copy.deepcopy(self.config)

    def update(self, mutator):
        self.update_calls += 1
        if self.before_update is not None:
            self.before_update(self.config)
        candidate = self.load()
        updated = mutator(candidate)
        if self.update_error is not None:
            raise self.update_error
        self.config = copy.deepcopy(candidate if updated is None else updated)
        return self.load()


class BrowserSessionConfirmationTest(unittest.TestCase):
    def account(self, platform="cn", **overrides):
        values = {
            "platform": platform,
            "cookie": "token=same-account; refreshToken=saved-refresh; cf_clearance=saved-clearance",
            "username": "saved-user",
            "account_id": "saved-account",
            "status": "ok",
            "message": "keep account health message",
            "updated_at": "2026-11-17T10:00:00+08:00",
            "browser_profile_id": "profile-" + platform,
            "browser_status": "action_required",
            "browser_message": "stale login warning",
            "browser_synced_at": "2026-11-17T09:00:00+08:00",
        }
        values.update(overrides)
        return CookiePair(**values)

    def fresh(self, expected, **overrides):
        values = {
            "profile_id": expected.browser_profile_id,
            "cookie": "token=same-account; refreshToken=fresh-refresh; cf_clearance=fresh-clearance",
            # URL 校验由产生非空 Cookie 的上游负责，确认 helper 不重复解析。
            "current_url": "",
        }
        values.update(overrides)
        return CloakBrowserSessionResult(**values)

    def snapshot(self, store):
        config = store.load()
        return {"cookies": [account.model_dump() for account in config.cookies],
                "unrelated": config.unrelated}

    def test_same_account_confirmation_updates_only_browser_metadata_for_both_platforms(self):
        for platform in ("cn", "global"):
            with self.subTest(platform=platform):
                expected = self.account(platform)
                other = self.account("global" if platform == "cn" else "cn")
                store = _CopyingStore([expected, other])
                before = self.snapshot(store)
                expected_before = expected.model_dump()
                fresh = self.fresh(expected)

                self.assertIs(confirmation.confirm_browser_session(store, platform, expected, fresh), True)

                after = self.snapshot(store)
                current = after["cookies"][0]
                self.assertEqual(current["browser_status"], "synced")
                self.assertTrue(current["browser_message"])
                self.assertNotEqual(current["browser_message"], expected.browser_message)
                self.assertNotEqual(current["browser_synced_at"], expected.browser_synced_at)
                datetime.fromisoformat(current["browser_synced_at"])
                for field in ("browser_status", "browser_message", "browser_synced_at"):
                    before["cookies"][0][field] = current[field]
                self.assertEqual(after, before)
                self.assertEqual(expected.model_dump(), expected_before)
                self.assertEqual(current["cookie"], expected.cookie)
                self.assertNotEqual(current["cookie"], fresh.cookie)

    def test_missing_or_rotated_fresh_token_cannot_confirm(self):
        for cookie in ("", "cf_clearance=only", "refreshToken=only", "token= ", "token=another-account"):
            with self.subTest(cookie=cookie):
                expected = self.account()
                store = _CopyingStore([expected])
                before = self.snapshot(store)
                self.assertIs(confirmation.confirm_browser_session(
                    store, "cn", expected, self.fresh(expected, cookie=cookie)), False)
                self.assertEqual(self.snapshot(store), before)

    def test_missing_expected_token_cannot_be_confirmed_by_a_fresh_browser(self):
        for cookie in ("", "refreshToken=only", "cf_clearance=only"):
            with self.subTest(cookie=cookie):
                expected = self.account(cookie=cookie)
                store = _CopyingStore([expected])
                before = self.snapshot(store)
                self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected)), False)
                self.assertEqual(self.snapshot(store), before)

    def test_wrong_platform_or_profile_never_updates_any_account(self):
        for platform, expected_profile, result_profile in (
            ("global", "profile-cn", "profile-cn"),
            ("unsupported", "profile-cn", "profile-cn"),
            ("cn", "profile-cn", "profile-global"),
            ("cn", "profile-cn", ""),
            ("cn", "", ""),
        ):
            with self.subTest(platform=platform, expected_profile=expected_profile, result_profile=result_profile):
                expected = self.account(browser_profile_id=expected_profile)
                store = _CopyingStore([expected, self.account("global")])
                before = self.snapshot(store)
                result = self.fresh(expected, profile_id=result_profile)
                self.assertIs(confirmation.confirm_browser_session(store, platform, expected, result), False)
                self.assertEqual(self.snapshot(store), before)

    def test_expected_and_current_must_both_still_require_action(self):
        for side in ("expected", "current"):
            for status in ("account_mismatch", "waiting", "synced", "error", ""):
                with self.subTest(side=side, status=status):
                    expected = self.account()
                    store = _CopyingStore([expected])
                    if side == "expected":
                        expected.browser_status = status
                    else:
                        store.config.cookies[0].browser_status = status
                    before = self.snapshot(store)
                    self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected)), False)
                    self.assertEqual(self.snapshot(store), before)

    def test_atomic_update_rechecks_cookie_profile_platform_status_and_newer_browser_metadata(self):
        changes = (
            ("cookie", "token=same-account; refreshToken=concurrent-refresh; cf_clearance=saved-clearance"),
            ("cookie", "token=another-account"),
            ("browser_profile_id", "replacement-profile"),
            ("platform", "global"),
            ("browser_status", "waiting"),
            ("browser_status", "account_mismatch"),
            ("browser_message", "a newer login failure"),
            ("browser_synced_at", "2026-11-18T11:00:00+08:00"),
        )
        for field, value in changes:
            with self.subTest(field=field, value=value):
                expected = self.account()
                store = _CopyingStore([expected])
                raced = []

                def concurrent_update(config):
                    setattr(config.cookies[0], field, value)
                    raced.append(self.snapshot(store))

                store.before_update = concurrent_update
                self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected)), False)
                self.assertEqual(store.update_calls, 1)
                self.assertEqual(self.snapshot(store), raced[0])

    def test_atomic_update_does_not_recreate_a_concurrently_deleted_account(self):
        expected = self.account()
        store = _CopyingStore([expected])
        store.before_update = lambda config: config.cookies.clear()
        self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected)), False)
        self.assertEqual(store.load().cookies, [])

    def test_second_confirmation_is_a_noop_and_does_not_refresh_the_timestamp(self):
        expected = self.account()
        store = _CopyingStore([expected])
        result = self.fresh(expected)
        self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, result), True)
        confirmed = self.snapshot(store)
        self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, result), False)
        self.assertEqual(self.snapshot(store), confirmed)

    def test_failed_update_never_reports_success_or_retries(self):
        expected = self.account()
        store = _CopyingStore([expected])
        store.update_error = RuntimeError("offline store failure")
        before = self.snapshot(store)
        try:
            result = confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected))
        except RuntimeError as error:
            self.assertEqual(str(error), "offline store failure")
        else:
            self.assertIs(result, False)
        self.assertEqual(self.snapshot(store), before)
        self.assertEqual(store.update_calls, 1)

    def test_helper_leaves_event_publication_to_its_callers(self):
        expected = self.account()
        store = _CopyingStore([expected])
        with patch.object(confirmation, "publish_state_event", create=True) as local_event, \
                patch("app.services.state_events.publish_state_event") as shared_event:
            self.assertIs(confirmation.confirm_browser_session(store, "cn", expected, self.fresh(expected)), True)
        local_event.assert_not_called()
        shared_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
