# MakerHub System Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve MakerHub maintainability and runtime stability by governing state contracts, refresh behavior, write frequency, API route boundaries, large service boundaries, frontend page logic, and App/Worker diagnostics without changing public API paths or redesigning the UI.

**Architecture:** Implement the approved governance design as continuous independent batches. Each batch preserves runtime behavior, adds or updates tests where behavior is formalized, and commits separately. Route and service decompositions are done through compatibility wrappers and stable imports so existing callers keep working while files become smaller and clearer.

**Tech Stack:** Python 3, FastAPI, Pydantic, Postgres-backed JSON state via psycopg, Vue 3, Vite, Node test runner, unittest/pytest-compatible Python tests.

---

## Source Design

Use this approved spec as the source of truth:

- `docs/superpowers/specs/2026-06-03-makerhub-system-governance-design.md`

## Execution Rules

- Do not push unless the user explicitly asks.
- Do not bump version or README release notes until the user explicitly asks to push.
- Do not change public API paths.
- Do not rewrite MakerWorld archive/download logic.
- Do not redesign UI visuals.
- Commit each task or tightly related task group separately.
- Before route splitting, run tests that prove current route registration behavior.
- After every extraction, run the narrow tests listed in that task.
- When editing files with existing unrelated changes, stop and inspect before touching them.

## File Map

### Documentation

- Create: `docs/modules/state_contracts.md`
  - Authoritative runtime state key and field contract.
- Modify: `docs/MODULES.md`
  - Link state contract and route-boundary guidance.
- Modify: `docs/ARCHITECTURE.md`
  - Add short state/event/log governance guidance.

### Backend State and Events

- Create: `app/services/state_contracts.py`
  - Constants for state keys, event scopes, common statuses, and safe helper functions.
- Modify: `app/services/task_state.py`
  - Use constants where low risk; keep existing JSON shape.
- Modify: `app/services/state_events.py`
  - Reuse scope constants and keep event behavior stable.
- Test: `tests/test_state_contracts.py`
  - Validate constants, scope mapping, and backward-compatible status sets.

### Backend Logs and Write Frequency

- Modify: `app/services/business_logs.py`
  - Add or reuse message sanitization boundaries if missing.
- Modify: `app/services/remote_refresh.py`
  - Verify batch progress writes are not per-item durable success logs.
- Modify: `app/services/subscriptions.py`
  - Verify subscription sync writes only meaningful summaries.
- Modify: `app/services/archive_worker.py`
  - Verify batch enqueue and missing 3MF retry write useful summaries.
- Test: existing tests under `tests/test_business_logs.py`, `tests/test_remote_refresh.py`, `tests/test_subscriptions.py`, `tests/test_archive_worker_batch_retry.py`.

### API Route Boundaries

- Create: `app/api/dependencies.py`
  - Shared singleton managers and stores currently created in `app/api/config.py`.
- Create route modules while preserving existing paths:
  - `app/api/config_routes.py`
  - `app/api/sharing_routes.py`
  - `app/api/models_routes.py`
  - `app/api/source_library_routes.py`
  - `app/api/tasks_routes.py`
  - `app/api/subscriptions_routes.py`
  - `app/api/remote_refresh_routes.py`
  - `app/api/logs_routes.py`
- Modify: `app/api/config.py`
  - Reduce to compatibility imports or delete only after all routers are registered elsewhere.
- Modify: `app/main.py`
  - Register new routers exactly once.
- Test: `tests/test_web_routes.py`, `tests/test_auth_guard.py`, targeted API tests that use route handlers.

### Backend Service Decomposition

- Create: `app/services/archive_naming.py`
  - Pure filename/path sanitization wrappers if extracted from `legacy_archiver.py`.
- Create: `app/services/archive_comments.py`
  - Comment/profile normalization wrappers if extracted from `legacy_archiver.py`.
- Create: `app/services/task_messages.py`
  - Message sanitization and status text helpers extracted from `task_state.py` only when tests cover them.
- Create: `app/services/remote_refresh_summary.py`
  - Batch summary and message builder helpers extracted from `remote_refresh.py`.
- Modify existing modules to import helpers while keeping compatibility re-exports where required.
- Test: `tests/test_legacy_archiver_validation.py`, `tests/test_comment_replies.py`, `tests/test_task_state.py`, `tests/test_remote_refresh.py`.

### Frontend Refresh and Page Logic

- Create: `frontend/src/lib/stateRefresh.js`
  - Shared state-event debounce and visibility refresh helper.
