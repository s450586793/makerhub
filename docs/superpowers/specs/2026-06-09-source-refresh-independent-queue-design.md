# Source Refresh Independent Queue Design

Date: 2026-06-09

## Purpose

The current source refresh flow is not an `archive_queue` task, but it still reuses the archive execution core for each model. This means a source refresh batch has its own scheduler and batch state, while the expensive per-model work still inherits archive-chain behavior: MakerWorld page/API blocking, verification failures, Scrapling/requests fallback, asset synchronization, resource-slot waits, and archive-style failure handling.

This design splits source refresh into an independent service and queue. Source refresh should own its task semantics, runtime state, worker lifecycle, timeout policy, and UI status. Archive should remain responsible for real model archiving and 3MF downloads.

## Current Findings

- `RemoteRefreshManager` is started independently from the archive worker in `app/worker.py`.
- Source refresh does not enqueue its normal model-refresh work into `archive_queue`.
- Source refresh checks archive/local busy state before starting, so valid archive work can delay source refresh.
- `_refresh_one()` calls `run_archive_model_job()` for metadata refresh, and may call it a second time when page/comment/asset resources need synchronization.
- New 3MF files discovered by source refresh are submitted through `archive_manager.submit_three_mf_download()`.
- A stuck `run_archive_model_job()` call can stop batch completion progress even though the source refresh scheduler itself is separate.

## Goals

- Make source refresh independent from archive task state and archive task execution semantics.
- Give source refresh its own durable queue, run records, heartbeat, timeout, retry, and stale-task repair.
- Replace normal source refresh use of `run_archive_model_job()` with a lightweight source-refresh job.
- Keep newly discovered 3MF downloads routed to archive, because downloading 3MF is archive work.
- Keep the existing dashboard and source refresh page usable during migration.
- Preserve low-write behavior: batch-level durable updates, throttled active item heartbeats, and final summary writes.
- Make stuck source refresh work visible and recoverable without requiring archive queue repair.

## Non-Goals

- Do not rewrite the full archive worker.
- Do not merge archive and source refresh into one queue.
- Do not add browser verification or CAPTCHA automation.
- Do not move all model metadata out of `meta.json`.
- Do not remove existing remote refresh API routes until the new API and frontend are fully compatible.
- Do not make source refresh download 3MF files directly.

## Selected Approach

Build a dedicated source refresh service:

- `SourceRefreshTaskManager`: owns queue operations and worker control.
- `source_refresh_queue`: durable task queue state.
- `source_refresh_runs`: durable batch/run state.
- `source_refresh_worker`: background worker loop for source refresh tasks.
- `run_source_refresh_model_job()`: lightweight per-model job that fetches and parses remote metadata without invoking full archive execution.

Archive and source refresh should share only low-level infrastructure:

- MakerWorld URL normalization and site detection.
- Cookie selection.
- Proxy handling.
- Low-level MakerWorld client functions.
- Parsing helpers for model page/API payloads.
- Resource-slot/limit primitives where they protect the same upstream service.

They should not share queue state, archive task status values, archive retry semantics, or archive UI summaries.

## Runtime Model

### Source Refresh Queue

Add a new runtime state key or relational-backed equivalent:

- `source_refresh_queue`

Shape:

- `active`: currently leased source refresh tasks.
- `queued`: pending tasks.
- `recent_failures`: compact failure samples.
- `updated_at`: last mutation time.
- `version`: schema version.

Each task stores:

- `id`: source refresh task ID.
- `run_id`: owning batch/run ID.
- `model_dir`: local model directory.
- `title`: display title.
- `url`: normalized source URL.
- `site`: `cn` or `global`.
- `status`: `queued`, `running`, `succeeded`, `failed`, `skipped`, `timed_out`, or `cancelled`.
- `attempts`: retry count.
- `created_at`, `started_at`, `updated_at`, `finished_at`.
- `lease_expires_at`: stale detection.
- `last_heartbeat_at`: worker liveness.
- `message`: sanitized status text.
- `metrics`: compact timing counters.

### Source Refresh Runs

Add a new runtime state key:

- `source_refresh_runs`

