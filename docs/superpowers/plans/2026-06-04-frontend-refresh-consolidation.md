# Frontend Refresh Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate MakerHub Vue page refresh scheduling so page-level timers, hidden-tab deferral, and state-event refresh cleanup share one tested implementation.

**Architecture:** Add a pure scheduler helper in `frontend/src/lib/pageRefresh.js` and a thin Vue-facing wrapper in `frontend/src/lib/usePageRefresh.js`. Migrate pages incrementally without changing API routes, response payloads, or visible UI behavior.

**Tech Stack:** Vue 3 Composition API, native `setTimeout`/`clearTimeout`, existing `subscribeStateRefresh`, Node test runner, Vite build.

---

## File Structure

- Create `frontend/src/lib/pageRefresh.js`
  - Pure scheduling helpers. No Vue imports.
  - Owns delayed refresh, in-flight coalescing, hidden-tab deferral, and cleanup.
- Create `frontend/src/lib/pageRefresh.test.mjs`
  - Node tests for scheduler behavior.
- Create `frontend/src/lib/usePageRefresh.js`
  - Vue-facing wrapper around `createPageRefreshScheduler` and `subscribeStateRefresh`.
  - Owns lifecycle cleanup via explicit `dispose()` calls from pages.
- Modify `frontend/src/pages/LogsPage.vue`
  - Replace `refreshTimer` auto-refresh interval with shared scheduler.
  - Keep search debounce separate because it is input debounce, not page-state refresh.
- Modify `frontend/src/pages/TasksPage.vue`
  - Replace local `refreshTimer`, `clearRefreshTimer()`, and `refreshFromStateEvent()` with shared scheduler.
- Modify `frontend/src/pages/RemoteRefreshPage.vue`
  - Replace local timer/in-flight coalescing with shared scheduler configured with active/idle intervals.
- Modify `frontend/src/pages/OrganizerPage.vue`
  - Replace local organize refresh timer with shared scheduler while preserving `refreshLibrary` decision logic.
- Modify `frontend/src/pages/SettingsPage.vue`
  - Use shared scheduler only for system-update state refresh.
  - Keep `accountCodeTimer` unchanged because it is a UI countdown, not page refresh.
- Do not modify backend routes or state-event payloads in this stage.

---

### Task 1: Add Pure Page Refresh Scheduler

**Files:**
- Create: `frontend/src/lib/pageRefresh.js`
- Create: `frontend/src/lib/pageRefresh.test.mjs`

- [ ] **Step 1: Write the failing scheduler tests**

Create `frontend/src/lib/pageRefresh.test.mjs` with:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

import { createPageRefreshScheduler } from "./pageRefresh.js";

function createTimerHarness() {
  const scheduled = [];
  const cleared = [];
  let nextTimerId = 1;
  return {
    scheduled,
    cleared,
    setTimeoutFn: (fn, ms) => {
      const timer = { id: nextTimerId, fn, ms };
      nextTimerId += 1;
      scheduled.push(timer);
      return timer.id;
    },
    clearTimeoutFn: (timerId) => {
      cleared.push(timerId);
    },
  };
}

test("createPageRefreshScheduler schedules a visible refresh", () => {
  const timers = createTimerHarness();
  const calls = [];
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => calls.push(reason),
    delayMs: 250,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("state-event");

  assert.equal(timers.scheduled.length, 1);
  assert.equal(timers.scheduled[0].ms, 250);
  timers.scheduled[0].fn();
  assert.deepEqual(calls, ["state-event"]);
});

test("createPageRefreshScheduler coalesces repeated schedules", () => {
  const timers = createTimerHarness();
  const calls = [];
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => calls.push(reason),
    delayMs: 300,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("first");
  scheduler.schedule("second");

  assert.deepEqual(timers.cleared, [1]);
  assert.equal(timers.scheduled.length, 2);
  timers.scheduled.at(-1).fn();
  assert.deepEqual(calls, ["second"]);
});

test("createPageRefreshScheduler defers while hidden and refreshes when visible", () => {
  const timers = createTimerHarness();
  const calls = [];
  let hidden = true;
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => calls.push(reason),
    delayMs: 100,
    isHidden: () => hidden,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("hidden-event");
  assert.equal(timers.scheduled.length, 0);

  hidden = false;
  scheduler.handleVisible();
  assert.equal(timers.scheduled.length, 1);
  timers.scheduled[0].fn();
  assert.deepEqual(calls, ["visibility-resumed"]);
});