- Test: `frontend/src/lib/stateRefresh.test.mjs`
  - Node tests for debounce, event scope filtering, visibility refresh hooks with fake timers or injectable scheduler.
- Modify pages to use helper in small batches:
  - `frontend/src/pages/DashboardPage.vue`
  - `frontend/src/pages/TasksPage.vue`
  - `frontend/src/pages/RemoteRefreshPage.vue`
  - `frontend/src/pages/OrganizerPage.vue`
  - `frontend/src/pages/SubscriptionsPage.vue`
  - `frontend/src/pages/SubscriptionsManagePage.vue`
- Create optional helper modules if extraction stays small:
  - `frontend/src/lib/modelDetailPayload.js`
  - `frontend/src/lib/organizerPayload.js`
  - `frontend/src/lib/settingsForms.js`
- Test: existing Node tests plus new tests for extracted helpers.

### Runtime Diagnostics

- Modify: `app/api/system.py` or existing `/api/bootstrap` payload source.
- Modify: `app/core/settings.py` only if needed to expose already-computed values.
- Modify: `frontend/src/pages/SettingsPage.vue` only if showing diagnostics is small and consistent with current UI.
- Test: `tests/test_self_update.py`, `tests/test_config_cookies.py`, targeted bootstrap/system tests.

---

## Task 1: Add State Contract Documentation and Constants

**Files:**
- Create: `docs/modules/state_contracts.md`
- Modify: `docs/MODULES.md`
- Modify: `docs/ARCHITECTURE.md`
- Create: `app/services/state_contracts.py`
- Create: `tests/test_state_contracts.py`

- [ ] **Step 1: Write the failing test for state constants**

Create `tests/test_state_contracts.py` with:

```python
from app.services import state_contracts


def test_core_state_keys_are_stable():
    assert state_contracts.ARCHIVE_QUEUE_STATE_KEY == "archive_queue"
    assert state_contracts.MISSING_3MF_STATE_KEY == "missing_3mf"
    assert state_contracts.ORGANIZE_TASKS_STATE_KEY == "organize_tasks"
    assert state_contracts.SUBSCRIPTIONS_STATE_KEY == "subscriptions_state"
    assert state_contracts.REMOTE_REFRESH_STATE_KEY == "remote_refresh_state"


def test_state_event_scopes_cover_dashboard_consumers():
    scopes = state_contracts.dashboard_event_scopes()
    assert scopes == [
        "archive_queue",
        "missing_3mf",
        "organize_tasks",
        "subscriptions_state",
        "remote_refresh_state",
    ]


def test_status_sets_include_existing_values():
    assert {"queued", "running", "completed", "failed"}.issubset(state_contracts.ARCHIVE_TASK_STATUSES)
    assert {"missing", "queued", "failed", "download_limited"}.issubset(state_contracts.MISSING_3MF_STATUSES)
    assert {"idle", "running", "success", "error", "disabled"}.issubset(state_contracts.REMOTE_REFRESH_STATUSES)
    assert {"idle", "running", "success", "error", "pending"}.issubset(state_contracts.SUBSCRIPTION_STATUSES)
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
.venv/bin/python -m pytest tests/test_state_contracts.py -q
```

Expected: FAIL because `app.services.state_contracts` does not exist.

- [ ] **Step 3: Add the constants module**

Create `app/services/state_contracts.py`:

```python
from __future__ import annotations

ARCHIVE_QUEUE_STATE_KEY = "archive_queue"
MISSING_3MF_STATE_KEY = "missing_3mf"
ORGANIZE_TASKS_STATE_KEY = "organize_tasks"
SUBSCRIPTIONS_STATE_KEY = "subscriptions_state"
REMOTE_REFRESH_STATE_KEY = "remote_refresh_state"
MODEL_FLAGS_STATE_KEY = "model_flags"
THREE_MF_LIMIT_GUARD_STATE_KEY = "three_mf_limit_guard"
THREE_MF_DAILY_QUOTA_STATE_KEY = "three_mf_daily_quota"

ARCHIVE_TASK_STATUSES = frozenset({
    "queued",
    "running",
    "completed",
    "success",
    "failed",
    "cancelled",
    "skipped",
})

MISSING_3MF_STATUSES = frozenset({
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
})

ORGANIZE_TASK_STATUSES = frozenset({
    "queued",
    "running",
    "success",
    "failed",
    "skipped",
})

SUBSCRIPTION_STATUSES = frozenset({
    "idle",
    "pending",
    "running",
    "success",
    "error",
    "deleted",
})

REMOTE_REFRESH_STATUSES = frozenset({
    "idle",
    "running",
    "success",
    "error",
    "disabled",
})

DASHBOARD_EVENT_SCOPES = (
    ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_STATE_KEY,
    ORGANIZE_TASKS_STATE_KEY,
    SUBSCRIPTIONS_STATE_KEY,
    REMOTE_REFRESH_STATE_KEY,
)


def dashboard_event_scopes() -> list[str]:
    return list(DASHBOARD_EVENT_SCOPES)
```