It tracks the active run and the last completed run:

- `active_run`: current batch, if any.
- `last_completed_run`: summary of the latest finished batch.
- `last_attempt_at`: latest scheduler/manual attempt.
- `last_deferred_at`: latest deferral time.
- `last_defer_reason`: sanitized deferral reason.
- `last_interrupted_at`: latest interruption time.
- `last_interrupted_reason`: sanitized interruption reason.
- `next_run_at`: next scheduled run time.

`active_run` stores:

- `run_id`
- `status`: `queued`, `running`, `paused`, `resuming`, `completed`, `failed`, `interrupted`, or `cancelled`
- `manual`
- `created_at`, `started_at`, `updated_at`, `finished_at`
- `candidate_total`
- `queued_total`
- `completed_total`
- `succeeded_total`
- `failed_total`
- `skipped_total`
- `timed_out_total`
- `remaining_total`
- `current_items`
- `manifest_path`
- `result_path`
- `message`

## Worker Lifecycle

The worker process starts the archive worker and source refresh worker separately:

1. Archive startup continues to call `archive_manager.resume_pending_tasks()`.
2. Source refresh startup calls `source_refresh_manager.resume_pending_tasks()`.
3. The main worker loop calls both `archive_manager.ensure_worker_for_pending()` and `source_refresh_manager.ensure_worker_for_pending()`.
4. Source refresh scheduled runs are created by `source_refresh_manager.tick()` or an equivalent timer.

The source refresh worker:

- Leases one or more source refresh tasks according to configured concurrency.
- Updates `last_heartbeat_at` on a throttled interval.
- Enforces per-model timeout.
- Writes per-model results to the run result journal.
- Updates run counters from queue state.
- Finalizes the run when all run tasks are terminal.

Source refresh should not wait for archive queue idleness before refreshing metadata. It only interacts with archive when it submits newly discovered 3MF download tasks.

## Lightweight Source Refresh Job

Add `run_source_refresh_model_job()` as the normal per-model path.

Responsibilities:

- Fetch model page/API data for the normalized source URL.
- Detect login, verification, deleted model, daily limit, and network failure states.
- Parse title, description, author, images, profile/instance metadata, comments, attachments, and changed resource URLs as needed for source refresh.
- Write or return a metadata patch that `_finalize_refreshed_meta()` can apply.
- Report discovered missing or new 3MF instances without downloading them.
- Return compact metrics and sanitized failure details.

It must not:

- Run full archive mode detection.
- Create archive tasks.
- Download 3MF files.
- Write archive task state.
- Emit archive history entries.
- Depend on archive task status values.

The implementation can initially reuse internal helper functions from the archive modules, but those helpers should be extracted behind neutral names if they are not archive-specific.

## 3MF Boundary

3MF download remains archive-owned.

When source refresh finds new 3MF instances:

1. It records missing/new 3MF state for the model.
2. It calls `archive_manager.submit_three_mf_download()` with the model URL, model ID, title, and instance IDs.
3. If archive queue rejects or defers the download, source refresh records that the metadata refresh succeeded but 3MF enqueue was deferred or failed.
4. Source refresh run completion is not blocked by actual 3MF download completion.

This keeps "metadata refresh" and "file archive/download" as separate workflows.

## Stuck Task Handling

Source refresh needs its own recovery path:

- Per-model hard timeout, configurable with a conservative default.
- Lease expiry for tasks left `running` after worker restart.
- Startup repair that requeues stale active tasks when attempts remain.
- Startup repair that marks over-retried stale tasks as `failed` or `timed_out`.
- Manual "repair source refresh state" API for operator recovery.
- Run finalization that can finish even when some tasks fail or time out.

Timeouts should produce source refresh failures, not archive failures.

## API Compatibility

Keep existing routes during migration:

- `GET /api/remote-refresh`
- `POST /api/remote-refresh/run`
- `POST /api/config/remote-refresh`

They can become compatibility wrappers over the new source refresh manager.

Add or internally model new API concepts:

- `GET /api/source-refresh`
- `POST /api/source-refresh/run`
- `POST /api/source-refresh/pause`
- `POST /api/source-refresh/resume`
- `POST /api/source-refresh/repair`

