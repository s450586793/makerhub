# MakerHub Project Simplification And Optimization Design

Date: 2026-06-04

## Purpose

MakerHub has grown into a modular monolith with a worker and Postgres-backed state. The current architecture is workable, but several files and page-level refresh patterns are now large enough that small changes often require reading unrelated code. This design defines a staged optimization route that simplifies the project without changing user-facing behavior in the same step.

## Goals

- Reduce maintenance cost by shrinking the largest API, service, and Vue page files into clearer modules.
- Reduce avoidable frontend refresh complexity by sharing page refresh/timer behavior.
- Keep each optimization independently shippable, testable, and reversible.
- Preserve current API routes, response shapes, data migration behavior, and UI behavior unless a later implementation plan explicitly changes them.

## Non-Goals

- Do not redesign the MakerHub UI.
- Do not remove offline model page generation.
- Do not rewrite the MakerWorld archiver from scratch.
- Do not remove legacy migration inputs until a dedicated audit proves they are no longer needed.
- Do not combine behavior changes with mechanical extraction work.

## Current Evidence

- Largest backend files:
  - `app/services/legacy_archiver.py`: about 7200 lines.
  - `app/api/config.py`: about 4187 lines.
  - `app/services/batch_discovery.py`: about 3472 lines.
  - `app/services/catalog.py`: about 2751 lines.
- Largest frontend files:
  - `frontend/src/pages/ModelDetailPage.vue`: about 3872 lines.
  - `frontend/src/pages/SettingsPage.vue`: about 2396 lines.
  - `frontend/src/pages/OrganizerPage.vue`: about 1672 lines.
- Refresh behavior is partly shared through `subscribeStateRefresh`, but some pages still own local timers or page-specific refresh scheduling.
- `app/templates` and `app/static` still appear to support offline archived model pages, so they are not assumed to be dead code.

## Approach

Use staged, behavior-preserving simplification:

1. Add or tighten tests around the behavior being moved.
2. Extract shared utilities or modules without changing public contracts.
3. Verify using module-specific tests plus frontend build where relevant.
4. Release small patch versions after each coherent stage when pushing.

## Stage 1: Frontend Refresh Consolidation

### Scope

Consolidate page refresh scheduling in Vue pages that currently mix `subscribeStateRefresh`, `setTimeout`, `setInterval`, visibility handlers, and page-local debounce logic.

Candidate pages:

- `frontend/src/pages/LogsPage.vue`
- `frontend/src/pages/TasksPage.vue`
- `frontend/src/pages/RemoteRefreshPage.vue`
- `frontend/src/pages/OrganizerPage.vue`
- `frontend/src/pages/SettingsPage.vue`

### Design

Create one or more small frontend helpers, likely in `frontend/src/lib/`, for:

- scoped SSE refresh subscription
- hidden-tab deferral
- running-task polling or throttled refresh
- cleanup on component unmount

The existing `subscribeStateRefresh` behavior should remain the foundation. New helpers should wrap it instead of replacing it with a parallel event system.

### Success Criteria

- Pages keep the same visible refresh behavior.
- Running tasks still update while visible.
- Hidden tabs defer nonessential refreshes.
- Each page has less custom timer cleanup code.
- Tests cover timer scheduling and cleanup logic outside Vue components where practical.

## Stage 2: API Route File Split

### Scope

Split `app/api/config.py` by responsibility while preserving route paths and response shapes.

Candidate route modules:

- `dashboard_routes.py` for `/api/dashboard`
- `mobile_import_routes.py` for `/api/mobile-import/*`
- `events_routes.py` for `/api/events/archive` and `/api/events/state`
- `settings_routes.py` for settings writes such as cookies, proxy, user, theme, notifications, runtime, and advanced settings
- account routes may stay with settings initially or move to `online_accounts_routes.py` if the extraction remains small

### Design

Move route handlers and directly related helpers together. Keep shared payload assembly helpers in a service or narrow API helper module only when multiple route modules need them.

This is a mechanical extraction stage:

- no route path changes
- no schema changes
- no auth behavior changes
- no frontend request changes

### Success Criteria