- [ ] **Step 4: Run state constants tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_state_contracts.py -q
```

Expected: PASS.

- [ ] **Step 5: Add state contract documentation**

Create `docs/modules/state_contracts.md` with sections for:

```markdown
# Runtime State Contracts

## Purpose

This document records MakerHub's Postgres-backed JSON state keys, common fields, status values, writers, readers, frontend consumers, and event scopes.

## Common Fields

| Field | Meaning | Notes |
| --- | --- | --- |
| `status` | Current lifecycle status for one item or process | Use module-specific allowed values below. |
| `state` | Diagnostic state for source/platform checks | Used when `status` would be too coarse. |
| `running` | Boolean process marker | Should match `status == "running"` when both exist. |
| `message` | Current item-level message | Keep short and sanitized. |
| `last_message` | Most recent process summary | Keep short and sanitized. |
| `updated_at` | Last state mutation time | ISO text in China timezone where existing code uses it. |

## State Keys

| Key | Owner | Main Writers | Main Readers | Event Scope |
| --- | --- | --- | --- | --- |
| `archive_queue` | TaskStateStore | archive worker, task APIs | dashboard, tasks page, worker | `archive_queue` |
| `missing_3mf` | TaskStateStore | archive worker, remote refresh, task APIs | dashboard, tasks page, source health | `missing_3mf` |
| `organize_tasks` | TaskStateStore | local organizer/import | dashboard, organizer page | `organize_tasks` |
| `subscriptions_state` | TaskStateStore | subscription manager | dashboard, subscription pages | `subscriptions_state` |
| `remote_refresh_state` | TaskStateStore | remote refresh manager | dashboard, remote refresh page | `remote_refresh_state` |
| `model_flags` | TaskStateStore | model APIs, source deletion checks | model pages, catalog | `model_flags` |
| `three_mf_limit_guard` | source health/archive worker | 3MF quota logic | dashboard, tasks, archive worker | `three_mf_limit_guard` |
| `three_mf_daily_quota` | 3MF quota service | archive worker | archive worker, settings | `three_mf_daily_quota` |

## Write Frequency Rules

- Durable logs should capture batch start, batch end, failures, and unusual diagnostics.
- Per-item success progress should prefer in-memory/current state fields instead of durable log rows.
- State events should trigger relevant payload refreshes, not broad full-page refreshes.
- Messages must be sanitized before entering JSON state or logs.
```

- [ ] **Step 6: Link the document**

Modify `docs/MODULES.md` and `docs/ARCHITECTURE.md` to mention `docs/modules/state_contracts.md` near the existing state/data sections.

- [ ] **Step 7: Validate docs and tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_state_contracts.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

Run:

```bash
git add app/services/state_contracts.py tests/test_state_contracts.py docs/modules/state_contracts.md docs/MODULES.md docs/ARCHITECTURE.md
git commit -m "docs: add runtime state contracts"
```

---

## Task 2: Reuse State Constants in Low-Risk Consumers

**Files:**
- Modify: `app/services/task_state.py`
- Modify: `app/services/source_health.py`
- Modify: `app/services/state_events.py`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Test: `tests/test_task_state.py`
- Test: `tests/test_source_health.py`
- Test: `frontend/src/lib/dashboardStatus.test.mjs`

- [ ] **Step 1: Write or extend tests for dashboard scopes**

If `tests/test_state_contracts.py` exists from Task 1, add:

```python
def test_dashboard_scopes_are_plain_strings_for_frontend_payloads():
    assert all(isinstance(scope, str) for scope in state_contracts.dashboard_event_scopes())
    assert "archive_queue" in state_contracts.dashboard_event_scopes()
