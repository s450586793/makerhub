# MakerHub Lightweight Runtime Engine Design

## Goal

Reduce MakerHub's runtime, page-load, workflow, and maintenance weight by replacing the current mixed queue/state model with one unified runtime engine.

This is the aggressive option: archive, subscription sync, source refresh, and missing `3MF` retry all converge on one scheduler, one execution state model, one retry model, one snapshot contract, and one logging policy.

Success priority:

1. Workflow stability: no more thousands of visible model-level tasks, stale running queues, or conflicting source-refresh/archive states.
2. Resource pressure: lower CPU, memory, Postgres writes, log writes, and state event churn.
3. Page speed: login, dashboard, task page, model library, and subscription library should read bounded snapshots on first paint.
4. Maintenance weight: remove duplicated queue, refresh, retry, and status ownership.

Deployment weight is out of scope for this design. The App / Worker / Postgres deployment remains.

## Current Problems

The project has already improved several hot spots: database initialization is cached, duplicate JSON state writes are reduced, slow API logging exists, source refresh is split into smaller batches, and dashboard source health now reads `account_health`.

The remaining weight is structural:

- `archive_queue`, `missing_3mf`, `remote_refresh_state`, `source_refresh_queue`, `source_refresh_runs`, `subscriptions_state`, `makerhub_logs`, and `state_events` can all describe overlapping runtime truth.
- Batch sources can expand into hundreds or thousands of model-level queue entries, making task pages heavy and recovery confusing.
- Successful model-level work leaves too much transient state behind.
- The frontend often has to understand old and new state shapes.
- The worker writes many small progress updates instead of committing batch summaries.
- Maintenance work repeatedly touches multiple status systems for one behavior change.

## Non-Goals

- Do not rewrite MakerWorld fetching, model parsing, file layout, model index, or local import as part of the first runtime engine milestone.
- Do not migrate successful historical task records into the new engine. The model library is the durable success record.
- Do not remove old state files/tables immediately. Old states are frozen, migrated, or kept as read-only compatibility data.
- Do not introduce a browser automation or CAPTCHA bypass layer.
- Do not change the three-container deployment model.

## Core Concepts

Create a new service boundary under `app/services/runtime_engine/`.

Primary concepts:

- `Run`: one user-triggered or scheduled operation. Examples: archive one URL, sync one subscription, refresh source data, retry missing `3MF`.
- `Batch`: a bounded executable chunk inside a run. Default target size is configurable, initially 50 models/items.
- `Item`: a model or source entry selected for execution. Item details are short-lived while running.
- `Failure`: durable per-item result only for failed, skipped, blocked, missing `3MF`, verification, cookie, network, or limit outcomes.
- `Snapshot`: bounded read model for dashboard, task page, source refresh page, and diagnostics.
- `Adapter`: existing business logic wrapped behind the new engine. Adapters perform actual discovery, archive, source refresh, subscription sync, and `3MF` retry work.

The new execution flow:

```text
submit run
  -> discover candidates
  -> plan bounded batches
  -> execute batch through adapter
  -> buffer item results
  -> commit batch summary and failures
  -> refresh snapshots
  -> emit throttled state event
```

## Runtime Types

The engine supports these run types:

- `archive`: single model, author upload page, collection, favorite page, or batch archive source.
- `subscription_sync`: sync configured subscription sources and submit discovered archive runs/batches.
- `source_refresh`: refresh metadata/file state for existing archived models.
- `missing_3mf_retry`: retry missing `3MF` instances by platform, model, failure class, or batch.

Each run type must implement the same adapter interface:

```text
discover(context) -> CandidateSet
plan(candidate_set, limits) -> list[BatchPlan]
execute_item(item, context) -> ItemResult
commit_success(result, context) -> None
classify_failure(error_or_result) -> Failure
```

Adapters may reuse existing modules:

- Archive adapter wraps `ArchiveTaskManager` / `run_archive_model_job`.
- Subscription adapter wraps `SubscriptionManager` discovery and source account sync.
- Source-refresh adapter wraps the existing source refresh manager and remote refresh core logic.
- Missing-3MF adapter wraps existing 3MF retry/download helpers.

Existing managers should become execution helpers. They should stop owning durable queue shape, frontend task shape, and retry orchestration.

## Data Model

Store engine state in Postgres-backed JSON state first, then move to dedicated tables if profiling shows JSON state is still too heavy. The design must keep that migration path open by isolating all reads/writes in `runtime_engine.store`.

Initial JSON state keys:

- `runtime_runs`: recent active and terminal run summaries.
- `runtime_batches`: active/pending batch summaries.
- `runtime_failures`: durable failure/skipped/missing detail records.
- `runtime_snapshots`: dashboard/task/source/subscription snapshots.
- `runtime_migration`: one-time migration markers and old-state freeze metadata.

Run summary fields:

- `run_id`
- `type`
- `source_url` or `source_id`
- `platform`
- `status`: `queued`, `discovering`, `planned`, `running`, `paused`, `blocked`, `completed`, `failed`, `cancelled`
- `total`
- `completed`
- `failed`
- `skipped`
- `missing_3mf`
- `current_batch_id`
- `message`
- `created_at`
- `started_at`
- `updated_at`
- `completed_at`

Batch summary fields:

- `batch_id`
- `run_id`
- `type`
- `status`
- `offset`
- `limit`
- `total`
- `completed`
- `failed`
- `skipped`
- `lease_owner`
- `lease_expires_at`
- `message`
- timestamps

Failure fields:

- `failure_id`
- `run_id`
- `batch_id`
- `type`
- `platform`
- `model_id`
- `model_url`
- `instance_id`
- `title`
- `status`: `failed`, `skipped`, `missing_3mf`, `verification_required`, `cookie_invalid`, `daily_limit`, `network_error`, `not_found`
- `message`
- `retryable`
- `retry_count`
- `last_attempt_at`

Success item history is not durable runtime state. A successful item updates the model library/index and increments summary counters.

## State Migration

Only unfinished work and actionable failure data is migrated.

Migration rules:

- Old `archive_queue.queued` becomes new `archive` runs/batches.
- Recoverable old `archive_queue.active` becomes interrupted new batches unless the old worker is known to still own the lease.
- Old `missing_3mf` items become `runtime_failures` with `retryable=true`, grouped into `missing_3mf_retry` runs only when the user or scheduler requests retry.
- Old `recent_failures` become `runtime_failures` if retryable or useful for display; old non-actionable success/history entries are not migrated.
- Old `remote_refresh_state`, `source_refresh_queue`, and `source_refresh_runs` are frozen into a legacy summary if not active. Active recoverable work becomes `source_refresh` runs/batches.
- Old `subscriptions_state` keeps subscription configuration and last result, but active sync work becomes `subscription_sync` runs.
- Existing `account_health` remains the source of dashboard account status. The runtime engine can update it after adapter results.
- Old logs and state events remain read-only diagnostic history.

The migration must be idempotent:

- It records a `runtime_migration` marker with source state versions/digests.
- Re-running migration must not duplicate runs, batches, or failures.
- If migration fails, the old state remains readable and the engine does not partially take ownership.

## Write and Refresh Policy

Runtime writes are batch-oriented:

- Running items write to an in-memory buffer or temporary file/table.
- Batch summary is flushed at most every 30 seconds or every 50 completed items, whichever comes first.
- Batch completion commits all summary counters and failure records.
- Dashboard/task/source snapshots are refreshed after throttled progress commits and at run completion.
- `state_events` emits only coarse events:
  - `runtime.run.started`
  - `runtime.batch.progress`
  - `runtime.batch.completed`
  - `runtime.run.completed`
  - `runtime.run.blocked`
  - `runtime.failure.created`
  - `account_health.changed`

Business logs are summary-first:

- One log at run start.
- One log per batch completion if there are failures or meaningful totals.
- One log at run completion.
- No per-success-item business log by default.

## Frontend Changes

Task page becomes batch-first:

- Top cards show active runs and active batches.
- Default list shows run/batch cards, not model-level tasks.
- A batch card can expand to paginated failure/skipped/missing details.
- Successful item rows are not shown as historical task rows.
- Retry actions operate on failure filters: by run, platform, failure type, model, or selected failures.

Dashboard reads `runtime_snapshots.dashboard`.

Source refresh page reads `runtime_snapshots.source_refresh`.

Subscription pages keep subscription cards/list pagination, but sync progress comes from runtime snapshots rather than subscription-owned running state.

Model library remains model/index driven and should not read runtime success history.

## API Shape

Add new API routes behind session auth:

- `GET /api/runtime`：dashboard-friendly active run/batch snapshot.
- `GET /api/runtime/runs`：paginated runs.
- `GET /api/runtime/runs/{run_id}`：run detail.
- `GET /api/runtime/runs/{run_id}/failures`：paginated failure/skipped/missing details.
- `POST /api/runtime/runs`：submit a run.
- `POST /api/runtime/runs/{run_id}/pause`
- `POST /api/runtime/runs/{run_id}/resume`
- `POST /api/runtime/runs/{run_id}/cancel`
- `POST /api/runtime/failures/retry`
- `POST /api/runtime/repair`

