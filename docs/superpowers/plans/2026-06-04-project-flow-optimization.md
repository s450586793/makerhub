# MakerHub Project Flow Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MakerHub long-running background workflows recoverable, cheaper to observe, and clearer in the UI by standardizing task status, stale-task repair, diagnostics, and user actions.

**Architecture:** Keep the current Postgres-backed JSON state model and existing API paths. Add focused runtime helper modules for task lifecycle/status and repair summaries, wire them into `TaskStateStore` and diagnostics, then update frontend status/action helpers so pages display the normalized runtime vocabulary without broad layout changes.

**Tech Stack:** FastAPI, Python services, Postgres-backed JSON state, unittest/pytest, Vue 3 helper modules, Node test runner, Vite build.

---

## Files

- Create `app/services/task_runtime.py`: shared runtime status constants, lease/heartbeat helpers, stale task classification, and action metadata helpers.
- Modify `app/services/state_contracts.py`: expose runtime status constants and keep existing state-key constants stable.
- Modify `tests/test_state_contracts.py`: verify new shared runtime statuses are present.
- Modify `app/services/task_state.py`: normalize archive queue runtime fields, add task repair/recovery summary, and expose queue status summary.
- Modify `tests/test_task_state.py`: cover lease expiry, retry-budget failure, paused-skip behavior, parent `waiting_children`, and repair summary.
- Modify `app/services/runtime_diagnostics.py`: include queue counts, stale lease candidates, and sensitive-safe runtime details.
- Modify `tests/test_runtime_diagnostics.py`: cover diagnostics additions and unavailable database fallback.
- Modify `app/api/tasks_routes.py`: add authenticated queue repair endpoint.
- Modify `tests/test_runtime_diagnostics.py`: add a direct route-function test for the repair endpoint.
- Modify `frontend/src/lib/dashboardStatus.js`: centralize status/action normalization for dashboard cards and task rows.
- Modify `frontend/src/lib/dashboardStatus.test.mjs`: cover external verification, repair, retry, pause/resume, and diagnostics actions.
- Modify `frontend/src/pages/DashboardPage.vue`: consume normalized action/status helpers without visual redesign.
- Modify `frontend/src/pages/TasksPage.vue`: show `waiting_children`, `blocked`, stale/recoverable, and actionable labels.
- Modify `docs/modules/tasks_worker.md`: document runtime status vocabulary and repair behavior.
- Modify `docs/modules/core.md`: document diagnostics payload additions.
- Modify `README.md`, `app/core/settings.py`, and `frontend/package.json` only during release/push preparation.

## Task 1: Runtime Status Contract

**Files:**
- Create: `app/services/task_runtime.py`
- Modify: `app/services/state_contracts.py`
- Test: `tests/test_state_contracts.py`

- [ ] **Step 1: Write failing tests for shared runtime statuses**

Add this test to `tests/test_state_contracts.py`:

```python
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
```

- [ ] **Step 2: Run the contract test and verify it fails**

Run:

```bash
pytest tests/test_state_contracts.py::test_runtime_statuses_cover_task_governance_values -q
```

Expected: fails with `AttributeError: module 'app.services.state_contracts' has no attribute 'RUNTIME_TASK_STATUSES'`.

- [ ] **Step 3: Add runtime helper module**

Create `app/services/task_runtime.py`:

```python
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


def task_attempt_count(item: dict[str, Any]) -> int:
    try:
        return int(item.get("attempt_count") or item.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def task_attempts_remaining(item: dict[str, Any], max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> bool:
    return task_attempt_count(item) < max(int(max_attempts or 0), 1)
```

- [ ] **Step 4: Re-export status contract**

Add this import and constant to `app/services/state_contracts.py`:

```python
from app.services.task_runtime import RUNTIME_TASK_STATUSES
```