```

- [ ] **Step 2: Run the focused test**

Run:

```bash
.venv/bin/python -m pytest tests/test_state_contracts.py -q
```

Expected: PASS after Task 1; this guards the helper before using it.

- [ ] **Step 3: Replace low-risk duplicate state key strings**

In backend files, import constants only for module-level state keys where it does not create circular imports:

```python
from app.services.state_contracts import (
    ARCHIVE_QUEUE_STATE_KEY,
    MISSING_3MF_STATE_KEY,
    ORGANIZE_TASKS_STATE_KEY,
    REMOTE_REFRESH_STATE_KEY,
    SUBSCRIPTIONS_STATE_KEY,
)
```

Use constants in safe locations such as event scope lists and state key reads/writes. Do not replace every status string in this task.

- [ ] **Step 4: Keep frontend scope strings stable**

Do not introduce generated frontend constants from Python. If `DashboardPage.vue` has the list:

```javascript
["archive_queue", "missing_3mf", "organize_tasks", "subscriptions_state", "remote_refresh_state"]
```

leave it as literal unless a JS helper is added in Task 5. The test from Task 1 documents the matching backend list.

- [ ] **Step 5: Run focused regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py tests/test_source_health.py tests/test_state_contracts.py -q
node --test frontend/src/lib/dashboardStatus.test.mjs
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add app/services/task_state.py app/services/source_health.py app/services/state_events.py tests/test_state_contracts.py
git commit -m "refactor: centralize runtime state constants"
```

If no backend file changes are necessary after inspection, skip this commit and record the result in the final summary.

---

## Task 3: Add Shared Frontend State Refresh Helper

**Files:**
- Create: `frontend/src/lib/stateRefresh.js`
- Create: `frontend/src/lib/stateRefresh.test.mjs`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/TasksPage.vue`
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`

- [ ] **Step 1: Write tests for refresh scheduling**

Create `frontend/src/lib/stateRefresh.test.mjs`:

```javascript
import assert from "node:assert/strict";
import { test } from "node:test";

import { createScopedRefreshScheduler, shouldHandleStateEvent } from "./stateRefresh.js";

test("shouldHandleStateEvent matches configured scopes", () => {
  assert.equal(shouldHandleStateEvent({ scope: "archive_queue" }, ["archive_queue"]), true);
  assert.equal(shouldHandleStateEvent({ scope: "remote_refresh_state" }, ["archive_queue"]), false);
  assert.equal(shouldHandleStateEvent(null, ["archive_queue"]), false);
});

test("createScopedRefreshScheduler coalesces events", () => {
  const calls = [];
  const timers = [];
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue"],
    delay: 25,
    setTimeoutFn: (fn, delay) => {
      timers.push({ fn, delay });
      return timers.length;
    },
    clearTimeoutFn: () => {},
    refresh: () => calls.push("refresh"),
  });

  scheduler.handleEvent({ scope: "archive_queue" });
  scheduler.handleEvent({ scope: "archive_queue" });

  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 25);
  timers[0].fn();
  assert.deepEqual(calls, ["refresh"]);
});
```

- [ ] **Step 2: Run the failing frontend test**

Run:

```bash
node --test frontend/src/lib/stateRefresh.test.mjs
```

Expected: FAIL because `stateRefresh.js` does not exist.

- [ ] **Step 3: Implement the helper**

Create `frontend/src/lib/stateRefresh.js`:

```javascript
export function shouldHandleStateEvent(event, scopes = []) {
  const scope = String(event?.scope || "").trim();
  return Boolean(scope && scopes.includes(scope));
}

export function createScopedRefreshScheduler({
  scopes = [],
  delay = 250,
  refresh,
  setTimeoutFn = typeof window !== "undefined" ? window.setTimeout.bind(window) : setTimeout,
  clearTimeoutFn = typeof window !== "undefined" ? window.clearTimeout.bind(window) : clearTimeout,
} = {}) {
  let timer = null;

  function clear() {
    if (timer) {
      clearTimeoutFn(timer);
      timer = null;
    }
  }

  function schedule() {
    if (timer || typeof refresh !== "function") return;
    timer = setTimeoutFn(() => {
      timer = null;
      refresh();
    }, delay);
  }

  function handleEvent(event) {
    if (shouldHandleStateEvent(event, scopes)) {
      schedule();
    }
  }

  return { handleEvent, schedule, clear };
}
```

- [ ] **Step 4: Run helper tests**

Run:

```bash
node --test frontend/src/lib/stateRefresh.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Migrate one page at a time**

Start with `DashboardPage.vue` because it already subscribes to state events. Replace local debounce/visibility refresh code only if the helper cleanly maps to existing behavior. Keep `load()` semantics unchanged.

Then migrate `TasksPage.vue` and `RemoteRefreshPage.vue` only if the mapping remains straightforward. If a page has special in-flight behavior, keep its local logic and document why.

- [ ] **Step 6: Run frontend regression tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add frontend/src/lib/stateRefresh.js frontend/src/lib/stateRefresh.test.mjs frontend/src/pages/DashboardPage.vue frontend/src/pages/TasksPage.vue frontend/src/pages/RemoteRefreshPage.vue
git commit -m "refactor: share frontend state refresh scheduling"
```

