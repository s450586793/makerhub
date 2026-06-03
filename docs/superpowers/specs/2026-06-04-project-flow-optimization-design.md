# MakerHub Project Flow Optimization Design

Date: 2026-06-04

## Purpose

MakerHub's recent performance work reduced database and log pressure, but several long-running workflows still share the same operational risks:

- Batch parent tasks can appear to be running even when no executable child work is progressing.
- Remote refresh, archive, subscription sync, and local organizer flows use similar status concepts but different state shapes.
- Running batches can still create unnecessary state churn if progress and final results are not separated clearly.
- Users need clearer actions when a workflow is blocked by cookies, MakerWorld verification, stale queue state, or a stopped worker.

This design defines one project-wide optimization pass across background task flow, data/write flow, and user operation flow. The goal is to make long-running work easier to recover, cheaper to observe, and clearer in the UI without rewriting MakerWorld archiving or changing existing public API routes.

## Goals

- Standardize runtime task semantics for batch parents, executable child tasks, leases, heartbeats, attempts, and stale-task recovery.
- Reduce avoidable database writes during active batches by separating running summaries from detailed per-item results.
- Make UI status labels and actions match the real runtime state.
- Add read-only diagnostics and explicit repair actions so online instance checks do not require manual SQL for common cases.
- Keep changes incremental, testable, and compatible with the current running instance.

## Non-Goals

- Do not replace the existing Postgres-backed JSON state system with a full relational task model in this pass.
- Do not redesign the frontend visual language or page layout beyond status/action clarity.
- Do not reintroduce embedded MakerWorld browser verification flows.
- Do not rewrite the core MakerWorld archive implementation.
- Do not change API paths unless a compatibility route remains in place.

## Approaches Considered

### Option A: Runtime Governance First

Add a shared task lifecycle model for leases, heartbeats, stale recovery, write budgets, diagnostics, and UI status normalization.

Tradeoff: This keeps the blast radius manageable and directly targets the current operational failures. It does not fully normalize every old state document immediately.

Decision: Recommended.

### Option B: Data Model First

Move runtime JSON state into normalized task, event, and batch-result tables before changing worker behavior.

Tradeoff: This is cleaner long term, but it creates migration risk and would delay fixes for stuck tasks and confusing UI states.

Decision: Defer. Use lightweight helper contracts now and leave room for relational tables later.

### Option C: User Flow First

Start with dashboard and task-page action changes, then backfill worker behavior.

Tradeoff: This improves visibility quickly, but it can mask incorrect backend state instead of fixing it.

Decision: Include the critical UI pieces, but anchor the work in runtime governance.

## Selected Design

Use Option A as the spine, include the highest-value pieces of Option C, and avoid Option B except for small compatibility-safe data helpers.

The implementation should be split into four phases.

## Phase 1: Runtime Task Governance

Define shared runtime semantics that can be applied to archive queue, remote refresh, subscriptions, and local organizer flows.

### Task Categories

- Batch parent task: tracks a batch request, counters, summary, and child relationships. It is not executable work by itself.
- Executable child task: represents work a worker can claim, run, heartbeat, retry, fail, or complete.
- Scheduler task: represents timed triggers such as subscription sync or source refresh scheduling.

### Standard Runtime Fields

Executable tasks should normalize these fields where the existing state shape allows it:

- `status`: one of `queued`, `running`, `waiting_children`, `paused`, `blocked`, `failed`, `completed`.
- `lease_owner`: worker/process identifier that currently owns the task.
- `lease_expires_at`: timestamp after which the task may be recovered.
- `heartbeat_at`: last worker heartbeat.
- `started_at`: first run start time for the current attempt.
- `last_progress_at`: last meaningful progress time.
- `attempt_count`: number of execution attempts.
- `parent_task_id`: batch parent if applicable.
- `blocked_reason`: short machine-readable reason such as `needs_cookie`, `needs_verification`, `rate_limited`, `source_unavailable`, or `worker_stopped`.

Existing state documents do not need a disruptive migration. New helpers should read missing fields safely and only write normalized fields when touching active tasks.

### Recovery Behavior

Worker startup and a manual repair action should both detect stale runtime state:

- A `running` executable task with an expired lease can be requeued if attempts remain.
- A `running` executable task with no heartbeat and no retry budget should become `failed`.
- A batch parent with unfinished children should be `waiting_children`, not `running`.
- A batch parent with no active or queued children should be finalized as `completed`, `failed`, or `blocked` based on child outcomes.
- Paused tasks must not be auto-requeued.

The repair operation should produce a small structured summary: examined, requeued, failed, finalized, skipped, and errors.

## Phase 2: Data And Write Governance

Keep current durable JSON state and business logs, but make batch writes boundary-oriented.