Do not remove existing constants.

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_state_contracts.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/task_runtime.py app/services/state_contracts.py tests/test_state_contracts.py
git commit -m "feat: add runtime task status contract"
```

## Task 2: Archive Queue Runtime Normalization

**Files:**
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing tests for normalized runtime fields**

Add these tests to `ArchiveQueueStateTest` in `tests/test_task_state.py`:

```python
    def test_start_archive_task_assigns_runtime_lease_fields(self):
        state = {"archive_queue": {"active": [], "queued": [{"id": "task-1", "title": "Demo"}], "recent_failures": []}}
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"), \
                patch("app.services.task_state.lease_expiry_from_now", return_value="2026-06-04T10:30:00+08:00"):
            queue = store.start_archive_task("task-1")

        task = queue["active"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["started_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["heartbeat_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["last_progress_at"], "2026-06-04T10:00:00+08:00")
        self.assertEqual(task["lease_expires_at"], "2026-06-04T10:30:00+08:00")
        self.assertGreaterEqual(task["attempt_count"], 1)

    def test_batch_parent_normalizes_to_waiting_children(self):
        store = TaskStateStore()
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "batch-1",
                        "title": "Batch",
                        "mode": "author_upload",
                        "status": "running",
                        "meta": {"batch_expected_items": [{"url": "https://makerworld.com.cn/zh/models/1", "status": "queued"}]},
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)):
            queue = store.load_archive_queue()

        self.assertEqual(queue["active"][0]["status"], "waiting_children")
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_task_state.py::ArchiveQueueStateTest::test_start_archive_task_assigns_runtime_lease_fields tests/test_task_state.py::ArchiveQueueStateTest::test_batch_parent_normalizes_to_waiting_children -q
```

Expected: missing `lease_expires_at`/`attempt_count` and batch status remains `running`.

- [ ] **Step 3: Import runtime helpers in `task_state.py`**

Add:

```python
from app.services.task_runtime import lease_expiry_from_now, normalize_runtime_status, task_attempt_count
```

- [ ] **Step 4: Normalize archive task runtime fields**

Update `_normalize_task_item()` usage by adding a small helper near `_normalize_archive_queue()`:

```python
def _normalize_archive_runtime_item(item: Any, default_status: str) -> dict:
    normalized = _normalize_task_item(item, default_status)
    meta = normalized.get("meta") if isinstance(normalized.get("meta"), dict) else {}
    status_default = "waiting_children" if meta.get("batch_expected_items") else default_status
    normalized["status"] = normalize_runtime_status(normalized.get("status"), status_default)
    if meta.get("batch_expected_items") and normalized["status"] == "running":
        normalized["status"] = "waiting_children"
    if normalized["status"] == "running":
        normalized["attempt_count"] = max(task_attempt_count(normalized), 1)
    return normalized
```

Then replace archive queue normalization list comprehensions with `_normalize_archive_runtime_item(...)` for active, queued, and recent failures.

- [ ] **Step 5: Assign lease fields when starting archive tasks**

In `start_archive_task()`, when a task is found and moved to active, set:

```python
now = china_now_iso()
normalized["status"] = "running"
normalized["started_at"] = normalized.get("started_at") or now
normalized["heartbeat_at"] = now
normalized["last_progress_at"] = now
normalized["lease_expires_at"] = lease_expiry_from_now()
normalized["attempt_count"] = max(task_attempt_count(normalized) + 1, 1)
normalized["updated_at"] = now
```

When the task is already active, refresh `heartbeat_at`, `last_progress_at`, `lease_expires_at`, and `updated_at` without incrementing `attempt_count`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
pytest tests/test_task_state.py::ArchiveQueueStateTest -q
```