- `app/api/config.py` is reduced to a compatibility/sharing helper module or a much smaller route module.
- FastAPI still registers the same endpoint paths.
- Existing backend tests for config, cookies, dashboard, mobile import, SSE, sharing, and auth pass.
- `tests/test_web_routes.py` still passes.

## Stage 3: Large Vue Page Decomposition

### Scope

Decompose the largest Vue pages, starting with:

- `frontend/src/pages/ModelDetailPage.vue`
- `frontend/src/pages/SettingsPage.vue`

### Design

Prefer composables and narrow child components over visual redesign.

For `ModelDetailPage.vue`, candidate separations:

- media/gallery state
- comments loading/render scheduling
- attachments upload/delete
- local model edit flows
- 3D preview lifecycle

For `SettingsPage.vue`, candidate separations:

- online account login and sync
- proxy settings
- token management
- sharing and mobile import settings
- runtime/system update state

### Success Criteria

- Page routes and UI behavior remain stable.
- Extracted helpers expose small, named APIs.
- Business request payload normalization moves into tested frontend lib functions where practical.
- `npm --prefix frontend run build` passes after each extraction.

## Stage 4: MakerWorld Discovery And Archiver Decomposition

### Scope

Split pure or near-pure parts of `legacy_archiver.py` and `batch_discovery.py` after adding coverage around the behavior being moved.

Candidate modules:

- design API and HTML payload extraction
- comments and threaded reply normalization
- attachment and media download helpers
- offline archived model page rendering
- author upload discovery
- collection discovery
- account followed authors/collections/favorites discovery

### Design

Start with pure functions and stable helpers. Avoid changing network behavior, fallback order, or MakerWorld parsing rules in the same step as moving code.

### Success Criteria

- Existing MakerWorld-related tests pass.
- Public imports used by `archive_worker.py`, `process_jobs.py`, `subscriptions.py`, and `remote_refresh.py` remain stable or are migrated in one narrow step.
- Error messages for Cookie invalidation, Cloudflare verification, missing 3MF, daily limits, and source deletion remain diagnostic and sanitized.

## Stage 5: Legacy Surface Audit

### Scope

Audit old templates, static assets, compatibility state paths, and migration-only code.

Candidate areas:

- `app/templates/*`
- `app/static/js/app.js`
- `app/static/css/app.css`
- legacy JSON/log migration inputs
- compatibility state fallbacks

### Design

Classify each item as:

- active runtime dependency
- offline archive page dependency
- migration-only dependency
- removable dead code

Only remove code after there is evidence that no runtime, offline archive, or migration path uses it.

### Success Criteria

- A removal candidate list exists before any deletion.
- Offline archived model pages still render.
- First-run migration from old JSON/log files remains intact unless explicitly deprecated.

## Verification Strategy

Use targeted verification per stage:

- Frontend refresh: Node tests for scheduling helpers and `npm --prefix frontend run build`.
- API split: relevant backend unit tests plus route shape tests.
- Vue page decomposition: frontend build and existing shape tests; add tests for extracted lib functions.
- Archiver/discovery split: MakerWorld parsing, batch discovery, missing 3MF, Scrapling, and process job tests.
- Legacy audit: route tests, offline page generation coverage, and migration tests.

Before pushing any implementation stage:

- run the stage-specific tests
- run `git diff --check`
- bump patch version
- update README and CHANGELOG with a focused note

## Risk Controls

- Prefer move-only commits before behavior changes.
- Avoid changing route paths, JSON state keys, or frontend API calls during extraction.
- Keep each stage small enough to revert independently.
- Do not touch unrelated modules while extracting.
- When a file has weak test coverage, add tests before moving code.

## Recommended Execution Order

1. Frontend refresh consolidation.
2. `app/api/config.py` route split.
3. `ModelDetailPage.vue` and `SettingsPage.vue` decomposition.
4. `legacy_archiver.py` and `batch_discovery.py` decomposition.
5. Legacy surface audit and removals.

This order gives early runtime and maintainability benefits while delaying the riskiest MakerWorld parsing changes until coverage and boundaries are clearer.