The frontend can continue calling existing routes until the route names are changed in a later cleanup. The payload should expose both compatibility fields and new explicit fields:

- `remote_refresh_state`: legacy compatibility.
- `source_refresh`: new canonical state.

## Frontend Behavior

The dashboard and source refresh page should show source refresh as its own workflow:

- Current source refresh run status.
- Run totals and remaining count.
- Current active source refresh items.
- Last completed source refresh summary.
- Last source refresh failure samples.
- New 3MF enqueue counts.
- Verification/login/daily-limit state by site when detected.

The page should not imply that source refresh is blocked just because old archive failures exist. Archive status should be shown separately.

Actions:

- Start source refresh.
- Pause source refresh.
- Resume source refresh.
- Repair source refresh state.
- Visit MakerWorld homepage/model page when verification is required.

## Migration Plan

### Phase 1: Queue And State Split

- Add `source_refresh_queue` and `source_refresh_runs` state normalization.
- Add `SourceRefreshTaskManager`.
- Keep old `RemoteRefreshManager` API shape as wrapper.
- Generate source refresh queue tasks from the current candidate picker.
- Keep using the existing per-model refresh path only behind the new queue boundary.
- Update frontend to read new state when present, legacy state otherwise.

This phase gives source refresh independent status, leases, stale repair, and UI semantics before replacing the fetch implementation.

### Phase 2: Lightweight Job Replacement

- Add `run_source_refresh_model_job()`.
- Move neutral MakerWorld fetch/parse helpers out of archive-specific modules where needed.
- Replace normal source refresh calls to `run_archive_model_job()`.
- Keep archive-owned 3MF enqueue behavior.
- Add timeout and failure classification tests.

This phase removes the main cause of archive-chain coupling.

### Phase 3: Cleanup

- Stop writing old ambiguous `remote_refresh_state` fields except compatibility summaries.
- Rename frontend/API labels from remote refresh to source refresh where appropriate.
- Remove obsolete source refresh busy checks against archive queue.
- Keep historical data readable.

Cleanup should happen only after the new queue has run successfully online.

## Testing

Backend tests:

- Source refresh queue normalization handles empty, legacy, active, queued, and malformed payloads.
- Startup repair requeues stale active source refresh tasks.
- Startup repair marks over-retried stale tasks terminal.
- Manual run creates a source refresh run and queue tasks without touching `archive_queue`.
- Worker processes source refresh tasks and updates run counters.
- Per-model timeout marks only that source refresh task as `timed_out`.
- New 3MF discovery calls `submit_three_mf_download()` and does not wait for archive completion.
- `GET /api/remote-refresh` remains backward-compatible.
- Existing archive queue tests still pass unchanged.

Frontend tests:

- Dashboard prefers `source_refresh` state when present.
- Source refresh page renders running, paused, completed, interrupted, timed-out, and verification-required states.
- Legacy `remote_refresh_state` still renders when new state is absent.
- Archive failures do not force source refresh card into abnormal state.

Integration checks:

- Run source refresh while archive queue has historical failures.
- Run source refresh while archive queue is actively downloading 3MF.
- Restart worker during source refresh and confirm source refresh repairs or resumes independently.
- Trigger new 3MF discovery and confirm archive queue receives download tasks.

## Rollout

Roll out in a minor release because this changes runtime architecture.

Safe rollout order:

1. Deploy Phase 1 with compatibility wrappers.
2. Verify online state shows independent source refresh queue/run fields.
3. Deploy Phase 2 after lightweight job tests pass.
4. Observe one full online source refresh run.
5. Deploy Phase 3 cleanup only after the online run is stable.

Rollback should keep the old `remote_refresh_state` readable. If Phase 1 fails, disable the new manager and fall back to the existing `RemoteRefreshManager` while preserving source refresh state files for diagnosis.

## Open Decisions

- Exact default per-model timeout should be chosen from online timing metrics, not guessed.
- Route renaming can wait; compatibility routes should remain through at least one release.
- Whether source refresh uses a JSON-state queue or relational tables can be decided during implementation. The interface should not expose that storage choice.