Expected: all `ArchiveQueueStateTest` tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/task_state.py tests/test_task_state.py
git commit -m "feat: normalize archive task runtime state"
```

## Task 3: Queue Repair Summary

**Files:**
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing repair tests**

Add these tests to `ArchiveQueueStateTest`:

```python
    def test_repair_archive_queue_requeues_expired_running_task(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-expired",
                        "title": "Expired",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                        "attempt_count": 1,
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["examined"], 1)
        self.assertEqual(result["summary"]["requeued"], 1)
        self.assertEqual(result["queue"]["queued"][0]["id"], "task-expired")
        self.assertEqual(result["queue"]["queued"][0]["status"], "queued")

    def test_repair_archive_queue_fails_expired_task_without_attempts(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "task-dead",
                        "title": "Dead",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                        "attempt_count": 3,
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True), \
                patch("app.services.task_state.china_now_iso", return_value="2026-06-04T10:00:00+08:00"):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual(result["queue"]["recent_failures"][0]["id"], "task-dead")

    def test_repair_archive_queue_skips_paused_task(self):
        state = {
            "archive_queue": {
                "active": [{"id": "paused-1", "title": "Paused", "status": "paused", "lease_expires_at": "2026-06-04T09:00:00+08:00"}],
                "queued": [],
                "recent_failures": [],
            }
        }
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.is_lease_expired", return_value=True):
            result = store.repair_archive_queue()

        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertEqual(result["queue"]["active"][0]["status"], "paused")
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_task_state.py::ArchiveQueueStateTest::test_repair_archive_queue_requeues_expired_running_task tests/test_task_state.py::ArchiveQueueStateTest::test_repair_archive_queue_fails_expired_task_without_attempts tests/test_task_state.py::ArchiveQueueStateTest::test_repair_archive_queue_skips_paused_task -q
```

Expected: `TaskStateStore` has no `repair_archive_queue`.

- [ ] **Step 3: Import repair helpers**

In `app/services/task_state.py`, extend the runtime import:

```python
from app.services.task_runtime import (
    DEFAULT_MAX_ATTEMPTS,
    is_lease_expired,
    lease_expiry_from_now,
    normalize_runtime_status,
    task_attempt_count,
    task_attempts_remaining,
)
```

- [ ] **Step 4: Implement `repair_archive_queue()`**

Add method to `TaskStateStore` near `requeue_active_tasks()`:

```python
    def repair_archive_queue(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
        summary = {
            "examined": 0,
            "requeued": 0,
            "failed": 0,
            "finalized": 0,
            "skipped": 0,
            "errors": [],
        }

        def _mutate(payload: dict) -> dict:
            now = china_now_iso()
            active = []
            queued = [_normalize_archive_runtime_item(item, "queued") for item in (payload.get("queued") or [])]
            recent_failures = [_normalize_archive_runtime_item(item, "failed") for item in (payload.get("recent_failures") or [])]

            for item in payload.get("active") or []:
                normalized = _normalize_archive_runtime_item(item, "running")
                status = normalize_runtime_status(normalized.get("status"), "running")
                summary["examined"] += 1

                if status in {"paused", "waiting_children", "blocked"}:
                    summary["skipped"] += 1
                    active.append(normalized)
                    continue

                if status != "running" or not is_lease_expired(normalized.get("lease_expires_at")):
                    summary["skipped"] += 1
                    active.append(normalized)
                    continue

                normalized["updated_at"] = now
                normalized["heartbeat_at"] = ""
                normalized["lease_expires_at"] = ""

                if task_attempts_remaining(normalized, max_attempts=max_attempts):
                    normalized["status"] = "queued"
                    normalized["message"] = "检测到任务心跳过期，已重新排队。"
                    queued.insert(0, normalized)
                    summary["requeued"] += 1
                else:
                    normalized["status"] = "failed"
                    normalized["message"] = "任务心跳过期且已达到最大重试次数。"
                    recent_failures.insert(0, normalized)
                    summary["failed"] += 1

            payload["active"] = active
            payload["queued"] = queued
            payload["recent_failures"] = recent_failures[:20]
            return payload

        queue = self._update_archive_queue(_mutate)
        return {"summary": summary, "queue": queue}
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
pytest tests/test_task_state.py::ArchiveQueueStateTest -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/task_state.py tests/test_task_state.py
git commit -m "feat: add archive queue repair summary"
```

## Task 4: Diagnostics Queue And Stale Candidate Payload

**Files:**
- Modify: `app/services/runtime_diagnostics.py`
- Test: `tests/test_runtime_diagnostics.py`

- [ ] **Step 1: Write failing diagnostics test**

Add this test to `tests/test_runtime_diagnostics.py`:

```python
    def test_build_runtime_diagnostics_includes_archive_queue_summary(self):
        queue = {
            "active": [
                {"id": "task-1", "status": "running", "title": "Running", "lease_expires_at": "2026-06-04T09:00:00+08:00"},
                {"id": "batch-1", "status": "waiting_children", "title": "Batch"},
            ],
            "queued": [{"id": "task-2", "status": "queued", "title": "Queued"}],
            "recent_failures": [{"id": "task-3", "status": "failed", "title": "Failed"}],
            "running_count": 2,
            "queued_count": 1,
            "failed_count": 1,
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics.task_state_store, "load_archive_queue", return_value=queue), \
                patch.object(runtime_diagnostics, "is_lease_expired", return_value=True):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["archive_queue"]["running_count"], 2)
        self.assertEqual(payload["archive_queue"]["queued_count"], 1)
        self.assertEqual(payload["archive_queue"]["failed_count"], 1)
        self.assertEqual(payload["archive_queue"]["stale_candidates"][0]["id"], "task-1")
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest tests/test_runtime_diagnostics.py::RuntimeDiagnosticsTest::test_build_runtime_diagnostics_includes_archive_queue_summary -q
```

Expected: missing `archive_queue` key.

- [ ] **Step 3: Import task state and lease helper**

In `app/services/runtime_diagnostics.py`, add:

```python
from app.api.dependencies import task_state_store
from app.services.task_runtime import is_lease_expired, normalize_runtime_status
```

- [ ] **Step 4: Add queue summary helpers**

Add helper functions:

```python
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
        return {"available": False, "error": str(exc), "running_count": 0, "queued_count": 0, "failed_count": 0, "stale_candidates": []}

    active = [item for item in queue.get("active") or [] if isinstance(item, dict)]
    stale = [
        _safe_task_preview(item)
        for item in active
        if normalize_runtime_status(item.get("status"), "running") == "running"
        and is_lease_expired(item.get("lease_expires_at"))
    ][:20]
    return {
        "available": True,
        "running_count": _int(queue.get("running_count") or len(active)),
        "queued_count": _int(queue.get("queued_count") or len(queue.get("queued") or [])),
        "failed_count": _int(queue.get("failed_count") or len(queue.get("recent_failures") or [])),
        "waiting_children_count": sum(1 for item in active if normalize_runtime_status(item.get("status"), "running") == "waiting_children"),
        "stale_candidates": stale,
    }
