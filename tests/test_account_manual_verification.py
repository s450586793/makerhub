from __future__ import annotations

import base64
from concurrent.futures import Future
from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import account_verification_routes as routes
from app.schemas.models import CookiePair
from app.services import account_manual_verification as manual


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

    def update(self, update):
        self.config = update(self.load())
        return self.load()


class AccountManualVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = _Store()
        self.manager = manual.ManualVerificationManager(self.store, state_dir=Path(self.temp.name))
        self.snapshot = {
            "status": "ok", "three_mf_gate": "verification_required", "three_mf_reason": "challenge",
            "three_mf_detail": "verify", "model_url": "https://makerworld.com.cn/zh/models/123",
            "model_id": "123", "instance_id": "456",
        }
        self.target = {key: self.snapshot[key] for key in ("model_url", "model_id", "instance_id")}

    def session(self, *, target=True):
        session = manual._Session(
            "session-one", "cn", self.manager._owner("owner-one"), self.store.load().cookies[0],
            copy.deepcopy(self.snapshot), copy.deepcopy(self.target) if target else {}, "future",
            time.monotonic() + 60, lease=Mock(),
        )
        self.manager.sessions[session.session_id] = session
        return session

    def browser_snapshot(self, token="saved"):
        return {"authenticated": True, "current_url": self.target["model_url"],
                "cookies": [{"name": "token", "value": token, "domain": "makerworld.com.cn", "path": "/", "expires": -1},
                            {"name": "cf_clearance", "value": "private-clearance", "domain": "makerworld.com.cn", "path": "/", "expires": -1}]}

    def test_public_response_never_contains_credentials_or_internal_browser_state(self):
        session = self.session()
        image = base64.b64encode(b"\xff\xd8\xfftest").decode()
        self.manager._safe_frame(session, {"status": "challenge", "image": image, "width": 800, "height": 600,
                                          "cookie": "secret", "auth_token": "secret", "snapshot": self.browser_snapshot(),
                                          "cdp_url": "private", "message": "secret"})
        payload = session.public()
        self.assertEqual(set(payload), {"session_id", "platform", "status", "message", "image", "width", "height", "expires_at", "completed"})
        self.assertNotIn("secret", str(payload))
        self.assertEqual(payload["image"], image)

    def test_image_requires_valid_jpeg_and_bounded_geometry(self):
        session = self.session()
        for image, width in (("not-base64", 800), (base64.b64encode(b"not-jpeg").decode(), 800),
                             (base64.b64encode(b"\xff\xd8\xff").decode(), 99999)):
            self.manager._safe_frame(session, {"image": image, "width": width, "height": 600})
            self.assertEqual(session.image, "")

    def test_cancel_while_frame_is_decoding_cannot_be_overwritten_by_late_browser_status(self):
        session = self.session()
        decoding, release = threading.Event(), threading.Event()
        decode = base64.b64decode
        finished = Future()

        def blocked_decode(*args, **kwargs):
            decoding.set()
            self.assertTrue(release.wait(timeout=2))
            return decode(*args, **kwargs)

        def receive_frame():
            try:
                self.manager._safe_frame(session, {"status": "authorizing", "image": base64.b64encode(b"\xff\xd8\xfftest").decode(),
                                                  "width": 800, "height": 600})
                finished.set_result(True)
            except Exception as exc:
                finished.set_exception(exc)

        with patch.object(manual.base64, "b64decode", side_effect=blocked_decode):
            thread = threading.Thread(target=receive_frame)
            thread.start()
            try:
                self.assertTrue(decoding.wait(timeout=2))
                self.assertEqual(self.manager.execute("cn", session.session_id, "owner-one", "close")["status"], "cancelled")
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertTrue(finished.result(timeout=1))
            self.assertEqual(session.status, "cancelled")
            self.assertEqual(session.message, "验证窗口已关闭。")
            self.assertEqual(session.image, "")

    def test_owner_and_platform_cannot_access_another_session(self):
        self.session()
        for platform, owner in (("cn", "other-owner"), ("global", "owner-one")):
            with self.assertRaises(manual.ManualVerificationError) as raised:
                self.manager.execute(platform, "session-one", owner, "frame")
            self.assertEqual(raised.exception.status_code, 404)

    def test_create_is_deduplicated_for_owner_and_rejects_other_owner(self):
        with patch.object(manual.browser, "cloakbrowser_configured", return_value=True), \
                patch.object(manual, "get_account_health", return_value=self.snapshot), \
                patch.object(manual, "_find_target", return_value=self.target), \
                patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(manual.threading.Thread, "start"):
            first = self.manager.create("cn", "owner-one")
            second = self.manager.create("cn", "owner-one")
            self.assertEqual(first["session_id"], second["session_id"])
            with self.assertRaises(manual.ManualVerificationError):
                self.manager.create("cn", "owner-two")
            self.manager.execute("cn", first["session_id"], "owner-one", "close")

    def test_active_archive_rejects_creation_before_acquiring_browser_slot(self):
        with patch.object(manual.browser, "cloakbrowser_configured", return_value=True), \
                patch.object(manual, "_queue_blockers", return_value={"active": True}), \
                patch.object(manual.browser, "_profile_operation") as operation, \
                patch.object(manual.threading.Thread, "start") as start:
            with self.assertRaises(manual.ManualVerificationError) as raised:
                self.manager.create("cn", "owner-one")
            self.assertIn("归档任务正在运行", str(raised.exception))
            operation.assert_not_called()
            start.assert_not_called()

    def test_cancel_before_browser_slot_releases_lease_and_allows_reopening(self):
        session = self.session()
        result = self.manager.execute("cn", session.session_id, "owner-one", "close")
        self.assertEqual(result["status"], "cancelled")
        session.lease.close.assert_called_once()
        self.assertTrue(session.done.is_set())
        self.assertEqual(self.manager.execute("cn", session.session_id, "owner-one", "finish")["status"], "cancelled")

    def test_expired_session_cannot_receive_input(self):
        session = self.session()
        session.deadline = time.monotonic() - 1
        result = self.manager.execute("cn", session.session_id, "owner-one", "input", events=[{"type": "text", "text": "ignored"}])
        self.assertEqual(result["status"], "expired")
        self.assertTrue(session.commands.empty())
        self.assertTrue(session.done.is_set())

    def test_cross_process_manual_lease_cannot_be_stolen(self):
        first = self.manager._lease()
        try:
            other = manual.ManualVerificationManager(self.store, state_dir=Path(self.temp.name))
            with self.assertRaises(manual.ManualVerificationError):
                other._lease()
        finally:
            first.close()
        self.manager._lease().close()

    def test_dispatch_checks_pause_and_quota_before_authorizing(self):
        session = self.session()
        session.bridge = Mock()
        output = {"status": "ready", "preflight": {}}
        attempt = MagicMock(response=None)
        with patch.object(manual, "_fresh_target", return_value=self.target), \
                patch.object(manual, "get_account_health", return_value=self.snapshot), \
                patch.object(manual, "_queue_blockers", return_value={"manual": True}), \
                patch.object(manual, "reserve_three_mf_download_slot") as quota:
            self.assertIsNone(self.manager._dispatch(session, output))
            quota.assert_not_called()
            session.bridge.command.assert_not_called()
        with patch.object(manual, "_fresh_target", return_value=self.target), \
                patch.object(manual, "get_account_health", return_value=self.snapshot), \
                patch.object(manual, "_queue_blockers", return_value={"active": False, "manual": False}), \
                patch.object(manual, "ThreeMfAuthorizationAttempt", return_value=attempt), \
                patch.object(manual, "reserve_three_mf_download_slot", return_value={"allowed": False}):
            self.assertIsNone(self.manager._dispatch(session, output))
            session.bridge.command.assert_not_called()
            self.assertFalse(session.dispatched)
            self.assertFalse(attempt.request_started)
            self.manager._safe_frame(session, {"status": "ready"})
            self.assertIn("今日", session.message)

    def test_dispatch_reserves_once_and_does_not_accept_changed_instance(self):
        session = self.session()
        session.bridge = Mock()
        session.bridge.command.return_value = {"status": "authorizing"}
        attempt = MagicMock(response=None)
        with patch.object(manual, "_fresh_target", return_value=self.target), \
                patch.object(manual, "get_account_health", return_value=self.snapshot), \
                patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(manual, "ThreeMfAuthorizationAttempt", return_value=attempt), \
                patch.object(manual, "reserve_three_mf_download_slot", return_value={"allowed": True}) as quota:
            self.assertIs(self.manager._dispatch(session, {"status": "ready", "preflight": {}}), attempt)
            self.assertIsNone(self.manager._dispatch(session, {"status": "ready", "preflight": {}}))
            self.assertEqual(quota.call_count, 1)
            session.bridge.command.assert_called_once_with("authorize")
        other = self.session()
        with patch.object(manual, "_fresh_target", return_value={**self.target, "instance_id": "999"}):
            self.assertIsNone(self.manager._dispatch(other, {"status": "ready"}))

    def test_authorization_requires_valid_response_same_account_and_successful_sync(self):
        for response, snapshot, sync in (
            ({"status_code": 418, "payload": {}}, self.browser_snapshot(), True),
            ({"status_code": 200, "payload": {}}, self.browser_snapshot(), True),
            ({"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}, self.browser_snapshot("other-account"), True),
            ({"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}, self.browser_snapshot(), False),
        ):
            with self.subTest(response=response, sync=sync):
                session = self.session()
                session.dispatched = True
                with patch.object(manual, "_queue_blockers", return_value={}), \
                        patch.object(self.manager, "_sync_snapshot", return_value=sync), \
                        patch.object(manual, "_clear_verified_gate") as clear:
                    with self.assertRaises(manual.ManualVerificationError):
                        self.manager._complete_authorization(session, {"status": "authorized", "authorization": response, "snapshot": snapshot}, Mock())
                    clear.assert_not_called()

    def test_cancelled_or_expired_session_cannot_reopen_gate_from_late_success(self):
        for expired in (False, True):
            session = self.session()
            session.dispatched = True
            if expired:
                session.deadline = time.monotonic() - 1
            else:
                session.stopped.set()
            with patch.object(manual, "_clear_verified_gate") as clear:
                self.assertFalse(self.manager._complete_authorization(
                    session, {"status": "authorized", "snapshot": self.browser_snapshot(),
                              "authorization": {"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}}, Mock(),
                ))
                clear.assert_not_called()

    def test_cancel_during_snapshot_sync_wins_for_download_and_page_only_sessions(self):
        for has_target in (True, False):
            with self.subTest(has_target=has_target):
                session = self.session(target=has_target)
                session.dispatched = has_target
                syncing, release = threading.Event(), threading.Event()
                result = Future()

                def sync(*args, **kwargs):
                    syncing.set()
                    self.assertTrue(release.wait(timeout=2))
                    return True

                output = {"status": "authorized" if has_target else "page_ready", "snapshot": self.browser_snapshot(),
                          "authorization": {"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}}

                def complete():
                    try:
                        value = (self.manager._complete_authorization(session, output, Mock()) if has_target
                                 else self.manager._finish_page(session, output))
                        result.set_result(value)
                    except Exception as exc:
                        result.set_exception(exc)

                with patch.object(manual, "_queue_blockers", return_value={}), \
                        patch.object(self.manager, "_sync_snapshot", side_effect=sync), \
                        patch.object(manual, "_clear_verified_gate") as clear, \
                        patch("app.api.dependencies.crawler.manager.ensure_worker_for_pending") as resume:
                    thread = threading.Thread(target=complete)
                    thread.start()
                    try:
                        self.assertTrue(syncing.wait(timeout=2))
                        self.assertEqual(self.manager.execute("cn", session.session_id, "owner-one", "close")["status"], "cancelled")
                    finally:
                        release.set()
                        thread.join(timeout=2)
                    self.assertFalse(result.result(timeout=1))
                    self.assertEqual(session.status, "cancelled")
                    clear.assert_not_called()
                    resume.assert_not_called()

    def test_authorization_commit_serializes_cancel_until_gate_and_completion_are_saved(self):
        session = self.session()
        session.dispatched = True
        session.entered.set()
        session.done.set()
        clearing, release, cancelling, cancelled = (threading.Event() for _ in range(4))
        result, cancel_result = Future(), Future()

        def clear(*args):
            clearing.set()
            self.assertTrue(release.wait(timeout=2))
            return True

        def complete():
            try:
                result.set_result(self.manager._complete_authorization(
                    session, {"status": "authorized", "snapshot": self.browser_snapshot(),
                              "authorization": {"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}}, Mock(),
                ))
            except Exception as exc:
                result.set_exception(exc)

        def cancel():
            cancelling.set()
            try:
                cancel_result.set_result(self.manager.execute("cn", session.session_id, "owner-one", "close"))
            except Exception as exc:
                cancel_result.set_exception(exc)
            finally:
                cancelled.set()

        with patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(self.manager, "_sync_snapshot", return_value=True), \
                patch.object(manual, "_clear_verified_gate", side_effect=clear), \
                patch("app.api.dependencies.crawler.manager.ensure_worker_for_pending") as resume:
            completion_thread = threading.Thread(target=complete)
            cancel_thread = threading.Thread(target=cancel)
            completion_thread.start()
            try:
                self.assertTrue(clearing.wait(timeout=2))
                cancel_thread.start()
                self.assertTrue(cancelling.wait(timeout=2))
                self.assertFalse(cancelled.wait(timeout=0.05))
            finally:
                release.set()
                completion_thread.join(timeout=2)
                if cancel_thread.ident is not None:
                    cancel_thread.join(timeout=2)
            self.assertTrue(result.result(timeout=1))
            self.assertEqual(cancel_result.result(timeout=1)["status"], "completed")
            self.assertFalse(session.stopped.is_set())
            resume.assert_called_once_with(force=True)

    def test_true_authorization_syncs_cookie_before_opening_gate_and_resuming(self):
        session = self.session()
        session.dispatched = True
        order = []
        output = {"status": "authorized", "authorization": {"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}},
                  "snapshot": self.browser_snapshot()}
        with patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(manual, "publish_state_event"), \
                patch.object(manual, "_clear_verified_gate", side_effect=lambda *args: order.append("clear") or True), \
                patch("app.api.dependencies.crawler.manager.ensure_worker_for_pending", side_effect=lambda **kwargs: order.append("resume")):
            self.assertTrue(self.manager._complete_authorization(session, output, Mock()))
        self.assertIn("cf_clearance=private-clearance", self.store.config.cookies[0].cookie)
        self.assertTrue(session.completed)
        self.assertEqual(order, ["clear", "resume"])
        self.assertEqual(self.manager.execute("cn", session.session_id, "owner-one", "finish")["status"], "completed")

    def test_worker_closes_only_own_bridge_and_pacing_before_releasing_profile_lock(self):
        session = self.session(target=False)
        future = Future()
        session.commands.put(("finish", [], future))
        events = []

        @contextmanager
        def operation(*args, **kwargs):
            events.append("locked")
            try:
                yield
            finally:
                events.append("unlocked")

        bridge = Mock()
        bridge.command.return_value = {"status": "page_ready", "snapshot": self.browser_snapshot()}
        bridge.close.side_effect = lambda: events.append("closed")
        with patch.object(manual.browser, "_profile_operation", side_effect=operation), \
                patch.object(manual.browser, "_request", return_value={"id": "profile-cn", "name": "MakerHub CN", "status": "running"}), \
                patch.object(manual.browser, "launch_profile", side_effect=lambda profile: (profile, False)), \
                patch.object(manual.browser, "_configured_url", return_value="http://browser.internal"), \
                patch.object(manual.browser, "_auth_token", return_value="private-browser-token"), \
                patch.object(manual.browser, "touch_profile_activity"), \
                patch.object(manual, "_ManualBridge", return_value=bridge), \
                patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(manual, "publish_state_event"), \
                patch("app.api.config._schedule_online_account_cookie_test"):
            self.manager._worker(session)
        self.assertEqual(events, ["locked", "closed", "unlocked"])
        self.assertTrue(session.done.is_set())
        self.assertEqual(future.result()["status"], "completed")
        self.assertFalse(any("cookie" in kwargs for _args, kwargs in bridge.command.call_args_list))

    def test_worker_keeps_human_challenge_open_then_resumes_after_real_authorization(self):
        session = self.session()
        input_done, frame_done = Future(), Future()
        pointer = [{"type": "pointerdown", "x": 120, "y": 90}, {"type": "pointerup", "x": 120, "y": 90}]
        session.commands.put(("input", pointer, input_done))
        session.commands.put(("frame", [], frame_done))
        order = []
        attempt = MagicMock(response=None)
        attempt.__exit__.side_effect = lambda *args: order.append("pacing")

        @contextmanager
        def operation(*args, **kwargs):
            order.append("locked")
            try:
                yield
            finally:
                order.append("unlocked")

        def command(action, **payload):
            if action == "init":
                self.assertNotIn("cookies", payload)
                return {"status": "ready", "preflight": {}}
            if action == "authorize":
                return {"status": "authorizing"}
            if action == "input":
                self.assertEqual(payload["events"], pointer)
                return {"status": "challenge", "authorization": {"status_code": 418, "payload": {"captchaId": "private-id"}}}
            self.assertEqual(action, "frame")
            self.assertEqual(payload, {})
            return {"status": "authorized", "snapshot": self.browser_snapshot(),
                    "authorization": {"status_code": 200, "payload": {"url": "https://files.test/model.3mf"}}}

        bridge = Mock()
        bridge.command.side_effect = command
        bridge.close.side_effect = lambda: order.append("closed")
        with patch.object(manual.browser, "_profile_operation", side_effect=operation), \
                patch.object(manual.browser, "_request", return_value={"id": "profile-cn", "name": "MakerHub CN", "status": "running"}), \
                patch.object(manual.browser, "launch_profile", side_effect=lambda profile: (profile, False)), \
                patch.object(manual.browser, "_configured_url", return_value="http://browser.internal"), \
                patch.object(manual.browser, "_auth_token", return_value="private-browser-token"), \
                patch.object(manual.browser, "touch_profile_activity"), \
                patch.object(manual, "_ManualBridge", return_value=bridge), \
                patch.object(manual, "_fresh_target", return_value=self.target), \
                patch.object(manual, "get_account_health", return_value=self.snapshot), \
                patch.object(manual, "_queue_blockers", return_value={}), \
                patch.object(manual, "ThreeMfAuthorizationAttempt", return_value=attempt), \
                patch.object(manual, "reserve_three_mf_download_slot", return_value={"allowed": True}) as quota, \
                patch.object(manual, "_clear_verified_gate", return_value=True) as clear, \
                patch.object(manual, "publish_state_event"), \
                patch("app.api.dependencies.crawler.manager.ensure_worker_for_pending") as resume:
            self.manager._worker(session)
        self.assertEqual(input_done.result()["status"], "challenge")
        self.assertEqual(frame_done.result()["status"], "completed")
        self.assertEqual(attempt.response["status_code"], 200)
        self.assertEqual(order, ["locked", "closed", "pacing", "unlocked"])
        quota.assert_called_once()
        clear.assert_called_once()
        resume.assert_called_once_with(force=True)

    def test_changed_health_or_account_never_dispatches(self):
        for snapshot, mutate in (({**self.snapshot, "three_mf_reason": "new-gate"}, False), (self.snapshot, True)):
            session = self.session()
            if mutate:
                self.store.config.cookies[0].cookie = "token=changed"
            with patch.object(manual, "_fresh_target", return_value=self.target), \
                    patch.object(manual, "get_account_health", return_value=snapshot), \
                    patch.object(manual, "reserve_three_mf_download_slot") as quota:
                with self.assertRaises(manual.ManualVerificationError):
                    self.manager._dispatch(session, {"status": "ready"})
                quota.assert_not_called()

    def test_http_input_preserves_events_and_rejects_invalid_data(self):
        app = FastAPI()
        app.include_router(routes.router)

        @app.middleware("http")
        async def identity(request, call_next):
            request.state.auth_identity = {"kind": "session", "username": "admin", "session_id": "private-owner"}
            return await call_next(request)

        with patch.object(routes, "store", self.store), \
                patch.object(routes.manual_verification_manager, "execute", return_value={"status": "challenge"}) as execute:
            client = TestClient(app)
            response = client.post("/api/config/online-accounts/cn/verification/session-one/input",
                                   json={"events": [{"type": "text", "text": "verification answer"}]})
            self.assertEqual(response.status_code, 200)
            args, kwargs = execute.call_args
            self.assertEqual(args, ("cn", "session-one", "private-owner", "input"))
            self.assertEqual(kwargs["events"][0]["text"], "verification answer")
            self.assertEqual(client.post("/api/config/online-accounts/cn/verification/session-one/input",
                                         json={"events": [{"type": "pointerdown", "x": -1}]}).status_code, 422)

    def test_input_schema_rejects_non_finite_coordinates_and_large_batches(self):
        for events in ([{"type": "pointerdown", "x": float("nan")}],
                       [{"type": "text", "text": "x" * 2001}], [{"type": "unknown"}],
                       [{"type": "pointermove"}] * 81, [{"type": "pointermove", "delay_ms": 51}],
                       [{"type": "pointermove", "delay_ms": -1}]):
            with self.assertRaises(ValidationError):
                routes.VerificationInput(events=events)

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

    def test_registered_main_route_requires_same_origin_session_and_rejects_api_tokens(self):
        from app import main as main_app

        client = TestClient(main_app.app)
        identity = {"kind": "session", "username": "admin", "session_id": "private-owner"}
        with patch.object(routes, "store", self.store), \
                patch.object(main_app.auth_manager, "resolve_request_auth", return_value=identity), \
                patch.object(routes.manual_verification_manager, "create", return_value={"status": "starting"}) as create:
            endpoint = "/api/config/online-accounts/cn/verification"
            self.assertEqual(client.post(endpoint).status_code, 403)
            self.assertEqual(client.post(endpoint, headers={"Origin": "https://other.test"}).status_code, 403)
            create.assert_not_called()
            self.assertEqual(client.post(endpoint, headers={"Origin": "http://testserver"}).status_code, 200)
            create.assert_called_once_with("cn", "private-owner")
        with patch.object(routes, "store", self.store), \
                patch.object(main_app.auth_manager, "resolve_request_auth", return_value={"kind": "token", "username": ""}), \
                patch.object(routes.manual_verification_manager, "create") as create:
            self.assertEqual(client.post(endpoint, headers={"Origin": "http://testserver"}).status_code, 403)
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
