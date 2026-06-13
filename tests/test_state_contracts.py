from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services import state_contracts
from app.services import task_runtime
from app.services.runtime_engine import contracts as runtime_contracts
from app.services.task_runtime import (
    normalize_blocked_reason,
    normalize_runtime_status,
    task_attempt_count,
    task_attempts_remaining,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_core_state_keys_are_stable():
    assert state_contracts.ARCHIVE_QUEUE_STATE_KEY == "archive_queue"
    assert state_contracts.MISSING_3MF_STATE_KEY == "missing_3mf"
    assert state_contracts.ORGANIZE_TASKS_STATE_KEY == "organize_tasks"
    assert state_contracts.SUBSCRIPTIONS_STATE_KEY == "subscriptions_state"
    assert state_contracts.REMOTE_REFRESH_STATE_KEY == "remote_refresh_state"
    assert state_contracts.SOURCE_REFRESH_QUEUE_STATE_KEY == "source_refresh_queue"
    assert state_contracts.SOURCE_REFRESH_RUNS_STATE_KEY == "source_refresh_runs"


def test_runtime_state_keys_are_stable():
    assert state_contracts.RUNTIME_RUNS_STATE_KEY == "runtime_runs"
    assert state_contracts.RUNTIME_BATCHES_STATE_KEY == "runtime_batches"
    assert state_contracts.RUNTIME_FAILURES_STATE_KEY == "runtime_failures"
    assert state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY == "runtime_snapshots"
    assert state_contracts.RUNTIME_MIGRATION_STATE_KEY == "runtime_migration"
    assert state_contracts.RUNTIME_STATE_KEYS == (
        "runtime_runs",
        "runtime_batches",
        "runtime_failures",
        "runtime_snapshots",
        "runtime_migration",
    )


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


def test_runtime_engine_statuses_cover_run_batch_and_failure_values():
    assert {"queued", "discovering", "planned", "running", "blocked", "completed"}.issubset(runtime_contracts.RUNTIME_RUN_STATUSES)
    assert {"queued", "running", "paused", "blocked", "completed", "interrupted"}.issubset(runtime_contracts.RUNTIME_BATCH_STATUSES)
    assert {"failed", "missing_3mf", "verification_required", "daily_limit", "not_found"}.issubset(runtime_contracts.RUNTIME_FAILURE_STATUSES)


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


def test_remote_refresh_module_doc_names_source_refresh_public_api():
    text = (PROJECT_ROOT / "docs/modules/remote_refresh.md").read_text(encoding="utf-8")
    assert "`GET /api/source-refresh`" in text
    assert "`POST /api/source-refresh/run`" in text
    assert "`POST /api/source-refresh/repair`" in text
    assert "`GET /api/remote-refresh`" in text
    assert "selected_candidates" in text
    assert "_pick_candidates()" in text
    assert "兼容" in text


def test_state_contract_doc_covers_source_refresh_projection_state():
    text = (PROJECT_ROOT / "docs/modules/state_contracts.md").read_text(encoding="utf-8")
    assert "`source_refresh_queue`" in text
    assert "`source_refresh_runs`" in text
    assert "`runtime_runs`" in text
    assert "`runtime_failures`" in text
    assert "Source refresh tasks" in text
    assert "Source refresh runs" in text
    assert "Runtime runs" in text
    assert "Runtime failures" in text
    assert "`resuming`" in text
    assert "`interrupted`" in text
    assert "`runtime.run.started`" in text
    assert "`runtime.failure.created`" in text


def test_project_docs_point_to_source_refresh_projection_state():
    modules_text = (PROJECT_ROOT / "docs/MODULES.md").read_text(encoding="utf-8")
    architecture_text = (PROJECT_ROOT / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "`source_refresh.py`" in modules_text
    assert "`app/api/remote_refresh_routes.py`" in modules_text
    assert "`source_refresh_queue`" in modules_text
    assert "`source_refresh_runs`" in modules_text
    assert "`SourceRefreshTaskManager`" in architecture_text
    assert "`source_refresh_queue`" in architecture_text
    assert "`source_refresh_runs`" in architecture_text


def test_project_docs_point_to_runtime_engine_contracts():
    modules_text = (PROJECT_ROOT / "docs/MODULES.md").read_text(encoding="utf-8")
    assert "| Runtime Engine |" in modules_text
    assert "`app/services/runtime_engine/*`" in modules_text
    assert "`test_runtime_engine_*`" in modules_text
