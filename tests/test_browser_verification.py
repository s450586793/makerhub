import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.services.archive_worker as archive_worker_module
import app.services.browser_verification as browser_verification_module
import app.services.legacy_archiver as legacy_archiver_module
import app.services.task_state as task_state_module
import app.worker as worker_module
from app.services.archive_worker import ArchiveTaskManager
from app.services.browser_verification import BrowserVerificationStore, proof_store
from app.services.legacy_archiver import _classify_3mf_fetch_failure, fetch_instance_3mf
from app.services.task_state import _normalize_missing_3mf


class BrowserVerificationCoreTest(unittest.TestCase):
    def test_418_captcha_failure_preserves_safe_verification_metadata(self):
        failure = _classify_3mf_fetch_failure(
            status_code=418,
            payload={
                "captchaId": "geetest-id",
                "code": 418,
                "error": "We need to confirm that you are not a robot.",
            },
            source="cn",
        )

        self.assertEqual(failure["state"], "verification_required")
        self.assertEqual(
            failure["verification"],
            {
                "captcha_id": "geetest-id",
                "provider": "geetest",
            },
        )
        self.assertNotIn("x-bbl-captcha-result", str(failure).lower())

    def test_missing_3mf_normalization_preserves_verification_retry_fields(self):
        payload = {
            "items": [
                {
                    "model_id": "1063416",
                    "model_url": "https://makerworld.com.cn/zh/models/1063416",
                    "title": "0.2mm layer",
                    "instance_id": "profile-1",
                    "status": "verification_required",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "api_url": "https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf?type=download&fileType=",
                    "captcha_id": "geetest-id",
                    "source": "cn",
                    "verification": {
                        "captcha_id": "geetest-id",
                        "provider": "geetest",
                    },
                    "x-bbl-captcha-result": "secret",
                }
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        item = normalized["items"][0]
        self.assertEqual(item["status"], "verification_required")
        self.assertEqual(item["api_url"], payload["items"][0]["api_url"])
        self.assertEqual(item["captcha_id"], "geetest-id")
        self.assertEqual(item["source"], "cn")
        self.assertNotIn("x-bbl-captcha-result", item)
        self.assertNotIn("secret", str(item))

    def test_fetch_instance_3mf_sends_captcha_result_header(self):
        session = Mock()
        session.headers = {"User-Agent": "UnitTest"}
        session.get.return_value = SimpleNamespace(
            status_code=200,
            text='{"name":"demo.3mf","url":"https://download.example/demo.3mf"}',
            json=lambda: {"name": "demo.3mf", "url": "https://download.example/demo.3mf"},
        )

        with patch.object(
            legacy_archiver_module,
            "_build_instance_api_candidates",
            return_value=["https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf?type=download&fileType="],
        ), patch.object(
            legacy_archiver_module,
            "fetch_json_with_scrapling",
            return_value=(None, SimpleNamespace(engine="scrapling", status_code=0, text="", error="")),
        ) as scrapling_mock, patch.object(
            legacy_archiver_module,
            "scrapling_only",
            return_value=False,
        ), patch.object(
            legacy_archiver_module,
            "_wait_before_three_mf_download",
            return_value=None,
        ):
            name, url, _used_api_url, failure = fetch_instance_3mf(
                session,
                1063416,
                "token=ok",
                "https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf?type=download&fileType=",
                origin="https://makerworld.com.cn",
                captcha_result_header="proof-token",
            )

        self.assertEqual(name, "demo.3mf")
        self.assertEqual(url, "https://download.example/demo.3mf")
        self.assertEqual(failure["state"], "available")
        scrapling_headers = scrapling_mock.call_args.kwargs["headers"]
        requests_headers = session.get.call_args.kwargs["headers"]
        self.assertEqual(scrapling_headers["x-bbl-captcha-result"], "proof-token")
        self.assertEqual(requests_headers["x-bbl-captcha-result"], "proof-token")

    def test_retry_verification_missing_3mf_filters_same_platform_verification_states(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "1",
                        "model_url": "https://makerworld.com.cn/zh/models/1",
                        "title": "primary",
                        "instance_id": "a",
                        "status": "verification_required",
                    },
                    {
                        "model_id": "2",
                        "model_url": "https://makerworld.com.cn/zh/models/2",
                        "title": "cloudflare",
                        "instance_id": "b",
                        "status": "cloudflare",
                    },
                    {
                        "model_id": "3",
                        "model_url": "https://makerworld.com/zh/models/3",
                        "title": "global",
                        "instance_id": "c",
                        "status": "verification_required",
                    },
                    {
                        "model_id": "4",
                        "model_url": "https://makerworld.com.cn/zh/models/4",
                        "title": "limited",
                        "instance_id": "d",
                        "status": "download_limited",
                    },
                    {
                        "model_id": "5",
                        "model_url": "https://makerworld.com.cn/zh/models/5",
                        "title": "missing",
                        "instance_id": "e",
                        "status": "missing",
                    },
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
        submitted = []
        manager.retry_missing_3mf = lambda **kwargs: submitted.append(kwargs) or {"accepted": True}

        result = manager.retry_verification_missing_3mf(
            platform="cn",
            primary={
                "model_id": "1",
                "model_url": "https://makerworld.com.cn/zh/models/1",
                "title": "primary",
                "instance_id": "a",
            },
            proof_id="proof-1",
        )

        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual([item["model_id"] for item in submitted], ["1", "2"])
        self.assertTrue(all(item["browser_verification_proof_id"] == "proof-1" for item in submitted))


    def test_single_task_resolves_proof_id_before_archive_job(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(proxy=SimpleNamespace(), cookies=[]))
        active_updates = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_kwargs: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **kwargs: active_updates.append((_args, kwargs)),
            complete_archive_task=lambda *_args, **_kwargs: None,
        )
        archive_calls = []

        with patch.object(archive_worker_module, "_select_cookie", return_value="token=ok"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_three_mf_daily_limits", return_value=(100, 100)), \
                patch.object(archive_worker_module, "run_archive_model_job", side_effect=lambda **kwargs: archive_calls.append(kwargs) or {"model_id": "1063416", "missing_3mf": [], "base_name": "Demo"}), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "consume_browser_verification_proof", return_value="proof-secret") as consume_mock:
            manager._run_single_task(
                "task-1",
                "https://makerworld.com.cn/zh/models/1063416",
                meta={
                    "missing_3mf_retry": True,
                    "browser_verification_proof_id": "proof-1",
                    "model_id": "1063416",
                },
            )

        consume_mock.assert_called_once_with("proof-1")
        self.assertEqual(archive_calls[0]["three_mf_captcha_result_header"], "proof-secret")
        scrub_updates = [kwargs for _args, kwargs in active_updates if kwargs.get("meta")]
        self.assertEqual(scrub_updates[0]["meta"].get("browser_verification_proof_id"), "")
        self.assertNotIn("proof-secret", str({"browser_verification_proof_id": "proof-1"}))


class BrowserVerificationSessionTest(unittest.TestCase):
    def test_create_session_from_missing_item_redacts_sensitive_fields(self):
        state = {}

        with patch.object(browser_verification_module, "load_database_json_state", side_effect=lambda _key, default: dict(state or default)), \
                patch.object(browser_verification_module, "save_database_json_state", side_effect=lambda _key, payload: state.clear() or state.update(payload) or payload):
            store = BrowserVerificationStore()
            session = store.create_session(
                {
                    "model_id": "1063416",
                    "model_url": "https://makerworld.com.cn/zh/models/1063416",
                    "title": "Demo",
                    "instance_id": "profile-1",
                    "api_url": "https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf?type=download&fileType=",
                    "captcha_id": "geetest-id",
                    "x-bbl-captcha-result": "secret",
                }
            )

            loaded = store.get_session(session["id"])

        self.assertEqual(session["platform"], "cn")
        self.assertEqual(session["status"], "queued")
        self.assertEqual(session["captcha_id"], "geetest-id")
        self.assertEqual(session["target"]["api_url"], "https://makerworld.com.cn/api/v1/design-service/instance/1063416/f3mf?type=download&fileType=")
        self.assertNotIn("secret", str(session))
        self.assertNotIn("secret", str(loaded))

    def test_proof_store_returns_once_and_does_not_expose_secret_in_state(self):
        proof_store.clear()
        proof_id = proof_store.store("proof-secret")

        self.assertTrue(proof_id.startswith("proof_"))
        self.assertEqual(proof_store.pop(proof_id), "proof-secret")
        self.assertEqual(proof_store.pop(proof_id), "")

    def test_input_commands_are_append_only_and_redacted(self):
        state = {}

        with patch.object(browser_verification_module, "load_database_json_state", side_effect=lambda _key, default: dict(state or default)), \
                patch.object(browser_verification_module, "save_database_json_state", side_effect=lambda _key, payload: state.clear() or state.update(payload) or payload):
            store = BrowserVerificationStore()
            session = store.create_session({"model_url": "https://makerworld.com.cn/zh/models/1063416"})
            command = store.enqueue_input(
                session["id"],
                {
                    "type": "click",
                    "x": 320,
                    "y": 240,
                    "text": "secret text should not persist",
                },
            )
            commands = store.consume_input_commands(session["id"])
            commands_after_consume = store.consume_input_commands(session["id"])

        self.assertEqual(command["type"], "click")
        self.assertEqual(commands[0]["x"], 320)
        self.assertEqual(commands[0]["y"], 240)
        self.assertNotIn("text", commands[0])
        self.assertEqual(commands_after_consume, [])

    def test_retry_after_verification_omits_origin_only_primary(self):
        retry_calls = []
        manager = SimpleNamespace(
            retry_verification_missing_3mf=lambda **kwargs: retry_calls.append(kwargs) or {"accepted_count": 0}
        )
        runtime = browser_verification_module.BrowserVerificationRuntime(archive_manager=manager)

        result = runtime._retry_after_verification(
            {
                "platform": "cn",
                "target": {
                    "model_url": "https://makerworld.com.cn",
                    "title": "国区",
                },
            },
            "proof-1",
        )

        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(retry_calls[0]["platform"], "cn")
        self.assertEqual(retry_calls[0]["primary"], {})
        self.assertEqual(retry_calls[0]["proof_id"], "proof-1")


class BrowserVerificationWorkerTest(unittest.TestCase):
    def test_worker_main_polls_browser_verification_runtime_with_archive_manager(self):
        class OneLoopEvent:
            def __init__(self):
                self.wait_calls = 0

            def set(self):
                self.wait_calls = 99

            def wait(self, _seconds):
                self.wait_calls += 1
                return self.wait_calls > 1

        fake_store = Mock(name="store")
        fake_task_store = Mock(name="task_store")
        fake_archive_manager = Mock(name="archive_manager")
        fake_archive_manager.resume_pending_tasks.return_value = {"queued_count": 0, "recovered_count": 0}
        fake_archive_manager.ensure_worker_for_pending.return_value = {"queued_count": 0}
        fake_runtime = Mock(name="browser_verification_runtime")
        fake_runtime.poll_once.return_value = {"started": 0, "running": 0}

        with patch.object(worker_module.signal, "signal"), \
                patch.object(worker_module.threading, "Event", OneLoopEvent), \
                patch.object(worker_module, "JsonStore", return_value=fake_store), \
                patch.object(worker_module, "TaskStateStore", return_value=fake_task_store), \
                patch.object(worker_module, "ArchiveTaskManager", return_value=fake_archive_manager), \
                patch.object(worker_module, "SubscriptionManager") as subscription_manager, \
                patch.object(worker_module, "LocalOrganizerService") as local_organizer_service, \
                patch.object(worker_module, "SourceLibraryManager") as source_library_manager, \
                patch.object(worker_module, "RemoteRefreshManager") as remote_refresh_manager, \
                patch.object(worker_module, "BrowserVerificationRuntime", return_value=fake_runtime) as runtime_cls, \
                patch.object(worker_module, "read_profile_backfill_status", return_value={"running": False}), \
                patch.object(worker_module, "should_auto_run_database_migration", return_value=False), \
                patch.object(worker_module, "local_preview_queue_marker_mtime", return_value=0.0), \
                patch.object(worker_module, "run_local_preview_generation_once", return_value={"processed": False}), \
                patch.object(worker_module, "append_business_log"):
            result = worker_module.main()

        self.assertEqual(result, 0)
        runtime_cls.assert_called_once_with(archive_manager=fake_archive_manager, json_store=fake_store)
        fake_runtime.poll_once.assert_called_once_with()
        fake_archive_manager.ensure_worker_for_pending.assert_called_once_with()
        local_organizer_service.return_value.stop.assert_called_once_with()
        subscription_manager.return_value.start.assert_called_once_with()
        source_library_manager.return_value.start.assert_called_once_with()
        remote_refresh_manager.return_value.start.assert_called_once_with()

    def test_worker_main_logs_browser_verification_poll_errors_and_continues(self):
        class OneLoopEvent:
            def __init__(self):
                self.wait_calls = 0

            def set(self):
                self.wait_calls = 99

            def wait(self, _seconds):
                self.wait_calls += 1
                return self.wait_calls > 1

        fake_archive_manager = Mock(name="archive_manager")
        fake_archive_manager.resume_pending_tasks.return_value = {"queued_count": 0, "recovered_count": 0}
        fake_archive_manager.ensure_worker_for_pending.return_value = {"queued_count": 0}
        fake_runtime = Mock(name="browser_verification_runtime")
        fake_runtime.poll_once.side_effect = RuntimeError("browser failed")
        log_calls = []

        with patch.object(worker_module.signal, "signal"), \
                patch.object(worker_module.threading, "Event", OneLoopEvent), \
                patch.object(worker_module, "JsonStore", return_value=Mock()), \
                patch.object(worker_module, "TaskStateStore", return_value=Mock()), \
                patch.object(worker_module, "ArchiveTaskManager", return_value=fake_archive_manager), \
                patch.object(worker_module, "SubscriptionManager"), \
                patch.object(worker_module, "LocalOrganizerService") as local_organizer_service, \
                patch.object(worker_module, "SourceLibraryManager"), \
                patch.object(worker_module, "RemoteRefreshManager"), \
                patch.object(worker_module, "BrowserVerificationRuntime", return_value=fake_runtime), \
                patch.object(worker_module, "read_profile_backfill_status", return_value={"running": False}) as backfill_status, \
                patch.object(worker_module, "should_auto_run_database_migration", return_value=False), \
                patch.object(worker_module, "local_preview_queue_marker_mtime", return_value=0.0), \
                patch.object(worker_module, "run_local_preview_generation_once", return_value={"processed": False}), \
                patch.object(worker_module, "append_business_log", side_effect=lambda *args, **kwargs: log_calls.append((args, kwargs))):
            result = worker_module.main()

        self.assertEqual(result, 0)
        fake_runtime.poll_once.assert_called_once_with()
        self.assertGreaterEqual(backfill_status.call_count, 2)
        self.assertTrue(
            any(
                call[0][:2] == ("missing_3mf", "browser_verification_worker_poll_failed")
                and call[1].get("level") == "warning"
                and call[1].get("error") == "browser failed"
                for call in log_calls
            )
        )
        local_organizer_service.return_value.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
