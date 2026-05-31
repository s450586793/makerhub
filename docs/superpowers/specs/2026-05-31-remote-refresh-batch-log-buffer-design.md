# Remote Refresh Batch Log Buffer Design

## Context

The production worker showed high CPU and large write volume while a source refresh batch was running. The current remote refresh path writes status and logs at model-level granularity:

- `model_started`, `model_succeeded`, `model_failed`, and `model_deleted_on_source` can write log rows per model.
- `patch_remote_refresh_state()` saves remote refresh state and publishes a state event on every call.
- `_set_current_item()` can be called by progress callbacks while a model is being refreshed.
- `_run_batch()` updates remote refresh counters after every completed model.

This creates repeated writes to `makerhub_logs`, `makerhub_state_events`, and the remote refresh state document during large batches.

The approved direction is C only: reduce log and state write frequency. Do not lower concurrency, pause batches, or add batch-size throttling as the primary fix.

## Goals

- Keep source refresh worker throughput unchanged.
- Stop writing per-model success/progress logs to the database during a running batch.
- Stop publishing per-model remote refresh state events during a running batch.
- Avoid keeping all model results in memory while a large batch runs.
- Refresh the frontend once when the batch completes.
- Preserve enough failure and summary detail for debugging.

## Non-Goals

- Do not change source refresh concurrency settings.
- Do not change MakerWorld request limits.
- Do not redesign the logs page.
- Do not batch every MakerHub log category. Low-frequency operational logs such as auth, settings, browser verification, and update events should remain immediate.
- Do not introduce a Postgres staging table for this path.

## Selected Approach

Use a per-batch temporary NDJSON file as the running batch buffer.

The worker writes one compact JSON line per completed model to a local temporary file. Batch start and batch finish remain durable state boundaries. Between those boundaries, model-level completion writes stay out of the database.

At batch completion, the worker reads the temp file, aggregates the results, then performs the final durable writes once:

- update `remote_refresh_state`
- publish one `remote_refresh_state` event
- append one `batch_finished` structured log
- append one `batch_finished` business log

This keeps memory bounded and removes database writes from the hot per-model completion path.

## Data Flow

### Batch Start

1. Generate a `batch_id`.
2. Create a temporary NDJSON file under a dedicated runtime directory, for example `data/runtime/remote_refresh_batches/<batch_id>.ndjson`.
3. Write one remote refresh state update:
   - `running=true`
   - `status=running`
   - `last_run_at`
   - `last_batch_total`
   - initial candidate and skip counts
   - clear `current_item` and `current_items`
   - message explaining that the batch is running and results will refresh when it completes
4. Write one `batch_started` log.
5. Publish one state event for the batch start.

### Batch Running

For each model:

1. Keep only minimal in-memory counters needed by the executor loop.
2. Append one compact result line to the batch NDJSON file when the model finishes.
3. Do not write `makerhub_logs` for successful models.
4. Do not call `append_remote_refresh_history()` per model.
5. Do not publish `remote_refresh_state` events per model.
6. Do not persist progress callback updates to global state.

The result line should contain only fields needed for final aggregation:

```json
{"model_dir":"...","title":"...","url":"...","status":"success","message":"...","updated_at":"...","change_labels":["..."],"metrics":{"total_duration_ms":1234.5}}
```

Failure and deleted-source records use the same shape with `status="failed"` or `status="source_deleted"` and an error message.

Because models run in a `ThreadPoolExecutor`, temp-file writes must go through one of these safe paths:

- a small `threading.Lock` around line append and flush; or
- a single writer helper owned by the batch loop.

The implementation should not let multiple worker threads write raw lines to the same file without coordination.

### Batch Finish

1. Read the NDJSON file.
2. Aggregate:
   - succeeded count
   - failed count
   - skipped count
   - source deleted count
   - slow models
   - resource waits
   - batch duration
   - recent 50 items
   - failure samples
3. Write final `remote_refresh_state` once.
4. Append final `batch_finished` structured and business logs once.
5. Publish one `remote_refresh_state` event.
6. Invalidate the archive snapshot once.
7. Delete the temp file after successful aggregation and durable writes.

### Batch Error Or Interruption

If the batch crashes or is interrupted:

1. Preserve the NDJSON file for short-term debugging.
2. On next startup or next batch start, detect stale batch files.
3. Mark the previous batch as interrupted/error if state still says it was running.
4. Keep a small number of stale temp files or files newer than a retention window.

## Logging Policy

Remote refresh logging becomes batch-oriented:

- Keep immediate logs:
  - manual trigger accepted/rejected
  - batch started
  - batch finished
  - scheduler error
  - whole-batch crash/interruption
- Suppress default DB logs:
  - model started
  - model succeeded
  - progress callback
- Aggregate into batch finish:
  - successful model count
  - skipped model count
  - failed model count
  - source-deleted count
  - slow model list
  - top failure samples
  - batch metrics

Single-model failure logs do not need immediate DB writes during normal batch execution; they are captured in the temp file and summarized at batch finish. If a failure prevents the whole batch from finishing, the preserved temp file is the fallback diagnostic artifact.

## State Event Policy

Remote refresh state events become boundary events:

- publish at batch start
- publish at batch finish
- publish on whole-batch error or interrupted recovery
- publish on manual trigger rejection or scheduler-level error

Do not publish events for:

- every model completion
- progress callback changes
- current item changes
- recent item append during the running batch

The frontend may continue showing the running state from batch start. It should refresh final counts and recent items after the batch finish event.

## Implementation Boundaries

The expected code changes are scoped to:

- `app/services/remote_refresh.py`
  - create and write the per-batch NDJSON buffer
  - return model result records instead of directly writing success history/logs
  - aggregate the temp file at batch finish
- `app/services/task_state.py`
  - add a way to save remote refresh state without publishing on every internal mutation, or add explicit boundary publish methods
- `app/services/business_logs.py`
  - no broad global batching required
  - optional helper for one-shot batch summary entries only if it reduces duplication

The implementation should keep existing public API responses compatible.

## Testing

Add or update tests to cover:

- After the batch-start boundary, a successful batch writes one final remote refresh state update and one finish event instead of per-model events.
- Per-model success does not call business/structured log appenders.
- Failure records are written to the temp file and included in the final batch summary.
- Batch finish deletes the temp file after successful aggregation.
- Interrupted/stale temp files are detected and do not leave state stuck as running.
- Existing manual trigger rejection and scheduler error paths still publish immediate state/log updates.

## Acceptance Criteria

- During a large source refresh batch, `makerhub_logs` and `makerhub_state_events` no longer grow per completed successful model.
- The frontend shows the batch as running after start and refreshes final counts once after completion.
- CPU and DB write pressure from log/state persistence is reduced without changing worker concurrency.
- Failure information remains available in the final batch summary and preserved temp files for interrupted batches.