```

Add `"archive_queue": _archive_queue_diagnostics()` to the payload in `build_runtime_diagnostics()`.

- [ ] **Step 5: Run diagnostics tests**

Run:

```bash
pytest tests/test_runtime_diagnostics.py -q
```

Expected: all diagnostics tests pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/runtime_diagnostics.py tests/test_runtime_diagnostics.py
git commit -m "feat: include archive queue diagnostics"
```

## Task 5: Authenticated Queue Repair API

**Files:**
- Modify: `app/api/tasks_routes.py`
- Test: `tests/test_runtime_diagnostics.py` or `tests/test_web_routes.py`

- [ ] **Step 1: Write failing route test**

Add this to `tests/test_runtime_diagnostics.py`:

```python
    def test_repair_archive_queue_route_requires_session_and_returns_summary(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        repair_payload = {
            "summary": {"examined": 1, "requeued": 1, "failed": 0, "finalized": 0, "skipped": 0, "errors": []},
            "queue": {"running_count": 0, "queued_count": 1, "failed_count": 0, "active": [], "queued": [], "recent_failures": []},
        }

        with patch.object(tasks_routes.task_state_store, "repair_archive_queue", return_value=repair_payload), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(tasks_routes.repair_archive_queue(request))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["requeued"], 1)
```

Add these imports at the top of `tests/test_runtime_diagnostics.py`:

```python
import asyncio
from types import SimpleNamespace
from app.api import tasks_routes
```

- [ ] **Step 2: Run route test and verify it fails**

Run:

```bash
pytest tests/test_runtime_diagnostics.py::RuntimeDiagnosticsTest::test_repair_archive_queue_route_requires_session_and_returns_summary -q
```

Expected: missing `repair_archive_queue` route function.

- [ ] **Step 3: Add route**

In `app/api/tasks_routes.py`, add:

