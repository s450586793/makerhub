from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.services import state_contracts
from app.services.runtime_engine import store


def _items(payload: Any, key: str = "items") -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get(key), list):
        return [item for item in payload.get(key) or [] if isinstance(item, dict)]
    return []


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preview_migration(legacy: dict[str, Any]) -> dict[str, Any]:
    archive_queue = legacy.get("archive_queue") if isinstance(legacy.get("archive_queue"), dict) else {}
    missing_3mf = legacy.get("missing_3mf") if isinstance(legacy.get("missing_3mf"), dict) else {}
    remote_refresh = legacy.get("remote_refresh_state") if isinstance(legacy.get("remote_refresh_state"), dict) else {}
    source_runs = legacy.get("source_refresh_runs") if isinstance(legacy.get("source_refresh_runs"), dict) else {}
    subscriptions = legacy.get("subscriptions_state") if isinstance(legacy.get("subscriptions_state"), dict) else {}
    return {
        "archive_queued": len(_items(archive_queue, "queued")),
        "archive_active": len(_items(archive_queue, "active")),
        "legacy_failures": len(_items(archive_queue, "recent_failures")),
        "missing_3mf": len(_items(missing_3mf)),
        "remote_refresh_active": str(remote_refresh.get("status") or "").lower()
        in {"running", "resuming", "interrupted"},
        "source_refresh_active": bool(source_runs.get("active_run")),
        "subscription_active": sum(1 for item in _items(subscriptions) if str(item.get("status") or "").lower() == "running"),
    }


def load_migration_state() -> dict[str, Any]:
    payload = load_database_json_state(state_contracts.RUNTIME_MIGRATION_STATE_KEY, {})
    return payload if isinstance(payload, dict) else {}


def save_migration_state(payload: dict[str, Any]) -> dict[str, Any]:
    return save_database_json_state(state_contracts.RUNTIME_MIGRATION_STATE_KEY, payload)


def _submit_archive_migration_run(item: dict[str, Any]) -> None:
    store.upsert_run(
        {
            "run_id": str(item.get("id") or item.get("task_id") or item.get("url") or ""),
            "type": "archive",
            "source_url": item.get("url") or item.get("model_url") or "",
            "platform": item.get("platform") or item.get("source") or "",
            "status": "queued",
            "message": "由旧归档队列迁移。",
        }
    )


def apply_migration(legacy: dict[str, Any]) -> dict[str, Any]:
    digest = _digest(legacy)
    state = load_migration_state()
    preview = preview_migration(legacy)
    if state.get("legacy_digest") == digest and state.get("applied"):
        return {"success": True, "applied": False, "message": "迁移已应用。", "preview": preview}

    archive_queue = legacy.get("archive_queue") if isinstance(legacy.get("archive_queue"), dict) else {}
    for item in [*_items(archive_queue, "active"), *_items(archive_queue, "queued")]:
        _submit_archive_migration_run(item)

    missing_3mf = legacy.get("missing_3mf") if isinstance(legacy.get("missing_3mf"), dict) else {}
    for item in _items(missing_3mf):
        store.append_failure(
            {
                "failure_id": f"missing-3mf-{item.get('model_id')}-{item.get('instance_id')}",
                "type": "missing_3mf_retry",
                "platform": item.get("source") or "",
                "model_id": item.get("model_id") or "",
                "model_url": item.get("model_url") or "",
                "instance_id": item.get("instance_id") or "",
                "title": item.get("title") or "",
                "status": item.get("status") or "missing_3mf",
                "message": item.get("message") or "旧缺失 3MF 记录迁移。",
                "retryable": True,
            }
        )

    save_migration_state({"legacy_digest": digest, "applied": True, "preview": preview})
    return {"success": True, "applied": True, "message": "旧运行状态迁移完成。", "preview": preview}
