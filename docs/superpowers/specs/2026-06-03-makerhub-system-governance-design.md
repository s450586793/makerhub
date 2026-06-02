# MakerHub System Governance Design

Date: 2026-06-03

## Purpose

MakerHub has moved beyond single-feature work. The current risk is not missing functionality; it is accumulated complexity around state, API boundaries, long-running background flows, and large files. The goal is to optimize the project in one continuous effort while keeping each change reversible, testable, and compatible with the running instance.

This design covers code and workflow governance for:

- Core task state fields and JSON state contracts.
- Frontend state-event refresh behavior.
- Business log and progress-write frequency.
- API route boundaries.
- Large service and frontend page decomposition.
- App/Worker runtime responsibility diagnostics.

The work should preserve current user-facing behavior unless a specific improvement is called out below.

## Current Findings

The first read-only scan found these high-risk areas:

- `app/api/config.py` is over 5,200 lines and owns unrelated API domains including configuration, sharing, models, source library, tasks, subscriptions, logs, remote refresh, and archive preview.
- `app/services/legacy_archiver.py` is over 7,100 lines and is still a core MakerWorld implementation dependency despite its legacy name.
- Several service files are large and cross-cutting: `batch_discovery.py`, `catalog.py`, `local_organizer.py`, `remote_refresh.py`, `subscriptions.py`, and `task_state.py`.
- Core runtime state is stored as Postgres-backed JSON blobs with string status values. Important keys include `archive_queue`, `missing_3mf`, `organize_tasks`, `subscriptions_state`, and `remote_refresh_state`.
- Frontend pages implement their own variants of state-event subscription, debouncing, visibility refresh, timers, and manual reload behavior.
- Business logs are centralized in Postgres, but task progress messages, result messages, and error strings are still created in many places.
- App and Worker responsibilities depend on runtime environment flags, so misconfigured deployments can be hard to diagnose.
- Large frontend pages such as `ModelDetailPage.vue`, `SettingsPage.vue`, and `OrganizerPage.vue` combine API calls, state adaptation, form behavior, and UI rendering.

## Principles

1. Preserve API paths and response shape during route splitting.
2. Prefer contract documentation and constants before behavior changes.
3. Keep state writes and events meaningful; avoid per-item write storms during large batches.
4. Extract low-risk pure helpers before touching high-risk workflows.
5. Do not rewrite MakerWorld archiving logic in this pass.
6. Keep UI appearance stable unless the change is directly about refresh or state behavior.
7. Use one focused commit per batch so regressions can be isolated.
8. Run targeted tests after each batch, then broader validation at the end.

## Approach Options Considered

### Option A: Big-bang refactor

Refactor API, services, frontend pages, state fields, and logging in one large change.

Tradeoff: This may look efficient, but it would create a very large blast radius. If the running instance breaks, it would be hard to identify whether the cause is API routing, state normalization, frontend refresh logic, or a service extraction.

Decision: Reject.

### Option B: Continuous batched governance

Execute all requested optimization work in a single continuous effort, but split it into independent batches with tests and commits between them.

Tradeoff: Slightly more ceremony than one large commit, but it keeps the work debuggable and lets each batch be validated.

Decision: Recommended.

### Option C: Audit-only roadmap

Only document issues and defer implementation.

Tradeoff: Lowest risk now, but it does not address the user's request to actually optimize the project.

Decision: Reject for this task, but keep generated docs as part of the work.

## Design

### Batch 1: State Contract and Refresh Governance

Create a documented contract for core runtime JSON state. The contract should list key names, important fields, status values, writer modules, reader modules, frontend consumers, and event scopes.

Expected outputs:

- A state-contract document under `docs/modules/` or `docs/`.
- A small constants/helper module only if it can be added without large migrations.
- Tests for normalization helpers if new helpers are introduced.

Initial state keys:

- `archive_queue`
- `missing_3mf`
- `organize_tasks`
- `subscriptions_state`
- `remote_refresh_state`
- `model_flags`
- `three_mf_limit_guard`
- `three_mf_daily_quota`

Important field families:

- Identity: `id`, `task_id`, `model_id`, `model_dir`, `url`, `platform`.
- Status: `status`, `state`, `running`, `queued_count`, `active_count`.
- Time: `created_at`, `updated_at`, `last_run_at`, `last_success_at`, `last_error_at`, `next_run_at`.
- Message: `message`, `last_message`, `detail`, `error`.
- Batch counters: `total`, `succeeded`, `failed`, `skipped`, `remaining`.

Frontend refresh governance should identify pages that currently subscribe to state events or use timers. It should consolidate shared behavior into a helper when the existing patterns are equivalent.

Do not make every state change trigger a full source-library refresh. Pages should refresh only the API payload they actually need.

### Batch 2: Log and Event Write Frequency

Audit business log writes, state-event publication, and task-state saves in batch-heavy flows:

- Source refresh.
- Subscription sync.
- Batch archive discovery/enqueue.
- Missing 3MF retry.
- Local organize/import.

The optimization target is to reduce repeated write amplification while keeping useful operational diagnostics.

Rules:

- Batch start and batch completion should be logged.
- Per-item failures should remain inspectable but may be buffered or summarized.
- Per-item success logs should be avoided unless they contain meaningful diagnostics.
- UI progress may use lightweight current-state fields rather than persistent log rows.
- Error messages must stay sanitized and should not include HTML verification bodies, raw cookies, tokens, signed URLs, or public base URLs.

