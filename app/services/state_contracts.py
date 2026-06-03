from __future__ import annotations

from app.services.task_runtime import RUNTIME_TASK_STATUSES


ARCHIVE_QUEUE_STATE_KEY = "archive_queue"
MISSING_3MF_STATE_KEY = "missing_3mf"
ORGANIZE_TASKS_STATE_KEY = "organize_tasks"
SUBSCRIPTIONS_STATE_KEY = "subscriptions_state"
REMOTE_REFRESH_STATE_KEY = "remote_refresh_state"
MODEL_FLAGS_STATE_KEY = "model_flags"
THREE_MF_LIMIT_GUARD_STATE_KEY = "three_mf_limit_guard"
THREE_MF_DAILY_QUOTA_STATE_KEY = "three_mf_daily_quota"

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
        "success",
        "error",
        "disabled",
    }
)

DASHBOARD_EVENT_SCOPES = (
    ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_STATE_KEY,
    ORGANIZE_TASKS_STATE_KEY,
    SUBSCRIPTIONS_STATE_KEY,
    REMOTE_REFRESH_STATE_KEY,
)


def dashboard_event_scopes() -> list[str]:
    return list(DASHBOARD_EVENT_SCOPES)
