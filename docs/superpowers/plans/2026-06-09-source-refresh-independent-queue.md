# Source Refresh Independent Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add the Phase 1 independent source refresh queue/run boundary while preserving the current remote refresh API and existing per-model refresh implementation.

**Architecture:** Add canonical `source_refresh_queue` and `source_refresh_runs` runtime state in `TaskStateStore`, then introduce `SourceRefreshTaskManager` as a compatibility subclass of `RemoteRefreshManager`. The manager mirrors source refresh run/queue state, no longer blocks metadata refresh on archive queue busy state, and keeps existing `/api/remote-refresh` routes compatible while also exposing `/api/source-refresh` wrappers.

**Tech Stack:** Python 3, FastAPI, existing `TaskStateStore`, existing `RemoteRefreshManager`, unittest/pytest, Vue frontend state consumers.

---

### Task 1: Source Refresh Runtime State

**Files:**
- Modify: `app/services/state_contracts.py`
- Modify: `app/services/task_state.py`
- Modify: `app/services/database_migration.py`
- Test: `tests/test_task_state.py`

- [x] **Step 1: Add failing tests**

Add tests for source refresh queue/run normalization, save/load, stale repair, and dashboard scopes.

- [x] **Step 2: Implement state contracts**

Add `SOURCE_REFRESH_QUEUE_STATE_KEY`, `SOURCE_REFRESH_RUNS_STATE_KEY`, status sets, and dashboard event scopes.

- [x] **Step 3: Implement TaskStateStore methods**

Add load/save/update/patch helpers for `source_refresh_queue` and `source_refresh_runs`, with normalized counters.

- [x] **Step 4: Add migration keys**

Include the two new state files in JSON state migration and runtime protection.

- [x] **Step 5: Verify**

Run: `.venv/bin/python -m pytest tests/test_task_state.py tests/test_state_contracts.py -q`

### Task 2: Source Refresh Manager Boundary

**Files:**
- Create: `app/services/source_refresh.py`
- Modify: `app/api/dependencies.py`
- Modify: `app/worker.py`
- Test: `tests/test_source_refresh.py`

- [x] **Step 1: Add failing tests**

Test manual trigger creates source refresh run/queue state without writing archive queue and archive busy state no longer rejects source refresh.

- [x] **Step 2: Implement SourceRefreshTaskManager**

Subclass/compose `RemoteRefreshManager` for Phase 1, mirror active run and queue state, override busy semantics, expose repair/resume helpers, and keep old refresh implementation under the new boundary.

- [x] **Step 3: Wire dependencies and worker**

Instantiate `SourceRefreshTaskManager` as `remote_refresh_manager` compatibility object and call source refresh worker startup/resume hooks.

- [x] **Step 4: Verify**

Run: `.venv/bin/python -m pytest tests/test_source_refresh.py tests/test_remote_refresh.py -q`

### Task 3: API And Frontend Compatibility

**Files:**
- Modify: `app/api/remote_refresh_routes.py`
- Modify: `app/services/catalog.py`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`

- [x] **Step 1: Add failing tests**

Add assertions that payloads expose canonical `source_refresh` state and frontend prefers it when present.

- [x] **Step 2: Add source-refresh API wrappers**

Keep `/api/remote-refresh` and `/api/remote-refresh/run`; add `/api/source-refresh`, `/api/source-refresh/run`, and `/api/source-refresh/repair`.

- [x] **Step 3: Update dashboard payload**

Expose `source_refresh` alongside legacy `remote_refresh`, with normalized counters and active run.

- [x] **Step 4: Verify frontend**

Run: `node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/dashboardStatus.test.mjs`

### Task 4: Full Verification

**Files:**
- Modify only files touched by the above tasks.

- [x] **Step 1: Run backend focused tests**

Run: `.venv/bin/python -m pytest tests/test_source_refresh.py tests/test_remote_refresh.py tests/test_task_state.py tests/test_state_contracts.py -q`

- [x] **Step 2: Run frontend focused tests**

Run: `node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/dashboardStatus.test.mjs`

- [x] **Step 3: Run build**

Run: `npm --prefix frontend run build`

- [x] **Step 4: Check diff**

Run: `git diff --check`

