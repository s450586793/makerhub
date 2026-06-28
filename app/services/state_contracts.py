from __future__ import annotations

from app.services.task_runtime import RUNTIME_TASK_STATUSES


ARCHIVE_QUEUE_STATE_KEY = "archive_queue"
MISSING_3MF_STATE_KEY = "missing_3mf"
ORGANIZE_TASKS_STATE_KEY = "organize_tasks"
SUBSCRIPTIONS_STATE_KEY = "subscriptions_state"
REMOTE_REFRESH_STATE_KEY = "remote_refresh_state"
SOURCE_REFRESH_QUEUE_STATE_KEY = "source_refresh_queue"
SOURCE_REFRESH_RUNS_STATE_KEY = "source_refresh_runs"
RUNTIME_RUNS_STATE_KEY = "runtime_runs"
RUNTIME_BATCHES_STATE_KEY = "runtime_batches"
RUNTIME_FAILURES_STATE_KEY = "runtime_failures"
RUNTIME_SNAPSHOTS_STATE_KEY = "runtime_snapshots"
RUNTIME_MIGRATION_STATE_KEY = "runtime_migration"
MODEL_FLAGS_STATE_KEY = "model_flags"
THREE_MF_LIMIT_GUARD_STATE_KEY = "three_mf_limit_guard"
THREE_MF_DAILY_QUOTA_STATE_KEY = "three_mf_daily_quota"
ACCOUNT_HEALTH_STATE_KEY = "account_health"
DASHBOARD_STATE_KEY = "dashboard"

RUNTIME_STATE_KEYS = (
    RUNTIME_RUNS_STATE_KEY,
    RUNTIME_BATCHES_STATE_KEY,
    RUNTIME_FAILURES_STATE_KEY,
    RUNTIME_SNAPSHOTS_STATE_KEY,
    RUNTIME_MIGRATION_STATE_KEY,
)

ARCHIVE_TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "completed",
        "success",
        "failed",
        "cancelled",
        "skipped",
    }
)

MISSING_3MF_STATUSES = frozenset(
    {
        "missing",
        "queued",
        "running",
        "failed",
        "cancelled",
        "download_limited",
        "verification_required",
        "cloudflare",
        "auth_required",
        "cookie_invalid",
        "pending_download",
    }
)

ORGANIZE_TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "success",
        "failed",
        "skipped",
    }
)

SUBSCRIPTION_STATUSES = frozenset(
    {
        "idle",
        "pending",
        "running",
        "success",
        "error",
        "deleted",
    }
)

REMOTE_REFRESH_STATUSES = frozenset(
    {
        "idle",
        "running",
        "resuming",
        "deferred",
        "interrupted",
        "success",
        "error",
        "disabled",
    }
)

SOURCE_REFRESH_TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "succeeded",
        "failed",
        "skipped",
        "timed_out",
        "cancelled",
    }
)

SOURCE_REFRESH_RUN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "paused",
        "resuming",
        "completed",
        "failed",
        "interrupted",
        "cancelled",
    }
)

DASHBOARD_EVENT_SCOPES = (
    ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_STATE_KEY,
    ORGANIZE_TASKS_STATE_KEY,
    SUBSCRIPTIONS_STATE_KEY,
    REMOTE_REFRESH_STATE_KEY,
    SOURCE_REFRESH_QUEUE_STATE_KEY,
    SOURCE_REFRESH_RUNS_STATE_KEY,
    ACCOUNT_HEALTH_STATE_KEY,
    DASHBOARD_STATE_KEY,
)


def dashboard_event_scopes() -> list[str]:
    return list(DASHBOARD_EVENT_SCOPES)
