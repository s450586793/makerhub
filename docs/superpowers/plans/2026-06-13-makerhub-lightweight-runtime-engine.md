# MakerHub Lightweight Runtime Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MakerHub's mixed model-level runtime queues with a unified batch-oriented runtime engine for archive, subscription sync, source refresh, and missing `3MF` retry, then verify the main online flows still work.

**Architecture:** Build a new `app/services/runtime_engine/` boundary behind a feature flag and compatibility APIs. Existing archive, subscription, source-refresh, and missing-3MF logic becomes adapter logic while durable runtime ownership moves to runs, batches, failures, and snapshots. Cut over one flow at a time, preserving a runnable application and old-compatible payloads until the frontend switches to runtime snapshots.

**Tech Stack:** Python 3, FastAPI, Postgres-backed JSON state, existing `TaskStateStore`, Vue 3 SPA, unittest, Node test runner.

---

## File Structure

Create:

- `app/services/runtime_engine/__init__.py`：public package exports.
- `app/services/runtime_engine/contracts.py`：runtime constants, allowed statuses, dataclass-style dict normalizers, ID helpers.
- `app/services/runtime_engine/store.py`：all Postgres JSON-state reads/writes for `runtime_runs`, `runtime_batches`, `runtime_failures`, `runtime_snapshots`, and `runtime_migration`; short-lived batch item JSONL files under `STATE_DIR/runtime_engine/batches/`.
- `app/services/runtime_engine/engine.py`：submit, plan, execute, repair, pause/resume/cancel orchestration.
- `app/services/runtime_engine/migration.py`：idempotent migration preview and apply logic for unfinished old states.
- `app/services/runtime_engine/adapters.py`：adapter protocol and initial no-op/test adapters.
- `app/services/runtime_engine/archive_adapter.py`：archive adapter wrapping existing archive submit/discovery/job logic.
- `app/services/runtime_engine/missing_3mf_adapter.py`：missing `3MF` retry adapter wrapping existing retry/download helpers.
- `app/services/runtime_engine/source_refresh_adapter.py`：source refresh adapter wrapping existing source refresh manager/core.
- `app/services/runtime_engine/subscription_adapter.py`：subscription adapter wrapping subscription discovery/sync.
- `app/api/runtime_routes.py`：new `/api/runtime*` routes.
- `frontend/src/lib/runtimeStatus.js`：frontend helpers for run/batch/failure labels and actions.
- `tests/test_runtime_engine_contracts.py`
- `tests/test_runtime_engine_store.py`
- `tests/test_runtime_engine_migration.py`
- `tests/test_runtime_engine_api.py`
- `tests/test_runtime_engine_archive_adapter.py`
- `tests/test_runtime_engine_missing_3mf_adapter.py`
- `tests/test_runtime_engine_source_refresh_adapter.py`
- `tests/test_runtime_engine_subscription_adapter.py`
- `frontend/src/lib/runtimeStatus.test.mjs`

Modify:

- `app/services/state_contracts.py`：add runtime state keys and status constants.
- `docs/modules/state_contracts.md`：document runtime state ownership and legacy freeze rules.
- `docs/MODULES.md`：add Runtime Engine module row and update cross-module guidance.
- `app/main.py`：include `runtime_routes`; start runtime engine only in worker when enabled.
- `app/worker.py`：run runtime engine worker loop behind feature flag.
- `app/api/tasks_routes.py`：return runtime snapshot-compatible task payload when enabled.
- `app/api/config.py`：route archive submit/preview compatibility through runtime engine when enabled.
- `app/api/remote_refresh_routes.py`：route source-refresh compatibility through runtime engine when enabled.
- `app/api/subscriptions_routes.py`：route subscription sync progress through runtime engine when enabled.
- `app/services/catalog.py`：read dashboard/task snapshots from runtime snapshots when enabled.
- `frontend/src/pages/TasksPage.vue`：batch-first task page when runtime snapshot is present.
- `frontend/src/pages/DashboardPage.vue`：prefer runtime dashboard snapshot fields.
- `frontend/src/pages/RemoteRefreshPage.vue`：prefer runtime source-refresh snapshot fields.
- `frontend/src/pages/SubscriptionsPage.vue` / `SubscriptionsManagePage.vue`：show sync progress from runtime snapshots when available.
- `README.md`, `VERSION`：only during release/push, per project rule.

Do not touch:

- `videos/makerhub-intro/output/`
- Browser verification/bypass code paths.
- Docker/compose deployment shape in this plan.

---

### Task 1: Runtime Contracts and State Keys

**Files:**
- Create: `app/services/runtime_engine/__init__.py`
- Create: `app/services/runtime_engine/contracts.py`
- Modify: `app/services/state_contracts.py`
- Modify: `docs/modules/state_contracts.md`
- Modify: `docs/MODULES.md`
- Test: `tests/test_runtime_engine_contracts.py`
- Test: `tests/test_state_contracts.py`

- [ ] **Step 1: Write failing contract tests**

Create `tests/test_runtime_engine_contracts.py`:

```python
import unittest

from app.services import state_contracts
from app.services.runtime_engine import contracts


class RuntimeEngineContractsTest(unittest.TestCase):
    def test_runtime_state_keys_are_registered(self):
        self.assertEqual(state_contracts.RUNTIME_RUNS_STATE_KEY, "runtime_runs")
        self.assertEqual(state_contracts.RUNTIME_BATCHES_STATE_KEY, "runtime_batches")
        self.assertEqual(state_contracts.RUNTIME_FAILURES_STATE_KEY, "runtime_failures")
        self.assertEqual(state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY, "runtime_snapshots")
        self.assertEqual(state_contracts.RUNTIME_MIGRATION_STATE_KEY, "runtime_migration")

    def test_normalize_run_summary_keeps_known_fields_and_defaults(self):
        run = contracts.normalize_run_summary(
            {
                "run_id": "run-1",
                "type": "archive",
                "status": "running",
                "total": "12",
                "completed": "3",
                "failed": 1,
                "extra": "ignored",
            }
        )

        self.assertEqual(run["run_id"], "run-1")
        self.assertEqual(run["type"], "archive")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["total"], 12)
        self.assertEqual(run["completed"], 3)
        self.assertEqual(run["failed"], 1)
        self.assertEqual(run["skipped"], 0)
        self.assertNotIn("extra", run)

    def test_normalize_batch_summary_rejects_unknown_status_to_queued(self):
        batch = contracts.normalize_batch_summary(
            {
                "batch_id": "batch-1",
                "run_id": "run-1",
                "type": "archive",
                "status": "mystery",
            }
        )

        self.assertEqual(batch["status"], "queued")
        self.assertEqual(batch["completed"], 0)
        self.assertEqual(batch["failed"], 0)

    def test_normalize_failure_keeps_retryable_failure_detail(self):
        failure = contracts.normalize_failure(
            {
                "failure_id": "failure-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "type": "archive",
                "platform": "global",
                "model_id": "123",
                "status": "verification_required",
                "message": "Needs verification",
                "retryable": True,
            }
        )

        self.assertEqual(failure["status"], "verification_required")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["model_id"], "123")
        self.assertEqual(failure["platform"], "global")

    def test_runtime_event_scopes_are_coarse(self):
        self.assertEqual(
            contracts.RUNTIME_EVENT_SCOPES,
            {
                "runtime.run.started",
                "runtime.batch.progress",
                "runtime.batch.completed",
                "runtime.run.completed",
                "runtime.run.blocked",
                "runtime.failure.created",
                "account_health.changed",
            },
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_contracts
```

Expected: fails because `app.services.runtime_engine` and runtime constants do not exist.

- [ ] **Step 3: Add runtime state constants**

In `app/services/state_contracts.py`, add:

```python
RUNTIME_RUNS_STATE_KEY = "runtime_runs"
RUNTIME_BATCHES_STATE_KEY = "runtime_batches"
RUNTIME_FAILURES_STATE_KEY = "runtime_failures"
RUNTIME_SNAPSHOTS_STATE_KEY = "runtime_snapshots"
RUNTIME_MIGRATION_STATE_KEY = "runtime_migration"

RUNTIME_STATE_KEYS = (
    RUNTIME_RUNS_STATE_KEY,
    RUNTIME_BATCHES_STATE_KEY,
    RUNTIME_FAILURES_STATE_KEY,
    RUNTIME_SNAPSHOTS_STATE_KEY,
    RUNTIME_MIGRATION_STATE_KEY,
)
```

- [ ] **Step 4: Create runtime engine package**

Create `app/services/runtime_engine/__init__.py`:

```python
from app.services.runtime_engine.contracts import (
    RUNTIME_BATCH_STATUSES,
    RUNTIME_EVENT_SCOPES,
    RUNTIME_FAILURE_STATUSES,
    RUNTIME_RUN_STATUSES,
    RUNTIME_RUN_TYPES,
    normalize_batch_summary,
    normalize_failure,
    normalize_run_summary,
)

__all__ = [
    "RUNTIME_BATCH_STATUSES",
    "RUNTIME_EVENT_SCOPES",
    "RUNTIME_FAILURE_STATUSES",
    "RUNTIME_RUN_STATUSES",
    "RUNTIME_RUN_TYPES",
    "normalize_batch_summary",
    "normalize_failure",
    "normalize_run_summary",
]
```

Create `app/services/runtime_engine/contracts.py`:

```python
from __future__ import annotations

from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.three_mf import normalize_makerworld_source


RUNTIME_RUN_TYPES = {"archive", "subscription_sync", "source_refresh", "missing_3mf_retry"}
RUNTIME_RUN_STATUSES = {
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
RUNTIME_BATCH_STATUSES = {
    "queued",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}
RUNTIME_FAILURE_STATUSES = {
    "failed",
    "skipped",
    "missing_3mf",
    "verification_required",
    "cookie_invalid",
    "daily_limit",
    "network_error",
    "not_found",
}
RUNTIME_EVENT_SCOPES = {
    "runtime.run.started",
    "runtime.batch.progress",
    "runtime.batch.completed",
    "runtime.run.completed",
    "runtime.run.blocked",
    "runtime.failure.created",
    "account_health.changed",
}


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _status(value: Any, allowed: set[str], fallback: str) -> str:
    clean = _text(value, 80).lower()
    return clean if clean in allowed else fallback


def normalize_run_type(value: Any) -> str:
    clean = _text(value, 80).lower()
    return clean if clean in RUNTIME_RUN_TYPES else "archive"


def normalize_run_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload or {}
    run_type = normalize_run_type(raw.get("type"))
    return {
        "run_id": _text(raw.get("run_id") or raw.get("id"), 120),
        "type": run_type,
        "source_url": _text(raw.get("source_url") or raw.get("url"), 1000),
        "source_id": _text(raw.get("source_id"), 240),
        "platform": normalize_makerworld_source(raw.get("platform"), raw.get("source_url") or raw.get("url")) or "",
        "status": _status(raw.get("status"), RUNTIME_RUN_STATUSES, "queued"),
        "total": _int(raw.get("total")),
        "completed": _int(raw.get("completed")),
        "failed": _int(raw.get("failed")),
        "skipped": _int(raw.get("skipped")),
        "missing_3mf": _int(raw.get("missing_3mf")),
        "current_batch_id": _text(raw.get("current_batch_id"), 120),
        "message": _text(raw.get("message"), 500),
        "created_at": _text(raw.get("created_at")),
        "started_at": _text(raw.get("started_at")),
        "updated_at": _text(raw.get("updated_at") or china_now_iso()),
        "completed_at": _text(raw.get("completed_at")),
    }


def normalize_batch_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload or {}
    return {
        "batch_id": _text(raw.get("batch_id") or raw.get("id"), 120),
        "run_id": _text(raw.get("run_id"), 120),
        "type": normalize_run_type(raw.get("type")),
        "status": _status(raw.get("status"), RUNTIME_BATCH_STATUSES, "queued"),
        "offset": _int(raw.get("offset")),
        "limit": _int(raw.get("limit")),
        "total": _int(raw.get("total")),
        "completed": _int(raw.get("completed")),
        "failed": _int(raw.get("failed")),
        "skipped": _int(raw.get("skipped")),
        "lease_owner": _text(raw.get("lease_owner"), 160),
        "lease_expires_at": _text(raw.get("lease_expires_at")),
        "message": _text(raw.get("message"), 500),
        "created_at": _text(raw.get("created_at")),
        "started_at": _text(raw.get("started_at")),
        "updated_at": _text(raw.get("updated_at") or china_now_iso()),
        "completed_at": _text(raw.get("completed_at")),
    }


def normalize_failure(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload or {}
    status = _status(raw.get("status"), RUNTIME_FAILURE_STATUSES, "failed")
    return {
        "failure_id": _text(raw.get("failure_id") or raw.get("id"), 120),
        "run_id": _text(raw.get("run_id"), 120),
        "batch_id": _text(raw.get("batch_id"), 120),
        "type": normalize_run_type(raw.get("type")),
        "platform": normalize_makerworld_source(raw.get("platform"), raw.get("model_url")) or "",
        "model_id": _text(raw.get("model_id"), 120),
        "model_url": _text(raw.get("model_url"), 1000),
        "instance_id": _text(raw.get("instance_id"), 160),
        "title": _text(raw.get("title"), 240),
        "status": status,
        "message": _text(raw.get("message"), 500),
        "retryable": bool(raw.get("retryable")),
        "retry_count": _int(raw.get("retry_count")),
        "last_attempt_at": _text(raw.get("last_attempt_at")),
        "updated_at": _text(raw.get("updated_at") or china_now_iso()),
    }
```

- [ ] **Step 5: Update docs**

In `docs/modules/state_contracts.md`, add state-key rows for:

```markdown
| `runtime_runs` | `runtime_engine` | runtime engine | dashboard, tasks page, diagnostics | `runtime` |
| `runtime_batches` | `runtime_engine` | runtime engine | dashboard, tasks page, diagnostics | `runtime` |
| `runtime_failures` | `runtime_engine` | runtime engine | tasks page, retry APIs, diagnostics | `runtime` |
| `runtime_snapshots` | `runtime_engine` | runtime engine | dashboard, tasks/source/subscription pages | `dashboard`, `runtime` |
| `runtime_migration` | `runtime_engine` | runtime migration | diagnostics, repair tools | none |
```

Add runtime statuses:

```markdown
| Runtime runs | `queued`, `discovering`, `planned`, `running`, `paused`, `blocked`, `completed`, `failed`, `cancelled`, `interrupted` |
| Runtime batches | `queued`, `running`, `paused`, `blocked`, `completed`, `failed`, `cancelled`, `interrupted` |
| Runtime failures | `failed`, `skipped`, `missing_3mf`, `verification_required`, `cookie_invalid`, `daily_limit`, `network_error`, `not_found` |
```

In `docs/MODULES.md`, add a module row:

