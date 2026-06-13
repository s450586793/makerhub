from __future__ import annotations

from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.three_mf import normalize_makerworld_source


RUNTIME_RUN_TYPES = frozenset(
    {
        "archive",
        "subscription_sync",
        "source_refresh",
        "missing_3mf_retry",
    }
)

RUNTIME_RUN_STATUSES = frozenset(
    {
        "queued",
        "discovering",
        "planned",
        "running",
        "paused",
        "blocked",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
)

RUNTIME_BATCH_STATUSES = frozenset(
    {
        "queued",
        "running",
        "paused",
        "blocked",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    }
)

RUNTIME_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "skipped",
        "missing_3mf",
        "verification_required",
        "cookie_invalid",
        "daily_limit",
        "network_error",
        "not_found",
    }
)

RUNTIME_EVENT_SCOPES = {
    "runtime.run.started",
    "runtime.batch.progress",
    "runtime.batch.completed",
    "runtime.run.completed",
    "runtime.run.blocked",
    "runtime.failure.created",
    "account_health.changed",
}


def _clean_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _normalize_status(value: Any, allowed: frozenset[str], default: str = "queued") -> str:
    status = _clean_text(value).lower()
    return status if status in allowed else default


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def normalize_run_type(value: Any, default: str = "archive") -> str:
    run_type = _clean_text(value).lower()
    return run_type if run_type in RUNTIME_RUN_TYPES else default


def normalize_run_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    updated_at = _clean_text(source.get("updated_at")) or china_now_iso()

    return {
        "run_id": _clean_text(source.get("run_id") or source.get("id"), 120),
        "type": normalize_run_type(source.get("type")),
        "source_url": _clean_text(source.get("source_url") or source.get("url"), 1000),
        "source_id": _clean_text(source.get("source_id"), 240),
        "platform": normalize_makerworld_source(
            source.get("platform"),
            source.get("source_url") or source.get("url"),
        ),
        "status": _normalize_status(source.get("status"), RUNTIME_RUN_STATUSES),
        "total": _non_negative_int(source.get("total")),
        "completed": _non_negative_int(source.get("completed")),
        "failed": _non_negative_int(source.get("failed")),
        "skipped": _non_negative_int(source.get("skipped")),
        "missing_3mf": _non_negative_int(source.get("missing_3mf")),
        "current_batch_id": _clean_text(source.get("current_batch_id"), 120),
        "message": _clean_text(source.get("message")),
        "created_at": _clean_text(source.get("created_at")),
        "started_at": _clean_text(source.get("started_at")),
        "updated_at": updated_at,
        "completed_at": _clean_text(source.get("completed_at")),
    }


def normalize_batch_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    updated_at = _clean_text(source.get("updated_at")) or china_now_iso()

    return {
        "batch_id": _clean_text(source.get("batch_id") or source.get("id"), 120),
        "run_id": _clean_text(source.get("run_id"), 120),
        "type": normalize_run_type(source.get("type")),
        "status": _normalize_status(source.get("status"), RUNTIME_BATCH_STATUSES),
        "offset": _non_negative_int(source.get("offset")),
        "limit": _non_negative_int(source.get("limit")),
        "total": _non_negative_int(source.get("total")),
        "completed": _non_negative_int(source.get("completed")),
        "failed": _non_negative_int(source.get("failed")),
        "skipped": _non_negative_int(source.get("skipped")),
        "lease_owner": _clean_text(source.get("lease_owner"), 160),
        "lease_expires_at": _clean_text(source.get("lease_expires_at")),
        "message": _clean_text(source.get("message")),
        "created_at": _clean_text(source.get("created_at")),
        "started_at": _clean_text(source.get("started_at")),
        "updated_at": updated_at,
        "completed_at": _clean_text(source.get("completed_at")),
    }


def normalize_failure(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    updated_at = _clean_text(source.get("updated_at")) or china_now_iso()
    platform = normalize_makerworld_source(source.get("platform"), source.get("model_url"))

    return {
        "failure_id": _clean_text(source.get("failure_id") or source.get("id"), 120),
        "run_id": _clean_text(source.get("run_id"), 120),
        "batch_id": _clean_text(source.get("batch_id"), 120),
        "type": normalize_run_type(source.get("type")),
        "platform": platform,
        "model_id": _clean_text(source.get("model_id"), 120),
        "model_url": _clean_text(source.get("model_url"), 1000),
        "instance_id": _clean_text(source.get("instance_id"), 160),
        "title": _clean_text(source.get("title"), 240),
        "status": _normalize_status(source.get("status"), RUNTIME_FAILURE_STATUSES, default="failed"),
        "message": _clean_text(source.get("message")),
        "retryable": bool(source.get("retryable")),
        "retry_count": _non_negative_int(source.get("retry_count")),
        "last_attempt_at": _clean_text(source.get("last_attempt_at")),
        "updated_at": updated_at,
    }
