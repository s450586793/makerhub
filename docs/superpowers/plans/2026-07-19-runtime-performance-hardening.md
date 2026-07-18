# Runtime Performance Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate update-version drift, remove request-path database logging stalls, and bound recurring database and browser work without changing archive semantics.

**Architecture:** Treat a release tag as the only update source of truth and pin each update to that tag. Move best-effort telemetry off request paths, retain synchronous durable errors, and make expensive read models explicitly cacheable or scoped. Preserve current APIs while extracting small service facades from `config.py`.

**Tech Stack:** FastAPI, PostgreSQL/psycopg, Vue 3, Vite, Docker/GHCR, pytest, Vitest.

## Global Constraints

- Preserve existing archive concurrency and download limits.
- Never replace a running container until the requested version image is pulled and verified.
- Warning and error business logs remain durable; bounded telemetry may be coalesced with counters.
- Keep public API response shapes backward compatible.
- Bump the user-facing version and release notes before release; do not push without explicit user instruction.

---

### Task 1: Pin Release Discovery And Self-Update Images

**Files:**
- Modify: `.github/workflows/docker.yml`
- Modify: `app/api/config.py`
- Create: `app/services/release_status.py`
- Modify: `app/services/self_update.py`
- Test: `tests/test_config.py`
- Test: `tests/test_self_update.py`

- [ ] Write failing tests proving that `main/VERSION` is never advertised as an update and an update for `0.13.0` pulls `:v0.13.0`.
- [ ] Implement GitHub Release lookup, exact image-reference construction, and pull-time version verification.
- [ ] Publish a GitHub Release only after the immutable GHCR tag succeeds; serialize promotion of `latest` globally.
- [ ] Run `pytest tests/test_config.py tests/test_self_update.py -q`.

### Task 2: Remove Database Logging From Request Latency

**Files:**
- Modify: `app/services/business_logs.py`
- Modify: `app/services/performance.py`
- Modify: `app/main.py`
- Modify: `app/api/performance_routes.py`
- Test: `tests/test_business_logs.py`
- Test: `tests/test_performance.py`

- [ ] Write failing tests for bounded asynchronous info telemetry, immediate warning/error persistence, and shutdown flush.
- [ ] Add a process-local batching writer with explicit queue bounds and dropped-entry counters.
- [ ] Route request performance telemetry through the writer and flush it during application shutdown.
- [ ] Run the targeted backend tests.

### Task 3: Bound Log-Page, Worker And State Database Load

**Files:**
- Modify: `app/services/business_logs.py`
- Modify: `app/api/logs_routes.py`
- Modify: `frontend/src/pages/LogsPage.vue`
- Modify: `app/worker.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/task_state.py`
- Test: `tests/test_business_logs.py`
- Test: `tests/test_worker.py`
- Test: `tests/test_task_state.py`

- [ ] Write failing tests for optional log facets, idle worker backoff, and independent state-key locking.
- [ ] Skip facet aggregation during live log refreshes, retain short TTL facet caches, and preserve filters after refresh.
- [ ] Use an idle worker wait with immediate wake-up checks; retain the active-task polling cadence.
- [ ] Replace the global state mutation lock with per-key locks and remove redundant post-save reads where callers need no normalized payload.
- [ ] Run targeted tests and frontend unit tests.

### Task 4: Scope State Events And Browser Memory

**Files:**
- Modify: `app/services/state_events.py`
- Modify: `app/api/config.py`
- Modify: `frontend/src/lib/stateEvents.js`
- Modify: `frontend/src/lib/pageCache.js`
- Modify: `frontend/src/components/ModelCard.vue`
- Modify: `frontend/src/lib/performance.js`
- Test: `tests/test_state_events.py`
- Test: `frontend/src/lib/*.test.js`

- [ ] Write failing tests for scope-filtered event queries, bounded page cache eviction, and bounded performance history.
- [ ] Filter state-event reads by subscribed scopes and carry the union of active client scopes in the EventSource URL.
- [ ] Add LRU/TTL cache limits, a fixed-size performance ring buffer, and avoid per-card resize observers for unchanged layout.
- [ ] Run targeted tests and `npm test`.

### Task 5: Bound Deep List Refresh And Extract Release Facades

**Files:**
- Modify: `frontend/src/pages/ModelsPage.vue`
- Modify: `frontend/src/pages/ModelLibraryGroupPage.vue`
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Modify: `app/api/config.py`
- Modify: dependent route imports and tests

- [ ] Write failing tests for bounded list restoration after a deep scroll and for release-status API compatibility.
- [ ] Retain a limited recent page window on background refresh; preserve current filter, selection and scroll-anchor behavior.
- [ ] Move release retrieval/cache logic behind `release_status.py`, leaving HTTP/auth adaptation in the API layer.
- [ ] Run frontend and backend targeted tests.

### Task 6: Release Hygiene And Independent Review

**Files:**
- Modify: `Dockerfile`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `VERSION`, `frontend/package.json`, `README.md`, `CHANGELOG.md`
- Modify: release contract tests

- [ ] Remove only verified unused production frontend dependencies and prevent runtime `node_modules` from entering the image.
- [ ] Bump the minor version, update the latest-three release notes, and make release metadata tests derive the current version dynamically.
- [ ] Run full backend suite, frontend tests/build, compose validation, release-version check, image smoke test, and a diff-based self-review.