---

## Task 4: Audit and Reduce Batch Write Amplification

**Files:**
- Modify: `docs/modules/state_contracts.md`
- Modify: `app/services/remote_refresh.py`
- Modify: `app/services/subscriptions.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/local_organizer.py`
- Tests: existing module tests listed below

- [ ] **Step 1: Inventory write points**

Use these searches and record notable results in `docs/modules/state_contracts.md` under a "Write Frequency Audit" section:

```bash
rg -n "append_business_log|publish_state_event|save_.*state|patch_.*state|update_.*task" app/services/remote_refresh.py app/services/subscriptions.py app/services/archive_worker.py app/services/local_organizer.py app/services/task_state.py
```

- [ ] **Step 2: Identify safe reductions**

Only modify code when a repeated per-item success log or event is clearly redundant with an existing batch summary. Do not remove failure logs or user-visible progress fields.

Safe example pattern:

```python
# Keep this: one batch completion log with counts.
append_business_log("remote_refresh", "batch_completed", message, total=total, succeeded=succeeded, failed=failed)

# Avoid adding/removing unrelated per-item failure logs in this task.
```

- [ ] **Step 3: Add tests only for changed behavior**

If changing a helper that summarizes batch results, add or update tests in the owning test file. Example for `remote_refresh` summary helper if extracted:

```python
def test_remote_refresh_batch_summary_includes_counts():
    message = remote_refresh._build_batch_summary_message(total=3, succeeded=2, failed=1, remaining=0)
    assert "3" in message
    assert "2" in message
    assert "1" in message
```

Use the actual helper name present after inspection.

- [ ] **Step 4: Run batch-flow tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_subscriptions.py tests/test_archive_worker_batch_retry.py tests/test_local_organizer.py tests/test_business_logs.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add docs/modules/state_contracts.md app/services/remote_refresh.py app/services/subscriptions.py app/services/archive_worker.py app/services/local_organizer.py tests/test_remote_refresh.py tests/test_subscriptions.py tests/test_archive_worker_batch_retry.py tests/test_local_organizer.py tests/test_business_logs.py
git commit -m "refactor: document and reduce batch state writes"
```

If the audit finds no safe code change, commit only the documentation update:

```bash
git add docs/modules/state_contracts.md
git commit -m "docs: document batch write frequency audit"
```

---

## Task 5: Prepare API Route Split with Dependency Module

**Files:**
- Create: `app/api/dependencies.py`
- Modify: `app/api/config.py`
- Modify: `app/main.py`
- Test: `tests/test_web_routes.py`
- Test: `tests/test_auth_guard.py`

- [ ] **Step 1: Add route registration smoke test**

Add a test to `tests/test_web_routes.py` or a new focused test file if existing patterns require it:

```python
def test_core_api_routes_are_registered():
    from app.main import app

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/api/dashboard" in paths
    assert "/api/models" in paths
    assert "/api/tasks" in paths
    assert "/api/remote-refresh" in paths
    assert "/api/subscriptions" in paths
    assert "/api/logs" in paths
```

- [ ] **Step 2: Run the smoke test before code changes**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_routes.py -q
```

Expected: PASS. This proves current route registration before splitting.

- [ ] **Step 3: Create dependency module without changing behavior**

Create `app/api/dependencies.py` and move only singleton construction from `app/api/config.py` if inspection confirms no side effects are altered. The module should expose existing manager instances with the same objects:

```python
from app.core.store import JsonStore
from app.services.archive_worker import ArchiveTaskManager
from app.services.local_organizer import LocalOrganizerService
from app.services.remote_refresh import RemoteRefreshManager
from app.services.source_library import SourceLibraryManager
from app.services.subscriptions import SubscriptionManager
from app.services.task_state import TaskStateStore

store = JsonStore()
task_store = TaskStateStore()
crawler = ArchiveTaskManager()
subscription_manager = SubscriptionManager(archive_manager=crawler.manager if hasattr(crawler, "manager") else crawler, store=store, task_store=task_store)
local_organizer = LocalOrganizerService(store=store, task_store=task_store)
source_library_manager = SourceLibraryManager(store=store, task_store=task_store)
remote_refresh_manager = RemoteRefreshManager(store=store, task_store=task_store, archive_manager=crawler.manager if hasattr(crawler, "manager") else crawler)
```