```python
@router.post("/tasks/archive-queue/repair")
async def repair_archive_queue(request: Request):
    _require_session_auth(request)

    def _repair_payload() -> dict:
        result = task_state_store.repair_archive_queue()
        return {
            "success": True,
            "message": "队列状态修复完成。",
            "summary": result.get("summary") or {},
            "archive_queue": result.get("queue") or {},
        }

    result = await run_task_api(_repair_payload)
    append_business_log(
        "archive",
        "queue_repair_requested",
        result.get("message") or "队列状态修复完成。",
        **(result.get("summary") or {}),
    )
    return result
```

- [ ] **Step 4: Run route and auth tests**

Run:

```bash
pytest tests/test_runtime_diagnostics.py tests/test_auth_guard.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/tasks_routes.py tests/test_runtime_diagnostics.py
git commit -m "feat: add archive queue repair endpoint"
```

## Task 6: Frontend Runtime Status And Actions

**Files:**
- Modify: `frontend/src/lib/dashboardStatus.js`
- Modify: `frontend/src/lib/dashboardStatus.test.mjs`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/TasksPage.vue`

- [ ] **Step 1: Write failing frontend helper tests**

Add tests to `frontend/src/lib/dashboardStatus.test.mjs`:

```javascript
test("runtime status labels distinguish waiting children and blocked verification", () => {
  assert.equal(normalizeRuntimeStatusLabel("waiting_children"), "等待子任务");
  assert.equal(normalizeRuntimeStatusLabel("blocked", "needs_verification"), "需要验证");
});

test("queue repair action points to archive queue repair endpoint", () => {
  assert.deepEqual(runtimeTaskAction({ status: "running", stale: true }), {
    kind: "api",
    label: "修复队列",
    endpoint: "/api/tasks/archive-queue/repair",
    method: "POST",
  });
});

test("blocked verification action opens official homepage", () => {
  assert.deepEqual(runtimeTaskAction({ status: "blocked", blocked_reason: "needs_verification", url: "https://makerworld.com" }), {
    kind: "external",
    label: "访问主页",
    href: "https://makerworld.com",
  });
});
```

Update the import block:

```javascript
import {
  dashboardStatusAction,
  dashboardStatusElementKind,
  normalizeRuntimeStatusLabel,
  runtimeTaskAction,
} from "./dashboardStatus.js";
```

- [ ] **Step 2: Run frontend helper test and verify it fails**

Run:

```bash
npm --prefix frontend exec node --test src/lib/dashboardStatus.test.mjs
```

Expected: missing exported helper functions.

- [ ] **Step 3: Implement helper exports**

Add to `frontend/src/lib/dashboardStatus.js`:

```javascript
const RUNTIME_STATUS_LABELS = {
  queued: "排队中",
  running: "运行中",
  waiting_children: "等待子任务",
  paused: "已暂停",
  blocked: "需处理",
  failed: "失败",
  completed: "已完成",
};

const BLOCKED_REASON_LABELS = {
  needs_cookie: "需要 Cookie",
  needs_verification: "需要验证",
  rate_limited: "访问受限",
  source_unavailable: "源端不可用",
  worker_stopped: "Worker 未运行",
};

export function normalizeRuntimeStatusLabel(status, blockedReason = "") {
  const cleanStatus = String(status || "").trim().toLowerCase();
  const cleanReason = String(blockedReason || "").trim().toLowerCase();
  if (cleanStatus === "blocked" && BLOCKED_REASON_LABELS[cleanReason]) {
    return BLOCKED_REASON_LABELS[cleanReason];
  }
  return RUNTIME_STATUS_LABELS[cleanStatus] || "未知";
}

