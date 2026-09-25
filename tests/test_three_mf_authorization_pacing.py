import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import account_health, cloakbrowser_session, resource_limiter
from app.services import three_mf_pacing


SUCCESS = {"status_code": 200, "payload": {"name": "demo.3mf", "url": "https://cdn.example.test/demo.3mf"}}


def running_profile(platform, profile_id):
    profile = cloakbrowser_session.CloakBrowserProfile(id=profile_id, name=platform, status="running")
    return profile, profile, False


@pytest.fixture
def authorization_environment(tmp_path):
    health = {"cn": {"three_mf_gate": "open"}, "global": {"three_mf_gate": "open"}}

    def update(platform, **fields):
        health[platform] = {
            "three_mf_gate": account_health.normalize_three_mf_gate(fields["gate"]),
            "three_mf_detail": fields["detail"],
            "updated_at": three_mf_pacing.china_now().isoformat(),
        }
        return health[platform]

    with ExitStack() as stack:
        stack.enter_context(patch.object(cloakbrowser_session, "_ensure_running_profile", side_effect=running_profile))
        stack.enter_context(patch.object(cloakbrowser_session, "STATE_DIR", tmp_path))
        stack.enter_context(patch.object(resource_limiter, "STATE_DIR", tmp_path))
        stack.enter_context(patch.object(account_health, "get_account_health", side_effect=lambda platform: health[platform]))
        stack.enter_context(patch.object(account_health, "update_three_mf_gate", side_effect=update))
        stack.enter_context(patch.dict(os.environ, {
            "MAKERHUB_CLOAKBROWSER_URL": "http://browser.test",
            "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "test-token",
            "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS": "0.06",
            "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS": "0.06",
        }))
        yield health


def authorize(platform="cn", instance_id="123"):
    domain = "cn" if platform == "cn" else "com"
    return cloakbrowser_session.browser_authorize_3mf_download(
        platform,
        f"https://api.bambulab.{domain}/v1/design-service/instance/{instance_id}/f3mf",
        profile_id=f"profile-{platform}",
        model_url=f"https://makerworld.com{'.cn' if platform == 'cn' else ''}/zh/models/456",
        instance_id=instance_id,
    )


def test_queued_authorizations_keep_gap_after_previous_response(authorization_environment):
    calls = []

    def bridge(*args, **kwargs):
        started = time.monotonic()
        time.sleep(0.08)
        calls.append((started, time.monotonic()))
        return dict(SUCCESS)

    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda number: authorize(instance_id=str(number)), range(3)))

    assert all(result["payload"]["url"] == SUCCESS["payload"]["url"] for result in results)
    assert len(calls) == 3
    assert all(current[0] - previous[1] >= 0.05 for previous, current in zip(calls, calls[1:]))


def test_explicit_verification_stops_queued_authorizations_until_confirmed(authorization_environment):
    calls = []

    def bridge(*args, **kwargs):
        calls.append(True)
        return {"status_code": 418, "payload": {"captchaId": "test-captcha"}}

    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        first = authorize()
        second = authorize(instance_id="124")

    assert first["status_code"] == 418
    assert second["status_code"] == 418
    assert len(calls) == 1
    assert authorization_environment["cn"]["three_mf_gate"] == "verification_required"
    authorization_environment["cn"] = {"three_mf_gate": "open"}
    with patch.object(cloakbrowser_session, "_run_bridge", return_value=dict(SUCCESS)):
        assert authorize()["status_code"] == 200


def test_rate_limit_cools_down_only_affected_account(authorization_environment):
    calls = []

    def bridge(payload, **kwargs):
        calls.append(payload["platform"])
        if payload["platform"] == "cn":
            return {"status_code": 429, "payload": {"message": "Too Many Requests"}, "headers": {"retry-after": "120"}}
        return dict(SUCCESS)

    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        assert authorize()["status_code"] == 429
        assert authorize(instance_id="124")["status_code"] == 429
        assert authorize("global")["status_code"] == 200

    assert calls == ["cn", "global"]
    assert authorization_environment["cn"]["three_mf_gate"] == "open"


