# MakerHub Runtime Optimization Design

## Context

The live DSM instance shows the project has moved runtime configuration and task state into Postgres-backed `makerhub_json_state`. The remaining operational pressure is not a failed JSON migration; it is high write volume and task churn:

- `makerhub_logs` has more than 530k rows.
- `makerhub_state_events` has more than 280k rows.
- App, worker, and Postgres can all sit above one CPU core during active work.
- Legacy `/app/config/logs/missing_3mf.log` is still written by the archiver.

## Goals

- Reduce Postgres write pressure from high-frequency state events and noisy logs.
- Keep user-visible task progress responsive enough for the dashboard and task pages.
- Remove the remaining legacy missing-3MF file log write path.
- Add a small diagnostics API so future CPU/log/event checks do not require SSH and manual SQL.
- Keep the change low-risk: no data model migration, no rewrite of archive/subscription workflows.

## Non-Goals

- Do not move model `meta.json` files into relational tables in this pass.
- Do not remove legitimate runtime cache files such as source-library payload cache or remote-refresh temporary batch buffers.
- Do not redesign the frontend UI beyond consuming a diagnostics endpoint if needed.
- Do not split every large file immediately; only touch large modules where it directly reduces runtime pressure.

## Approach

### 1. State Event Coalescing

Add a lightweight server-side throttle for ordinary `state.changed` events by scope. The durable JSON state should still be saved immediately, but repeated event rows for the same scope should be coalesced within a short window. Semantic events such as `archive.completed`, `archive.failed`, `system_update` status changes, and manual one-off events should still publish immediately.

This directly targets the largest `makerhub_state_events` scopes: `remote_refresh_state`, `organize_tasks`, and `archive_queue`.

### 2. Log Noise Reduction

Add a central skip/throttle policy in `business_logs.py` for known noisy success/trace events. Failures and warnings remain durable. The first pass should target high-volume, low-action events observed online, such as `scrapling.fetch_trace`, repeated subscription success metadata logs, and repeated archive success noise. The policy must be conservative and test-covered.

### 3. Legacy Missing 3MF Log Removal

Replace the direct append to `/app/config/logs/missing_3mf.log` in `legacy_archiver.py` with structured database logging or existing `missing_3mf` state paths. Since missing 3MF is already represented in `makerhub_json_state:missing_3mf`, the file append should stop.

### 4. Diagnostics Summary

Add a read-only diagnostics service/API that reports:

- database availability and schema version,
- table row counts and approximate sizes,
- state event counts by scope,
- recent log counts by file/category/event/level,
- optional latest state key update times.

This should avoid exposing secrets and should degrade gracefully when Postgres is unavailable.

### 5. Follow-Up Refactors

After the write pressure is reduced, split large files by runtime boundary:

- `legacy_archiver.py`: missing 3MF recording, metadata assembly, asset download helpers.
- `batch_discovery.py`: MakerWorld URL/source parsing and discovery clients.
- `ModelDetailPage.vue`, `SettingsPage.vue`, `OrganizerPage.vue`: composables for derived state and API payload construction.

These are follow-up tasks unless implementation touches the same boundary.

## Testing

- Unit tests for event coalescing:
  - repeated ordinary `state.changed` events for one scope publish at most once per window,
  - semantic events bypass throttling,
  - different scopes do not block each other.
- Unit tests for log policy:
  - noisy info events can be skipped or throttled,
  - warnings/errors are preserved,
  - sensitive field redaction remains intact.
- Unit tests for diagnostics summary with mocked database rows.
- Regression test for `legacy_archiver.py` proving missing 3MF file logging no longer writes a file while archive result still reports missing items.

## Rollout

This is safe as a patch/minor release because it changes internal write frequency and adds diagnostics. User-facing release notes should mention lower DB/log pressure and the new diagnostics endpoint.