### Write Budget Policy

Each long-running workflow should define three write levels:

- Boundary writes: batch accepted, started, completed, failed, interrupted, or recovered. These are durable and publish state events.
- Summary writes: throttled running counters or current phase. These may be saved at a minimum interval and should not force full page reloads.
- Detail writes: per-item success/failure/progress records. During active batches, these should go to a temp NDJSON file or an existing purpose-specific buffer, then be aggregated at batch completion.

Failures remain inspectable. The optimization target is repeated success/progress noise, not error visibility.

### Batch Result Buffer

For batch flows that can process many models, use a bounded running buffer:

- Prefer a temporary NDJSON file under a runtime directory for large batches.
- Use a small in-memory buffer only when the flow is already small and bounded.
- Aggregate final counters, recent items, slow items, and failure samples at completion.
- Preserve stale temp files briefly after crashes for diagnostics, then clean them by retention policy.

Remote refresh already has an approved NDJSON direction. Archive, subscriptions, and organizer flows should adopt the same principle only where they currently write per-item durable progress.

### Diagnostics

Add or extend a read-only diagnostics payload that reports:

- App/worker role and version.
- Database availability and relevant table row counts.
- State-event counts by scope.
- Business-log counts by category/event/level.
- Runtime queue counts by status.
- Stale lease candidates.
- Batch temp-buffer files and ages.
- Recent whole-batch errors.

The diagnostics response must not expose cookies, tokens, signed URLs, or raw verification page bodies.

## Phase 3: User Operation Flow

Make UI state explain what is actually happening and what action is available.

### Dashboard Status Cards

The dashboard should show separate operational cards for:

- China MakerWorld account status.
- Global MakerWorld account status.
- Worker/queue status.
- Source refresh status.
- Archive queue status.

Each card should show an action only when it is meaningful:

- Visit official site or model page for manual verification.
- Retry failed work.
- Repair queue state.
- Pause or resume a queue.
- Open diagnostics.

### Task And Queue Pages

Task pages should distinguish:

- `running`: an executable child task is actively owned and heartbeating.
- `waiting_children`: a batch parent is waiting for child tasks.
- `blocked`: user or external action is required.
- `paused`: user intentionally stopped progress.
- `queued`: task is waiting for worker capacity.
- `failed`: task ended unsuccessfully and can be retried if supported.

The page should avoid implying that a batch parent is consuming worker CPU when it is only tracking children.

### Manual Verification Flow

MakerWorld verification should remain an external official-site action:

- Show account/site status in MakerHub.
- Provide visit-homepage or visit-model-page actions.
- Do not embed a browser automation component in this pass.
- Treat verification-required as a `blocked` state with a clear recovery path after the user finishes verification manually.

## Phase 4: Observability And Testing

Add focused verification before broad refactors.

### Backend Tests

Cover:

- Lease expiry requeues stale executable tasks.
- Exhausted retry budget marks stale tasks failed.
- Batch parents move to `waiting_children` while children remain.
- Batch parents finalize when children are done.
- Paused tasks are not recovered automatically.
- Write-budget helpers suppress repeated progress-only updates while preserving boundary events.
- Diagnostics redacts sensitive values and degrades when optional data is missing.

### Frontend Tests

Cover:

- Status labels for `running`, `waiting_children`, `blocked`, `paused`, `queued`, `failed`, and `completed`.
- Action visibility for verification, retry, repair, pause, resume, and diagnostics.
- No full source-library refresh on every child completion event.

### Operational Verification

Before release:

- Run focused backend tests for task state, worker recovery, diagnostics, logs, and state events.
- Run the frontend build.
- Run the runtime pressure/diagnostics script against local containers when available.
- Inspect `git diff --check`.

## Rollout

This should be released as a minor version because it changes visible task semantics and adds operational actions.

Rollout order:

1. Add shared status/lease helpers and tests without changing every workflow at once.
2. Apply helpers to archive queue and source refresh first because they have the highest operational impact.
3. Add diagnostics and repair summary.
4. Update UI labels and actions to consume the normalized status.
5. Extend the same patterns to subscriptions and local organizer flows.

If online validation shows unexpected recovery behavior, the repair action should be disabled first while leaving read-only diagnostics available.

## Success Criteria

- A batch parent no longer stays indefinitely in a misleading `running` state when child work is not progressing.
- Stale executable tasks can be detected and recovered or failed with an auditable summary.
- Running batches do not write durable per-item success/progress records unless explicitly needed.
- Dashboard and task pages show the same status vocabulary as the worker.
- Manual MakerWorld verification is represented as a blocked external action, not an embedded browser flow.
- Existing backend tests and frontend build pass after implementation.