Important: inspect current `config.py` object construction before using this exact snippet. The current code may define `crawler` as a wrapper rather than `ArchiveTaskManager` directly. Preserve exact current object graph.

- [ ] **Step 4: Re-export dependencies from `config.py` temporarily**

Modify `app/api/config.py` so imports used by `app/main.py` still work:

```python
from app.api.dependencies import crawler, local_organizer, remote_refresh_manager, source_library_manager, subscription_manager
```

Do not split routes yet.

- [ ] **Step 5: Run route and startup-adjacent tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_routes.py tests/test_auth_guard.py tests/test_config_cookies.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add app/api/dependencies.py app/api/config.py app/main.py tests/test_web_routes.py
git commit -m "refactor: isolate api singleton dependencies"
```

---

## Task 6: Split Low-Risk API Route Domains

**Files:**
- Create: `app/api/logs_routes.py`
- Create: `app/api/remote_refresh_routes.py`
- Create: `app/api/subscriptions_routes.py`
- Modify: `app/api/config.py`
- Modify: `app/main.py`
- Tests: relevant route tests

- [ ] **Step 1: Start with logs route**

Move only `/api/logs` handler and directly required helper imports from `app/api/config.py` to `app/api/logs_routes.py`.

Use:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/api")
```

Keep path decorator as:

```python
@router.get("/logs")
```

- [ ] **Step 2: Register logs router**

Modify `app/main.py`:

```python
from app.api.logs_routes import router as logs_router
...
app.include_router(logs_router)
```

Remove or disable the old `/logs` route from `config.py` so it is not registered twice.

- [ ] **Step 3: Test logs route registration**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_routes.py tests/test_business_logs.py -q
```

Expected: PASS.

- [ ] **Step 4: Split remote refresh routes**

Move `/api/remote-refresh`, `/api/remote-refresh/run`, and `/api/config/remote-refresh` to `app/api/remote_refresh_routes.py`. Import `remote_refresh_manager`, `store`, and `task_store` from `app/api/dependencies.py` as needed.

- [ ] **Step 5: Test remote refresh route behavior**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_process_jobs.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 6: Split subscription routes**

Move `/api/subscriptions`, `/api/subscriptions/{subscription_id}`, `/api/subscriptions/{subscription_id}/sync`, and `/api/config/subscriptions` to `app/api/subscriptions_routes.py`.

- [ ] **Step 7: Test subscriptions**

Run:

```bash
.venv/bin/python -m pytest tests/test_subscriptions.py tests/test_source_library.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

Run:

```bash
git add app/api/logs_routes.py app/api/remote_refresh_routes.py app/api/subscriptions_routes.py app/api/config.py app/main.py tests/test_web_routes.py
git commit -m "refactor: split logs remote refresh and subscription routes"
```

---

## Task 7: Split Model, Source Library, Sharing, and Task Routes

**Files:**
- Create: `app/api/models_routes.py`
- Create: `app/api/source_library_routes.py`
- Create: `app/api/sharing_routes.py`
- Create: `app/api/tasks_routes.py`
- Modify: `app/api/config.py`
- Modify: `app/main.py`
- Tests: catalog, sharing, task, auth tests

- [ ] **Step 1: Split source library routes first**

Move source-library routes from `config.py` to `app/api/source_library_routes.py`:

- `/api/source-library`
- `/api/source-library/snapshots/{filename}`
- `/api/source-library/sources/{source_type}/{source_key}`
- `/api/source-library/states/{state_key}`

Run:

```bash
.venv/bin/python -m pytest tests/test_source_library.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 2: Split task and archive action routes**

Move task routes to `app/api/tasks_routes.py`:

- `/api/tasks`
- `/api/tasks/recent-failures/clear`
- `/api/tasks/organize/clear`
- `/api/tasks/missing-3mf/retry`
- `/api/tasks/missing-3mf/retry-all`
- `/api/tasks/missing-3mf/cancel`
- `/api/archive`
- `/api/archive/preview`
- archive repair/backfill admin endpoints if imports remain manageable.

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py tests/test_missing_3mf.py tests/test_archive_worker_batch_retry.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 3: Split sharing routes**

Move sharing and public share routes to `app/api/sharing_routes.py`:

- `/api/sharing/create`
- `/api/sharing/shares`
- `/api/sharing/shares/{share_id}/code`
- `/api/sharing/shares/{share_id}`
- `/api/sharing/shares/cleanup`
- `/api/sharing/receive/preview`
- `/api/sharing/receive/import`
- `/api/public/shares/{share_id}/manifest`
- `/api/public/share-access/{access_code}/manifest`
- `/api/public/shares/{share_id}/files/{file_id}`

Run:

```bash
.venv/bin/python -m pytest tests/test_share_receive_security.py tests/test_mobile_import.py tests/test_upload_limits.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 4: Split model routes**

