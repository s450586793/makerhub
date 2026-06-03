# MakerHub Runtime Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce MakerHub live runtime pressure by coalescing noisy state events, filtering low-value logs, stopping the legacy missing-3MF file log, and exposing a read-only diagnostics summary.

**Architecture:** Keep durable state writes unchanged, but reduce secondary writes to `makerhub_state_events` and `makerhub_logs`. Add a small diagnostics service that reads aggregate database metadata and expose it through the existing API layer. Keep all behavior behind focused helpers so large modules are only touched at their boundary.

**Tech Stack:** FastAPI, Python services, Postgres via `app.core.database`, unittest/pytest, Vue build unchanged.

---

## Files

- Modify `app/services/state_events.py`: add ordinary `state.changed` coalescing by scope.
- Modify `tests/test_task_state.py`: verify coalescing integration through `TaskStateStore`.
- Modify `app/services/business_logs.py`: add conservative noisy-log suppression helper.
- Modify `tests/test_business_logs.py`: cover skip policy and preservation of warnings/errors.
- Modify `app/services/legacy_archiver.py`: remove direct `missing_3mf.log` append and use structured DB logging summary.
- Modify `tests/test_legacy_archiver_validation.py` or add focused test if a better existing fixture exists.
- Create `app/services/runtime_diagnostics.py`: aggregate DB status, table sizes, event counts, and log counts.
- Modify `app/api/system.py` or an existing system route module: add read-only diagnostics endpoint.
- Add `tests/test_runtime_diagnostics.py`: mocked database coverage.
- Modify docs/release files if the endpoint or user-visible behavior changes.

## Tasks

### Task 1: State Event Coalescing

- [ ] Add tests in `tests/test_task_state.py`:
  - patch `time.monotonic` and `publish_state_event`,
  - call ordinary archive queue updates twice within the window,
  - assert only one ordinary `state.changed` is persisted,
  - assert `archive.completed` still publishes immediately.
- [ ] Implement coalescing in `app/services/state_events.py`:
  - default window around 2 seconds,
  - only coalesce `event_type == "state.changed"` for dashboard scopes,
  - always call `wake_state_event_subscribers()` even when DB insert is skipped.
- [ ] Run `pytest tests/test_task_state.py tests/test_state_contracts.py -q`.

### Task 2: Business Log Noise Policy

- [ ] Add tests in `tests/test_business_logs.py`:
  - repeated info `scrapling/fetch_trace` can be skipped,
  - warning `scrapling/fetch_trace` is preserved,
  - repeated low-value subscription success logs can be skipped,
  - redaction still masks sensitive fields.
- [ ] Implement `_should_persist_log_entry()` in `app/services/business_logs.py`.
- [ ] Apply the policy before `append_database_log_entry()` in `append_business_log()` and `append_structured_log()`.
- [ ] Run `pytest tests/test_business_logs.py -q`.

### Task 3: Legacy Missing 3MF Log Removal

- [ ] Add or update a focused archiver test so `record_missing_3mf_log=True` does not create `/logs/missing_3mf.log`.
- [ ] Replace the direct file append in `app/services/legacy_archiver.py` with a structured summary log through `append_business_log()`.
- [ ] Run the focused legacy archiver test.

### Task 4: Runtime Diagnostics API

- [ ] Add `app/services/runtime_diagnostics.py` with `build_runtime_diagnostics()`.
- [ ] Mock database calls in `tests/test_runtime_diagnostics.py` to cover available/unavailable DB.
- [ ] Add a route such as `GET /api/system/diagnostics` in `app/api/system.py`.
- [ ] Run `pytest tests/test_runtime_diagnostics.py tests/test_auth_guard.py -q`.

### Task 5: Docs, Release Notes, Verification

- [ ] Update module docs if new diagnostics endpoint belongs in Core.
- [ ] Bump version and latest README notes because this changes user-visible diagnostics behavior.
- [ ] Run focused backend tests:
  - `pytest tests/test_task_state.py tests/test_state_contracts.py tests/test_business_logs.py tests/test_runtime_diagnostics.py -q`
- [ ] Run relevant wider tests:
  - `pytest tests/test_database_json_state.py tests/test_auth_guard.py tests/test_self_update.py -q`
- [ ] Run frontend build only if a frontend page changes.
