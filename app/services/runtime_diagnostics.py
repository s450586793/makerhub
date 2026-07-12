from __future__ import annotations

from datetime import datetime
from typing import Any

from app.api.dependencies import task_state_store
from app.core.database import database_connection, database_status
from app.core.timezone import now_iso as china_now_iso
from app.services.account_health import load_account_health
from app.services.task_runtime import is_lease_expired, normalize_runtime_status


def _safe_account_health() -> dict[str, Any]:
    try:
        return load_account_health()
    except Exception:
        return {
            "cn": {
                "platform": "cn",
                "status": "unknown",
                "reason": "",
                "source": "system",
                "detail": "",
                "model_url": "",
                "model_id": "",
                "instance_id": "",
                "updated_at": "",
            },
            "global": {
                "platform": "global",
                "status": "unknown",
                "reason": "",
                "source": "system",
                "detail": "",
                "model_url": "",
                "model_id": "",
                "instance_id": "",
                "updated_at": "",
            },
        }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value or "")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fetch_table_stats(connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            relname,
            pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
            n_live_tup AS rows,
            n_dead_tup AS dead_rows
        FROM pg_stat_user_tables
        WHERE relname IN (
            'makerhub_json_state',
            'makerhub_logs',
            'makerhub_state_events',
            'archive_model_index'
        )
        ORDER BY pg_total_relation_size(relid) DESC
        """
    ).fetchall()
    return [
        {
            "name": str(row.get("relname") or ""),
            "size": str(row.get("total_size") or ""),
            "rows_estimate": _int(row.get("rows")),
            "dead_rows_estimate": _int(row.get("dead_rows")),
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


def _fetch_state_event_stats(connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT scope, count(*) AS rows, max(created_at) AS newest
        FROM makerhub_state_events
        GROUP BY scope
        ORDER BY rows DESC
        LIMIT 20
        """
    ).fetchall()
    return [
        {
            "scope": str(row.get("scope") or ""),
            "rows": _int(row.get("rows")),
            "newest": _iso(row.get("newest")),
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


def _fetch_recent_log_stats(connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT file_name, category, event, level, count(*) AS rows, max(created_at) AS newest
        FROM makerhub_logs
        WHERE created_at > now() - interval '24 hours'
        GROUP BY file_name, category, event, level
        ORDER BY rows DESC
        LIMIT 30
        """
    ).fetchall()
    return [
        {
            "file_name": str(row.get("file_name") or ""),
            "category": str(row.get("category") or ""),
            "event": str(row.get("event") or ""),
            "level": str(row.get("level") or ""),
            "rows": _int(row.get("rows")),
            "newest": _iso(row.get("newest")),
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


def _fetch_json_state_stats(connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT key, jsonb_typeof(value) AS type, updated_at
        FROM makerhub_json_state
        ORDER BY updated_at DESC
        LIMIT 40
        """
    ).fetchall()
    return [
        {
            "key": str(row.get("key") or ""),
            "type": str(row.get("type") or ""),
            "updated_at": _iso(row.get("updated_at")),
        }
        for row in rows or []
        if isinstance(row, dict)
    ]


def _safe_task_preview(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or "")[:160],
        "status": normalize_runtime_status(item.get("status"), "queued"),
        "updated_at": _iso(item.get("updated_at")),
        "lease_expires_at": _iso(item.get("lease_expires_at")),
    }


def _archive_queue_diagnostics() -> dict[str, Any]:
    try:
        queue = task_state_store.load_archive_queue()
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "running_count": 0,
            "queued_count": 0,
            "failed_count": 0,
            "waiting_children_count": 0,
            "stale_candidates": [],
        }

    active = [item for item in queue.get("active") or [] if isinstance(item, dict)]
    queued = [item for item in queue.get("queued") or [] if isinstance(item, dict)]
    recent_failures = [item for item in queue.get("recent_failures") or [] if isinstance(item, dict)]
    stale = [
        _safe_task_preview(item)
        for item in active
        if normalize_runtime_status(item.get("status"), "running") == "running"
        and is_lease_expired(item.get("lease_expires_at"))
    ][:20]

    return {
        "available": True,
        "running_count": _int(queue.get("running_count") or len(active)),
        "queued_count": _int(queue.get("queued_count") or len(queued)),
        "failed_count": _int(queue.get("failed_count") or len(recent_failures)),
        "waiting_children_count": sum(
            1
            for item in active
            if normalize_runtime_status(item.get("status"), "running") == "waiting_children"
        ),
        "stale_candidates": stale,
    }


def build_runtime_diagnostics() -> dict[str, Any]:
    status = database_status()
    payload: dict[str, Any] = {
        "generated_at": china_now_iso(),
        "runtime_engine": {
            "enabled": False,
            "writable": False,
            "reason": "运行核心已冻结，本版本仅保留只读诊断。",
        },
        "database": status,
        "account_health": _safe_account_health(),
        "tables": [],
        "state_events_by_scope": [],
        "recent_logs": [],
        "json_states": [],
        "archive_queue": _archive_queue_diagnostics(),
    }
    if not bool(status.get("available")):
        return payload

    try:
        with database_connection() as connection:
            payload["tables"] = _fetch_table_stats(connection)
            payload["state_events_by_scope"] = _fetch_state_event_stats(connection)
            payload["recent_logs"] = _fetch_recent_log_stats(connection)
            payload["json_states"] = _fetch_json_state_stats(connection)
    except Exception as exc:
        payload["database"] = {**status, "available": False, "error": str(exc)}
        payload["tables"] = []
        payload["state_events_by_scope"] = []
        payload["recent_logs"] = []
        payload["json_states"] = []
    return payload