Move model routes to `app/api/models_routes.py`:

- `/api/models`
- `/api/models/{model_dir:path}`
- comments/download-all/bambu-studio link/public file routes
- model flags
- local model edit/image/file/preview routes
- attachments
- local library merge/import

Run:

```bash
.venv/bin/python -m pytest tests/test_archive_model_index.py tests/test_model_attachments.py tests/test_model_downloads.py tests/test_comment_replies.py tests/test_local_model_edit.py tests/test_web_routes.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

Run:

```bash
git add app/api/models_routes.py app/api/source_library_routes.py app/api/sharing_routes.py app/api/tasks_routes.py app/api/config.py app/main.py tests/test_web_routes.py
git commit -m "refactor: split model sharing source and task routes"
```

---

## Task 8: Extract Low-Risk Backend Helpers

**Files:**
- Create: `app/services/task_messages.py`
- Create: `app/services/remote_refresh_summary.py`
- Modify: `app/services/task_state.py`
- Modify: `app/services/remote_refresh.py`
- Tests: `tests/test_task_state.py`, `tests/test_remote_refresh.py`

- [ ] **Step 1: Identify pure helper candidates**

Search:

```bash
rg -n "def _sanitize|def _normalize|def _build_.*message|def _.*summary" app/services/task_state.py app/services/remote_refresh.py
```

Only extract helpers that:

- Have no direct store/database dependency.
- Are covered by existing tests or can be covered with focused tests.
- Do not require importing manager classes.

- [ ] **Step 2: Add tests for extracted behavior before moving code**

For `task_state` message sanitizer, add a test like:

```python
def test_task_message_sanitizer_truncates_html():
    from app.services.task_messages import sanitize_task_message

    message = sanitize_task_message("<html>verification</html>" * 100)
    assert len(message) <= 400
    assert "<html>" not in message.lower()
```

Adapt exact assertions to current sanitizer behavior after inspection.

- [ ] **Step 3: Move helper implementation**

Create `app/services/task_messages.py` with extracted helpers and import them from `task_state.py`. Keep compatibility wrappers if existing tests import private helper names from `task_state.py`.

- [ ] **Step 4: Extract remote refresh summary helper**

Create `app/services/remote_refresh_summary.py` with pure message/counter helper functions. Import them into `remote_refresh.py`.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py tests/test_remote_refresh.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 8**

Run:

```bash
git add app/services/task_messages.py app/services/remote_refresh_summary.py app/services/task_state.py app/services/remote_refresh.py tests/test_task_state.py tests/test_remote_refresh.py
git commit -m "refactor: extract task and refresh message helpers"
```

---

## Task 9: Extract Frontend Payload Helpers from Large Pages

**Files:**
- Create: `frontend/src/lib/modelDetailPayload.js`
- Create: `frontend/src/lib/modelDetailPayload.test.mjs`
- Create: `frontend/src/lib/organizerPayload.js`
- Create: `frontend/src/lib/organizerPayload.test.mjs`
- Modify: `frontend/src/pages/ModelDetailPage.vue`
- Modify: `frontend/src/pages/OrganizerPage.vue`

- [ ] **Step 1: Identify pure functions already present in pages**

Search:

```bash
rg -n "function prepareDetailPayload|function applyDetailPayload|function .*Status|function .*Message|computed\(" frontend/src/pages/ModelDetailPage.vue frontend/src/pages/OrganizerPage.vue
```

Only extract functions that can run without Vue refs or DOM access.

- [ ] **Step 2: Write tests for extracted model detail payload helper**

Create `frontend/src/lib/modelDetailPayload.test.mjs` with at least:

```javascript
import assert from "node:assert/strict";
import { test } from "node:test";

import { prepareModelDetailPayload } from "./modelDetailPayload.js";

