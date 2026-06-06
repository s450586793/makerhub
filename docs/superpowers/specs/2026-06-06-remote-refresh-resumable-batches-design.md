# Remote Refresh Resumable Batches Design

Date: 2026-06-06

## Purpose

The online MakerHub instance showed that source refresh is not simply failing to start. Logs prove later batches started after the last displayed successful run, but the page still showed the May 25 summary. The investigation found three overlapping problems:

- A source refresh batch can be interrupted by worker replacement or restart before it writes `batch_finished`.
- Runtime JSON state can be force-migrated from old files back into Postgres, making the UI show stale `remote_refresh_state`.
- Stale archive queue active items can keep source refresh deferred forever, while the UI collapses "last run", "last result", and "last deferred reason" into one confusing story.

This design makes source refresh resumable, protects live runtime state from stale JSON backfill, and makes the UI explain the real scheduler state.

## Goals

- Resume an interrupted source refresh batch after worker restart when enough batch data still exists.
- Mark interrupted source refresh work explicitly when it cannot be resumed.
- Prevent legacy JSON migration from overwriting newer Postgres runtime state.
- Keep source refresh from being blocked forever by stale archive active entries.
- Show separate timestamps and reasons for last batch start, last completion, last scheduler attempt, last deferral, and interruption.
- Preserve the current low-write NDJSON batch-result pattern.

## Non-Goals

- Do not rewrite the MakerWorld archive implementation.
- Do not embed browser verification or add browser automation.
- Do not move all runtime state into new relational task tables in this pass.
- Do not make source refresh run concurrently with real active archive or local organizer work.
- Do not remove the existing `/api/remote-refresh` API route.

## Findings From The Online Instance

The current state endpoint reported:

- `last_run_at`: `2026-05-25T03:42:02+08:00`
- `last_success_at`: `2026-05-25T05:32:26+08:00`
- `last_message`: source refresh deferred because archive queue or local organizer work was running.
- Last visible batch summary: `1670` succeeded and `22` failed.

Business logs showed later `batch_started` entries on June 1 and June 5, including a June 5 run with `2773` selected models. There was no matching `batch_finished` for those later starts. System logs showed a self-update at `2026-06-05T18:08:00+08:00`, followed by worker replacement and restart at `2026-06-05T18:18:56+08:00`, which explains how the June 5 source refresh could be interrupted mid-batch.

Database logs also showed repeated `json_state_migrated` events with `updated=20` and `skipped=0`, which is the force-migration shape. Force migration can overwrite live Postgres runtime state from stale JSON files.

The archive queue also contained long-lived active entries, including an old May 25 running child. Since source refresh currently treats any active or queued archive item as busy, stale active queue state can defer source refresh forever.

## Approaches Considered

### Option A: UI Explanation Only

Only split the source refresh UI fields and change labels.

Tradeoff: This would reduce confusion but leave interrupted batches, stale queue blockers, and JSON state overwrite risks in place.

Decision: Rejected as insufficient.

### Option B: Scheduler And State Cleanup

Track deferrals separately, repair stale queue blockers, and prevent runtime state from being force-overwritten.

Tradeoff: This fixes the misleading state and permanent deferral, but interrupted source refresh batches still lose their work and restart from scratch.

Decision: Useful baseline, but not enough for large source refresh batches.

### Option C: Resumable Source Refresh Batch

Persist a source refresh run manifest, keep the existing NDJSON completion records, resume unfinished work after worker restart, and add runtime-state safeguards.

Tradeoff: This is larger than a UI fix, but it addresses the real failure mode and avoids wasting hours of already completed source refresh work.

Decision: Selected.

## Selected Design

Implement resumable source refresh batches around the current `RemoteRefreshManager` and `TaskStateStore` contracts. The implementation keeps current public routes and the existing temp NDJSON result buffer, but adds a durable batch manifest and clearer runtime fields.

## Source Refresh Run State

Add an `active_run` object under `remote_refresh_state`:

- `batch_id`: stable ID for the running or resumable batch.
- `status`: `running`, `resuming`, `interrupted`, `completed`, or `abandoned`.
- `started_at`: first start time for this batch.
- `resumed_at`: last resume time, if any.
- `finished_at`: completion time.
- `scheduled_cron`: cron expression used for this run.
- `manual`: whether it came from a manual trigger.
- `candidate_total`: number of selected models in the manifest.
- `completed_total`: number of manifest entries already represented in the NDJSON result file.
- `remaining_total`: candidate total minus completed total.
- `manifest_path`: relative path to the manifest file.
- `result_path`: relative path to the NDJSON result file.
- `interrupted_reason`: short sanitized reason when the batch cannot continue.

Existing fields remain for compatibility. `last_run_at` keeps its current meaning as the latest batch start. New fields make the page clearer:

- `last_attempt_at`: last scheduler or manual trigger attempt.
- `last_deferred_at`: last time source refresh was deferred by archive/local work.
- `last_defer_reason`: sanitized reason for the latest deferral.
- `last_interrupted_at`: last interrupted batch time.
- `last_interrupted_reason`: sanitized interruption message.
- `last_completed_at`: latest batch completion, success or error.

`last_success_at` and `last_error_at` keep their current compatibility semantics.

## Batch Manifest

When `_run_batch()` selects candidates, it writes a manifest before model processing starts:

- Location: `/app/config/state/remote_refresh_batches/<batch_id>.manifest.json`.
- Contents: schema version, batch ID, created time, selected model list, selected stats, cron, manual flag, and app version if available.
- Each selected model entry stores only the safe fields needed to rerun that model: model directory, title, normalized source URL, and priority metadata. It must not store cookies, signed URLs, or raw upstream HTML.

The existing NDJSON result file becomes the completion journal for the manifest. Each record must include `model_dir` and source URL so resume can identify completed entries.

## Resume Behavior

Worker startup calls source refresh state normalization before scheduling new work:

1. Load `remote_refresh_state.active_run`.
2. If no active run exists, continue normally.
3. If active run is `running` or `resuming`, check the manifest and result file.
4. If the manifest and result file are compatible, compute completed keys from NDJSON and resume only incomplete manifest entries.
5. If no incomplete entries remain, finalize the batch from the NDJSON summary.
6. If the manifest is missing, corrupt, or from an incompatible schema, mark it `interrupted`, preserve diagnostic paths, and schedule the next normal run.

Resume should reuse the same `batch_id` and append to the same NDJSON result file. It should update `active_run.status` to `resuming`, then back to `running` while processing.

Manual trigger behavior:

- If a resumable active run exists, manual trigger should resume it instead of starting a second full batch.
- If the user explicitly asks for a fresh run later, that should be a separate future control, not part of this pass.

## Interruption Behavior

If a worker exits cleanly, shutdown should best-effort mark the current active run as interrupted with reason `worker_stopped`. Because abrupt container replacement may not run cleanup, startup resume is the authoritative recovery path.

If `_run_batch()` raises outside per-model handling, the active run should be marked `interrupted` instead of leaving stale `running=True` state.

If a run is interrupted by system update and later resumed, the final message should say it resumed an interrupted batch and include processed totals.

## Deferral And Busy-State Semantics

Source refresh should not start while real archive work or local organizer work is active. The busy check should change from "any active archive entry" to "any executable non-stale active archive child or queued work that is still valid".

Rules:

- `waiting_children` batch parents do not count as executable busy work by themselves.
- Active single-model archive tasks count as busy only while their lease or heartbeat is fresh.
- Stale active tasks should be repaired by the archive queue repair path and should not block source refresh indefinitely.
- Pending, queued, or running local organizer items still count as busy while fresh.

When busy, source refresh writes:

- `last_attempt_at`: now.
- `last_deferred_at`: now.
- `last_defer_reason`: `archive_queue_busy`, `local_organizer_busy`, or `stale_runtime_state`.
- `next_run_at`: now plus the existing retry delay.

It should not replace the last completed batch message with the deferral message.

## JSON Migration Safeguards

`migrate_json_files_to_database(force=True)` must not blindly overwrite runtime keys that are newer or active in Postgres. Runtime keys include at least:

- `archive_queue`
- `missing_3mf`
- `organize_tasks`
- `subscriptions_state`
- `remote_refresh_state`
- `three_mf_limit_guard`
- `three_mf_daily_quota`
- `archive_repair_status`
- `archive_profile_backfill_status`
- `system_update`

For runtime keys, forced migration should:

- Skip if Postgres already has non-empty data and the source file is older or lacks an explicit restore marker.
- Record item status as `protected_runtime_state` when skipped for this reason.
- Continue to allow first-time database bootstrap from JSON files.
- Continue to migrate static configuration and marker-like keys safely.

The migration log should include per-key statuses in the payload for future diagnosis.

## UI Design

The source refresh page should keep the existing compact workstation layout but change the status cards:

- `当前状态`: idle, running, resuming, deferred, interrupted, disabled, or error.
- `本轮计划`: active or last batch candidate count.
- `已完成/剩余`: for active/resumable runs.
- `最近完成`: last completed batch time and success/failure counts.

The settings card should replace the ambiguous three fields with:

- `下次运行`
- `上次尝试`
- `上次批次开始`
- `上次完成`
- `最近阻塞`
- `最近中断`

The batch summary should clearly label whether it is the "last completed batch" or the "current resumable batch".

If a resumable batch exists, show one primary action:

- `继续源端刷新`

If source refresh is blocked by stale queue state, show:

- `修复队列状态`

This action should call the existing archive queue repair endpoint or a small compatibility wrapper, then reload source refresh state.

## API Shape

Keep `GET /api/remote-refresh` and `POST /api/remote-refresh/run`.

Extend the state payload with the new fields. Existing frontend consumers should keep working because existing fields remain.

`POST /api/remote-refresh/run` should return whether it started a new run or resumed an interrupted one:

- `accepted`
- `mode`: `new`, `resume`, `queued_for_worker`, or `rejected`
- `message`
- `state`

## Error Handling And Redaction

All persisted messages must use existing source refresh sanitizers. The manifest must not store cookies, request headers, signed asset URLs, or raw HTML error pages.

If manifest parsing fails, store only the exception class and a short sanitized message. Keep the corrupt file for retention-based cleanup unless it contains unsafe content.

## Tests

Backend tests should cover:

- A started batch writes `active_run`, manifest, and result path.
- Worker startup resumes a partial batch and skips completed manifest entries.
- A fully completed manifest finalizes from NDJSON without rerunning models.
- Missing or invalid manifest marks the run interrupted and schedules the next run.
- Manual trigger resumes a resumable batch instead of starting a duplicate.
- Busy-state check ignores `waiting_children` batch parents.
- Busy-state check treats stale active archive tasks as repair candidates, not permanent blockers.
- Deferral updates `last_attempt_at`, `last_deferred_at`, and `last_defer_reason` without overwriting the last completed batch message.
- Forced JSON migration protects runtime keys when Postgres already has non-empty state.
- Migration logs include per-key statuses.

Frontend tests should cover:

- Source refresh page labels last attempt, last start, last completion, deferral, and interruption separately.
- Resumable batch action appears only when `active_run` is resumable.
- Queue repair action appears when the state reports stale queue blocking.
- Existing empty and idle states still render.

Verification commands:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_database_json_state.py tests/test_archive_worker_batch_retry.py -q
cd frontend && npm test -- --run
cd frontend && npm run build
git diff --check
```

## Rollout

1. Add backend manifest and state helpers behind the existing API.
2. Add resume logic and tests.
3. Add migration runtime-state protection and tests.
4. Add stale queue busy-state semantics and tests.
5. Update the source refresh UI labels and actions.
6. Run focused tests and build.
7. Bump patch version and update release notes when implementation is complete.

## Success Criteria

- A source refresh batch interrupted by worker replacement can resume without reprocessing already completed models.
- Source refresh state no longer falls back to stale JSON after database state exists.
- The page no longer shows a deferral message as the last completed result.
- A stale archive active item cannot block source refresh forever.
- The online instance can explain whether source refresh is idle, deferred, interrupted, resumable, or completed.