def test_cloudflare_interstitial_stops_next_authorization_immediately(authorization_environment):
    with patch.object(cloakbrowser_session, "_run_bridge", return_value={
        "status_code": 403, "payload": {"code": "MAKERHUB_CLOUDFLARE"},
    }) as bridge:
        assert authorize()["status_code"] == 403
        assert authorize(instance_id="124")["status_code"] == 418
    assert bridge.call_count == 1
    assert authorization_environment["cn"]["three_mf_gate"] == "verification_required"
    assert "Cloudflare" in authorization_environment["cn"]["three_mf_detail"]
    assert authorization_environment["global"]["three_mf_gate"] == "open"


def test_transport_error_does_not_set_verification_gate(authorization_environment):
    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=cloakbrowser_session.CloakBrowserError("bridge failed")):
        with pytest.raises(cloakbrowser_session.CloakBrowserError, match="bridge failed"):
            authorize()
    assert authorization_environment["cn"]["three_mf_gate"] == "open"


def test_browser_fetch_authorization_uses_same_pacing_as_click(authorization_environment):
    calls = []

    def bridge(payload, **kwargs):
        calls.append(time.monotonic())
        return {**SUCCESS, "url": payload["target_url"], "text": json.dumps(SUCCESS["payload"])}

    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        authorize()
        result = cloakbrowser_session.browser_fetch(
            "cn", "https://api.bambulab.cn/v1/design-service/instance/124/f3mf", profile_id="profile-cn",
        )
    assert result.status_code == 200
    assert calls[1] - calls[0] >= 0.05


def _process_authorization(state_dir, ready, start, output):
    cloakbrowser_session.STATE_DIR = resource_limiter.STATE_DIR = Path(state_dir)
    os.environ.update({
        "MAKERHUB_CLOAKBROWSER_URL": "http://browser.test",
        "MAKERHUB_CLOAKBROWSER_AUTH_TOKEN": "test-token",
        "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MIN_SECONDS": "0.12",
        "MAKERHUB_THREE_MF_DOWNLOAD_WAIT_MAX_SECONDS": "0.12",
    })

    def bridge(*args, **kwargs):
        output.put(time.time())
        return dict(SUCCESS)

    with patch.object(account_health, "get_account_health", return_value={"three_mf_gate": "open"}), \
            patch.object(cloakbrowser_session, "_ensure_running_profile", side_effect=running_profile), \
            patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        ready.put(True)
        assert start.wait(15)
        authorize()


