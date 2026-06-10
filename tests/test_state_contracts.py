from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import state_contracts
from app.services import task_runtime
from app.services.task_runtime import (
    normalize_blocked_reason,
    normalize_runtime_status,
    task_attempt_count,
    task_attempts_remaining,
)


def test_core_state_keys_are_stable():
    assert state_contracts.ARCHIVE_QUEUE_STATE_KEY == "archive_queue"
    assert state_contracts.MISSING_3MF_STATE_KEY == "missing_3mf"
    assert state_contracts.ORGANIZE_TASKS_STATE_KEY == "organize_tasks"
    assert state_contracts.SUBSCRIPTIONS_STATE_KEY == "subscriptions_state"
    assert state_contracts.REMOTE_REFRESH_STATE_KEY == "remote_refresh_state"
    assert state_contracts.SOURCE_REFRESH_QUEUE_STATE_KEY == "source_refresh_queue"
    assert state_contracts.SOURCE_REFRESH_RUNS_STATE_KEY == "source_refresh_runs"


def test_state_event_scopes_cover_dashboard_consumers():
    scopes = state_contracts.dashboard_event_scopes()
    assert scopes == [
        "archive_queue",
        "missing_3mf",
        "organize_tasks",
        "subscriptions_state",
        "remote_refresh_state",
        "source_refresh_queue",
        "source_refresh_runs",
        "dashboard",
    ]


def test_status_sets_include_existing_values():
    assert {"queued", "running", "completed", "failed"}.issubset(state_contracts.ARCHIVE_TASK_STATUSES)
    assert {"missing", "queued", "failed", "download_limited"}.issubset(state_contracts.MISSING_3MF_STATUSES)
    assert {"idle", "running", "success", "error", "disabled"}.issubset(state_contracts.REMOTE_REFRESH_STATUSES)
    assert {"queued", "running", "succeeded", "failed", "skipped", "timed_out"}.issubset(state_contracts.SOURCE_REFRESH_TASK_STATUSES)
    assert {"queued", "running", "paused", "completed", "failed", "interrupted"}.issubset(state_contracts.SOURCE_REFRESH_RUN_STATUSES)
    assert {"idle", "running", "success", "error", "pending"}.issubset(state_contracts.SUBSCRIPTION_STATUSES)


def test_runtime_statuses_cover_task_governance_values():
    assert {
        "queued",
        "running",
        "waiting_children",
        "paused",
        "blocked",
        "failed",
        "completed",
    }.issubset(state_contracts.RUNTIME_TASK_STATUSES)


def test_task_attempt_count_honors_zero_attempt_count():
    assert task_attempt_count({"attempt_count": 0, "attempts": 3}) == 0


def test_task_attempt_count_falls_back_for_empty_attempt_count():
    assert task_attempt_count({"attempt_count": None, "attempts": 3}) == 3
    assert task_attempt_count({"attempt_count": "", "attempts": 2}) == 2


def test_runtime_status_normalization_trims_and_defaults():
    assert normalize_runtime_status(" RUNNING ") == "running"
    assert normalize_runtime_status("unknown") == "queued"
    assert normalize_runtime_status(None, default="paused") == "paused"


def test_blocked_reason_normalization_trims_and_rejects_unknowns():
    assert normalize_blocked_reason(" NEEDS_COOKIE ") == "needs_cookie"
    assert normalize_blocked_reason("manual_review") == ""
    assert normalize_blocked_reason(None) == ""


def test_lease_expiry_handles_expired_future_and_invalid_values(monkeypatch):
    now = datetime(2026, 1, 2, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(task_runtime, "china_now", lambda: now)

    assert task_runtime.is_lease_expired((now - timedelta(seconds=1)).isoformat())
    assert not task_runtime.is_lease_expired((now + timedelta(seconds=1)).isoformat())
    assert task_runtime.is_lease_expired("not-a-date")
    assert task_runtime.is_lease_expired(None)


def test_task_attempts_remaining_respects_max_attempt_boundary():
    assert task_attempts_remaining({"attempt_count": 2}, max_attempts=3)
    assert not task_attempts_remaining({"attempt_count": 3}, max_attempts=3)
    assert not task_attempts_remaining({"attempt_count": 1}, max_attempts=0)


def test_dashboard_scopes_are_plain_strings_for_frontend_payloads():
    scopes = state_contracts.dashboard_event_scopes()
    assert all(isinstance(scope, str) for scope in scopes)
    assert "archive_queue" in scopes
    assert "source_refresh_queue" in scopes
    assert "source_refresh_runs" in scopes
    assert "dashboard" in scopes
