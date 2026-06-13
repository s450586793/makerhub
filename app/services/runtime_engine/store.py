from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.database import append_state_event
from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import STATE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services import state_contracts
from app.services.runtime_engine.contracts import (
    normalize_batch_summary,
    normalize_failure,
    normalize_run_summary,
)


DEFAULT_SNAPSHOTS = {
    "dashboard": {"active_runs": [], "active_batches": [], "summary": {}, "updated_at": ""},
    "tasks": {"runs": [], "batches": [], "failures": [], "updated_at": ""},
    "source_refresh": {"active_runs": [], "recent_runs": [], "updated_at": ""},
    "subscriptions": {"active_runs": [], "recent_runs": [], "updated_at": ""},
}
RUNTIME_BATCH_ITEM_DIR = STATE_DIR / "runtime_engine" / "batches"


def _load_items(key: str) -> dict[str, Any]:
    payload = load_database_json_state(key, {"items": [], "updated_at": ""})
    if not isinstance(payload, dict):
        payload = {"items": [], "updated_at": ""}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return {"items": items, "updated_at": str(payload.get("updated_at") or "")}


def _save_items(key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {"items": items, "updated_at": china_now_iso()}
    return save_database_json_state(key, payload)


def load_runs() -> dict[str, Any]:
    payload = _load_items(state_contracts.RUNTIME_RUNS_STATE_KEY)
    payload["items"] = [normalize_run_summary(item) for item in payload["items"] if isinstance(item, dict)]
    return payload


def load_batches() -> dict[str, Any]:
    payload = _load_items(state_contracts.RUNTIME_BATCHES_STATE_KEY)
    payload["items"] = [normalize_batch_summary(item) for item in payload["items"] if isinstance(item, dict)]
    return payload


def load_failures() -> dict[str, Any]:
    payload = _load_items(state_contracts.RUNTIME_FAILURES_STATE_KEY)
    payload["items"] = [normalize_failure(item) for item in payload["items"] if isinstance(item, dict)]
    return payload


def load_snapshots() -> dict[str, Any]:
    payload = load_database_json_state(state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY, dict(DEFAULT_SNAPSHOTS))
    if not isinstance(payload, dict):
        payload = {}
    merged = dict(DEFAULT_SNAPSHOTS)
    for key, value in payload.items():
        if isinstance(value, dict):
            merged[str(key)] = value
    return merged


def load_runtime_state() -> dict[str, Any]:
    return {
        "runs": load_runs(),
        "batches": load_batches(),
        "failures": load_failures(),
        "snapshots": load_snapshots(),
    }


def _publish(event_type: str, payload: dict[str, Any]) -> None:
    if not event_type:
        return
    append_state_event(event_type, "runtime", payload)


def upsert_run(run: dict[str, Any], *, event_type: str = "") -> dict[str, Any]:
    normalized = normalize_run_summary(run)
    payload = load_runs()
    items = [item for item in payload["items"] if item.get("run_id") != normalized["run_id"]]
    items.insert(0, normalized)
    _save_items(state_contracts.RUNTIME_RUNS_STATE_KEY, items[:500])
    _publish(event_type, {"run_id": normalized["run_id"], "status": normalized["status"], "type": normalized["type"]})
    return normalized


def upsert_batch(batch: dict[str, Any], *, event_type: str = "") -> dict[str, Any]:
    normalized = normalize_batch_summary(batch)
    payload = load_batches()
    items = [item for item in payload["items"] if item.get("batch_id") != normalized["batch_id"]]
    items.insert(0, normalized)
    _save_items(state_contracts.RUNTIME_BATCHES_STATE_KEY, items[:1000])
    _publish(
        event_type,
        {"batch_id": normalized["batch_id"], "run_id": normalized["run_id"], "status": normalized["status"]},
    )
    return normalized


def append_failure(failure: dict[str, Any], *, event_type: str = "runtime.failure.created") -> dict[str, Any]:
    normalized = normalize_failure(failure)
    payload = load_failures()
    items = [item for item in payload["items"] if item.get("failure_id") != normalized["failure_id"]]
    items.insert(0, normalized)
    _save_items(state_contracts.RUNTIME_FAILURES_STATE_KEY, items[:5000])
    _publish(
        event_type,
        {"failure_id": normalized["failure_id"], "run_id": normalized["run_id"], "status": normalized["status"]},
    )
    return normalized


def save_snapshot(name: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    clean_name = str(name or "").strip() or "dashboard"
    snapshots = load_snapshots()
    snapshots[clean_name] = {**(snapshot or {}), "updated_at": china_now_iso()}
    return save_database_json_state(state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY, snapshots)


def _batch_items_path(batch_id: str) -> Path:
    clean = "".join(ch for ch in str(batch_id or "") if ch.isalnum() or ch in {"-", "_"})[:160]
    clean = clean or "batch"
    return RUNTIME_BATCH_ITEM_DIR / f"{clean}.jsonl"


def save_batch_items(batch_id: str, items: list[dict[str, Any]]) -> Path:
    RUNTIME_BATCH_ITEM_DIR.mkdir(parents=True, exist_ok=True)
    path = _batch_items_path(batch_id)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            if isinstance(item, dict):
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
                handle.write("\n")
    return path


def load_batch_items(batch_id: str) -> list[dict[str, Any]]:
    path = _batch_items_path(batch_id)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                items.append(item)
    return items


def delete_batch_items(batch_id: str) -> bool:
    path = _batch_items_path(batch_id)
    if not path.exists():
        return False
    path.unlink()
    return True