### Batch 3: API Route Boundary Split

Split `app/api/config.py` by route domain while preserving existing route paths and behavior.

Target route modules:

- `app/api/config_routes.py`: settings and runtime configuration.
- `app/api/sharing_routes.py`: share creation, manifest, receive, cleanup, public file access.
- `app/api/models_routes.py`: model list, detail, comments, flags, local edits, attachments, downloads.
- `app/api/source_library_routes.py`: source library payloads, snapshots, source states.
- `app/api/tasks_routes.py`: archive tasks, missing 3MF actions, archive preview, repair/backfill admin endpoints if they fit better here.
- `app/api/subscriptions_routes.py`: subscription CRUD and manual sync.
- `app/api/remote_refresh_routes.py`: remote refresh state, run request, config update if not kept in config routes.
- `app/api/logs_routes.py`: logs and state events if not better under system routes.

Shared manager instances must remain singletons or be moved to a small dependency module so imports do not create duplicate workers.

Route split success criteria:

- All existing API paths still resolve.
- Existing tests pass without frontend route changes.
- `app/main.py` registers the new routers clearly.
- No domain route import should require reading unrelated route files for normal changes.

### Batch 4: Service Decomposition by Low-Risk Extraction

Start with pure or near-pure helpers. Do not rewrite the core archive job.

Potential extraction targets:

- From `legacy_archiver.py`:
  - URL normalization helpers.
  - filename/path sanitization helpers if they are widely imported.
  - comment/profile normalization helpers.
  - image/asset collection helpers.
  - 3MF candidate parsing helpers only if tests cover them.
- From `task_state.py`:
  - status normalization and message sanitization helpers.
  - summary payload builders.
- From `remote_refresh.py`:
  - batch summary/message generation.
  - history item normalization.
- From `catalog.py`:
  - model-detail payload shaping and filter/sort helper logic.

Service extraction success criteria:

- Import paths remain stable through compatibility re-exports if many modules currently import old names.
- Tests prove old public behavior remains intact.
- No generated circular imports.

### Batch 5: Frontend Page Logic Extraction

Extract logic from large pages without changing layout or visual style.

Targets:

- `ModelDetailPage.vue`: detail payload preparation, attachment actions, local model editing, image/file operations.
- `SettingsPage.vue`: system update state, online account/Cookie operations, proxy/sharing/runtime forms.
- `OrganizerPage.vue`: organizer payload adaptation, upload status matching, refresh scheduling.
- Shared state-event refresh behavior from dashboard, tasks, remote refresh, organizer, subscriptions, and model pages.

Use composables or helper modules under `frontend/src/lib/` or a small `frontend/src/composables/` folder if that matches the existing project style.

Frontend extraction success criteria:

- Visual output is unchanged.
- Existing Node tests pass.
- `npm --prefix frontend run build` passes.
- No page loses dark/light/auto theme support.

### Batch 6: App/Worker Runtime Diagnostics

Improve observability around deployment shape without changing the compose default.

Possible outputs:

- A lightweight runtime-role payload in existing system/config responses.
- Settings or dashboard diagnostics showing process role, background task enabled state, worker container config, database availability, and current version.
- Business logs on startup already include role data; confirm that the UI exposes enough of it for troubleshooting.

Success criteria:

- Misconfigured app-as-worker or worker-disabled states are easier to detect.
- No new background task is started in the App container by accident.

## Data Flow Expectations

A healthy long-running workflow should look like this:

1. User action writes a small request state.
2. Worker picks up the job and updates one authoritative state key.
3. State event tells interested frontend pages to refresh only relevant payloads.
4. Batch work avoids per-item durable writes unless a failure or meaningful checkpoint occurs.
5. Batch completion writes final summary, counters, and recent items.
6. Model index or source-library cache is refreshed only when data that affects them changes.

## Error Handling

- Errors returned to UI should be short and actionable.
- Raw upstream HTML, cookie values, auth tokens, share codes, signed download URLs, and proxy credentials must not appear in logs or UI messages.
- State normalization should gracefully handle old fields and old status values.
- Route splitting must preserve existing exception behavior unless tests show a bug.

## Testing Plan

Run focused tests after each batch. The likely command groups are:

```bash
.venv/bin/python -m unittest tests.test_task_state tests.test_database_json_state tests.test_business_logs tests.test_source_health
.venv/bin/python -m unittest tests.test_remote_refresh tests.test_process_jobs
.venv/bin/python -m unittest tests.test_subscriptions tests.test_source_library
.venv/bin/python -m unittest tests.test_batch_discovery tests.test_missing_3mf tests.test_scrapling_fetch tests.test_three_mf_quota
.venv/bin/python -m unittest tests.test_auth_guard tests.test_config_cookies tests.test_self_update tests.test_github_changelog
node --test frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

For route splitting, also run API smoke tests if available or add focused tests for route registration where useful.

## Rollout Plan

- Commit this design document first.
- Create an implementation plan that sequences the batches and identifies exact files.
- Execute each batch as a separate commit.
- Do not push until explicitly requested.
- Bump version and release notes only when the user asks to push.

## Non-Goals

- No database schema migration from JSON state to fully relational business tables in this pass.
- No rewrite of MakerWorld scraping/downloading behavior.
- No UI redesign.
- No change to public API paths.
- No removal of legacy compatibility behavior unless it is proven unused and covered by tests.