export function runtimeTaskAction(item = {}) {
  const status = String(item.status || "").trim().toLowerCase();
  const reason = String(item.blocked_reason || item.blockedReason || "").trim().toLowerCase();
  if (item.stale || item.recoverable) {
    return {
      kind: "api",
      label: "修复队列",
      endpoint: "/api/tasks/archive-queue/repair",
      method: "POST",
    };
  }
  if (status === "blocked" && ["needs_verification", "needs_cookie"].includes(reason) && item.url) {
    return {
      kind: "external",
      label: "访问主页",
      href: item.url,
    };
  }
  return null;
}
```

- [ ] **Step 4: Wire helpers into pages**

In `frontend/src/pages/TasksPage.vue`, import the helpers and replace inline status labels for archive queue rows with `normalizeRuntimeStatusLabel(item.status, item.blocked_reason)`. Add the returned action from `runtimeTaskAction(item)` only where the row already renders action buttons.

In `frontend/src/pages/DashboardPage.vue`, reuse `dashboardStatusAction()` for official-site actions. Add one compact queue diagnostics action when `payload.runtime_diagnostics.archive_queue.stale_candidates.length > 0`. Keep existing compact dark UI structure and do not create new card nesting.

- [ ] **Step 5: Run frontend tests and build**

Run:

```bash
npm --prefix frontend exec node --test src/lib/dashboardStatus.test.mjs src/lib/stateRefresh.test.mjs
npm --prefix frontend run build
```

Expected: tests and build pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/dashboardStatus.js frontend/src/lib/dashboardStatus.test.mjs frontend/src/pages/DashboardPage.vue frontend/src/pages/TasksPage.vue
git commit -m "feat: clarify runtime task status actions"
```

## Task 7: Documentation And Verification

**Files:**
- Modify: `docs/modules/tasks_worker.md`
- Modify: `docs/modules/core.md`
- Modify: `README.md`
- Modify: `app/core/settings.py`
- Modify: `frontend/package.json`

- [ ] **Step 1: Update module docs**

In `docs/modules/tasks_worker.md`, add a short section named `Runtime task governance` that documents:

```markdown
## Runtime task governance

Archive queue tasks use shared runtime status values: `queued`, `running`, `waiting_children`, `paused`, `blocked`, `failed`, and `completed`.

Batch parent tasks track child progress and should use `waiting_children` while children are still queued or running. Executable child tasks use leases and heartbeats so stale `running` work can be repaired. The repair action requeues expired tasks while retry budget remains, fails exhausted tasks, and skips paused or intentionally blocked tasks.
```

In `docs/modules/core.md`, add diagnostics fields:

```markdown
Runtime diagnostics include database table summaries, state-event counts, recent log counts, archive queue counts, waiting-child counts, and stale lease candidates. Diagnostics must not expose cookies, tokens, signed URLs, or raw verification page bodies.
```

- [ ] **Step 2: Bump version for release**

Bump the release version to `0.9.0` because this plan changes visible task semantics and adds an operational endpoint:

```python
APP_VERSION = "0.9.0"
```

in `app/core/settings.py`, and set `"version": "0.9.0"` in `frontend/package.json`.

- [ ] **Step 3: Update README latest release notes**

Add a top release note for `v0.9.0` covering:

```markdown
- Added shared runtime task status semantics for long-running queue work.
- Added archive queue stale-task repair diagnostics and endpoint.
- Clarified dashboard/task status labels for waiting children, blocked verification, and repairable work.
```

Keep only the latest three releases expanded and move older entries into the existing collapsed section.

- [ ] **Step 4: Run full focused verification**

Run:

```bash
pytest tests/test_state_contracts.py tests/test_task_state.py tests/test_runtime_diagnostics.py tests/test_auth_guard.py -q
npm --prefix frontend exec node --test src/lib/dashboardStatus.test.mjs src/lib/stateRefresh.test.mjs
npm --prefix frontend run build
git diff --check
```

Expected: all commands pass with no whitespace errors.

- [ ] **Step 5: Commit release docs**

```bash
git add docs/modules/tasks_worker.md docs/modules/core.md README.md app/core/settings.py frontend/package.json
git commit -m "chore: release v0.9.0"
```

## Self-Review Checklist

- Spec coverage:
  - Runtime task governance: Tasks 1-3.
  - Data/write governance: retained as boundary policy and diagnostics in Tasks 3-4; no high-risk relational migration.
  - User operation flow: Task 6.
  - Observability/testing: Tasks 4 and 7.
- No placeholders: every task includes concrete files, test names, code snippets, and commands.
- Scope control: this plan implements the first project-wide optimization pass without rewriting MakerWorld archiving, replacing JSON state, or changing public API paths without compatibility.
