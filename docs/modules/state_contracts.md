# Runtime State Contracts

## Purpose

This document records MakerHub's Postgres-backed JSON state keys, common fields, status values, writers, readers, frontend consumers, and event scopes. It is the coordination point for changes that affect task progress, dashboard cards, state events, and worker/App runtime visibility.

## Common Fields

| Field | Meaning | Notes |
| --- | --- | --- |
| `id` / `task_id` | Stable item identity | Use the existing field for the owning state key; do not introduce a second identity for the same item. |
| `model_id` / `model_dir` | MakerWorld ID or local model directory | Keep both only when both are needed for lookup. |
| `url` / `model_url` | Source URL | Avoid storing signed download URLs in durable state. |
| `platform` | MakerWorld site | Expected values are `cn` and `global`. |
| `status` | Current lifecycle status for one item or process | Use module-specific allowed values below. |
| `state` | Diagnostic state for source/platform checks | Used when `status` would be too coarse. |
| `running` | Boolean process marker | Should match `status == "running"` when both exist. |
| `message` | Current item-level message | Keep short and sanitized. |
| `last_message` | Most recent process summary | Keep short and sanitized. |
| `created_at` | Item creation time | ISO text in the project's existing timezone format. |
| `updated_at` | Last state mutation time | ISO text in the project's existing timezone format. |
| `last_run_at` | Most recent run start/end marker used by schedulers | Preserve existing semantics per module. |
| `last_success_at` | Most recent successful completion | Empty when never successful. |
| `last_error_at` | Most recent failed completion | Empty when never failed. |
| `next_run_at` | Next scheduled run | Empty when disabled or unscheduled. |

## State Keys

| Key | Owner | Main Writers | Main Readers | Event Scope |
| --- | --- | --- | --- | --- |
| `archive_queue` | `TaskStateStore` | archive worker, task APIs | dashboard, tasks page, worker | `archive_queue` |
| `missing_3mf` | `TaskStateStore` | archive worker, remote refresh, task APIs | dashboard, tasks page, source health | `missing_3mf` |
| `organize_tasks` | `TaskStateStore` | local organizer/import | dashboard, organizer page | `organize_tasks` |
| `subscriptions_state` | `TaskStateStore` | subscription manager | dashboard, subscription pages | `subscriptions_state` |
| `remote_refresh_state` | `TaskStateStore` | remote refresh manager | dashboard, remote refresh page | `remote_refresh_state` |
| `source_refresh_queue` | `TaskStateStore` | source refresh manager | dashboard, remote refresh page | `source_refresh_queue` |
| `source_refresh_runs` | `TaskStateStore` | source refresh manager | dashboard, remote refresh page | `source_refresh_runs` |
| `model_flags` | `TaskStateStore` | model APIs, source deletion checks | model pages, catalog | `model_flags` |
| `three_mf_limit_guard` | source health/archive worker | 3MF quota logic | dashboard, tasks, archive worker | `three_mf_limit_guard` |
| `three_mf_daily_quota` | 3MF quota service | archive worker | archive worker, settings | `three_mf_daily_quota` |

## Status Sets

| State Area | Existing Values |
| --- | --- |
| Archive tasks | `queued`, `running`, `completed`, `success`, `failed`, `cancelled`, `skipped` |
| Missing 3MF | `missing`, `queued`, `running`, `failed`, `cancelled`, `download_limited`, `verification_required`, `cloudflare`, `auth_required`, `pending_download` |
| Organizer tasks | `queued`, `running`, `success`, `failed`, `skipped` |
| Subscriptions | `idle`, `pending`, `running`, `success`, `error`, `deleted` |
| Remote refresh | `idle`, `running`, `resuming`, `deferred`, `interrupted`, `success`, `error`, `disabled` |
| Source refresh tasks | `queued`, `running`, `succeeded`, `failed`, `skipped`, `timed_out`, `cancelled` |
| Source refresh runs | `queued`, `running`, `paused`, `resuming`, `completed`, `failed`, `interrupted`, `cancelled` |

These values are centralized in `app/services/state_contracts.py` for tests and low-risk consumers. Do not replace every string in the codebase mechanically; move consumers to constants only when it does not create circular imports or obscure local domain logic.

## Dashboard Event Scopes

The dashboard listens to these state scopes:

- `archive_queue`
- `missing_3mf`
- `organize_tasks`
- `subscriptions_state`
- `remote_refresh_state`
- `source_refresh_queue`
- `source_refresh_runs`
- `dashboard`

State events should trigger relevant payload refreshes, not broad full-page refreshes. A page that only shows source refresh status should not refresh source-library snapshots because an archive queue event arrived.

`remote_refresh_state` is the legacy/core batch state for scheduling, resume manifests, batch buffers, and final summaries. `source_refresh_queue` and `source_refresh_runs` are the newer source-refresh projection used by dashboard and source-refresh pages to avoid representing source refresh as archive queue work. Keep both until the refresh engine is fully split.

## Write Frequency Rules

- Durable logs should capture batch start, batch end, failures, and unusual diagnostics.
- Per-item success progress should prefer current-state fields instead of durable log rows.
- Per-item failures should stay inspectable, but large batch summaries should avoid unbounded payloads.
- State events should be emitted at meaningful state boundaries or debounced by callers.
- Messages must be sanitized before entering JSON state or business logs.
- Raw upstream HTML, cookies, tokens, share codes, proxy credentials, public base URLs, and signed download URLs must not be written to logs or user-visible state.

## Write Frequency Audit

Audited services:

| Area | Finding | Rule |
| --- | --- | --- |
| Source refresh | `remote_refresh.py` writes per-model results to a temporary NDJSON batch buffer while running, then stores summary counts, recent items, metrics, and failure samples at batch finish. | Keep this pattern; do not publish durable per-model success events during a running batch. |
| Subscription sync | `subscriptions.py` records subscription-level start/end/error state. It does not write one durable log row for every discovered model. | Keep sync logs at subscription source granularity. |
| Archive batch enqueue | `archive_worker.py` previously updated parent progress and wrote structured success logs for every discovered child during batch enqueue. | Parent progress is now throttled to coarse stages; successful, already-queued, and already-archived child outcomes are represented by the final `batch_enqueued` summary. |
| Archive batch recovery | `archive_worker.py` still logs child requeue/lost diagnostics and parent restore/completion events. | Keep these because they explain failures, retries, and crash recovery. |
| Local organizer | `local_organizer.py` writes item-level diagnostics around filesystem import/move/failure paths. | Defer changes unless a future audit identifies pure success spam in a large batch path. |
| State events | `TaskStateStore` publishes events on state mutations, while frontend `subscribeStateRefresh` coalesces matching scopes. | Prefer caller-side event suppression only when a batch has an explicit final state mutation. |

## Change Checklist

Before changing any runtime state shape:

1. Identify the owning state key in this document.
2. Check the writer service and frontend consumers.
3. Preserve existing fields unless a migration or normalization path is added.
4. Add or update focused tests for new status values or field semantics.
5. Update this document and `app/services/state_contracts.py` if the contract changes.