def test_separate_worker_processes_share_authorization_gap(tmp_path):
    context = get_context("spawn")
    ready, output, start = context.Queue(), context.Queue(), context.Event()
    processes = [context.Process(target=_process_authorization, args=(str(tmp_path), ready, start, output)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=20)
        start.set()
        times = sorted(output.get(timeout=20) for _ in processes)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert all(right - left >= 0.11 for left, right in zip(times, times[1:]))
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        ready.close()
        output.close()


def test_cooldown_increases_and_success_restores_normal_pacing(tmp_path, authorization_environment):
    clock = [1000.0]
    fake_time = SimpleNamespace(time=lambda: clock[0], sleep=lambda delay: clock.__setitem__(0, clock[0] + delay))

    def run(result):
        with three_mf_pacing.ThreeMfAuthorizationAttempt("cn", "profile-cn", tmp_path) as attempt:
            assert attempt.response is None
            attempt.response = result
        return json.loads(attempt.path.read_text())

    with patch.object(three_mf_pacing, "time", fake_time), patch.object(three_mf_pacing, "_authorization_interval", return_value=7.5):
        first = run({"status_code": 429, "headers": {"retry-after": "120"}})
        assert first["cooldown_until"] == clock[0] + 120
        clock[0] = first["cooldown_until"]
        second = run({"status_code": 429})
        assert second["cooldown_until"] == clock[0] + 60
        clock[0] = second["cooldown_until"]
        recovered = run(dict(SUCCESS))
        assert recovered["rate_limit_count"] == 0
        assert recovered["cooldown_until"] == 0
        assert recovered["next_allowed_at"] == clock[0] + 7.5
        assert "cdn.example" not in json.dumps(recovered)


@pytest.mark.parametrize("value, expected", [
    ("120", 120), ("Thu, 01 Jan 1970 00:20:00 GMT", 200),
    ("invalid", 0), ("-20", 0), ("nan", 0), ("inf", 0), ("", 0),
])
def test_retry_after_accepts_seconds_and_http_dates(value, expected):
    assert three_mf_pacing._retry_after_seconds(value, 1000) == expected


def test_gate_is_rechecked_after_waiting(tmp_path, authorization_environment):
    def verification_during_wait(delay):
        authorization_environment["cn"] = {"three_mf_gate": "verification_required"}

    with patch.object(three_mf_pacing, "time", SimpleNamespace(time=lambda: 1000, sleep=verification_during_wait)):
        with three_mf_pacing.ThreeMfAuthorizationAttempt("cn", "profile-cn", tmp_path) as attempt:
            assert attempt.response["status_code"] == 418
            assert attempt.request_started is False


def test_unrelated_browser_requests_are_not_delayed(authorization_environment):
    with patch.object(three_mf_pacing, "_authorization_interval", side_effect=AssertionError("ordinary page fetch must not be paced")), \
            patch.object(cloakbrowser_session, "_run_bridge", return_value={"status_code": 200, "text": "model page"}):
        result = cloakbrowser_session.browser_fetch("cn", "https://makerworld.com.cn/zh/models/456", profile_id="profile-cn")
    assert result.text == "model page"


def test_successful_authorization_with_captcha_metadata_does_not_close_gate(authorization_environment):
    response = {**SUCCESS, "payload": {**SUCCESS["payload"], "captchaId": "completed-captcha"}}
    with patch.object(cloakbrowser_session, "_run_bridge", return_value=response):
        assert authorize()["status_code"] == 200
    assert authorization_environment["cn"]["three_mf_gate"] == "open"


def test_daily_quota_response_stops_other_requests_for_today(authorization_environment):
    calls = []

    def bridge(*args, **kwargs):
        calls.append(True)
        return {"status_code": 429, "payload": {"message": "Daily download limit reached"}}

    with patch.object(cloakbrowser_session, "_run_bridge", side_effect=bridge):
        authorize()
        repeated = authorize(instance_id="124")
    assert len(calls) == 1
    assert "Daily download limit" in repeated["payload"]["message"]
    assert authorization_environment["cn"]["three_mf_gate"] == "daily_limit"


def test_previous_days_quota_does_not_block_new_authorization(authorization_environment):
    authorization_environment["cn"] = {"three_mf_gate": "daily_limit", "updated_at": "2020-01-01T23:59:00+08:00"}
    with patch.object(cloakbrowser_session, "_run_bridge", return_value=dict(SUCCESS)):
        assert authorize()["status_code"] == 200


def test_failed_pacing_save_preserves_successful_download_address(authorization_environment):
    with patch.object(cloakbrowser_session, "_run_bridge", return_value=dict(SUCCESS)), \
            patch.object(three_mf_pacing.os, "replace", side_effect=OSError("disk unavailable")):
        assert authorize()["payload"]["url"] == SUCCESS["payload"]["url"]


def test_invalid_state_is_replaced_with_finite_schedule(tmp_path, authorization_environment):
    attempt = three_mf_pacing.ThreeMfAuthorizationAttempt("cn", "profile-cn", tmp_path)
    attempt.path.parent.mkdir(parents=True)
    attempt.path.write_text("{broken")
    with attempt:
        assert attempt.response is None
        attempt.response = dict(SUCCESS)
    state = json.loads(attempt.path.read_text())
    assert state["next_allowed_at"] > 0
    assert state["rate_limit_count"] == 0
