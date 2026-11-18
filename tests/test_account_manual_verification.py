from __future__ import annotations

import base64
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import account_verification_routes as routes
from app.schemas.models import CookiePair
from app.services import account_manual_verification as manual
from app.services.cloakbrowser_session import CloakBrowserSessionResult


CAPTCHA_ID = "00000000000000000000000000000001"
CHALLENGE_NONCE = "a" * 32
PROOF = {
    "captcha_id": CAPTCHA_ID,
    "lot_number": "00000000000000000000000000000002",
    "pass_token": "fixture-pass-token",
    "gen_time": "1795000000",
    "captcha_output": "fixture-captcha-output",
}
ENDPOINT = "/api/config/online-accounts/cn/verification"


class _Store:
    def __init__(self):
        self.config = SimpleNamespace(
            cookies=[CookiePair(platform="cn", cookie="token=saved", browser_profile_id="profile-cn")],
            user=SimpleNamespace(username="admin"),
            three_mf_limits=SimpleNamespace(cn_daily_limit=100, global_daily_limit=100),
            proxy=SimpleNamespace(),
        )

    def load(self):
        return copy.deepcopy(self.config)

    def update(self, mutator):
        self.config = mutator(self.load())
        return self.load()


class AccountManualVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = _Store()
        self.manager = manual.ManualVerificationManager(self.store, state_dir=Path(self.temp.name))
        self.snapshot = {
            "platform": "cn", "status": "ok", "three_mf_gate": "verification_required",
            "three_mf_reason": "challenge", "three_mf_detail": "verify",
            "model_url": "https://makerworld.com.cn/zh/models/123", "model_id": "123", "instance_id": "456",
        }
        self.target = {key: self.snapshot[key] for key in ("model_url", "model_id", "instance_id")}
        self.preflight = {
            "ready": True, "reason": "ready", "authenticated": True,
            "model": {"model_id": "123", "model_url": self.target["model_url"], "instances": [{"id": "456"}]},
        }
        self.blockers = {"active": False, "manual": False}
        self.pending = []
        self.operation_depth = 0
        self.operation_exits = 0
        self.attempts = []
        self.mocks = {}
        patches = {
            "configured": patch.object(manual.browser, "cloakbrowser_configured", return_value=True),
            "health": patch.object(manual, "get_account_health", side_effect=lambda *_: copy.deepcopy(self.snapshot)),
            "target": patch.object(manual, "_find_target", side_effect=lambda *_: copy.deepcopy(self.target)),
            "blockers": patch.object(manual, "_queue_blockers", side_effect=lambda *_: dict(self.blockers)),
            "operation": patch.object(manual.browser, "_profile_operation", side_effect=self.operation),
            "collect": patch.object(manual.browser, "collect_browser_session", return_value=CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=saved; cf_clearance=fixture-clearance")),
            "preflight": patch.object(manual.browser, "browser_prepare_3mf_download", side_effect=lambda *a, **kw: copy.deepcopy(self.preflight)),
            "fetch": patch.object(manual, "fetch_authorization", return_value=self.challenge_response(), create=True),
            "quota": patch.object(manual, "reserve_three_mf_download_slot", return_value={"allowed": True}),
            "attempt": patch.object(manual, "ThreeMfAuthorizationAttempt", side_effect=self.authorization_attempt),
            "clear": patch.object(manual, "_clear_verified_gate", return_value=True),
            "publish": patch.object(manual, "publish_state_event"),
            "worker": patch("app.api.dependencies.crawler.manager.ensure_worker_for_pending"),
            "thread": patch.object(manual.threading.Thread, "start", autospec=True, side_effect=lambda thread: self.pending.append(thread)),
        }
        for key, item in patches.items():
            self.mocks[key] = item.start()
            self.addCleanup(item.stop)

    def authorization_attempt(self, *args, **kwargs):
        attempt = MagicMock()
        attempt.__enter__.return_value = attempt
        attempt.response = None
        attempt.request_started = False
        self.attempts.append(attempt)
        return attempt

    @contextmanager
    def operation(self, *args, **kwargs):
        self.operation_depth += 1
        try:
            yield
        finally:
            self.operation_depth -= 1
            self.operation_exits += 1

    @staticmethod
    def challenge_response(captcha_id=CAPTCHA_ID):
        return {"status_code": 418, "payload": {"captchaId": captcha_id}, "headers": {}}

    @staticmethod
    def success_response():
        return {"status_code": 200, "payload": {"name": "model.3mf", "url": "https://download.example.test/model.3mf?signature=fixture-signed-url"}, "headers": {}}

    def run_next(self):
        self.assertTrue(self.pending, "expected a queued verification operation")
        thread = self.pending.pop(0)
        thread._target(*thread._args, **thread._kwargs)

    def create_challenge(self):
        result = self.manager.create("cn", "owner-one")
        self.assertEqual(result["status"], "preparing")
        self.run_next()
        result = self.status(result["session_id"])
        self.assertEqual(result["status"], "challenge", result)
        return result

    def status(self, session_id):
        return self.manager.execute("cn", session_id, "owner-one", "status")

    def complete(self, result, *, proof=None, challenge_id=None):
        return self.manager.execute(
            "cn", result["session_id"], "owner-one", "complete",
            challenge_id=result["challenge"]["challenge_id"] if challenge_id is None else challenge_id,
            proof=copy.deepcopy(PROOF) if proof is None else proof,
        )

    def test_create_is_deduplicated_for_owner_and_rejects_other_owner(self):
        first = self.manager.create("cn", "owner-one")
        second = self.manager.create("cn", "owner-one")
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(len(self.pending), 1)
        with self.assertRaises(manual.ManualVerificationError):
            self.manager.create("cn", "owner-two")
        self.mocks["fetch"].assert_not_called()

    def test_public_challenge_has_only_widget_config_and_releases_browser_and_pacing(self):
        result = self.create_challenge()
        self.assertEqual(set(result), {"session_id", "platform", "status", "message", "expires_at", "completed", "challenge"})
        self.assertEqual(result["challenge"]["captcha_id"], CAPTCHA_ID)
        self.assertTrue(result["challenge"]["challenge_id"])
        self.assertFalse(result["completed"])
        self.assertEqual(self.operation_depth, 0)
        self.assertGreater(self.operation_exits, 0)
        self.attempts[0].__exit__.assert_called_once()
        serialized = json.dumps(result)
        for private in ("saved", "fixture-clearance", "cdp", "fixture-signed-url", "fixture-pass-token", "image", "width", "height"):
            self.assertNotIn(private, serialized)
        self.mocks["clear"].assert_not_called()
        self.assertEqual(self.mocks["quota"].call_count, 1)
        self.assertTrue(self.mocks["attempt"].call_args.kwargs["verification_probe"])
        self.assertFalse(self.mocks["attempt"].call_args.kwargs["update_gate"])

    def test_status_does_not_start_operations_or_consume_more_quota(self):
        result = self.create_challenge()
        for _ in range(3):
            self.assertEqual(self.status(result["session_id"])["challenge"], result["challenge"])
        self.assertFalse(self.pending)
        self.assertEqual(self.mocks["fetch"].call_count, 1)
        self.assertEqual(self.mocks["quota"].call_count, 1)

    def test_valid_proof_resumes_exact_target_once_and_does_not_reserve_again(self):
        result = self.create_challenge()
        self.mocks["fetch"].return_value = self.success_response()
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            self.assertEqual(self.complete(result)["status"], "verifying")
            self.assertEqual(self.complete(result)["status"], "verifying")
            self.assertEqual(len(self.pending), 1)
            self.run_next()
            finished = self.status(result["session_id"])
            self.assertEqual(finished["status"], "completed", finished)
            self.assertTrue(finished["completed"])
            self.assertIsNone(finished["challenge"])
            self.complete(result)
            self.assertFalse(self.pending)
            sync.assert_called_once()
        self.assertEqual(self.mocks["fetch"].call_count, 2)
        self.assertEqual(self.mocks["quota"].call_count, 1)
        args, kwargs = self.mocks["fetch"].call_args
        self.assertEqual(args, ("cn", "profile-cn", self.target))
        self.assertEqual(json.loads(base64.b64decode(kwargs["proof_header"])), PROOF)
        self.mocks["clear"].assert_called_once_with("cn", self.snapshot, self.target)
        self.assertNotIn("fixture-signed-url", json.dumps(finished))
        self.assertNotIn("fixture-captcha-output", json.dumps(finished))
        self.manager.create("cn", "owner-two")

    def test_initial_real_authorization_can_complete_without_challenge(self):
        self.mocks["fetch"].return_value = self.success_response()
        with patch.object(self.manager, "_sync_cookie", return_value=True):
            result = self.manager.create("cn", "owner-one")
            self.run_next()
            result = self.status(result["session_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertIsNone(result["challenge"])
        self.mocks["clear"].assert_called_once()
        self.assertEqual(self.mocks["fetch"].call_count, 1)

    def test_success_syncs_same_account_cookie_only_after_download_permission(self):
        result = self.create_challenge()
        self.assertEqual(self.store.load().cookies[0].cookie, "token=saved")
        self.mocks["fetch"].return_value = self.success_response()
        self.complete(result)
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "completed")
        account = self.store.load().cookies[0]
        self.assertEqual(account.cookie, "token=saved; cf_clearance=fixture-clearance")
        self.assertEqual(account.browser_status, "synced")
        self.assertTrue(account.browser_synced_at)
        self.mocks["worker"].assert_called_once_with(force=True)

    def test_fresh_global_login_clears_stale_error_while_challenge_stays_pending(self):
        self.store.config.cookies = [CookiePair(
            platform="global", cookie="token=saved", browser_profile_id="profile-global",
            browser_status="action_required", browser_message="old login failure",
        )]
        self.snapshot.update(platform="global", model_url="https://makerworld.com/en/models/123")
        self.target["model_url"] = self.snapshot["model_url"]
        self.preflight["model"]["model_url"] = self.snapshot["model_url"]
        self.mocks["collect"].return_value = CloakBrowserSessionResult(
            profile_id="profile-global", cookie="token=saved; cf_clearance=fresh",
            current_url="https://makerworld.com/en",
        )
        health_before = copy.deepcopy(self.snapshot)

        result = self.manager.create("global", "owner-one")
        self.run_next()
        result = self.manager.execute("global", result["session_id"], "owner-one", "status")

        self.assertEqual(result["status"], "challenge", result)
        account = self.store.load().cookies[0]
        self.assertEqual(account.browser_status, "synced")
        self.assertTrue(account.browser_synced_at)
        self.assertNotEqual(account.browser_message, "old login failure")
        self.assertEqual(account.cookie, "token=saved")
        self.assertEqual(self.snapshot, health_before)
        self.mocks["clear"].assert_not_called()
        self.mocks["worker"].assert_not_called()

        self.manager.execute(
            "global", result["session_id"], "owner-one", "complete",
            challenge_id=result["challenge"]["challenge_id"], proof=copy.deepcopy(PROOF),
        )
        self.run_next()
        continued = self.manager.execute("global", result["session_id"], "owner-one", "status")
        self.assertEqual(continued["status"], "challenge", continued)
        self.assertEqual(self.mocks["quota"].call_count, 1)
        self.assertEqual(self.snapshot, health_before)
        self.mocks["clear"].assert_not_called()
        self.mocks["worker"].assert_not_called()

    def test_failed_proof_rotates_nonce_and_does_not_reserve_again(self):
        result = self.create_challenge()
        self.complete(result)
        self.run_next()
        refreshed = self.status(result["session_id"])
        self.assertEqual(refreshed["status"], "challenge", refreshed)
        self.assertNotEqual(refreshed["challenge"]["challenge_id"], result["challenge"]["challenge_id"])
        with self.assertRaises(manual.ManualVerificationError):
            self.complete(result)
        self.assertFalse(self.pending)
        self.assertEqual(self.mocks["fetch"].call_count, 2)
        self.assertEqual(self.mocks["quota"].call_count, 1)
        self.mocks["clear"].assert_not_called()

    def test_unknown_nonce_or_invalid_proof_never_dispatches(self):
        result = self.create_challenge()
        for proof in ({}, {**PROOF, "captcha_id": "00000000000000000000000000000009"},
                      {**PROOF, "gen_time": "not-a-time"}, {**PROOF, "captcha_output": ""},
                      {**PROOF, "unexpected": "not-permitted"}):
            with self.subTest(proof=proof):
                with self.assertRaises(manual.ManualVerificationError):
                    self.complete(result, proof=proof)
        with self.assertRaises(manual.ManualVerificationError):
            self.complete(result, challenge_id="unknown-challenge")
        self.assertFalse(self.pending)
        self.assertEqual(self.mocks["fetch"].call_count, 1)
        self.mocks["clear"].assert_not_called()

    def test_owner_and_platform_cannot_access_another_session(self):
        result = self.create_challenge()
        for platform, owner in (("cn", "other-owner"), ("global", "owner-one")):
            for action in ("status", "complete", "close"):
                with self.subTest(platform=platform, owner=owner, action=action):
                    with self.assertRaises(manual.ManualVerificationError) as raised:
                        self.manager.execute(platform, result["session_id"], owner, action)
                    self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.status(result["session_id"])["status"], "challenge")

    def test_cancel_before_preparation_prevents_browser_and_download_requests(self):
        result = self.manager.create("cn", "owner-one")
        cancelled = self.manager.execute("cn", result["session_id"], "owner-one", "close")
        self.assertEqual(cancelled["status"], "cancelled")
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "cancelled")
        self.mocks["collect"].assert_not_called()
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()
        self.manager.create("cn", "owner-two")

    def test_cancel_after_challenge_releases_lease_and_cannot_be_completed(self):
        result = self.create_challenge()
        closed = self.manager.execute("cn", result["session_id"], "owner-one", "close")
        self.assertEqual(closed["status"], "cancelled")
        self.assertIsNone(closed["challenge"])
        self.assertEqual(self.complete(result)["status"], "cancelled")
        self.assertFalse(self.pending)
        self.manager.create("cn", "owner-two")
        self.assertEqual(self.mocks["fetch"].call_count, 1)

    def test_cancel_during_authorization_cannot_clear_gate_after_late_success(self):
        result = self.create_challenge()
        def success_after_cancel(*args, **kwargs):
            self.manager.execute("cn", result["session_id"], "owner-one", "close")
            return self.success_response()
        self.mocks["fetch"].side_effect = success_after_cancel
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            self.complete(result)
            self.run_next()
            sync.assert_not_called()
        self.assertEqual(self.status(result["session_id"])["status"], "cancelled")
        self.mocks["clear"].assert_not_called()

    def test_expiration_blocks_proof_and_releases_manual_lease(self):
        result = self.create_challenge()
        self.manager.sessions[result["session_id"]].deadline = time.monotonic() - 1
        expired = self.complete(result)
        self.assertEqual(expired["status"], "expired")
        self.assertIsNone(expired["challenge"])
        self.assertFalse(self.pending)
        self.manager.create("cn", "owner-two")
        self.assertEqual(self.mocks["fetch"].call_count, 1)

    def test_background_expiry_releases_lease_without_browser_or_waiting_thread(self):
        result = self.create_challenge()
        self.manager.sessions[result["session_id"]].deadline = time.monotonic() - 1
        previous_fetches = self.mocks["fetch"].call_count
        previous_threads = self.mocks["thread"].call_count
        self.manager.prune_expired()
        self.assertEqual(self.mocks["fetch"].call_count, previous_fetches)
        self.assertEqual(self.mocks["thread"].call_count, previous_threads)
        self.assertFalse(self.pending)
        other = manual.ManualVerificationManager(self.store, state_dir=Path(self.temp.name))
        self.assertEqual(other.create("cn", "owner-two")["status"], "preparing")
        self.run_next()
        other.shutdown()

    def test_cross_process_lease_prevents_other_manager_from_opening_session(self):
        result = self.create_challenge()
        other = manual.ManualVerificationManager(self.store, state_dir=Path(self.temp.name))
        with self.assertRaises(manual.ManualVerificationError):
            other.create("cn", "owner-two")
        self.manager.execute("cn", result["session_id"], "owner-one", "close")
        self.assertEqual(other.create("cn", "owner-two")["status"], "preparing")
        other.shutdown()

    def test_active_archive_or_manual_pause_prevents_creation(self):
        for blocker in ("active", "manual"):
            with self.subTest(blocker=blocker):
                self.blockers[blocker] = True
                with self.assertRaises(manual.ManualVerificationError):
                    self.manager.create("cn", "owner-one")
                self.blockers[blocker] = False
        self.assertFalse(self.pending)
        self.mocks["operation"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_open_or_missing_gate_rejects_probe_before_browser_or_quota(self):
        for gate in ("open", ""):
            with self.subTest(gate=gate):
                self.snapshot["three_mf_gate"] = gate
                with self.assertRaises(manual.ManualVerificationError):
                    self.manager.create("cn", "owner-one")
        self.assertFalse(self.pending)
        self.mocks["operation"].assert_not_called()
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_new_manual_pause_or_active_archive_blocks_continuation(self):
        for blocker in ("active", "manual"):
            with self.subTest(blocker=blocker):
                result = self.create_challenge()
                before = self.mocks["fetch"].call_count
                self.blockers[blocker] = True
                try:
                    try:
                        self.complete(result)
                    except manual.ManualVerificationError:
                        pass
                    while self.pending:
                        self.run_next()
                    self.assertEqual(self.mocks["fetch"].call_count, before)
                    self.mocks["clear"].assert_not_called()
                finally:
                    self.blockers[blocker] = False
                    self.manager.execute("cn", result["session_id"], "owner-one", "close")

    def test_changed_account_prevents_initial_authorization(self):
        result = self.manager.create("cn", "owner-one")
        self.store.config.cookies[0].cookie = "token=different-account"
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_browser_account_mismatch_prevents_authorization(self):
        self.mocks["collect"].return_value = CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=different-account")
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_browser_profile_mismatch_with_same_token_preserves_stale_error(self):
        self.store.config.cookies[0].browser_status = "action_required"
        self.store.config.cookies[0].browser_message = "old login failure"
        self.mocks["collect"].return_value = CloakBrowserSessionResult(
            profile_id="another-profile", cookie="token=saved",
        )
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.assertEqual(self.store.load().cookies[0].browser_status, "action_required")
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_changed_account_blocks_existing_challenge(self):
        result = self.create_challenge()
        self.store.config.cookies[0].cookie = "token=different-account"
        try:
            self.complete(result)
        except manual.ManualVerificationError:
            pass
        while self.pending:
            self.run_next()
        self.assertEqual(self.mocks["fetch"].call_count, 1)
        self.mocks["clear"].assert_not_called()

    def test_browser_account_changed_during_fetch_cannot_unlock(self):
        self.mocks["collect"].side_effect = [
            CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=saved"),
            CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=saved"),
            CloakBrowserSessionResult(profile_id="profile-cn", cookie="token=different-account"),
        ]
        self.mocks["fetch"].return_value = self.success_response()
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            result = self.manager.create("cn", "owner-one")
            self.run_next()
            sync.assert_not_called()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["clear"].assert_not_called()

    def test_gate_or_pause_changed_during_request_preserves_current_state(self):
        result = self.create_challenge()
        def success_after_gate_change(*args, **kwargs):
            self.snapshot["three_mf_gate"] = "download_limited"
            self.blockers["manual"] = True
            return self.success_response()
        self.mocks["fetch"].side_effect = success_after_gate_change
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            self.complete(result)
            self.run_next()
            sync.assert_not_called()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["clear"].assert_not_called()

    def test_expiration_during_request_rejects_late_success(self):
        result = self.create_challenge()
        def success_after_expiry(*args, **kwargs):
            self.manager.sessions[result["session_id"]].deadline = time.monotonic() - 1
            return self.success_response()
        self.mocks["fetch"].side_effect = success_after_expiry
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            self.complete(result)
            self.run_next()
            sync.assert_not_called()
        self.assertEqual(self.status(result["session_id"])["status"], "expired")
        self.mocks["clear"].assert_not_called()

    def test_quota_denial_does_not_send_authorization_request(self):
        self.mocks["quota"].return_value = {"allowed": False}
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["fetch"].assert_not_called()
        self.mocks["clear"].assert_not_called()

    def test_browser_slot_timeout_is_retryable_and_does_not_consume_quota(self):
        self.mocks["operation"].side_effect = TimeoutError
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        result = self.status(result["session_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("浏览器仍忙", result["message"])
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()
        self.manager.create("cn", "owner-two")

    def test_changed_instance_is_not_silently_authorized(self):
        self.preflight["model"]["instances"] = [{"id": "789"}]
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["fetch"].assert_not_called()
        self.mocks["quota"].assert_not_called()

    def test_untrusted_challenge_servers_fail_without_exposing_external_config(self):
        self.mocks["fetch"].return_value = {"status_code": 418, "payload": {
            "captchaId": CAPTCHA_ID, "apiServers": ["untrusted.example.test"],
        }}
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        result = self.status(result["session_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["challenge"])
        self.assertNotIn("untrusted.example", json.dumps(result))
        self.mocks["clear"].assert_not_called()

    def test_unready_or_unauthenticated_preflight_does_not_consume_quota(self):
        for preflight in ({"ready": False, "authenticated": True, "reason": "page_verification_required"},
                          {"ready": False, "authenticated": False, "reason": "auth_required"},
                          {**self.preflight, "model": {"model_id": "999", "model_url": self.target["model_url"], "instances": [{"id": "456"}]}}):
            with self.subTest(preflight=preflight):
                self.preflight = preflight
                result = self.manager.create("cn", "owner-one")
                self.run_next()
                self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["quota"].assert_not_called()
        self.mocks["fetch"].assert_not_called()

    def test_unsigned_or_missing_download_cannot_unlock(self):
        result = self.create_challenge()
        self.mocks["fetch"].return_value = {"status_code": 200, "payload": {"url": self.target["model_url"]}}
        with patch.object(self.manager, "_sync_cookie", return_value=True) as sync:
            self.complete(result)
            self.run_next()
            sync.assert_not_called()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["clear"].assert_not_called()

    def test_cookie_sync_failure_preserves_gate(self):
        result = self.create_challenge()
        self.mocks["fetch"].return_value = self.success_response()
        with patch.object(self.manager, "_sync_cookie", return_value=False):
            self.complete(result)
            self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")
        self.mocks["clear"].assert_not_called()

    def test_gate_compare_and_swap_failure_cannot_claim_completed(self):
        result = self.create_challenge()
        self.mocks["fetch"].return_value = self.success_response()
        self.mocks["clear"].return_value = False
        with patch.object(self.manager, "_sync_cookie", return_value=True):
            self.complete(result)
            self.run_next()
        self.assertEqual(self.status(result["session_id"])["status"], "failed")

    def test_backend_exception_is_redacted_and_does_not_leave_session_busy(self):
        self.mocks["fetch"].side_effect = RuntimeError("private-token and private-upstream-url")
        result = self.manager.create("cn", "owner-one")
        self.run_next()
        result = self.status(result["session_id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(self.operation_depth, 0)
        self.manager.create("cn", "owner-two")


class AccountManualVerificationRoutesTest(unittest.TestCase):
    def setUp(self):
        self.store = _Store()

    def client(self):
        app = FastAPI()
        app.include_router(routes.router)

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.auth_identity = {"kind": "session", "username": "admin", "session_id": "private-owner"}
            return await call_next(request)

        return TestClient(app)

    def test_http_complete_preserves_bound_nonce_and_proof(self):
        with patch.object(routes, "store", self.store), \
                patch.object(routes.manual_verification_manager, "execute", return_value={"status": "verifying"}) as execute:
            response = self.client().post(f"{ENDPOINT}/session-one/complete", json={"challenge_id": CHALLENGE_NONCE, "proof": PROOF})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(execute.call_args.args, ("cn", "session-one", "private-owner", "complete"))
            self.assertEqual(execute.call_args.kwargs, {"challenge_id": CHALLENGE_NONCE, "proof": PROOF})

    def test_http_status_is_read_only_get_and_close_is_delete(self):
        with patch.object(routes, "store", self.store), \
                patch.object(routes.manual_verification_manager, "execute", return_value={"status": "challenge"}) as execute:
            client = self.client()
            self.assertEqual(client.get(f"{ENDPOINT}/session-one").status_code, 200)
            self.assertEqual(execute.call_args.args, ("cn", "session-one", "private-owner", "status"))
            self.assertEqual(client.delete(f"{ENDPOINT}/session-one").status_code, 200)
            self.assertEqual(execute.call_args.args, ("cn", "session-one", "private-owner", "close"))

    def test_old_remote_browser_input_frame_and_finish_endpoints_are_removed(self):
        with patch.object(routes, "store", self.store), patch.object(routes.manual_verification_manager, "execute") as execute:
            client = self.client()
            for action in ("input", "frame", "finish"):
                with self.subTest(action=action):
                    self.assertEqual(client.post(f"{ENDPOINT}/session-one/{action}", json={"events": []}).status_code, 404)
            execute.assert_not_called()

    def test_http_complete_rejects_unexpected_fields_and_oversized_values(self):
        invalid = [
            {}, {"challenge_id": "", "proof": PROOF}, {"challenge_id": "x" * 1000, "proof": PROOF},
            {"challenge_id": CHALLENGE_NONCE, "proof": PROOF, "script_url": "https://untrusted.example/script.js"},
            {"challenge_id": CHALLENGE_NONCE, "proof": {**PROOF, "unexpected": "field"}},
            {"challenge_id": CHALLENGE_NONCE, "proof": {**PROOF, "captcha_output": "x" * 100000}},
        ]
        with patch.object(routes, "store", self.store), patch.object(routes.manual_verification_manager, "execute") as execute:
            client = self.client()
            for payload in invalid:
                with self.subTest(keys=list(payload)):
                    self.assertEqual(client.post(f"{ENDPOINT}/session-one/complete", json=payload).status_code, 422)
            execute.assert_not_called()

    def test_routes_require_admin_session_and_reject_api_token(self):
        with patch.object(routes, "store", self.store):
            for identity in ({"kind": "token"}, {"kind": "session", "username": "guest", "session_id": "private"},
                             {"kind": "session", "username": "admin"}):
                request = SimpleNamespace(state=SimpleNamespace(auth_identity=identity))
                with self.assertRaises(HTTPException) as raised:
                    routes._owner(request)
                self.assertEqual(raised.exception.status_code, 403)
            request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin", "session_id": "private"}))
            self.assertEqual(routes._owner(request), "private")

    def test_http_manager_error_keeps_its_public_status_code(self):
        with patch.object(routes, "store", self.store), \
                patch.object(routes.manual_verification_manager, "execute", side_effect=manual.ManualVerificationError("验证码已失效。", 409)):
            response = self.client().get(f"{ENDPOINT}/session-one")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "验证码已失效。"})

    def test_registered_mutations_require_same_origin_and_reject_api_tokens(self):
        from app import main as main_app

        client = TestClient(main_app.app)
        identity = {"kind": "session", "username": "admin", "session_id": "private-owner"}
        with patch.object(routes, "store", self.store), \
                patch.object(main_app.auth_manager, "resolve_request_auth", return_value=identity), \
                patch.object(routes.manual_verification_manager, "create", return_value={"status": "preparing"}) as create, \
                patch.object(routes.manual_verification_manager, "execute", return_value={"status": "verifying"}) as execute:
            for method, endpoint, body in (("post", ENDPOINT, None),
                                            ("post", f"{ENDPOINT}/session-one/complete", {"challenge_id": CHALLENGE_NONCE, "proof": PROOF}),
                                            ("delete", f"{ENDPOINT}/session-one", None)):
                with self.subTest(method=method, endpoint=endpoint):
                    options = {"json": body} if body is not None else {}
                    self.assertEqual(client.request(method, endpoint, **options).status_code, 403)
                    self.assertEqual(client.request(method, endpoint, headers={"Origin": "https://other.test"}, **options).status_code, 403)
                    self.assertEqual(client.request(method, endpoint, headers={"Origin": "http://testserver"}, **options).status_code, 200)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(execute.call_count, 2)
        with patch.object(routes, "store", self.store), \
                patch.object(main_app.auth_manager, "resolve_request_auth", return_value={"kind": "token", "username": ""}), \
                patch.object(routes.manual_verification_manager, "execute") as execute:
            response = client.post(f"{ENDPOINT}/session-one/complete", headers={"Origin": "http://testserver"}, json={"challenge_id": CHALLENGE_NONCE, "proof": PROOF})
            self.assertEqual(response.status_code, 403)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