test("createPageRefreshScheduler runs pending refresh after in-flight refresh finishes", async () => {
  const timers = createTimerHarness();
  const calls = [];
  let resolveRefresh;
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => {
      calls.push(reason);
      return new Promise((resolve) => {
        resolveRefresh = resolve;
      });
    },
    delayMs: 0,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("first");
  timers.scheduled[0].fn();
  scheduler.schedule("second");

  assert.deepEqual(calls, ["first"]);
  resolveRefresh();
  await Promise.resolve();
  assert.equal(timers.scheduled.length, 2);
  timers.scheduled[1].fn();
  assert.deepEqual(calls, ["first", "second"]);
});

test("createPageRefreshScheduler clears timer on dispose", () => {
  const timers = createTimerHarness();
  const scheduler = createPageRefreshScheduler({
    refresh: () => {},
    delayMs: 100,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("event");
  scheduler.dispose();

  assert.deepEqual(timers.cleared, [1]);
});
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefresh.test.mjs
```

Expected: fails with `Cannot find module ... pageRefresh.js`.

- [ ] **Step 3: Implement the scheduler**

Create `frontend/src/lib/pageRefresh.js` with:

```js
export function createPageRefreshScheduler({
  refresh,
  delayMs = 250,
  hiddenResumeReason = "visibility-resumed",
  isHidden = () => false,
  setTimeoutFn = globalThis.setTimeout,
  clearTimeoutFn = globalThis.clearTimeout,
} = {}) {
  let timer = 0;
  let disposed = false;
  let inFlight = false;
  let pendingReason = "";
  let pendingWhenVisible = false;

  function clearTimer() {
    if (timer) {
      clearTimeoutFn(timer);
      timer = 0;
    }
  }

  async function run(reason) {
    if (disposed || typeof refresh !== "function") {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      pendingReason = reason || pendingReason;
      return;
    }
    if (inFlight) {
      pendingReason = reason || pendingReason;
      return;
    }
    inFlight = true;
    try {
      await refresh(reason);
    } finally {
      inFlight = false;
      const nextReason = pendingReason;
      pendingReason = "";
      if (nextReason && !disposed) {
        schedule(nextReason);
      }
    }
  }

  function schedule(reason = "scheduled") {
    if (disposed) {
      return;
    }
    if (isHidden()) {
      pendingWhenVisible = true;
      pendingReason = reason || pendingReason;
      clearTimer();
      return;
    }
    pendingReason = reason;
    clearTimer();
    timer = setTimeoutFn(() => {
      timer = 0;
      const nextReason = pendingReason || reason;
      pendingReason = "";
      void run(nextReason);
    }, Math.max(Number(delayMs) || 0, 0));
  }

  function handleVisible() {
    if (disposed || isHidden() || !pendingWhenVisible) {
      return;
    }
    pendingWhenVisible = false;
    schedule(hiddenResumeReason);
  }

  function dispose() {
    disposed = true;
    pendingReason = "";
    pendingWhenVisible = false;
    clearTimer();
  }

  return {
    clear: clearTimer,
    dispose,
    handleVisible,
    schedule,
  };
}
```

- [ ] **Step 4: Run the focused test and verify it passes**

Run:

```bash
node --test frontend/src/lib/pageRefresh.test.mjs
```

Expected: all tests pass.

- [ ] **Step 5: Run existing frontend lib tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add frontend/src/lib/pageRefresh.js frontend/src/lib/pageRefresh.test.mjs
git commit -m "test: add shared page refresh scheduler"
```

---

### Task 2: Add Vue-Facing Refresh Wrapper

**Files:**
- Create: `frontend/src/lib/usePageRefresh.js`
- Modify: `frontend/src/lib/pageRefresh.test.mjs`

- [ ] **Step 1: Add wrapper shape coverage**

Append this test to `frontend/src/lib/pageRefresh.test.mjs`:

```js
test("createPageRefreshScheduler exposes cleanup methods used by Vue wrapper", () => {
  const scheduler = createPageRefreshScheduler({
    refresh: () => {},
    isHidden: () => false,
  });

  assert.equal(typeof scheduler.schedule, "function");
  assert.equal(typeof scheduler.handleVisible, "function");
  assert.equal(typeof scheduler.dispose, "function");
  assert.equal(typeof scheduler.clear, "function");
});
```

- [ ] **Step 2: Run test to verify current scheduler contract passes**

Run:

```bash
node --test frontend/src/lib/pageRefresh.test.mjs
```

Expected: pass.

- [ ] **Step 3: Create `usePageRefresh.js`**

Create `frontend/src/lib/usePageRefresh.js` with:

```js
import { createPageRefreshScheduler } from "./pageRefresh";
import { subscribeStateRefresh } from "./stateEvents";

export function createPageRefreshController({
  scopes = [],
  types = [],
  refresh,
  delayMs = 250,
  debounceMs,
  subscribe = subscribeStateRefresh,
  isHidden = () => typeof document !== "undefined" && document.hidden,
  addVisibilityListener = (handler) => {
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", handler);
    }
  },
  removeVisibilityListener = (handler) => {
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", handler);
    }
  },
} = {}) {
  const scheduler = createPageRefreshScheduler({
    refresh,
    delayMs,
    isHidden,
  });
  const unsubscribe = typeof subscribe === "function" && scopes.length
    ? subscribe(scopes, (event) => scheduler.schedule(event?.type || "state-event"), {
        types,
        debounceMs: Number.isFinite(Number(debounceMs)) ? Number(debounceMs) : delayMs,
      })
    : null;
  const handleVisibilityChange = () => scheduler.handleVisible();

  addVisibilityListener(handleVisibilityChange);

  return {
    clear: () => scheduler.clear(),
    dispose: () => {
      scheduler.dispose();
      if (typeof unsubscribe === "function") {
        unsubscribe();
      }
      removeVisibilityListener(handleVisibilityChange);
    },
    schedule: (reason) => scheduler.schedule(reason),
  };
}
```

- [ ] **Step 4: Run frontend lib tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all tests pass.

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: Vite build succeeds.

- [ ] **Step 6: Commit Task 2**

```bash
git add frontend/src/lib/usePageRefresh.js frontend/src/lib/pageRefresh.test.mjs
git commit -m "feat: add page refresh controller"
```

---

### Task 3: Migrate LogsPage Auto Refresh

**Files:**
- Modify: `frontend/src/pages/LogsPage.vue`
- Test: `frontend/src/lib/pageRefresh.test.mjs`

- [ ] **Step 1: Add a shape assertion for LogsPage using the controller**

Create or extend `frontend/src/lib/pageRefreshShape.test.mjs` with:

```js
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const logsPageSource = readFileSync(new URL("../pages/LogsPage.vue", import.meta.url), "utf8");

test("LogsPage uses shared page refresh controller for auto tracking", () => {
  assert.match(logsPageSource, /createPageRefreshController/);
  assert.doesNotMatch(logsPageSource, /window\.setInterval\(load,\s*5000\)/);
  assert.match(logsPageSource, /logRefreshController/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: fails because `LogsPage.vue` still uses `setInterval(load, 5000)`.

- [ ] **Step 3: Update `LogsPage.vue` imports**

Change:

```js
import { apiRequest } from "../lib/api";
```

to:

```js
import { apiRequest } from "../lib/api";
import { createPageRefreshController } from "../lib/usePageRefresh";
```

- [ ] **Step 4: Replace auto-refresh state variables**

Replace:

```js
let refreshTimer = null;
let searchTimer = null;
let applyingRouteQuery = false;
```

with:

```js
let logRefreshController = null;
let searchTimer = null;
let applyingRouteQuery = false;
```

- [ ] **Step 5: Replace `syncAutoRefresh()`**

Replace the full `syncAutoRefresh()` function with:

```js
function stopLogRefreshController() {
  if (logRefreshController) {
    logRefreshController.dispose();
    logRefreshController = null;
  }
}

function syncAutoRefresh() {
  stopLogRefreshController();
  if (!autoRefresh.value) {
    return;
  }
  logRefreshController = createPageRefreshController({
    refresh: () => load(),
    delayMs: 5000,
    scopes: ["business_logs"],
  });
  logRefreshController.schedule("auto-refresh-started");
}
```

- [ ] **Step 6: Update cleanup**

In `onBeforeUnmount`, replace:

```js
if (refreshTimer) {
  window.clearInterval(refreshTimer);
}
```

with:

```js
stopLogRefreshController();
```

- [ ] **Step 7: Run shape and frontend tests**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/*.test.mjs
```

Expected: all tests pass.

- [ ] **Step 8: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: Vite build succeeds.

- [ ] **Step 9: Commit Task 3**

```bash
git add frontend/src/pages/LogsPage.vue frontend/src/lib/pageRefreshShape.test.mjs
git commit -m "refactor: use shared refresh controller in logs page"
```

---

### Task 4: Migrate TasksPage State Refresh

**Files:**
- Modify: `frontend/src/pages/TasksPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Add TasksPage shape test**

Append to `frontend/src/lib/pageRefreshShape.test.mjs`:

```js
const tasksPageSource = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");

test("TasksPage uses shared page refresh controller for state events", () => {
  assert.match(tasksPageSource, /createPageRefreshController/);
  assert.doesNotMatch(tasksPageSource, /function refreshFromStateEvent/);
  assert.match(tasksPageSource, /tasksRefreshController/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: fails because `TasksPage.vue` still defines `refreshFromStateEvent`.

- [ ] **Step 3: Update imports**

Add:

```js
import { createPageRefreshController } from "../lib/usePageRefresh";
```

Remove the direct import of `subscribeStateRefresh` from `TasksPage.vue`.

- [ ] **Step 4: Replace refresh controller state**

Replace the local `refreshTimer` and `unsubscribeStateRefresh` declarations with:

```js
let tasksRefreshController = null;
```

- [ ] **Step 5: Replace local refresh functions**

Remove `clearRefreshTimer()` and `refreshFromStateEvent()`.

Add:

```js
function stopTasksRefreshController() {
  if (tasksRefreshController) {
    tasksRefreshController.dispose();
    tasksRefreshController = null;
  }
}
```

- [ ] **Step 6: Update mounted setup**

Replace the `subscribeStateRefresh` setup with:

```js
tasksRefreshController = createPageRefreshController({
  scopes: ["archive_queue", "missing_3mf", "organize_tasks", "archive_repair_status", "archive_profile_backfill_status"],
  refresh: () => load(),
  delayMs: 250,
});
```

- [ ] **Step 7: Update visibility handling**

Remove the `handleVisibilityChange()` function from `TasksPage.vue`.

In `onMounted`, remove:

```js
document.addEventListener("visibilitychange", handleVisibilityChange);
```

In `onBeforeUnmount`, remove:

```js
document.removeEventListener("visibilitychange", handleVisibilityChange);
```

The shared `createPageRefreshController` owns hidden-tab deferral and visible-tab resume for this page.

- [ ] **Step 8: Update cleanup**

In `onBeforeUnmount`, replace timer and unsubscribe cleanup with:

```js
stopTasksRefreshController();
```

- [ ] **Step 9: Run tests and build**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: tests and build pass.

- [ ] **Step 10: Commit Task 4**

```bash
git add frontend/src/pages/TasksPage.vue frontend/src/lib/pageRefreshShape.test.mjs
git commit -m "refactor: use shared refresh controller in tasks page"
```

---

### Task 5: Migrate RemoteRefreshPage Throttled Refresh

**Files:**
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`
- Modify: `frontend/src/lib/pageRefresh.js`
- Modify: `frontend/src/lib/pageRefresh.test.mjs`

- [ ] **Step 1: Add test for dynamic delay support**

Append to `frontend/src/lib/pageRefresh.test.mjs`:

```js
test("createPageRefreshScheduler supports dynamic delay functions", () => {
  const timers = createTimerHarness();
  let active = true;
  const scheduler = createPageRefreshScheduler({
    refresh: () => {},
    delayMs: () => (active ? 1200 : 300),
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("active");
  active = false;
  scheduler.schedule("idle");

  assert.equal(timers.scheduled[0].ms, 1200);
  assert.equal(timers.scheduled[1].ms, 300);
});
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefresh.test.mjs
```

Expected: fails because `delayMs` currently expects a number.

- [ ] **Step 3: Add dynamic delay support**

In `frontend/src/lib/pageRefresh.js`, add:

```js
function currentDelayMs() {
  const rawDelay = typeof delayMs === "function" ? delayMs() : delayMs;
  return Math.max(Number(rawDelay) || 0, 0);
}
```

Then change:

```js
}, Math.max(Number(delayMs) || 0, 0));
```

to:

```js
}, currentDelayMs());
```

- [ ] **Step 4: Add RemoteRefreshPage shape test**

Append to `frontend/src/lib/pageRefreshShape.test.mjs`:

```js
const remoteRefreshPageSource = readFileSync(new URL("../pages/RemoteRefreshPage.vue", import.meta.url), "utf8");

test("RemoteRefreshPage uses shared page refresh controller for throttled refresh", () => {
  assert.match(remoteRefreshPageSource, /createPageRefreshController/);
  assert.doesNotMatch(remoteRefreshPageSource, /function scheduleRefresh/);
  assert.match(remoteRefreshPageSource, /remoteRefreshController/);
});
```

- [ ] **Step 5: Update RemoteRefreshPage imports and state**

Add:

```js
import { createPageRefreshController } from "../lib/usePageRefresh";
```

Replace local `refreshTimer`, `refreshPending`, and refresh scheduling state with:

```js
let remoteRefreshController = null;
```

Remove `refreshInFlight` and `refreshPending` from `RemoteRefreshPage.vue`; `createPageRefreshScheduler` owns in-flight coalescing after Task 1.

- [ ] **Step 6: Replace page-local scheduling**

Remove local `clearRefreshTimer()`, `canRefreshVisibleState()`, `scheduleRefresh()`, and `runScheduledRefresh()`.

Add:

```js
function stopRemoteRefreshController() {
  if (remoteRefreshController) {
    remoteRefreshController.dispose();
    remoteRefreshController = null;
  }
}

function activeRefreshDelayMs() {
  return remoteRefreshState.value?.running
    ? REMOTE_REFRESH_ACTIVE_REFRESH_MS
    : REMOTE_REFRESH_IDLE_REFRESH_MS;
}
```

On mount, create:

```js
remoteRefreshController = createPageRefreshController({
  scopes: ["remote_refresh_state"],
  refresh: () => load(),
  delayMs: activeRefreshDelayMs,
});
```

Replace calls to `scheduleRefresh()` with:

```js
remoteRefreshController?.schedule("remote-refresh-state");
```

- [ ] **Step 7: Cleanup**

In `onBeforeUnmount`, call:

```js
stopRemoteRefreshController();
```

- [ ] **Step 8: Run tests and build**

Run:

```bash
node --test frontend/src/lib/pageRefresh.test.mjs frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: tests and build pass.

- [ ] **Step 9: Commit Task 5**

```bash
git add frontend/src/lib/pageRefresh.js frontend/src/lib/pageRefresh.test.mjs frontend/src/lib/pageRefreshShape.test.mjs frontend/src/pages/RemoteRefreshPage.vue
git commit -m "refactor: use shared refresh controller in remote refresh page"
```

---

### Task 6: Migrate OrganizerPage Task Refresh

**Files:**
- Modify: `frontend/src/pages/OrganizerPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Add OrganizerPage shape test**

Append to `frontend/src/lib/pageRefreshShape.test.mjs`:

```js
const organizerPageSource = readFileSync(new URL("../pages/OrganizerPage.vue", import.meta.url), "utf8");

test("OrganizerPage uses shared page refresh controller for organize task refresh", () => {
  assert.match(organizerPageSource, /createPageRefreshController/);
  assert.doesNotMatch(organizerPageSource, /function syncTaskTimer/);
  assert.match(organizerPageSource, /organizerRefreshController/);
});
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: fails because `OrganizerPage.vue` still defines `syncTaskTimer`.

- [ ] **Step 3: Update imports and state**

Add:

```js
import { createPageRefreshController } from "../lib/usePageRefresh";
```

Replace local refresh timer state with:

```js
let organizerRefreshController = null;
```

- [ ] **Step 4: Replace timer functions**

Remove `clearTaskTimer()` and `syncTaskTimer()`.

Add:

```js
function stopOrganizerRefreshController() {
  if (organizerRefreshController) {
    organizerRefreshController.dispose();
    organizerRefreshController = null;
  }
}

function scheduleOrganizerRefresh(reason = "organizer-state") {
  organizerRefreshController?.schedule(reason);
}
```

- [ ] **Step 5: Create the controller on mount**

Add in `onMounted`:

```js
organizerRefreshController = createPageRefreshController({
  scopes: ["organize_tasks", "source_library", "archive_queue"],
  refresh: () => load({
    silent: true,
    refreshLibrary: !hasActiveOrganizeTasks() || !hasSourceLibraryPayload(),
  }),
  delayMs: 300,
});
```

Replace calls to `syncTaskTimer()` with:

```js
scheduleOrganizerRefresh("organizer-task");
```

- [ ] **Step 6: Cleanup**

In `onBeforeUnmount`, call:

```js
stopOrganizerRefreshController();
```

- [ ] **Step 7: Run tests and build**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: tests and build pass.

- [ ] **Step 8: Commit Task 6**

```bash
git add frontend/src/pages/OrganizerPage.vue frontend/src/lib/pageRefreshShape.test.mjs
git commit -m "refactor: use shared refresh controller in organizer page"
```

---

### Task 7: Migrate SettingsPage System Update Refresh Only

**Files:**
- Modify: `frontend/src/pages/SettingsPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Add SettingsPage shape test**

Append to `frontend/src/lib/pageRefreshShape.test.mjs`:

```js
const settingsPageSource = readFileSync(new URL("../pages/SettingsPage.vue", import.meta.url), "utf8");

test("SettingsPage uses shared page refresh controller for system update state", () => {
  assert.match(settingsPageSource, /createPageRefreshController/);
  assert.match(settingsPageSource, /settingsRefreshController/);
  assert.match(settingsPageSource, /accountCodeTimer/);
});
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: fails because `SettingsPage.vue` does not use `createPageRefreshController`.

- [ ] **Step 3: Update imports and state**

Add:

```js
import { createPageRefreshController } from "../lib/usePageRefresh";
```

Add:

```js
let settingsRefreshController = null;
```

Keep `accountCodeTimer` unchanged.

- [ ] **Step 4: Replace system update state subscription cleanup**

Remove the `unsubscribeStateRefresh` field and any cleanup that calls it from `SettingsPage.vue`.

Add:

```js
function stopSettingsRefreshController() {
  if (settingsRefreshController) {
    settingsRefreshController.dispose();
    settingsRefreshController = null;
  }
}
```

- [ ] **Step 5: Create controller on mount**

Add in `onMounted`:

```js
settingsRefreshController = createPageRefreshController({
  scopes: ["system_update", "cookie_source_sync_state"],
  refresh: () => load(),
  delayMs: 450,
});
```

- [ ] **Step 6: Cleanup**

In `onBeforeUnmount`, call:

```js
stopSettingsRefreshController();
```

Keep:

```js
clearAccountCodeTimer();
```

- [ ] **Step 7: Run tests and build**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/*.test.mjs
npm --prefix frontend run build
```

Expected: tests and build pass.

- [ ] **Step 8: Commit Task 7**

```bash
git add frontend/src/pages/SettingsPage.vue frontend/src/lib/pageRefreshShape.test.mjs
git commit -m "refactor: use shared refresh controller in settings page"
```

---

### Task 8: Final Verification And Documentation

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`

- [ ] **Step 1: Run all frontend tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all tests pass.

- [ ] **Step 2: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: Vite build succeeds.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Bump patch version**

Read the current version:

```bash
cat VERSION
```

If it prints `0.9.4`, update:

- `VERSION` to `0.9.5`
- `frontend/package.json` version to `0.9.5`
- top-level package entry in `frontend/package-lock.json` to `0.9.5`

If it prints a newer version, increment that version's patch number by one and use that exact next patch version in the same three files and in the release notes.

- [ ] **Step 5: Update release notes**

Add a new entry to `CHANGELOG.md`:

```md
## 2026-06-04 · v0.9.5

- 统一日志、任务、源端刷新、本地整理和设置页的前端刷新调度，减少页面级定时器重复逻辑。
- 新增共享页面刷新调度器和回归测试，覆盖隐藏页延迟刷新、重复事件合并和请求中刷新续跑。
- 保留现有页面可见行为和 API 契约，只收敛刷新实现。
```

Add the same entry to the top visible section of `README.md` update records and keep only the latest three visible releases before the collapsed history section.

- [ ] **Step 6: Run final verification**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
npm --prefix frontend run build
git diff --check
```

Expected: tests pass, build succeeds, diff check has no output.

- [ ] **Step 7: Commit release notes**

```bash
git add README.md CHANGELOG.md VERSION frontend/package.json frontend/package-lock.json
git commit -m "chore: release v0.9.5"
```

- [ ] **Step 8: Confirm clean status**

Run:

```bash
git status --short --branch
```

Expected: clean working tree, branch ahead by the task commits.

---

## Self-Review Checklist

- Spec coverage:
  - Frontend refresh consolidation is covered by Tasks 1-7.
  - Tests and build verification are covered in every migration task and final verification.
  - Version bump and release note rule is covered by Task 8.
- Scope:
  - This plan intentionally covers only Stage 1 from the approved simplification spec.
  - API route split, Vue detail/settings decomposition beyond refresh, MakerWorld archiver split, and legacy audit should each get separate plans.
- Placeholder scan:
  - This plan contains no placeholder sections.
  - Task 8 uses `0.9.5` as the expected next version from the current repository state and gives an explicit fallback rule if the execution-time version is newer.