Compatibility routes remain during transition:

- `/api/archive`
- `/api/tasks`
- `/api/remote-refresh`
- `/api/source-refresh`
- subscription sync endpoints
- missing `3MF` retry endpoints

Compatibility routes should call the runtime engine and return old-compatible payloads until the frontend is fully migrated.

## Recovery and Leases

The worker owns execution leases:

- A batch lease is renewed during execution.
- Expired running batches are marked `interrupted`.
- On worker startup, interrupted batches become `queued` unless the run was cancelled.
- `runtime repair` recomputes counters from batches and failures, releases expired leases, and regenerates snapshots.

No batch parent should occupy the executable lane without active work. Parent runs are summaries; batches are executable units.

## Error Handling

Failures are classified at adapter boundary:

- Verification or Cloudflare challenge: update failure as `verification_required`; update `account_health`.
- Cookie/session auth failure: update failure as `cookie_invalid`; update `account_health`.
- Daily MakerWorld limit: update failure as `daily_limit`; pause relevant platform/run until reset or manual override.
- Network/HTTP transient failure: retry according to bounded retry policy.
- Source not found: mark non-retryable skipped/not found.
- Unexpected adapter exception: mark batch failed, preserve error in sanitized failure detail, continue or block based on severity.

Sensitive data must never be stored in failure messages, logs, or snapshots.

## Testing Strategy

Test layers:

- Unit tests for runtime store normalization, idempotent migration, counters, and failure classification.
- Adapter contract tests for archive, subscription sync, source refresh, and missing `3MF`.
- Recovery tests for expired leases, interrupted batches, repair, pause/resume/cancel.
- API tests for new runtime routes and compatibility routes.
- Frontend tests for task page shape helpers and dashboard snapshot rendering.
- Flow tests for:
  - submit single archive
  - submit author/collection batch
  - subscription sync submits archive batches
  - source refresh completes a bounded run
  - missing `3MF` retry updates failures and account health
  - worker restart recovers interrupted batches

Final manual/online verification must check:

- Login and dashboard load.
- Task page opens quickly with batch cards.
- Single model archive reaches model library.
- Batch archive creates bounded batches, not thousands of visible tasks.
- Subscription sync discovers and submits archive work.
- Source refresh produces a completed run record.
- Missing `3MF` retry can be triggered and failures remain visible.
- Account health changes still show on dashboard.

## Rollout Plan

Implement behind a feature flag first:

- `MAKERHUB_RUNTIME_ENGINE=v2` or config flag.
- Default can remain legacy until tests pass.
- Add migration preview endpoint before enabling migration.
- Enable compatibility routes one by one.
- Once frontend task page reads runtime snapshots, switch default for new runs.
- Keep old states read-only for at least one release.

Although this is a core-engine replacement, implementation must not land as one unverified big-bang patch. Each cutover step must preserve a runnable application, include targeted tests, and keep compatibility routes working until the next step is proven.

Cutover order:

1. Runtime store, models, snapshots, repair, migration preview.
2. Archive adapter and compatibility `/api/archive`.
3. Missing `3MF` adapter and retry routes.
4. Source refresh adapter and source refresh pages.
5. Subscription sync adapter and subscription progress.
6. Task page batch-first UI.
7. Dashboard snapshot read path.
8. Legacy state freeze and cleanup.

## Acceptance Criteria

The implementation is complete only when these are true:

- New runtime engine can execute archive, subscription sync, source refresh, and missing `3MF` retry through one scheduler/state model.
- Old unfinished queued/recoverable work migrates without duplication.
- Success item runtime history is not retained as durable task state.
- Failure/skipped/missing details remain paginated and retryable.
- Dashboard and task page read bounded snapshots.
- Worker restart and repair recover interrupted batches.
- Flow verification confirms main user paths still work.
- Resource pressure is measurably lower: fewer state writes, fewer state events, fewer per-success logs, bounded task payloads.
- README/module docs explain the new runtime ownership.

## Risks

- This touches the core engine and can break working archive flows if done in one large patch.
- Compatibility route shape drift can break frontend pages during transition.
- Migration mistakes can duplicate old queued work.
- Snapshot freshness may feel less real-time.
- Existing tests assume old queue shapes and will need staged updates.

Mitigation:

- Build the engine behind a flag.
- Keep adapters thin and reuse current working fetch/archive logic.
- Make migration preview and idempotency mandatory.
- Convert one flow at a time while keeping compatibility payloads.
- Run online flow checks before claiming completion.
