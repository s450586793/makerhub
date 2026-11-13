import copy
import importlib
import json
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.schemas.models import AppConfig, CookiePair
from app.services import account_health, cloakbrowser_session, three_mf_pacing


class AccountVerificationRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("app.services.account_verification_recovery")
        self.config = AppConfig(cookies=[CookiePair(platform="cn", cookie="token=private-token; cf_clearance=old", browser_profile_id="profile-cn")])
        self.store = SimpleNamespace(load=lambda: copy.deepcopy(self.config), update=self.update_config)
        self.state = {
            "account_health": {"cn": self.snapshot("cn")},
            "three_mf_daily_quota": {"items": {}},
        }
        self.lock = threading.RLock()
        self.requests = []
        self.reservations = []
        self.blockers = {"active": False, "manual": False}
        self.missing = None
        self.index = {"items": []}
        self.response = {"status_code": 200, "payload": {"data": {"url": "https://download.example.test/file.3mf?signature=private-signature"}}}
        self.patches = [
            patch.object(self.module, "cloakbrowser_configured", return_value=True, create=True),
            patch.object(self.module, "load_database_json_state_without_initialization", side_effect=lambda key, default: copy.deepcopy(self.state.get(key, default))),
            patch.object(self.module, "update_database_json_state", side_effect=self.atomic_update),
            patch.object(self.module, "_queue_blockers", side_effect=lambda platform: dict(self.blockers)),
            patch.object(self.module, "_missing_candidate", side_effect=lambda platform: copy.deepcopy(self.missing)),
            patch.object(self.module, "query_archive_model_index", side_effect=lambda **kwargs: copy.deepcopy(self.index)),
            patch.object(self.module, "browser_authorize_3mf_download", side_effect=self.authorize),
            patch.object(self.module, "reserve_three_mf_download_slot", side_effect=self.reserve),
            patch.object(self.module, "publish_state_event"),
            patch.object(self.module, "china_now_iso", return_value="2026-11-13T17:00:00+08:00"),
            patch.dict("os.environ", {"MAKERHUB_AUTO_VERIFY_3MF": "true", "MAKERHUB_AUTO_VERIFY_CHALLENGES": "true"}),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def snapshot(platform):
        host = "makerworld.com.cn" if platform == "cn" else "makerworld.com"
        return {"platform": platform, "status": "ok", "three_mf_gate": "verification_required", "three_mf_reason": "three_mf_download_failed", "three_mf_detail": "needs verification", "model_url": f"https://{host}/zh/models/123", "model_id": "123", "instance_id": "456", "updated_at": "old"}

    def atomic_update(self, key, default, mutator, **kwargs):
        with self.lock:
            current = copy.deepcopy(self.state.get(key, default))
            result = mutator(current)
            self.state[key] = copy.deepcopy(current if result is None else result)
            return copy.deepcopy(self.state[key]), 1

    def update_config(self, mutator):
        with self.lock:
            latest = copy.deepcopy(self.config)
            updated = mutator(latest)
            self.config = latest if updated is None else updated
            return copy.deepcopy(self.config)

    def authorize(self, platform, api_url, **kwargs):
        before_dispatch = kwargs.get("before_dispatch")
        if before_dispatch is not None and not before_dispatch():
            return {"status_code": 0, "payload": {}}
        self.requests.append({"platform": platform, "api_url": api_url, **kwargs})
        return copy.deepcopy(self.response)

    def reserve(self, **kwargs):
        self.reservations.append(kwargs)
        return {"allowed": True}

    def run_recovery(self):
        return self.module.run_account_verification_recovery_once(store=self.store)

    def test_real_authorization_clears_old_gate_without_a_queue(self):
        result = self.run_recovery()
        self.assertEqual(result["recovered_platforms"], ["cn"])
        self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "open")
        self.assertEqual(self.requests[0]["instance_id"], "456")
        self.assertIs(self.requests[0]["verification_probe"], True)
        self.assertEqual(self.reservations[0]["limit"], 100)
        serialized = json.dumps({"result": result, "state": self.state})
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("private-signature", serialized)

    def test_empty_200_and_completed_challenge_do_not_clear_gate(self):
        self.response = {"status_code": 200, "payload": {}, "verification": {"completed": True}}
        self.assertEqual(self.run_recovery()["recovered_platforms"], [])
        self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "verification_required")

    def test_failed_probe_is_not_repeated_after_metadata_changes_or_reload(self):
        self.response = {"status_code": 403, "payload": {}}
        self.run_recovery()
        self.state["account_health"]["cn"].update(updated_at="new", reason="scheduled_cookie_check")
        self.config.cookies[0].cookie = "token=private-token; cf_clearance=new"
        self.run_recovery()
        self.module = importlib.reload(self.module)
        # reload 使用相同持久状态，而不是进程内尝试计数。
        with patch.object(self.module, "cloakbrowser_configured", return_value=True), patch.object(self.module, "load_database_json_state_without_initialization", side_effect=lambda key, default: copy.deepcopy(self.state.get(key, default))), patch.object(self.module, "update_database_json_state", side_effect=self.atomic_update), patch.object(self.module, "_queue_blockers", return_value=self.blockers), patch.object(self.module, "browser_authorize_3mf_download", side_effect=self.authorize):
            self.run_recovery()
        self.assertEqual(len(self.requests), 1)

    def test_concurrent_workers_claim_one_probe(self):
        entered = threading.Event()
        release = threading.Event()

        def authorize(*args, **kwargs):
            before_dispatch = kwargs.get("before_dispatch")
            if before_dispatch is not None and not before_dispatch():
                return {"status_code": 0, "payload": {}}
            self.requests.append(kwargs)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return {"status_code": 403, "payload": {}}

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=authorize), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_recovery)
            self.assertTrue(entered.wait(timeout=5))
            second = pool.submit(self.run_recovery)
            second.result(timeout=5)
            release.set()
            first.result(timeout=5)
        self.assertEqual(len(self.requests), 1)

    def test_sites_have_independent_claims(self):
        self.state["account_health"]["global"] = self.snapshot("global")
        self.config.cookies.append(CookiePair(platform="global", cookie="token=global-private-token", browser_profile_id="profile-global"))
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn", "global"])

    def test_token_change_permits_a_new_probe(self):
        self.response = {"status_code": 403, "payload": {}}
        self.run_recovery()
        self.config.cookies[0].cookie = "token=replaced-token"
        self.run_recovery()
        self.assertEqual(len(self.requests), 2)

    def test_unconfigured_browser_does_not_spend_attempt(self):
        with patch.object(self.module, "cloakbrowser_configured", return_value=False):
            self.run_recovery()
        self.assertEqual(self.requests, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)

    def test_archive_origin_url_is_a_valid_target(self):
        self.state["account_health"]["cn"].update(model_url="", model_id="", instance_id="")
        self.index = {"items": [{"id": "123", "origin_url": "https://makerworld.com.cn/zh/models/123", "instances": [{"id": 789}]}]}
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])

    def test_real_light_index_shape_uses_raw_metadata_remote_id(self):
        self.state["account_health"]["cn"].update(model_url="", model_id="", instance_id="")
        self.index = {"items": [{"id": "123", "origin_url": "https://makerworld.com.cn/zh/models/123", "model_dir": "cn/test-model", "profile_summary": {"profile_count": 1}}]}
        rows = [{"candidate": {"model_id": "123", "model_url": "https://makerworld.com.cn/zh/models/123", "instances": [{"id": 789}]}}]
        queries = []

        class Connection:
            def execute(self, sql, params=None):
                queries.append((sql, params))
                return self

            def fetchall(self):
                return rows

        with patch.object(self.module, "database_connection", return_value=nullcontext(Connection())):
            self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
        self.assertEqual(self.requests[0]["instance_id"], "789")
        self.assertEqual(queries[-1][1], ("cn", ["cn/test-model"]))

    def test_production_raw_ids_take_precedence_over_profile_ids(self):
        fixtures = (("cn", "1054330", "1090607", "51208919"), ("global", "2838040", "3163431", "780078580"))
        for platform, model_id, instance_id, profile_id in fixtures:
            with self.subTest(platform=platform):
                host = "makerworld.com.cn" if platform == "cn" else "makerworld.com"
                target = self.module._target(platform, {"model_id": model_id, "model_url": f"https://{host}/zh/models/{model_id}"}, instance={"id": instance_id, "profileId": profile_id})
                self.assertEqual(target["instance_id"], instance_id)

    def test_confirmed_authorization_syncs_action_required_browser_status(self):
        self.config.cookies[0].browser_status = "action_required"
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
        self.assertEqual(self.config.cookies[0].browser_status, "synced")
        self.assertEqual(self.config.cookies[0].browser_synced_at, "2026-11-13T17:00:00+08:00")

    def test_failed_authorization_preserves_action_required_browser_status(self):
        self.config.cookies[0].browser_status = "action_required"
        self.response = {"status_code": 403, "payload": {}}
        self.run_recovery()
        self.assertEqual(self.config.cookies[0].browser_status, "action_required")

    def test_account_mismatch_skips_without_claim(self):
        self.config.cookies[0].browser_status = "account_mismatch"
        self.run_recovery()
        self.assertEqual(self.requests, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)

    def test_flags_profiles_daily_limit_and_queue_blockers_skip_without_claim(self):
        for flag in ("MAKERHUB_AUTO_VERIFY_3MF", "MAKERHUB_AUTO_VERIFY_CHALLENGES"):
            with self.subTest(flag=flag), patch.dict("os.environ", {flag: "false"}):
                self.run_recovery()
        self.config.cookies[0].browser_profile_id = ""
        self.run_recovery()
        self.config.cookies[0].browser_profile_id = "profile-cn"
        for field, value in (("three_mf_gate", "daily_limit"), ("status", "daily_limit")):
            with self.subTest(field=field):
                old = self.state["account_health"]["cn"].get(field)
                self.state["account_health"]["cn"][field] = value
                self.run_recovery()
                self.state["account_health"]["cn"][field] = old
        for field in ("active", "manual"):
            self.blockers[field] = True
            self.run_recovery()
            self.blockers[field] = False
        self.assertEqual(self.requests, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)

    def test_exhausted_local_daily_quota_skips_without_claim(self):
        self.state["three_mf_daily_quota"] = {"items": {"cn": {"date": "2026-11-13", "used": 2}}}
        self.config.three_mf_limits.cn_daily_limit = 2
        self.run_recovery()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.reservations, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)

    def test_no_target_does_not_spend_claim_and_can_recover_later(self):
        self.state["account_health"]["cn"].update(model_url="", model_id="", instance_id="")
        self.run_recovery()
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)
        self.missing = {"model_url": "https://makerworld.com.cn/zh/models/123", "model_id": "123", "instance_id": "456"}
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])

    def test_archive_fallback_uses_numeric_instance_id_and_ignores_local_file(self):
        self.state["account_health"]["cn"].update(model_url="", model_id="", instance_id="")
        self.index = {"items": [{"id": "123", "source": "cn", "url": "https://makerworld.com.cn/zh/models/123", "instances": [{"instance_key": "123:789", "id": "789", "fileName": "already-local.3mf"}]}]}
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
        self.assertEqual(self.requests[0]["instance_id"], "789")

    def test_archive_fallback_accepts_remote_profile_id_alias(self):
        self.state["account_health"]["cn"].update(model_url="", model_id="", instance_id="")
        self.index = {"items": [{"id": "123", "url": "https://makerworld.com.cn/zh/models/123", "instances": [{"profileId": 789, "instance_key": "123:789"}]}]}
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
        self.assertEqual(self.requests[0]["instance_id"], "789")

    def test_unsafe_or_non_success_download_does_not_clear_gate(self):
        for status, url in ((403, "https://download.example.test/file.3mf"), (200, "file:///tmp/file.3mf"), (200, "http://download.example.test/file.3mf"), (200, "https://makerworld.com.cn/zh/models/123"), (200, "https://makerworld.com.cn/api/v1/design-service/instance/456/f3mf"), (200, "https://makerworld.com.cn/verify")):
            with self.subTest(status=status, url=url):
                self.state.pop(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, None)
                self.response = {"status_code": status, "payload": {"url": url}}
                self.assertEqual(self.run_recovery()["recovered_platforms"], [])
                self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "verification_required")

    def test_zero_limit_is_unlimited_and_denial_preserves_future_probe(self):
        self.config.three_mf_limits.cn_daily_limit = 0
        self.state["three_mf_daily_quota"] = {"items": {"cn": {"date": "2026-11-13", "used": 10000}}}
        with patch.object(self.module, "reserve_three_mf_download_slot", return_value={"allowed": False}):
            self.assertEqual(self.run_recovery()["attempted"], 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
        self.assertEqual(self.reservations[0]["limit"], 0)

    def test_changed_gate_details_or_manual_pause_during_probe_is_not_cleared(self):
        def new_gate(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            self.state["account_health"]["cn"]["three_mf_reason"] = "new_verification_required"
            return self.response

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=new_gate):
            self.assertEqual(self.run_recovery()["recovered_platforms"], [])
        self.state.pop(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY)

        def manually_paused(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            self.blockers["manual"] = True
            return self.response

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=manually_paused):
            self.assertEqual(self.run_recovery()["recovered_platforms"], [])
        self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "verification_required")

    def test_ordinary_metadata_updates_during_probe_allow_success(self):
        def metadata_update(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            self.state["account_health"]["cn"].update(updated_at="new", reason="scheduled_cookie_check")
            return self.response

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=metadata_update):
            self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])

    def test_wrong_platform_or_composite_instance_is_not_a_target(self):
        for changes in ({"model_url": "https://makerworld.com/zh/models/123"}, {"instance_id": "123:456"}, {"model_url": "http://makerworld.com.cn/zh/models/123"}, {"model_url": "https://makerworld.com.cn.evil.test/zh/models/123"}):
            with self.subTest(changes=changes):
                self.state["account_health"]["cn"] = {**self.snapshot("cn"), **changes}
                self.run_recovery()
        self.assertEqual(self.requests, [])

    def test_changed_login_or_new_daily_gate_is_not_cleared(self):
        def changed_login(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            self.config.cookies[0].cookie = "token=replaced-token"
            return self.response

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=changed_login):
            self.assertEqual(self.run_recovery()["recovered_platforms"], [])
        self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "verification_required")

        def daily_gate(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            self.state["account_health"]["cn"]["three_mf_gate"] = "daily_limit"
            return self.response

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=daily_gate):
            self.assertEqual(self.run_recovery()["recovered_platforms"], [])
        self.assertEqual(self.state["account_health"]["cn"]["three_mf_gate"], "daily_limit")

    def test_exception_is_sanitized_and_spends_attempt(self):
        def sent_then_failed(*args, **kwargs):
            if "before_dispatch" in kwargs:
                kwargs["before_dispatch"]()
            raise RuntimeError("token=private-token https://host/file?signature=private-signature")

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=sent_then_failed):
            self.run_recovery()
        self.run_recovery()
        self.assertEqual(self.requests, [])
        self.assertNotIn("private-token", json.dumps(self.state))
        self.assertNotIn("private-signature", json.dumps(self.state))

    def test_real_cooldown_defers_without_claim_or_quota_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            pacing = three_mf_pacing.ThreeMfAuthorizationAttempt("cn", "profile-cn", state_dir)
            pacing.path.parent.mkdir(parents=True)
            pacing.path.write_text(json.dumps({"cooldown_until": time.time() + 120}))
            profile = cloakbrowser_session.CloakBrowserProfile(id="profile-cn", name="CN", status="running")
            with patch.object(self.module, "browser_authorize_3mf_download", side_effect=cloakbrowser_session.browser_authorize_3mf_download), patch.object(cloakbrowser_session, "STATE_DIR", state_dir), patch.object(cloakbrowser_session, "_profile_operation", return_value=nullcontext()), patch.object(cloakbrowser_session, "_ensure_running_profile", return_value=(profile, profile, False)), patch.object(cloakbrowser_session, "_run_bridge", return_value=self.response) as bridge, patch.object(account_health, "get_account_health", side_effect=lambda platform: self.state["account_health"][platform]), patch.object(three_mf_pacing, "_authorization_interval", return_value=0), patch.dict("os.environ", {"MAKERHUB_CLOAKBROWSER_URL": "http://browser.test", "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "test-secret"}):
                first = self.run_recovery()
                self.assertEqual(first["attempted"], 0)
                self.assertEqual(self.reservations, [])
                self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)
                self.assertEqual(bridge.call_count, 0)
                pacing.path.write_text(json.dumps({"cooldown_until": 0, "next_allowed_at": 0}))
                self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])
                self.assertEqual(bridge.call_count, 1)

    def test_profile_lock_failure_preserves_future_probe(self):
        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=RuntimeError("profile slot unavailable")):
            self.assertEqual(self.run_recovery()["attempted"], 0)
        self.assertEqual(self.reservations, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)
        self.assertEqual(self.run_recovery()["recovered_platforms"], ["cn"])

    def test_new_manual_pause_before_dispatch_does_not_spend_attempt(self):
        def deferred(*args, **kwargs):
            self.blockers["manual"] = True
            return self.authorize(*args, **kwargs)

        with patch.object(self.module, "browser_authorize_3mf_download", side_effect=deferred):
            self.assertEqual(self.run_recovery()["attempted"], 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.reservations, [])

    def test_latest_runnable_queue_before_dispatch_does_not_spend_attempt(self):
        allowed = iter((True, False))
        result = self.module.run_account_verification_recovery_once(store=self.store, can_probe=lambda: next(allowed))
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.reservations, [])
        self.assertNotIn(self.module.ACCOUNT_VERIFICATION_RECOVERY_STATE_KEY, self.state)


if __name__ == "__main__":
    unittest.main()