```markdown
| Runtime Engine | [state_contracts.md](modules/state_contracts.md) | Unified run/batch/failure/snapshot ownership for archive, subscription sync, source refresh, and missing 3MF retry | `app/services/runtime_engine/*`, `app/api/runtime_routes.py`, runtime-aware page helpers | `test_runtime_engine_*`, `test_state_contracts.py` |
```

- [ ] **Step 6: Run contract tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_contracts tests.test_state_contracts
```

Expected: all pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/services/runtime_engine/__init__.py app/services/runtime_engine/contracts.py app/services/state_contracts.py docs/modules/state_contracts.md docs/MODULES.md tests/test_runtime_engine_contracts.py
git commit -m "feat: 定义统一运行核心契约"
```

---

### Task 2: Runtime Store and Snapshots

**Files:**
- Create: `app/services/runtime_engine/store.py`
- Test: `tests/test_runtime_engine_store.py`

- [ ] **Step 1: Write failing store tests**

Create `tests/test_runtime_engine_store.py`:

```python
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services.runtime_engine import store
from app.services import state_contracts


class RuntimeEngineStoreTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.load_patch = patch.object(
            store,
            "load_database_json_state",
            side_effect=lambda key, default: self.state.get(key, default),
        )
        self.save_patch = patch.object(
            store,
            "save_database_json_state",
            side_effect=lambda key, value: self.state.__setitem__(key, value) or value,
        )
        self.event_calls = []
        self.event_patch = patch.object(
            store,
            "append_state_event",
            side_effect=lambda scope, event_type, payload: self.event_calls.append((scope, event_type, payload)),
        )
        self.load_patch.start()
        self.save_patch.start()
        self.event_patch.start()

    def tearDown(self):
        self.event_patch.stop()
        self.save_patch.stop()
        self.load_patch.stop()

    def test_load_defaults_are_bounded_and_normalized(self):
        payload = store.load_runtime_state()

        self.assertEqual(payload["runs"]["items"], [])
        self.assertEqual(payload["batches"]["items"], [])
        self.assertEqual(payload["failures"]["items"], [])
        self.assertIn("dashboard", payload["snapshots"])

    def test_upsert_run_saves_normalized_run_and_publishes_event(self):
        run = store.upsert_run(
            {
                "run_id": "run-1",
                "type": "archive",
                "status": "running",
                "total": "3",
                "message": "Running",
            },
            event_type="runtime.run.started",
        )

        self.assertEqual(run["total"], 3)
        saved = self.state[state_contracts.RUNTIME_RUNS_STATE_KEY]
        self.assertEqual(saved["items"][0]["run_id"], "run-1")
        self.assertEqual(self.event_calls[-1][0], "runtime")
        self.assertEqual(self.event_calls[-1][1], "runtime.run.started")

    def test_upsert_batch_replaces_existing_batch(self):
        store.upsert_batch({"batch_id": "batch-1", "run_id": "run-1", "status": "queued"})
        store.upsert_batch({"batch_id": "batch-1", "run_id": "run-1", "status": "running", "completed": 2})

        saved = self.state[state_contracts.RUNTIME_BATCHES_STATE_KEY]["items"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["status"], "running")
        self.assertEqual(saved[0]["completed"], 2)

    def test_append_failure_keeps_failure_durable_and_retryable(self):
        failure = store.append_failure(
            {
                "failure_id": "failure-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "type": "archive",
                "status": "missing_3mf",
                "retryable": True,
            }
        )

        self.assertTrue(failure["retryable"])
        saved = self.state[state_contracts.RUNTIME_FAILURES_STATE_KEY]["items"]
        self.assertEqual(saved[0]["failure_id"], "failure-1")

    def test_save_snapshot_updates_named_snapshot_only(self):
        store.save_snapshot("dashboard", {"active_runs": [{"run_id": "run-1"}]})
        store.save_snapshot("tasks", {"runs": []})

        snapshots = self.state[state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY]
        self.assertEqual(snapshots["dashboard"]["active_runs"][0]["run_id"], "run-1")
        self.assertEqual(snapshots["tasks"]["runs"], [])

    def test_batch_item_temp_file_round_trips_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(store, "RUNTIME_BATCH_ITEM_DIR", Path(tmpdir)):
                store.save_batch_items("batch-1", [{"model_id": "1"}, {"model_id": "2"}])

                self.assertEqual(store.load_batch_items("batch-1"), [{"model_id": "1"}, {"model_id": "2"}])
                self.assertTrue(store.delete_batch_items("batch-1"))
                self.assertEqual(store.load_batch_items("batch-1"), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_store
```

Expected: fails because `runtime_engine.store` does not exist.

- [ ] **Step 3: Implement store module**

Create `app/services/runtime_engine/store.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.database import append_state_event
from app.core.database_json_state import load_database_json_state, save_database_json_state
from app.core.settings import STATE_DIR
from app.core.timezone import now_iso as china_now_iso
from app.services import state_contracts
from app.services.runtime_engine.contracts import normalize_batch_summary, normalize_failure, normalize_run_summary


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
    append_state_event("runtime", event_type, payload)


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
    _publish(event_type, {"batch_id": normalized["batch_id"], "run_id": normalized["run_id"], "status": normalized["status"]})
    return normalized


def append_failure(failure: dict[str, Any], *, event_type: str = "runtime.failure.created") -> dict[str, Any]:
    normalized = normalize_failure(failure)
    payload = load_failures()
    items = [item for item in payload["items"] if item.get("failure_id") != normalized["failure_id"]]
    items.insert(0, normalized)
    _save_items(state_contracts.RUNTIME_FAILURES_STATE_KEY, items[:5000])
    _publish(event_type, {"failure_id": normalized["failure_id"], "run_id": normalized["run_id"], "status": normalized["status"]})
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
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            if isinstance(item, dict):
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
                fh.write("\n")
    return path


def load_batch_items(batch_id: str) -> list[dict[str, Any]]:
    path = _batch_items_path(batch_id)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
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
```

- [ ] **Step 4: Run store tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_store
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/services/runtime_engine/store.py tests/test_runtime_engine_store.py
git commit -m "feat: 增加运行核心状态存储"
```

---

### Task 3: Runtime Engine Skeleton and Repair

**Files:**
- Create: `app/services/runtime_engine/adapters.py`
- Create: `app/services/runtime_engine/engine.py`
- Test: `tests/test_runtime_engine_store.py`
- Test: `tests/test_runtime_engine_api.py`

- [ ] **Step 1: Write failing engine tests**

Create `tests/test_runtime_engine_api.py` with engine-level tests first:

```python
import unittest
from unittest.mock import patch

from app.services.runtime_engine import engine


class _FakeAdapter:
    def discover(self, context):
        return [{"model_id": "1"}, {"model_id": "2"}, {"model_id": "3"}]

    def plan(self, candidates, limits):
        return [
            {"items": candidates[:2], "offset": 0, "limit": 2},
            {"items": candidates[2:], "offset": 2, "limit": 2},
        ]


class RuntimeEngineSkeletonTest(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.batches = []
        self.snapshots = {}
        self.store_patches = [
            patch.object(engine.store, "upsert_run", side_effect=lambda run, **kwargs: self.runs.append(run) or run),
            patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: self.batches.append(batch) or batch),
            patch.object(engine.store, "save_batch_items", side_effect=lambda batch_id, items: None),
            patch.object(engine.store, "save_snapshot", side_effect=lambda name, snapshot: self.snapshots.__setitem__(name, snapshot) or self.snapshots),
        ]
        for item in self.store_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.store_patches):
            item.stop()

    def test_submit_run_discovers_and_plans_bounded_batches(self):
        runtime = engine.RuntimeEngine(adapters={"archive": _FakeAdapter()}, batch_size=2)

        result = runtime.submit_run("archive", {"source_url": "https://makerworld.com/zh/models/1"})

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(self.batches), 2)
        self.assertEqual(self.batches[0]["total"], 2)
        self.assertEqual(self.batches[1]["total"], 1)
        self.assertIn("tasks", self.snapshots)

    def test_repair_regenerates_snapshots_without_adapter(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)

        with patch.object(engine.store, "load_runtime_state", return_value={
            "runs": {"items": [{"run_id": "run-1", "type": "archive", "status": "running"}]},
            "batches": {"items": [{"batch_id": "batch-1", "run_id": "run-1", "status": "queued"}]},
            "failures": {"items": []},
            "snapshots": {},
        }):
            result = runtime.repair()

        self.assertTrue(result["success"])
        self.assertIn("tasks", self.snapshots)

    def test_set_run_status_updates_existing_run(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)

        with patch.object(engine.store, "load_runs", return_value={"items": [{"run_id": "run-1", "type": "archive", "status": "running"}]}):
            result = runtime.set_run_status("run-1", "paused")

        self.assertEqual(result["status"], "paused")

    def test_retry_failures_submits_missing_3mf_retry_context(self):
        runtime = engine.RuntimeEngine(adapters={"missing_3mf_retry": _FakeAdapter()}, batch_size=2)

        with patch.object(engine.store, "load_failures", return_value={"items": [{"failure_id": "failure-1", "type": "missing_3mf_retry", "status": "missing_3mf"}]}):
            result = runtime.retry_failures({"failure_ids": ["failure-1"]})

        self.assertEqual(result["type"], "missing_3mf_retry")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api.RuntimeEngineSkeletonTest
```

Expected: fails because `runtime_engine.engine` does not exist.

- [ ] **Step 3: Create adapter protocol**

Create `app/services/runtime_engine/adapters.py`:

```python
from __future__ import annotations

from typing import Any, Protocol


class RuntimeAdapter(Protocol):
    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        ...

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        ...

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        ...

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        ...
```

- [ ] **Step 4: Create engine skeleton**

Create `app/services/runtime_engine/engine.py`:

```python
from __future__ import annotations

import hashlib
from typing import Any

from app.core.timezone import now_iso as china_now_iso
from app.services.runtime_engine import store
from app.services.runtime_engine.contracts import normalize_run_type


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("::".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


class RuntimeEngine:
    def __init__(self, *, adapters: dict[str, Any] | None = None, batch_size: int = 50) -> None:
        self.adapters = adapters or {}
        self.batch_size = max(1, min(int(batch_size or 50), 500))

    def submit_run(self, run_type: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_type = normalize_run_type(run_type)
        clean_context = dict(context or {})
        run_id = str(clean_context.get("run_id") or _stable_id("run", clean_type, clean_context.get("source_url"), china_now_iso()))
        adapter = self.adapters.get(clean_type)
        if adapter is None:
            run = store.upsert_run(
                {
                    "run_id": run_id,
                    "type": clean_type,
                    "status": "blocked",
                    "message": f"Runtime adapter not registered: {clean_type}",
                    "created_at": china_now_iso(),
                },
                event_type="runtime.run.blocked",
            )
            self.refresh_snapshots()
            return run

        run = store.upsert_run(
            {
                "run_id": run_id,
                "type": clean_type,
                "source_url": clean_context.get("source_url") or clean_context.get("url") or "",
                "source_id": clean_context.get("source_id") or "",
                "platform": clean_context.get("platform") or "",
                "status": "discovering",
                "created_at": china_now_iso(),
                "started_at": china_now_iso(),
                "message": "正在发现候选项。",
            },
            event_type="runtime.run.started",
        )
        candidates = adapter.discover({**clean_context, "run_id": run_id, "type": clean_type})
        batch_plans = adapter.plan(candidates, {"batch_size": self.batch_size})
        for index, plan in enumerate(batch_plans):
            items = list(plan.get("items") or [])
            batch_id = _stable_id("batch", run_id, index)
            store.save_batch_items(batch_id, items)
            store.upsert_batch(
                {
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "type": clean_type,
                    "status": "queued",
                    "offset": plan.get("offset") or index * self.batch_size,
                    "limit": plan.get("limit") or self.batch_size,
                    "total": len(items),
                    "message": "等待执行。",
                    "created_at": china_now_iso(),
                }
            )
        run = store.upsert_run(
            {
                **run,
                "status": "planned",
                "total": len(candidates),
                "message": f"已规划 {len(batch_plans)} 个批次。",
                "updated_at": china_now_iso(),
            }
        )
        self.refresh_snapshots()
        return run

    def refresh_snapshots(self) -> dict[str, Any]:
        state = store.load_runtime_state()
        runs = state["runs"]["items"]
        batches = state["batches"]["items"]
        failures = state["failures"]["items"]
        active_statuses = {"queued", "discovering", "planned", "running", "paused", "blocked", "interrupted"}
        active_runs = [run for run in runs if run.get("status") in active_statuses][:20]
        active_batches = [batch for batch in batches if batch.get("status") in active_statuses][:50]
        task_snapshot = {
            "runs": active_runs,
            "batches": active_batches,
            "failures": failures[:100],
        }
        dashboard_snapshot = {
            "active_runs": active_runs[:8],
            "active_batches": active_batches[:8],
            "summary": {
                "active_runs": len(active_runs),
                "active_batches": len(active_batches),
                "failures": len(failures),
            },
        }
        store.save_snapshot("tasks", task_snapshot)
        store.save_snapshot("dashboard", dashboard_snapshot)
        return {"tasks": task_snapshot, "dashboard": dashboard_snapshot}

    def repair(self) -> dict[str, Any]:
        snapshots = self.refresh_snapshots()
        return {"success": True, "message": "运行核心状态已修复。", "snapshots": snapshots}

    def set_run_status(self, run_id: str, status: str) -> dict[str, Any]:
        runs = store.load_runs()["items"]
        run = next((item for item in runs if item.get("run_id") == run_id), None)
        if not run:
            return {"success": False, "run_id": run_id, "status": "not_found", "message": "运行不存在。"}
        updated = store.upsert_run({**run, "status": status, "updated_at": china_now_iso()})
        self.refresh_snapshots()
        return updated

    def retry_failures(self, payload: dict[str, Any]) -> dict[str, Any]:
        failure_ids = {str(item) for item in payload.get("failure_ids") or []}
        failures = store.load_failures()["items"]
        selected = [item for item in failures if not failure_ids or item.get("failure_id") in failure_ids]
        context = {
            "failure_ids": [item.get("failure_id") for item in selected],
            "platform": payload.get("platform") or "",
            "status": payload.get("status") or "",
        }
        return self.submit_run("missing_3mf_retry", context)
```

- [ ] **Step 5: Run engine tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api.RuntimeEngineSkeletonTest
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/runtime_engine/adapters.py app/services/runtime_engine/engine.py tests/test_runtime_engine_api.py
git commit -m "feat: 增加运行核心调度骨架"
```

---

### Task 4: Runtime Migration Preview and Apply

**Files:**
- Create: `app/services/runtime_engine/migration.py`
- Test: `tests/test_runtime_engine_migration.py`

- [ ] **Step 1: Write failing migration tests**

Create `tests/test_runtime_engine_migration.py`:

```python
import unittest
from unittest.mock import patch

from app.services.runtime_engine import migration


class RuntimeEngineMigrationTest(unittest.TestCase):
    def test_preview_counts_unfinished_legacy_work(self):
        legacy = {
            "archive_queue": {
                "active": [{"id": "active-1", "url": "https://makerworld.com/zh/models/1", "status": "running"}],
                "queued": [{"id": "queued-1", "url": "https://makerworld.com/zh/models/2", "status": "queued"}],
                "recent_failures": [{"id": "failed-1", "url": "https://makerworld.com/zh/models/3", "status": "failed"}],
            },
            "missing_3mf": {
                "items": [{"model_id": "4", "model_url": "https://makerworld.com/zh/models/4", "status": "missing"}]
            },
            "remote_refresh_state": {"status": "running", "last_batch_total": 12},
            "source_refresh_runs": {"active_run": {"run_id": "src-1", "status": "running"}},
            "subscriptions_state": {"items": [{"id": "sub-1", "status": "running"}]},
        }

        preview = migration.preview_migration(legacy)

        self.assertEqual(preview["archive_queued"], 1)
        self.assertEqual(preview["archive_active"], 1)
        self.assertEqual(preview["legacy_failures"], 1)
        self.assertEqual(preview["missing_3mf"], 1)
        self.assertTrue(preview["source_refresh_active"])
        self.assertEqual(preview["subscription_active"], 1)

    def test_apply_migration_is_idempotent_by_digest(self):
        legacy = {
            "archive_queue": {"active": [], "queued": [{"id": "queued-1", "url": "https://makerworld.com/zh/models/2"}]},
            "missing_3mf": {"items": []},
        }
        saved_markers = {}
        submitted = []

        with patch.object(migration, "load_migration_state", side_effect=lambda: dict(saved_markers)), \
                patch.object(migration, "save_migration_state", side_effect=lambda value: saved_markers.update(value) or value), \
                patch.object(migration, "_submit_archive_migration_run", side_effect=lambda item: submitted.append(item)):
            first = migration.apply_migration(legacy)
            second = migration.apply_migration(legacy)

        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertEqual(len(submitted), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_migration
```

Expected: fails because `migration.py` does not exist.

- [ ] **Step 3: Implement migration module**

Create `app/services/runtime_engine/migration.py`:

```python
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
        "remote_refresh_active": str(remote_refresh.get("status") or "").lower() in {"running", "resuming", "interrupted"},
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
    if state.get("legacy_digest") == digest and state.get("applied"):
        return {"success": True, "applied": False, "message": "迁移已应用。", "preview": preview_migration(legacy)}

    archive_queue = legacy.get("archive_queue") if isinstance(legacy.get("archive_queue"), dict) else {}
    for item in [*_items(archive_queue, "active"), *_items(archive_queue, "queued")]:
        _submit_archive_migration_run(item)

    for item in _items(legacy.get("missing_3mf") if isinstance(legacy.get("missing_3mf"), dict) else {}):
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

    save_migration_state({"legacy_digest": digest, "applied": True, "preview": preview_migration(legacy)})
    return {"success": True, "applied": True, "message": "旧运行状态迁移完成。", "preview": preview_migration(legacy)}
```

- [ ] **Step 4: Run migration tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_migration
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add app/services/runtime_engine/migration.py tests/test_runtime_engine_migration.py
git commit -m "feat: 增加运行核心迁移预览"
```

---

### Task 5: Runtime API Routes

**Files:**
- Create: `app/api/runtime_routes.py`
- Modify: `app/main.py`
- Test: `tests/test_runtime_engine_api.py`
- Test: `tests/test_web_routes.py`
- Test: `tests/test_auth_guard.py`

- [ ] **Step 1: Append failing route tests**

Append to `tests/test_runtime_engine_api.py`:

```python
import asyncio
from types import SimpleNamespace

from app.api import runtime_routes


class RuntimeEngineRouteTest(unittest.TestCase):
    def _request(self):
        return SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

    def test_get_runtime_requires_session_and_returns_snapshot(self):
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.runtime_engine, "repair", return_value={"success": True, "snapshots": {"tasks": {"runs": []}}}):
            payload = asyncio.run(runtime_routes.get_runtime(self._request()))

        require_auth.assert_called_once()
        self.assertTrue(payload["success"])

    def test_submit_runtime_run_requires_session(self):
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.runtime_engine, "submit_run", return_value={"run_id": "run-1", "status": "planned"}) as submit:
            payload = asyncio.run(runtime_routes.submit_runtime_run({"type": "archive", "source_url": "https://makerworld.com/zh/models/1"}, self._request()))

        require_auth.assert_called_once()
        submit.assert_called_once()
        self.assertEqual(payload["run_id"], "run-1")

    def test_run_detail_and_failure_pages_are_session_only(self):
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes, "store") as store_mock:
            store_mock.load_runs.return_value = {"items": [{"run_id": "run-1", "status": "running"}]}
            store_mock.load_batches.return_value = {"items": [{"batch_id": "batch-1", "run_id": "run-1"}]}
            store_mock.load_failures.return_value = {"items": [{"failure_id": "failure-1", "run_id": "run-1"}]}

            detail = asyncio.run(runtime_routes.get_runtime_run("run-1", self._request()))
            failures = asyncio.run(runtime_routes.get_runtime_run_failures("run-1", self._request(), page=1, page_size=20))

        self.assertEqual(require_auth.call_count, 2)
        self.assertEqual(detail["run"]["run_id"], "run-1")
        self.assertEqual(failures["items"][0]["failure_id"], "failure-1")

    def test_pause_resume_cancel_and_failure_retry_require_session(self):
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.runtime_engine, "set_run_status", return_value={"run_id": "run-1", "status": "paused"}) as status_mock, \
                patch.object(runtime_routes.runtime_engine, "retry_failures", return_value={"run_id": "run-retry", "status": "planned"}) as retry_mock:
            pause = asyncio.run(runtime_routes.pause_runtime_run("run-1", self._request()))
            retry = asyncio.run(runtime_routes.retry_runtime_failures({"failure_ids": ["failure-1"]}, self._request()))

        self.assertEqual(require_auth.call_count, 2)
        status_mock.assert_called_once_with("run-1", "paused")
        retry_mock.assert_called_once()
        self.assertEqual(pause["status"], "paused")
        self.assertEqual(retry["run_id"], "run-retry")
```

Extend `tests/test_web_routes.py` route inventory assertion to include:

```python
"/api/runtime"
"/api/runtime/runs"
"/api/runtime/runs/{run_id}"
"/api/runtime/runs/{run_id}/failures"
"/api/runtime/failures/retry"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api.RuntimeEngineRouteTest tests.test_web_routes tests.test_auth_guard
```

Expected: fails because `runtime_routes` is missing or not mounted. If Python fails during module import with `cannot import name 'runtime_routes'`, that is the expected red state for this step.

- [ ] **Step 3: Create runtime API routes**

Create `app/api/runtime_routes.py`:

```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api import config as config_api
from app.services.runtime_engine import store
from app.services.runtime_engine.engine import RuntimeEngine
from app.services.request_threads import run_task_api, run_ui_io


router = APIRouter(prefix="/api")
runtime_engine = RuntimeEngine()


@router.get("/runtime")
async def get_runtime(request: Request):
    config_api._require_session_auth(request)
    return await run_ui_io(runtime_engine.repair)


@router.get("/runtime/runs")
async def get_runtime_runs(request: Request):
    config_api._require_session_auth(request)
    payload = await run_ui_io(runtime_engine.refresh_snapshots)
    return {"success": True, "runs": payload.get("tasks", {}).get("runs", [])}


@router.get("/runtime/runs/{run_id}")
async def get_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    runs = store.load_runs()["items"]
    batches = store.load_batches()["items"]
    failures = store.load_failures()["items"]
    run = next((item for item in runs if item.get("run_id") == run_id), None)
    return {
        "success": bool(run),
        "run": run,
        "batches": [item for item in batches if item.get("run_id") == run_id],
        "failure_count": sum(1 for item in failures if item.get("run_id") == run_id),
    }


@router.get("/runtime/runs/{run_id}/failures")
async def get_runtime_run_failures(run_id: str, request: Request, page: int = 1, page_size: int = 50):
    config_api._require_session_auth(request)
    clean_page = max(int(page or 1), 1)
    clean_size = max(1, min(int(page_size or 50), 200))
    failures = [item for item in store.load_failures()["items"] if item.get("run_id") == run_id]
    start = (clean_page - 1) * clean_size
    items = failures[start:start + clean_size]
    return {"success": True, "items": items, "page": clean_page, "page_size": clean_size, "total": len(failures)}


@router.post("/runtime/runs")
async def submit_runtime_run(payload: dict[str, Any], request: Request):
    config_api._require_session_auth(request)
    run_type = str(payload.get("type") or "archive")
    return await run_task_api(runtime_engine.submit_run, run_type, payload)


@router.post("/runtime/runs/{run_id}/pause")
async def pause_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "paused")


@router.post("/runtime/runs/{run_id}/resume")
async def resume_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "queued")


@router.post("/runtime/runs/{run_id}/cancel")
async def cancel_runtime_run(run_id: str, request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.set_run_status, run_id, "cancelled")


@router.post("/runtime/failures/retry")
async def retry_runtime_failures(payload: dict[str, Any], request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.retry_failures, payload)


@router.post("/runtime/repair")
async def repair_runtime(request: Request):
    config_api._require_session_auth(request)
    return await run_task_api(runtime_engine.repair)
```

Modify `app/main.py`:

```python
from app.api.runtime_routes import router as runtime_router
```

Add with other routers:

```python
app.include_router(runtime_router)
```

- [ ] **Step 4: Update auth route policy if needed**

If `tests.test_auth_guard` reports unresolved routes, update `app/core/api_permissions.py` so runtime routes are session-only, not public and not API-token accessible:

```python
SESSION_ONLY_API_PREFIXES = (
    ...
    "/api/runtime",
)
```

- [ ] **Step 5: Run route tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api.RuntimeEngineRouteTest tests.test_web_routes tests.test_auth_guard
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/api/runtime_routes.py app/main.py app/core/api_permissions.py tests/test_runtime_engine_api.py tests/test_web_routes.py tests/test_auth_guard.py
git commit -m "feat: 增加运行核心 API"
```

---

### Task 6: Archive Adapter and Compatibility Submit

**Files:**
- Create: `app/services/runtime_engine/archive_adapter.py`
- Modify: `app/api/config.py`
- Test: `tests/test_runtime_engine_archive_adapter.py`
- Test: `tests/test_batch_discovery.py`
- Test: `tests/test_web_routes.py`

- [ ] **Step 1: Write archive adapter tests**

Create `tests/test_runtime_engine_archive_adapter.py`:

```python
import unittest
from unittest.mock import patch

from app.services.runtime_engine.archive_adapter import ArchiveRuntimeAdapter


class ArchiveRuntimeAdapterTest(unittest.TestCase):
    def test_discover_single_model_returns_one_candidate(self):
        adapter = ArchiveRuntimeAdapter()

        candidates = adapter.discover({"source_url": "https://makerworld.com/zh/models/123"})

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["model_url"], "https://makerworld.com/zh/models/123")

    def test_plan_splits_candidates_by_batch_size(self):
        adapter = ArchiveRuntimeAdapter()
        candidates = [{"model_url": f"https://makerworld.com/zh/models/{index}"} for index in range(5)]

        batches = adapter.plan(candidates, {"batch_size": 2})

        self.assertEqual([len(batch["items"]) for batch in batches], [2, 2, 1])

    def test_execute_item_calls_existing_archive_submit_boundary(self):
        adapter = ArchiveRuntimeAdapter()

        with patch.object(adapter.manager, "submit", return_value={"accepted": True, "task_id": "task-1"}) as submit:
            result = adapter.execute_item({"model_url": "https://makerworld.com/zh/models/123"}, {"run_id": "run-1"})

        submit.assert_called_once_with("https://makerworld.com/zh/models/123")
        self.assertTrue(result["accepted"])

    def test_classify_failure_sanitizes_message(self):
        adapter = ArchiveRuntimeAdapter()

        failure = adapter.classify_failure(RuntimeError("<html>secret</html>"))

        self.assertEqual(failure["status"], "failed")
        self.assertNotIn("<html>", failure["message"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_archive_adapter
```

Expected: fails because archive adapter does not exist.

- [ ] **Step 3: Implement archive adapter**

Create `app/services/runtime_engine/archive_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from app.api.dependencies import crawler
from app.services.archive_worker import detect_archive_mode
from app.services.batch_discovery import discover_batch_model_urls
from app.services.three_mf import normalize_source_url


class ArchiveRuntimeAdapter:
    def __init__(self, manager=None) -> None:
        self.manager = manager or crawler.manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        source_url = normalize_source_url(str(context.get("source_url") or context.get("url") or ""))
        if not source_url:
            return []
        mode = detect_archive_mode(source_url)
        if mode == "single_model":
            return [{"model_url": source_url, "source_url": source_url, "mode": mode}]
        discovered = discover_batch_model_urls(source_url, mode=mode)
        items = discovered.get("items") if isinstance(discovered, dict) else discovered
        candidates = []
        for item in items or []:
            if isinstance(item, dict):
                model_url = normalize_source_url(str(item.get("url") or item.get("model_url") or ""))
            else:
                model_url = normalize_source_url(str(item or ""))
            if model_url:
                candidates.append({"model_url": model_url, "source_url": source_url, "mode": mode})
        return candidates

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return dict(self.manager.submit(str(item.get("model_url") or "")) or {})

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        message = str(error_or_result or "归档失败。")
        message = message.replace("<", "").replace(">", "")[:500]
        return {
            "type": "archive",
            "status": "failed",
            "message": message,
            "retryable": True,
        }
```

- [ ] **Step 4: Wire archive compatibility behind feature flag**

Add a helper near archive submission logic in `app/api/config.py`:

```python
def _runtime_engine_enabled() -> bool:
    return os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}
```

Import `runtime_engine` lazily inside the archive route to avoid circular imports:

```python
if _runtime_engine_enabled():
    from app.api.runtime_routes import runtime_engine
    return runtime_engine.submit_run("archive", {"source_url": payload.url})
```

Keep the old archive behavior when the flag is off.

- [ ] **Step 5: Run archive tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_archive_adapter tests.test_batch_discovery tests.test_web_routes
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/runtime_engine/archive_adapter.py app/api/config.py tests/test_runtime_engine_archive_adapter.py
git commit -m "feat: 接入归档运行适配器"
```

---

### Task 7: Missing 3MF Adapter and Retry Compatibility

**Files:**
- Create: `app/services/runtime_engine/missing_3mf_adapter.py`
- Modify: `app/api/tasks_routes.py`
- Test: `tests/test_runtime_engine_missing_3mf_adapter.py`
- Test: `tests/test_missing_3mf.py`

- [ ] **Step 1: Write missing 3MF adapter tests**

Create `tests/test_runtime_engine_missing_3mf_adapter.py`:

```python
import unittest
from types import SimpleNamespace

from app.services.runtime_engine.missing_3mf_adapter import Missing3mfRuntimeAdapter


class Missing3mfRuntimeAdapterTest(unittest.TestCase):
    def test_discover_filters_retryable_platform_items(self):
        task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {"model_id": "1", "source": "global", "status": "queued"},
                    {"model_id": "2", "source": "cn", "status": "verification_required"},
                    {"model_id": "3", "source": "global", "status": "missing"},
                ]
            }
        )
        adapter = Missing3mfRuntimeAdapter(task_store=task_store)

        candidates = adapter.discover({"platform": "global"})

        self.assertEqual([item["model_id"] for item in candidates], ["1", "3"])

    def test_plan_splits_candidates(self):
        adapter = Missing3mfRuntimeAdapter(task_store=SimpleNamespace(load_missing_3mf=lambda: {"items": []}))
        batches = adapter.plan([{"model_id": str(index)} for index in range(3)], {"batch_size": 2})

        self.assertEqual([len(batch["items"]) for batch in batches], [2, 1])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_missing_3mf_adapter
```

Expected: fails because adapter does not exist.

- [ ] **Step 3: Implement missing 3MF adapter**

Create `app/services/runtime_engine/missing_3mf_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from app.api.dependencies import crawler, task_state_store
from app.services.three_mf import normalize_makerworld_source


RETRYABLE_MISSING_3MF_STATUSES = {
    "missing",
    "queued",
    "failed",
    "verification_required",
    "cloudflare",
    "auth_required",
    "download_limited",
}


class Missing3mfRuntimeAdapter:
    def __init__(self, *, manager=None, task_store=None) -> None:
        self.manager = manager or crawler.manager
        self.task_store = task_store or task_state_store

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        platform = normalize_makerworld_source(context.get("platform")) or ""
        payload = self.task_store.load_missing_3mf()
        candidates = []
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_platform = normalize_makerworld_source(item.get("source"), item.get("model_url")) or ""
            status = str(item.get("status") or "").strip().lower()
            if platform and item_platform and item_platform != platform:
                continue
            if status not in RETRYABLE_MISSING_3MF_STATUSES:
                continue
            candidates.append(dict(item))
        return candidates

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self.manager.retry_missing_3mf(
                model_url=item.get("model_url") or "",
                model_id=item.get("model_id") or "",
                source=item.get("source") or "",
                title=item.get("title") or "",
                instance_id=item.get("instance_id") or "",
            )
            or {}
        )

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "missing_3mf_retry",
            "status": "missing_3mf",
            "message": str(error_or_result or "缺失 3MF 重试失败。")[:500],
            "retryable": True,
        }
```

- [ ] **Step 4: Wire retry routes behind feature flag**

In `app/api/tasks_routes.py`, for `/tasks/missing-3mf/retry-all` and `/tasks/missing-3mf/verification-verified`, when runtime engine is enabled call:

```python
from app.api.runtime_routes import runtime_engine
return await run_task_api(runtime_engine.submit_run, "missing_3mf_retry", {"platform": payload.platform})
```

For single retry, pass model fields in context and keep legacy behavior when flag is off.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_missing_3mf_adapter tests.test_missing_3mf tests.test_runtime_diagnostics
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/runtime_engine/missing_3mf_adapter.py app/api/tasks_routes.py tests/test_runtime_engine_missing_3mf_adapter.py
git commit -m "feat: 接入缺失 3MF 运行适配器"
```

---

### Task 8: Source Refresh Adapter

**Files:**
- Create: `app/services/runtime_engine/source_refresh_adapter.py`
- Modify: `app/api/remote_refresh_routes.py`
- Test: `tests/test_runtime_engine_source_refresh_adapter.py`
- Test: `tests/test_source_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write source refresh adapter tests**

Create `tests/test_runtime_engine_source_refresh_adapter.py`:

```python
import unittest
from types import SimpleNamespace

from app.services.runtime_engine.source_refresh_adapter import SourceRefreshRuntimeAdapter


class SourceRefreshRuntimeAdapterTest(unittest.TestCase):
    def test_discover_uses_manager_candidates_when_available(self):
        manager = SimpleNamespace(
            pick_runtime_candidates=lambda context: [
                {"model_id": "1", "model_url": "https://makerworld.com/zh/models/1"},
                {"model_id": "2", "model_url": "https://makerworld.com/zh/models/2"},
            ]
        )
        adapter = SourceRefreshRuntimeAdapter(manager=manager)

        candidates = adapter.discover({"limit": 2})

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["model_id"], "1")

    def test_execute_item_calls_refresh_one_when_available(self):
        calls = []
        manager = SimpleNamespace(refresh_runtime_item=lambda item, context: calls.append((item, context)) or {"success": True})
        adapter = SourceRefreshRuntimeAdapter(manager=manager)

        result = adapter.execute_item({"model_id": "1"}, {"run_id": "run-1"})

        self.assertTrue(result["success"])
        self.assertEqual(calls[0][0]["model_id"], "1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_source_refresh_adapter
```

Expected: fails because adapter does not exist.

- [ ] **Step 3: Implement adapter with compatibility hooks**

Create `app/services/runtime_engine/source_refresh_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from app.api.dependencies import remote_refresh_manager


class SourceRefreshRuntimeAdapter:
    def __init__(self, *, manager=None) -> None:
        self.manager = manager or remote_refresh_manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        picker = getattr(self.manager, "pick_runtime_candidates", None)
        if callable(picker):
            return list(picker(context) or [])
        state = self.manager.status_payload() if hasattr(self.manager, "status_payload") else {}
        candidates = state.get("candidates") if isinstance(state, dict) else []
        return [item for item in candidates or [] if isinstance(item, dict)]

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 50))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        refresh_one = getattr(self.manager, "refresh_runtime_item", None)
        if callable(refresh_one):
            return dict(refresh_one(item, context) or {})
        return {"success": False, "message": "source refresh runtime item hook is unavailable"}

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "source_refresh",
            "status": "failed",
            "message": str(error_or_result or "源端刷新失败。")[:500],
            "retryable": True,
        }
```

- [ ] **Step 4: Add manager hooks in source refresh service**

In `app/services/source_refresh.py`, add these public hooks to `SourceRefreshTaskManager`. They should delegate to the existing `RemoteRefreshManager._pick_candidates()` / `_run_batch(..., selected_candidates=...)` path and must not create a second source-refresh queue:

```python
def pick_runtime_candidates(self, context: dict) -> list[dict]:
    limit = int(context.get("limit") or 0) or None
    candidates = self._pick_candidates() if hasattr(self, "_pick_candidates") else []
    return list(candidates or [])[:limit]

def refresh_runtime_item(self, item: dict, context: dict) -> dict:
    return self._run_batch(selected_candidates=[item], selected_stats={"runtime_engine": True})
```

Because `RemoteRefreshManager._pick_candidates()` returns `(candidates, stats)`, implement the hook as:

```python
def pick_runtime_candidates(self, context: dict) -> list[dict]:
    candidates, _stats = self._pick_candidates()
    limit = int(context.get("limit") or 0) or None
    return list(candidates or [])[:limit]

def refresh_runtime_item(self, item: dict, context: dict) -> dict:
    config = self.store.load()
    self._run_batch(
        config,
        selected_candidates=[item],
        selected_stats={"runtime_engine": 1, "eligible_total": 1, "selected_total": 1, "remaining_total": 0},
    )
    return {"success": True, "model_id": item.get("model_id") or item.get("model_dir") or ""}
```

- [ ] **Step 5: Wire source-refresh routes behind feature flag**

In `app/api/remote_refresh_routes.py`, add `_runtime_engine_enabled()` near `_trigger_source_refresh_run()`. In `_trigger_source_refresh_run()`, branch before calling `remote_refresh_manager.trigger_manual_refresh()`:

```python
from app.api.runtime_routes import runtime_engine
return await run_task_api(runtime_engine.submit_run, "source_refresh", {"manual": True})
```

Keep old behavior when flag is off.

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_source_refresh_adapter tests.test_source_refresh tests.test_remote_refresh tests.test_process_jobs
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/services/runtime_engine/source_refresh_adapter.py app/services/source_refresh.py app/api/remote_refresh_routes.py tests/test_runtime_engine_source_refresh_adapter.py
git commit -m "feat: 接入源端刷新运行适配器"
```

---

### Task 9: Subscription Adapter

**Files:**
- Create: `app/services/runtime_engine/subscription_adapter.py`
- Modify: `app/api/subscriptions_routes.py`
- Test: `tests/test_runtime_engine_subscription_adapter.py`
- Test: `tests/test_subscriptions.py`
- Test: `tests/test_source_library.py`

- [ ] **Step 1: Write subscription adapter tests**

Create `tests/test_runtime_engine_subscription_adapter.py`:

```python
import unittest
from types import SimpleNamespace

from app.services.runtime_engine.subscription_adapter import SubscriptionRuntimeAdapter


class SubscriptionRuntimeAdapterTest(unittest.TestCase):
    def test_discover_uses_manager_runtime_sources(self):
        manager = SimpleNamespace(
            pick_runtime_subscriptions=lambda context: [
                {"subscription_id": "sub-1", "url": "https://makerworld.com/zh/@demo/upload"}
            ]
        )
        adapter = SubscriptionRuntimeAdapter(manager=manager)

        candidates = adapter.discover({})

        self.assertEqual(candidates[0]["subscription_id"], "sub-1")

    def test_execute_item_calls_sync_subscription_runtime(self):
        calls = []
        manager = SimpleNamespace(sync_subscription_runtime=lambda item, context: calls.append((item, context)) or {"success": True, "queued": 3})
        adapter = SubscriptionRuntimeAdapter(manager=manager)

        result = adapter.execute_item({"subscription_id": "sub-1"}, {"run_id": "run-1"})

        self.assertTrue(result["success"])
        self.assertEqual(result["queued"], 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_subscription_adapter
```

Expected: fails because adapter does not exist.

- [ ] **Step 3: Implement subscription adapter**

Create `app/services/runtime_engine/subscription_adapter.py`:

```python
from __future__ import annotations

from typing import Any

from app.api.dependencies import subscription_manager


class SubscriptionRuntimeAdapter:
    def __init__(self, *, manager=None) -> None:
        self.manager = manager or subscription_manager

    def discover(self, context: dict[str, Any]) -> list[dict[str, Any]]:
        picker = getattr(self.manager, "pick_runtime_subscriptions", None)
        if callable(picker):
            return list(picker(context) or [])
        payload = self.manager.list_payload() if hasattr(self.manager, "list_payload") else {}
        return [item for item in payload.get("items") or [] if isinstance(item, dict) and item.get("enabled", True)]

    def plan(self, candidates: list[dict[str, Any]], limits: dict[str, Any]) -> list[dict[str, Any]]:
        batch_size = max(1, int(limits.get("batch_size") or 20))
        return [
            {"items": candidates[index:index + batch_size], "offset": index, "limit": batch_size}
            for index in range(0, len(candidates), batch_size)
        ]

    def execute_item(self, item: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        sync_one = getattr(self.manager, "sync_subscription_runtime", None)
        if callable(sync_one):
            return dict(sync_one(item, context) or {})
        sync_now = getattr(self.manager, "sync_subscription_now", None)
        if callable(sync_now):
            return dict(sync_now(str(item.get("subscription_id") or item.get("id") or "")) or {})
        return {"success": False, "message": "subscription runtime sync hook is unavailable"}

    def commit_success(self, result: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    def classify_failure(self, error_or_result: Any) -> dict[str, Any]:
        return {
            "type": "subscription_sync",
            "status": "failed",
            "message": str(error_or_result or "订阅同步失败。")[:500],
            "retryable": True,
        }
```

- [ ] **Step 4: Add manager hooks**

In `app/services/subscriptions.py`, add small public hooks on `SubscriptionManager`:

```python
def pick_runtime_subscriptions(self, context: dict) -> list[dict]:
    payload = self.list_payload()
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items or [] if isinstance(item, dict) and item.get("enabled", True)]

def sync_subscription_runtime(self, item: dict, context: dict) -> dict:
    subscription_id = str(item.get("subscription_id") or item.get("id") or "")
    return self.sync_subscription_now(subscription_id)
```

- [ ] **Step 5: Wire subscription sync route behind feature flag**

In `app/api/subscriptions_routes.py`, when runtime engine is enabled for manual sync, submit:

```python
runtime_engine.submit_run("subscription_sync", {"source_id": subscription_id})
```

Keep existing sync behavior when flag is off.

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_subscription_adapter tests.test_subscriptions tests.test_source_library tests.test_source_health
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/services/runtime_engine/subscription_adapter.py app/services/subscriptions.py app/api/subscriptions_routes.py tests/test_runtime_engine_subscription_adapter.py
git commit -m "feat: 接入订阅运行适配器"
```

---

### Task 10: Worker Loop and Feature Flag Wiring

**Files:**
- Modify: `app/services/runtime_engine/engine.py`
- Modify: `app/api/runtime_routes.py`
- Modify: `app/worker.py`
- Test: `tests/test_runtime_engine_api.py`
- Test: `tests/test_process_jobs.py`

- [ ] **Step 1: Add failing worker-loop tests**

Append to `tests/test_runtime_engine_api.py`:

```python
class RuntimeEngineExecutionTest(unittest.TestCase):
    def test_execute_next_batch_runs_items_and_records_summary(self):
        class Adapter:
            def execute_item(self, item, context):
                if item["model_id"] == "bad":
                    raise RuntimeError("failed item")
                return {"success": True, "model_id": item["model_id"]}

            def commit_success(self, result, context):
                return None

            def classify_failure(self, error):
                return {"status": "failed", "message": str(error), "retryable": True}

        batches = [{"batch_id": "batch-1", "run_id": "run-1", "type": "archive", "status": "queued", "total": 2}]
        saved_batches = []
        failures = []

        runtime = engine.RuntimeEngine(adapters={"archive": Adapter()}, batch_size=2)

        with patch.object(engine.store, "load_batches", return_value={"items": batches}), \
                patch.object(engine.store, "load_batch_items", return_value=[{"model_id": "ok"}, {"model_id": "bad"}]), \
                patch.object(engine.store, "load_runs", return_value={"items": [{"run_id": "run-1", "type": "archive", "status": "running", "total": 2}]}), \
                patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: saved_batches.append(batch) or batch), \
                patch.object(engine.store, "append_failure", side_effect=lambda failure, **kwargs: failures.append(failure) or failure), \
                patch.object(engine.store, "delete_batch_items", return_value=True), \
                patch.object(runtime, "refresh_snapshots", return_value={}):
            result = runtime.execute_next_batch()

        self.assertTrue(result["executed"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(failures[0]["status"], "failed")
        self.assertEqual(saved_batches[-1]["status"], "completed")

    def test_repair_requeues_interrupted_batches_and_updates_run_totals(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)
        saved_batches = []
        saved_runs = []

        with patch.object(engine.store, "load_runtime_state", return_value={
            "runs": {"items": [{"run_id": "run-1", "type": "archive", "status": "running", "total": 2}]},
            "batches": {"items": [{"batch_id": "batch-1", "run_id": "run-1", "type": "archive", "status": "interrupted", "completed": 1, "failed": 1, "total": 2}]},
            "failures": {"items": [{"failure_id": "failure-1", "run_id": "run-1", "batch_id": "batch-1"}]},
            "snapshots": {},
        }), \
                patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: saved_batches.append(batch) or batch), \
                patch.object(engine.store, "upsert_run", side_effect=lambda run, **kwargs: saved_runs.append(run) or run), \
                patch.object(engine.store, "save_snapshot", return_value={}):
            result = runtime.repair()

        self.assertTrue(result["success"])
        self.assertEqual(saved_batches[0]["status"], "queued")
        self.assertEqual(saved_runs[-1]["completed"], 1)
        self.assertEqual(saved_runs[-1]["failed"], 1)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api.RuntimeEngineExecutionTest
```

Expected: fails because `execute_next_batch` is missing.

- [ ] **Step 3: Implement `execute_next_batch` and replace `repair`**

In `app/services/runtime_engine/engine.py`, add `execute_next_batch()` and `_update_run_totals()`. Replace the earlier `repair()` skeleton from Task 3 with the `repair()` implementation below:

```python
    def execute_next_batch(self) -> dict[str, Any]:
        batches = store.load_batches()["items"]
        target = next((batch for batch in batches if batch.get("status") == "queued"), None)
        if not target:
            return {"executed": False, "message": "没有等待执行的批次。"}
        adapter = self.adapters.get(target.get("type"))
        if adapter is None:
            store.upsert_batch({**target, "status": "blocked", "message": "运行适配器未注册。"}, event_type="runtime.run.blocked")
            self.refresh_snapshots()
            return {"executed": False, "message": "运行适配器未注册。"}
        items = store.load_batch_items(target["batch_id"])
        store.upsert_batch({**target, "status": "running", "started_at": china_now_iso()}, event_type="runtime.batch.progress")
        completed = 0
        failed = 0
        for item in items:
            try:
                result = adapter.execute_item(item, {"run_id": target["run_id"], "batch_id": target["batch_id"]})
                adapter.commit_success(result, {"run_id": target["run_id"], "batch_id": target["batch_id"]})
                completed += 1
            except Exception as exc:
                failure = adapter.classify_failure(exc)
                store.append_failure({**failure, "run_id": target["run_id"], "batch_id": target["batch_id"], "type": target["type"]})
                failed += 1
        status = "completed" if failed == 0 or completed > 0 else "failed"
        store.upsert_batch(
            {
                **target,
                "status": status,
                "completed": completed,
                "failed": failed,
                "completed_at": china_now_iso(),
                "message": f"批次完成：成功 {completed}，失败 {failed}。",
            },
            event_type="runtime.batch.completed",
        )
        store.delete_batch_items(target["batch_id"])
        self._update_run_totals(target["run_id"])
        self.refresh_snapshots()
        return {"executed": True, "completed": completed, "failed": failed}

    def _update_run_totals(self, run_id: str) -> dict[str, Any]:
        runs = store.load_runs()["items"]
        batches = [item for item in store.load_batches()["items"] if item.get("run_id") == run_id]
        run = next((item for item in runs if item.get("run_id") == run_id), None)
        if not run:
            return {}
        completed = sum(int(item.get("completed") or 0) for item in batches)
        failed = sum(int(item.get("failed") or 0) for item in batches)
        total = sum(int(item.get("total") or 0) for item in batches) or int(run.get("total") or 0)
        active = [item for item in batches if item.get("status") in {"queued", "running", "paused", "blocked", "interrupted"}]
        status = "completed" if not active and failed == 0 else "failed" if not active else run.get("status", "running")
        event_type = "runtime.run.completed" if status == "completed" else ""
        return store.upsert_run(
            {
                **run,
                "status": status,
                "total": total,
                "completed": completed,
                "failed": failed,
                "updated_at": china_now_iso(),
                "completed_at": china_now_iso() if status in {"completed", "failed"} else run.get("completed_at", ""),
            },
            event_type=event_type,
        )

    def repair(self) -> dict[str, Any]:
        state = store.load_runtime_state()
        for batch in state["batches"]["items"]:
            if batch.get("status") == "interrupted":
                store.upsert_batch({**batch, "status": "queued", "lease_owner": "", "lease_expires_at": "", "message": "已恢复为排队。"})
        for run in state["runs"]["items"]:
            self._update_run_totals(run["run_id"])
        snapshots = self.refresh_snapshots()
        return {"success": True, "message": "运行核心状态已修复。", "snapshots": snapshots}
```

- [ ] **Step 4: Register real adapters in runtime routes**

In `app/api/runtime_routes.py`, initialize:

```python
from app.services.runtime_engine.archive_adapter import ArchiveRuntimeAdapter
from app.services.runtime_engine.missing_3mf_adapter import Missing3mfRuntimeAdapter
from app.services.runtime_engine.source_refresh_adapter import SourceRefreshRuntimeAdapter
from app.services.runtime_engine.subscription_adapter import SubscriptionRuntimeAdapter

runtime_engine = RuntimeEngine(
    adapters={
        "archive": ArchiveRuntimeAdapter(),
        "missing_3mf_retry": Missing3mfRuntimeAdapter(),
        "source_refresh": SourceRefreshRuntimeAdapter(),
        "subscription_sync": SubscriptionRuntimeAdapter(),
    }
)
```

- [ ] **Step 5: Wire worker loop behind flag**

In `app/worker.py`, import runtime route singleton lazily inside the worker main loop or startup:

```python
if os.getenv("MAKERHUB_RUNTIME_ENGINE", "").strip().lower() in {"1", "true", "v2", "runtime"}:
    from app.api.runtime_routes import runtime_engine
    runtime_engine.execute_next_batch()
```

Call it on the same cadence as existing background managers, without blocking local organizer or source library startup.

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_engine_api tests.test_process_jobs tests.test_request_threads
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add app/services/runtime_engine/engine.py app/api/runtime_routes.py app/worker.py tests/test_runtime_engine_api.py
git commit -m "feat: 增加运行核心执行循环"
```

---

### Task 11: Batch-First Task Page

**Files:**
- Create: `frontend/src/lib/runtimeStatus.js`
- Modify: `frontend/src/pages/TasksPage.vue`
- Test: `frontend/src/lib/runtimeStatus.test.mjs`
- Test: `frontend/src/lib/tasksManualVerification.test.mjs`

- [ ] **Step 1: Write frontend helper tests**

Create `frontend/src/lib/runtimeStatus.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import {
  runtimeFailureLabel,
  runtimeRunLabel,
  runtimeTaskShape,
} from "./runtimeStatus.js";

test("runtime run labels are compact", () => {
  assert.equal(runtimeRunLabel("queued"), "排队中");
  assert.equal(runtimeRunLabel("running"), "运行中");
  assert.equal(runtimeRunLabel("blocked"), "需处理");
  assert.equal(runtimeRunLabel("completed"), "已完成");
});

test("runtime failure labels preserve actionable states", () => {
  assert.equal(runtimeFailureLabel("missing_3mf"), "缺失 3MF");
  assert.equal(runtimeFailureLabel("verification_required"), "需要验证");
  assert.equal(runtimeFailureLabel("cookie_invalid"), "Cookie 异常");
});

test("runtime task shape prefers runtime payload when present", () => {
  const payload = runtimeTaskShape({
    runtime: {
      runs: [{ run_id: "run-1", status: "running" }],
      batches: [{ batch_id: "batch-1", status: "queued" }],
      failures: [{ failure_id: "failure-1", status: "missing_3mf" }],
    },
    archive_queue: { active: [{ id: "legacy" }] },
  });

  assert.equal(payload.mode, "runtime");
  assert.equal(payload.runs.length, 1);
  assert.equal(payload.batches.length, 1);
  assert.equal(payload.failures.length, 1);
});

test("runtime task shape falls back to legacy payload", () => {
  const payload = runtimeTaskShape({ archive_queue: { active: [{ id: "legacy" }] } });

  assert.equal(payload.mode, "legacy");
  assert.equal(payload.legacy.archive_queue.active[0].id, "legacy");
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
node --test frontend/src/lib/runtimeStatus.test.mjs
```

Expected: fails because helper does not exist.

- [ ] **Step 3: Implement helper**

Create `frontend/src/lib/runtimeStatus.js`:

```javascript
const RUN_LABELS = {
  queued: "排队中",
  discovering: "发现中",
  planned: "已规划",
  running: "运行中",
  paused: "已暂停",
  blocked: "需处理",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "可恢复",
};

const FAILURE_LABELS = {
  failed: "失败",
  skipped: "已跳过",
  missing_3mf: "缺失 3MF",
  verification_required: "需要验证",
  cookie_invalid: "Cookie 异常",
  daily_limit: "每日上限",
  network_error: "网络异常",
  not_found: "源端无文件",
};

export function runtimeRunLabel(status = "") {
  return RUN_LABELS[String(status || "").trim().toLowerCase()] || "未知";
}

export function runtimeFailureLabel(status = "") {
  return FAILURE_LABELS[String(status || "").trim().toLowerCase()] || "失败";
}

export function runtimeTaskShape(payload = {}) {
  const runtime = payload?.runtime;
  if (runtime && Array.isArray(runtime.runs) && Array.isArray(runtime.batches)) {
    return {
      mode: "runtime",
      runs: runtime.runs,
      batches: runtime.batches,
      failures: Array.isArray(runtime.failures) ? runtime.failures : [],
    };
  }
  return {
    mode: "legacy",
    legacy: payload,
  };
}
```

- [ ] **Step 4: Add runtime branch to TasksPage**

In `frontend/src/pages/TasksPage.vue`:

1. Import:

```javascript
import { runtimeFailureLabel, runtimeRunLabel, runtimeTaskShape } from "../lib/runtimeStatus.js";
```

2. Add computed:

```javascript
const taskShape = computed(() => runtimeTaskShape(payload.value));
const runtimeMode = computed(() => taskShape.value.mode === "runtime");
const runtimeRuns = computed(() => runtimeMode.value ? taskShape.value.runs : []);
const runtimeBatches = computed(() => runtimeMode.value ? taskShape.value.batches : []);
const runtimeFailures = computed(() => runtimeMode.value ? taskShape.value.failures : []);
```

3. In template, before legacy columns, add a runtime-mode section:

```vue
<section v-if="runtimeMode" class="surface section-card">
  <div class="section-card__header">
    <div>
      <span class="eyebrow">运行核心</span>
      <h2>批次任务</h2>
    </div>
    <span class="count-pill">{{ runtimeRuns.length }} 个运行 / {{ runtimeBatches.length }} 个批次</span>
  </div>
  <div class="task-columns">
    <div class="task-column">
      <h3>运行</h3>
      <div v-if="runtimeRuns.length">
        <div v-for="run in runtimeRuns" :key="run.run_id" class="task-item">
          <strong>{{ run.message || run.source_url || run.run_id }}</strong>
          <span>{{ runtimeRunLabel(run.status) }}</span>
          <p>总数 {{ run.total || 0 }} · 完成 {{ run.completed || 0 }} · 失败 {{ run.failed || 0 }}</p>
        </div>
      </div>
      <p v-else class="empty-copy">当前没有运行中的批次。</p>
    </div>
    <div class="task-column">
      <h3>失败明细</h3>
      <div v-if="runtimeFailures.length">
        <div v-for="failure in runtimeFailures" :key="failure.failure_id" class="task-item task-item--error">
          <strong>{{ failure.title || failure.model_id || failure.failure_id }}</strong>
          <span>{{ runtimeFailureLabel(failure.status) }}</span>
          <p>{{ failure.message || "等待处理。" }}</p>
        </div>
      </div>
      <p v-else class="empty-copy">暂无失败明细。</p>
    </div>
  </div>
</section>
```

4. Wrap the existing legacy task list root section with `v-else`. If the current template has multiple sibling legacy sections, put them inside one `<template v-else>` so runtime mode renders only the batch-first section and the shared page toolbar remains visible.

- [ ] **Step 5: Run frontend tests/build**

Run:

```bash
node --test frontend/src/lib/runtimeStatus.test.mjs frontend/src/lib/tasksManualVerification.test.mjs
npm --prefix frontend run build
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add frontend/src/lib/runtimeStatus.js frontend/src/lib/runtimeStatus.test.mjs frontend/src/pages/TasksPage.vue
git commit -m "feat: 任务页支持批次运行视图"
```

---

### Task 12: Dashboard, Source Refresh, and Subscription Snapshot Reads

**Files:**
- Modify: `app/services/catalog.py`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Test: `tests/test_runtime_diagnostics.py`
- Test: `frontend/src/lib/dashboardStatus.test.mjs`
- Build: `npm --prefix frontend run build`

- [ ] **Step 1: Add backend snapshot test**

Add a focused test to `tests/test_runtime_diagnostics.py`:

```python
def test_dashboard_payload_can_include_runtime_snapshot(self):
    runtime_snapshot = {
        "dashboard": {
            "active_runs": [{"run_id": "run-1", "status": "running"}],
            "active_batches": [],
            "summary": {"active_runs": 1, "active_batches": 0, "failures": 0},
        }
    }

    with patch("app.services.catalog.load_database_json_state", return_value=runtime_snapshot):
        # Import locally if needed to avoid module-level patch conflicts.
        from app.services import catalog
        payload = catalog._runtime_dashboard_snapshot()

    self.assertEqual(payload["active_runs"][0]["run_id"], "run-1")
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_diagnostics
```

Expected: fails because `_runtime_dashboard_snapshot` does not exist.

- [ ] **Step 3: Add backend snapshot helper**

In `app/services/catalog.py`, add:

```python
from app.core.database_json_state import load_database_json_state
from app.services.state_contracts import RUNTIME_SNAPSHOTS_STATE_KEY


def _runtime_dashboard_snapshot() -> dict:
    payload = load_database_json_state(RUNTIME_SNAPSHOTS_STATE_KEY, {})
    if not isinstance(payload, dict):
        return {}
    dashboard = payload.get("dashboard")
    return dashboard if isinstance(dashboard, dict) else {}
```

In the dashboard payload builder, add:

```python
runtime_dashboard = _runtime_dashboard_snapshot()
if runtime_dashboard:
    payload["runtime"] = runtime_dashboard
```

Do not delete or rename any existing dashboard payload fields; the only backend payload addition in this task is `payload["runtime"] = runtime_dashboard` when a runtime dashboard snapshot exists.

- [ ] **Step 4: Update frontend pages to prefer runtime data**

Dashboard:

```javascript
const runtimeSummary = computed(() => payload.value.runtime?.summary || {});
```

Use runtime summary with explicit fallback:

```javascript
const activeRuntimeRuns = computed(() => Number(runtimeSummary.value.active_runs || 0));
const activeRuntimeBatches = computed(() => Number(runtimeSummary.value.active_batches || 0));
```

Display runtime counts only when `payload.value.runtime` exists; otherwise keep the current card values unchanged.

RemoteRefreshPage:

Add:

```javascript
const runtimeSourceRefresh = computed(() => payload.value.runtime?.source_refresh || {});
const sourceRefreshActiveRuns = computed(() => runtimeSourceRefresh.value.active_runs || []);
```

When `sourceRefreshActiveRuns.length > 0`, render the active run count in the existing status pill; otherwise keep current `source_refresh` state rendering.

SubscriptionsPage:

Add:

```javascript
const runtimeSubscriptions = computed(() => payload.value.runtime?.subscriptions || {});
const subscriptionSyncActiveRuns = computed(() => runtimeSubscriptions.value.active_runs || []);
```

When `subscriptionSyncActiveRuns.length > 0`, render the existing sync-in-progress pill with the runtime count; otherwise keep current subscription state rendering.

- [ ] **Step 5: Run tests/build**

Run:

```bash
.venv/bin/python -m unittest tests.test_runtime_diagnostics tests.test_source_refresh tests.test_subscriptions
node --test frontend/src/lib/dashboardStatus.test.mjs frontend/src/lib/runtimeStatus.test.mjs
npm --prefix frontend run build
```

Expected: pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add app/services/catalog.py frontend/src/pages/DashboardPage.vue frontend/src/pages/RemoteRefreshPage.vue frontend/src/pages/SubscriptionsPage.vue tests/test_runtime_diagnostics.py
git commit -m "feat: 页面读取运行核心快照"
```

---

### Task 13: Flow Verification Script

**Files:**
- Create: `scripts/check_runtime_engine_flows.sh`
- Modify: `README.md` only in release task, not here.

- [ ] **Step 1: Create verification script**

Create `scripts/check_runtime_engine_flows.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MAKERHUB_BASE_URL:-http://127.0.0.1:8000}"
USERNAME="${MAKERHUB_USERNAME:-admin}"
PASSWORD="${MAKERHUB_PASSWORD:-admin}"
COOKIE_FILE="$(mktemp)"
trap 'rm -f "$COOKIE_FILE"' EXIT

echo "[runtime-flow] login"
curl -fsS -c "$COOKIE_FILE" -b "$COOKIE_FILE" \
  -H 'Content-Type: application/json' \
  -X POST "$BASE_URL/api/auth/login" \
  --data "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}" >/dev/null

echo "[runtime-flow] dashboard"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/dashboard" >/tmp/makerhub-dashboard.json

echo "[runtime-flow] tasks"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/tasks" >/tmp/makerhub-tasks.json

echo "[runtime-flow] runtime"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/runtime" >/tmp/makerhub-runtime.json

echo "[runtime-flow] source refresh"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/source-refresh" >/tmp/makerhub-source-refresh.json

echo "[runtime-flow] subscriptions"
curl -fsS -b "$COOKIE_FILE" "$BASE_URL/api/subscriptions" >/tmp/makerhub-subscriptions.json

python3 - <<'PY'
import json
for path in [
    "/tmp/makerhub-dashboard.json",
    "/tmp/makerhub-tasks.json",
    "/tmp/makerhub-runtime.json",
    "/tmp/makerhub-source-refresh.json",
    "/tmp/makerhub-subscriptions.json",
]:
    with open(path, "r", encoding="utf-8") as fh:
        json.load(fh)
print("[runtime-flow] json payloads valid")
PY
```

- [ ] **Step 2: Make it executable**

Run:

```bash
chmod +x scripts/check_runtime_engine_flows.sh
```

- [ ] **Step 3: Run static shell check**

Run:

```bash
bash -n scripts/check_runtime_engine_flows.sh
```

Expected: no output and exit 0.

- [ ] **Step 4: Commit**

Run:

```bash
git add scripts/check_runtime_engine_flows.sh
git commit -m "test: 增加运行核心流程检查脚本"
```

---

### Task 14: Full Test, Online Flow Check, and Release

**Files:**
- Modify: `README.md`
- Modify: `VERSION`
- Modify: docs if Task 1-13 changed module contracts further.

- [ ] **Step 1: Run full targeted backend tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_runtime_engine_contracts \
  tests.test_runtime_engine_store \
  tests.test_runtime_engine_migration \
  tests.test_runtime_engine_api \
  tests.test_runtime_engine_archive_adapter \
  tests.test_runtime_engine_missing_3mf_adapter \
  tests.test_runtime_engine_source_refresh_adapter \
  tests.test_runtime_engine_subscription_adapter \
  tests.test_state_contracts \
  tests.test_task_state \
  tests.test_missing_3mf \
  tests.test_source_refresh \
  tests.test_remote_refresh \
  tests.test_subscriptions \
  tests.test_source_library \
  tests.test_runtime_diagnostics \
  tests.test_web_routes \
  tests.test_auth_guard
```

Expected: all pass.

- [ ] **Step 2: Run frontend tests and build**

Run:

```bash
node --test \
  frontend/src/lib/runtimeStatus.test.mjs \
  frontend/src/lib/dashboardStatus.test.mjs \
  frontend/src/lib/tasksManualVerification.test.mjs
npm --prefix frontend run build
```

Expected: all pass and build exits 0.

- [ ] **Step 3: Run flow script against local or online instance**

If a local dev server is running:

```bash
MAKERHUB_BASE_URL=http://127.0.0.1:8000 scripts/check_runtime_engine_flows.sh
```

If checking online instance:

```bash
MAKERHUB_BASE_URL=http://test.ace-station.top:1111 scripts/check_runtime_engine_flows.sh
```

Expected:

- login succeeds
- `/api/dashboard` returns JSON
- `/api/tasks` returns JSON
- `/api/runtime` returns JSON
- `/api/source-refresh` returns JSON
- `/api/subscriptions` returns JSON

- [ ] **Step 4: Manually verify main workflows**

Use browser/API and record results in final answer:

```text
Login/dashboard: pass/fail
Task page batch cards: pass/fail
Single archive submit: pass/fail
Batch archive creates bounded batches: pass/fail
Subscription sync submits/records runtime run: pass/fail
Source refresh produces runtime run/snapshot: pass/fail
Missing 3MF retry records failures/account health: pass/fail
Worker restart/repair recovers interrupted batches: pass/fail
```

- [ ] **Step 5: Bump version and README release notes only if pushing**

Patch bump from current version:

```bash
printf '0.9.25\n' > VERSION
```

Update README latest release section:

```markdown
### 2026-06-13 · v0.9.25

- 新增统一运行核心，归档、订阅同步、源端刷新和缺失 `3MF` 重试共用 run/batch/failure/snapshot 状态模型。
- 任务页支持批次优先视图，成功模型不再保留逐条运行历史，失败、跳过和缺失 `3MF` 保留分页明细和重试入口。
- 新增旧状态迁移预览/应用、运行核心修复和流程检查脚本，降低队列堆积、状态冲突和页面首屏负载。
```

Keep only the latest three release sections expanded, moving older ones into the existing collapsed history.

- [ ] **Step 6: Final diff checks**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors; only intended files changed plus existing untracked `videos/makerhub-intro/output/`.

- [ ] **Step 7: Commit release**

Run:

```bash
git add README.md VERSION
git commit -m "chore: 发布 v0.9.25"
```

- [ ] **Step 8: Push when requested**

Only if the user explicitly asks to push:

```bash
git push origin main
```

Expected: push succeeds.
