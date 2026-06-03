from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.core.timezone import now as china_now, now_iso as china_now_iso, parse_datetime


RUNTIME_TASK_STATUSES = frozenset(
    {
        "queued",
        "running",
        "waiting_children",
        "paused",
        "blocked",
        "failed",
        "completed",
    }
)

BLOCKED_REASONS = frozenset(
    {
        "needs_cookie",
        "needs_verification",
        "rate_limited",
        "source_unavailable",
        "worker_stopped",
    }
)

DEFAULT_LEASE_SECONDS = 30 * 60
DEFAULT_MAX_ATTEMPTS = 3


def normalize_runtime_status(value: Any, default: str = "queued") -> str:
    status = str(value or "").strip().lower()
    return status if status in RUNTIME_TASK_STATUSES else default


def normalize_blocked_reason(value: Any) -> str:
    reason = str(value or "").strip().lower()
    return reason if reason in BLOCKED_REASONS else ""


def runtime_now_iso() -> str:
    return china_now_iso()


def lease_expiry_from_now(seconds: int = DEFAULT_LEASE_SECONDS) -> str:
    return (china_now() + timedelta(seconds=max(int(seconds or 0), 1))).isoformat(timespec="seconds")


def is_lease_expired(value: Any) -> bool:
    parsed = parse_datetime(str(value or "").strip())
    if parsed is None:
        return True
    return parsed <= china_now()


def _parse_attempt_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def task_attempt_count(item: dict[str, Any]) -> int:
    attempt_count = _parse_attempt_count(item.get("attempt_count"))
    if attempt_count is not None:
        return attempt_count
    return _parse_attempt_count(item.get("attempts")) or 0


def task_attempts_remaining(item: dict[str, Any], max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    return task_attempt_count(item) < max(int(max_attempts or 0), 1)