test("prepareModelDetailPayload keeps comments and files arrays stable", () => {
  const payload = prepareModelDetailPayload({ title: "A", comments: null, files: null });
  assert.equal(payload.title, "A");
  assert.deepEqual(payload.comments, []);
  assert.deepEqual(payload.files, []);
});
```

Adjust field names to match actual page payload shape after inspection.

- [ ] **Step 3: Implement and import helper**

Move only the pure preparation logic from `ModelDetailPage.vue` into `frontend/src/lib/modelDetailPayload.js`. Keep Vue ref mutations in the page.

- [ ] **Step 4: Write organizer payload tests**

Create `frontend/src/lib/organizerPayload.test.mjs` with tests for a pure helper such as status normalization or upload/task matching after inspection.

- [ ] **Step 5: Implement organizer helper**

Move only pure mapping logic from `OrganizerPage.vue` into `frontend/src/lib/organizerPayload.js`.

- [ ] **Step 6: Run frontend tests and build**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 7: Commit Task 9**

Run:

```bash
git add frontend/src/lib/modelDetailPayload.js frontend/src/lib/modelDetailPayload.test.mjs frontend/src/lib/organizerPayload.js frontend/src/lib/organizerPayload.test.mjs frontend/src/pages/ModelDetailPage.vue frontend/src/pages/OrganizerPage.vue
git commit -m "refactor: extract frontend payload helpers"
```

---

## Task 10: Add Runtime Role Diagnostics

**Files:**
- Modify: `app/api/system.py`
- Modify: `app/api/config.py` or split config route module if route splitting is complete
- Modify: `frontend/src/pages/SettingsPage.vue`
- Test: `tests/test_self_update.py` or new route test

- [ ] **Step 1: Add backend test for runtime role payload**

Add a focused test to an existing system/config test file:

```python
def test_bootstrap_or_system_payload_includes_runtime_role():
    from app.core.settings import PROCESS_ROLE

    assert isinstance(PROCESS_ROLE, str)
```

If there is a TestClient pattern in existing tests, test the actual JSON response instead:

```python
def test_system_version_includes_runtime_role(client):
    response = client.get("/api/system/version")
    assert response.status_code == 200
    payload = response.json()
    assert "runtime" in payload
    assert "process_role" in payload["runtime"]
```

Use the project's existing authenticated client helper if required.

- [ ] **Step 2: Implement runtime payload helper**

Add a small helper in `app/api/system.py` or shared config API code:

```python
def runtime_role_payload() -> dict:
    return {
        "process_role": PROCESS_ROLE,
        "background_tasks_enabled": BACKGROUND_TASKS_ENABLED,
        "app_version": APP_VERSION,
    }
```

Include it in an existing system/config response where it fits without adding a new endpoint.

- [ ] **Step 3: Show diagnostics in Settings only if payload is already available**

In `SettingsPage.vue`, add compact text under the system/version area using existing surface styles. Do not create a new hero/card style. Display process role and background task status.

- [ ] **Step 4: Run tests/build**

Run:

```bash
.venv/bin/python -m pytest tests/test_self_update.py tests/test_config_cookies.py tests/test_web_routes.py -q
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 5: Commit Task 10**

Run:

```bash
git add app/api/system.py app/api/config.py frontend/src/pages/SettingsPage.vue tests/test_self_update.py tests/test_web_routes.py
git commit -m "feat: expose runtime role diagnostics"
```

---

## Task 11: Final Verification and Cleanup

**Files:**
- Modify docs only if final audit findings require it.

- [ ] **Step 1: Run broad backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py tests/test_database_json_state.py tests/test_business_logs.py tests/test_source_health.py tests/test_remote_refresh.py tests/test_process_jobs.py tests/test_subscriptions.py tests/test_source_library.py tests/test_batch_discovery.py tests/test_missing_3mf.py tests/test_scrapling_fetch.py tests/test_three_mf_quota.py tests/test_auth_guard.py tests/test_config_cookies.py tests/test_self_update.py tests/test_github_changelog.py -q
```

Expected: PASS.

- [ ] **Step 2: Run frontend tests and build**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 3: Run static diff check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Inspect final file sizes and route registrations**

Run:

```bash
wc -l app/api/*.py app/services/task_state.py app/services/remote_refresh.py frontend/src/pages/ModelDetailPage.vue frontend/src/pages/OrganizerPage.vue frontend/src/lib/*.js
```

Expected: `app/api/config.py` is materially smaller; new route files carry domain-specific routes.

- [ ] **Step 5: Inspect git history**

Run:

```bash
git status --short --branch
git log --oneline --decorate -12
```

Expected: worktree clean except intentional uncommitted final docs if any; commits are batch-scoped.

- [ ] **Step 6: Final summary**

Report:

- Which batches were completed.
- Which files were split or created.
- Which tests passed.
- Any skipped task and why.
- Whether version bump/push is pending user instruction.

Do not push unless the user explicitly says `推送`.
