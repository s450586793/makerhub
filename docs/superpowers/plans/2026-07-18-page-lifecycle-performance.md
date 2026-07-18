# Page Lifecycle Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make MakerHub's high-frequency work pages return instantly while preventing hidden cached pages from continuing network refreshes.

**Architecture:** Cache only the five operational list pages in `AppShell` using route metadata and a bounded `KeepAlive`. Extend the existing refresh controller with an active predicate, then each cached page pauses its refresh subscription and aborts in-flight resource requests while deactivated; activation retains rendered data and starts a silent light-payload refresh.

**Tech Stack:** Vue 3, Vue Router, native `AbortController`, existing `createHydratedResource`, existing state-event SSE controller, Node test runner.

## Global Constraints

- Keep server-side pagination and `/light` endpoints; do not introduce client-side full-list pagination.
- Cache only `models`, `subscriptions`, `organizer`, `remote-refresh`, and `tasks`; never cache settings, detail pages, or live logs.
- Preserve existing page cache, route query, scroll-anchor, and explicit full-enrichment behavior.
- Hidden or deactivated pages must not issue refresh requests or retain a scheduled refresh timer.
- Version the user-visible improvement as a minor release and keep only three visible README release entries.

---

### Task 1: Active Refresh Gate

**Files:**
- Modify: `frontend/src/lib/pageRefresh.js`
- Test: `frontend/src/lib/pageRefresh.test.mjs`

**Produces:** `createPageRefreshScheduler({ isActive })`, which clears pending work and skips refresh calls while a cached page is inactive.

- [x] Write failing tests that schedule an event while `isActive()` is false, then assert no timer or refresh is produced; assert `refreshNow()` is also ignored while inactive.
- [x] Implement an `isActive` option defaulting to `() => true`; use it in `schedule`, `run`, `handleVisible`, and `refreshNow`, clearing pending state when inactive.
- [x] Run the frontend test suite and verify the new tests pass.

### Task 2: Bounded Route Cache

**Files:**
- Modify: `frontend/src/router.js`
- Modify: `frontend/src/layouts/AppShell.vue`
- Test: `frontend/src/lib/routerShape.test.mjs`

**Produces:** Five named route records with `meta.keepAlive: true`, rendered by a keyed `KeepAlive` bounded to five instances.

- [x] Write a failing shape test requiring the five operational route names to opt into keep-alive and requiring `AppShell` to use a `RouterView` slot with `<KeepAlive :max="5">`.
- [x] Add metadata only to the five listed routes; render the cached component by route name/path while all other routes remain plain `RouterView` components.
- [x] Run the router shape test and verify it passes.

### Task 3: Cached Page Activation

**Files:**
- Create: `frontend/src/lib/useKeepAlivePage.js`
- Modify: `frontend/src/pages/ModelsPage.vue`
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Modify: `frontend/src/pages/OrganizerPage.vue`
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `frontend/src/pages/TasksPage.vue`
- Test: `frontend/src/lib/pageRefreshShape.test.mjs`

**Produces:** `useKeepAlivePage({ onActivate, onDeactivate })`, which exposes a reactive active predicate and guarantees deactivation cleanup. Each cached page uses it to abort its resource, clear page-specific timers/observers/controllers, retain the current projection, then reattach handlers and call the existing light loader with a silent refresh on reactivation.

- [x] Write failing shape tests requiring every cached page to use the shared lifecycle helper, cancel its resource on deactivation, and pass its active predicate to `createPageRefreshController` where applicable.
- [x] Implement the lifecycle helper with one initial activation marker, cleanup on `onDeactivated` and `onBeforeUnmount`, and no duplicate cleanup.
- [x] Convert each page's setup/teardown code into idempotent start/stop functions. Do not reset data, filters, page number, or status on activation.
- [x] Run focused frontend tests and verify the page shape tests pass.

### Task 4: Release Verification

**Files:**
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_release_contract.py`

**Produces:** `v0.12.0` release metadata documenting page lifecycle performance behavior.

- [x] Update version metadata and move the fourth visible README release into the collapsed history section.
- [x] Run `python scripts/check_release_version.py --tag v0.12.0`.
- [x] Run the release contract, frontend test suite, frontend production build, and `git diff --check`.
- [x] Commit only the implementation, tests, release files, and this plan; leave unrelated video output untracked.
