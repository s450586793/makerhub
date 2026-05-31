# Browser Verification Drag Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve MakerHub browser verification so manual slider verification can be pressed, dragged, and released accurately from the MakerHub popup.

**Architecture:** Keep the backend command contract unchanged and improve the frontend input layer. Extract coordinate and drag gesture logic into a pure JS helper with `node:test` coverage, then wire `BrowserVerificationPage.vue` to Pointer Events, dynamic screenshot aspect ratio, drag click suppression, and drag-aware screenshot refresh.

**Tech Stack:** Vue 3 `<script setup>`, existing `node:test` frontend tests, existing FastAPI browser-verification API, existing Python backend tests.

---

## File Structure

- Create: `frontend/src/lib/browserVerificationInput.js`
  - Pure helpers for coordinate scaling, viewport aspect ratio, pointer gesture state, drag thresholds, and movement throttle decisions.
- Create: `frontend/src/lib/browserVerificationInput.test.mjs`
  - `node:test` coverage for helper behavior without adding a Vue test framework.
- Modify: `frontend/src/pages/BrowserVerificationPage.vue`
  - Replace mouse-only interaction handlers with Pointer Events.
  - Keep wheel, keyboard, paste, and simple click support.
  - Use helper functions for coordinates, aspect ratio, drag state, and throttling.
  - Slow screenshot refresh while dragging and refresh once after release.
- Modify: `frontend/src/style.css`
  - Use CSS variable or inline-driven `aspect-ratio` fallback for the verification frame.
  - Add `touch-action: none` on the interactive frame so touch drag can map to remote pointer commands.
- Test: `tests/test_browser_verification.py`
  - Keep existing backend cropped-offset tests unchanged.
- Do not modify `VERSION` or README unless the user explicitly asks to push in that turn.

## Task 1: Pure Input Mapping And Drag Helper

**Files:**
- Create: `frontend/src/lib/browserVerificationInput.js`
- Create: `frontend/src/lib/browserVerificationInput.test.mjs`

- [ ] **Step 1: Write failing helper tests**

Create `frontend/src/lib/browserVerificationInput.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createDragState,
  frameAspectRatio,
  isDragClickSuppressed,
  mapFramePointToViewport,
  notePointerMove,
  shouldSendPointerMove,
} from "./browserVerificationInput.js";

test("maps rendered frame coordinates into backend viewport coordinates", () => {
  const rect = { left: 10, top: 20, width: 250, height: 125 };
  const viewport = { width: 500, height: 250 };

  assert.deepEqual(
    mapFramePointToViewport({ clientX: 135, clientY: 82 }, rect, viewport),
    { x: 250, y: 124 },
  );
});

test("coordinate mapping clamps negative positions to zero", () => {
  const rect = { left: 100, top: 100, width: 200, height: 100 };
  const viewport = { width: 400, height: 200 };

  assert.deepEqual(
    mapFramePointToViewport({ clientX: 90, clientY: 80 }, rect, viewport),
    { x: 0, y: 0 },
  );
});

test("frame aspect ratio follows valid viewport dimensions", () => {
  assert.equal(frameAspectRatio({ width: 376, height: 148 }), "376 / 148");
  assert.equal(frameAspectRatio({ width: 0, height: 148 }), "520 / 640");
  assert.equal(frameAspectRatio(null), "520 / 640");
});

test("drag state marks movement past threshold as dragged", () => {
  const state = createDragState({ pointerId: 7, x: 10, y: 10, now: 1000 });

  notePointerMove(state, { pointerId: 7, x: 12, y: 12, now: 1010 });
  assert.equal(state.dragged, false);

  notePointerMove(state, { pointerId: 7, x: 20, y: 10, now: 1020 });
  assert.equal(state.dragged, true);
  assert.equal(isDragClickSuppressed(state), true);
});

test("drag move throttle is denser than hover throttle", () => {
  const state = createDragState({ pointerId: 1, x: 0, y: 0, now: 1000 });

  assert.equal(shouldSendPointerMove({ state, pointerId: 1, now: 1030, dragging: true }), false);
  assert.equal(shouldSendPointerMove({ state, pointerId: 1, now: 1050, dragging: true }), true);

  state.lastSentAt = 1000;
  assert.equal(shouldSendPointerMove({ state, pointerId: 1, now: 1100, dragging: false }), false);
  assert.equal(shouldSendPointerMove({ state, pointerId: 1, now: 1180, dragging: false }), true);
});

test("move throttle ignores unrelated pointer ids", () => {
  const state = createDragState({ pointerId: 1, x: 0, y: 0, now: 1000 });

  assert.equal(shouldSendPointerMove({ state, pointerId: 2, now: 2000, dragging: true }), false);
});
```

- [ ] **Step 2: Run helper tests to verify they fail**

Run:

```bash
node --test frontend/src/lib/browserVerificationInput.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND` because `browserVerificationInput.js` does not exist.

- [ ] **Step 3: Implement the pure helper**

Create `frontend/src/lib/browserVerificationInput.js`:

```js
export const DEFAULT_FRAME_ASPECT_RATIO = "520 / 640";
export const DRAG_MOVE_INTERVAL_MS = 50;
export const HOVER_MOVE_INTERVAL_MS = 180;
export const DRAG_THRESHOLD_PX = 4;

function numberOrFallback(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function mapFramePointToViewport(event, rect, viewport = {}) {
  const width = numberOrFallback(rect?.width, 0);
  const height = numberOrFallback(rect?.height, 0);
  if (!rect || !width || !height) {
    return { x: 0, y: 0 };
  }
  const viewportWidth = numberOrFallback(viewport?.width, 1365);
  const viewportHeight = numberOrFallback(viewport?.height, 768);
  const scaleX = viewportWidth / width;
  const scaleY = viewportHeight / height;
  return {
    x: Math.max(0, Math.round((Number(event?.clientX || 0) - Number(rect.left || 0)) * scaleX)),
    y: Math.max(0, Math.round((Number(event?.clientY || 0) - Number(rect.top || 0)) * scaleY)),
  };
}

export function frameAspectRatio(viewport = {}) {
  const width = Number(viewport?.width || 0);
  const height = Number(viewport?.height || 0);
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return DEFAULT_FRAME_ASPECT_RATIO;
  }
  return `${Math.round(width)} / ${Math.round(height)}`;
}

export function createDragState({ pointerId, x, y, now = Date.now() }) {
  return {
    pointerId,
    startX: Number(x || 0),
    startY: Number(y || 0),
    lastX: Number(x || 0),
    lastY: Number(y || 0),
    lastSentAt: Number(now || 0),
    dragged: false,
  };
}

export function notePointerMove(state, { pointerId, x, y, now = Date.now() }) {
  if (!state || state.pointerId !== pointerId) {
    return state;
  }
  const nextX = Number(x || 0);
  const nextY = Number(y || 0);
  const dx = nextX - state.startX;
  const dy = nextY - state.startY;
  state.lastX = nextX;
  state.lastY = nextY;
  if (Math.sqrt(dx * dx + dy * dy) >= DRAG_THRESHOLD_PX) {
    state.dragged = true;
  }
  state.lastMoveAt = Number(now || 0);
  return state;
}

export function shouldSendPointerMove({ state, pointerId, now = Date.now(), dragging = false }) {
  if (!state || state.pointerId !== pointerId) {
    return false;
  }
  const interval = dragging ? DRAG_MOVE_INTERVAL_MS : HOVER_MOVE_INTERVAL_MS;
  return Number(now || 0) - Number(state.lastSentAt || 0) >= interval;
}

export function markPointerMoveSent(state, now = Date.now()) {
  if (state) {
    state.lastSentAt = Number(now || 0);
  }
  return state;
}

export function isDragClickSuppressed(state) {
  return Boolean(state?.dragged);
}
```

- [ ] **Step 4: Run helper tests to verify they pass**

Run:

```bash
node --test frontend/src/lib/browserVerificationInput.test.mjs
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit helper**

Run:

```bash
git add frontend/src/lib/browserVerificationInput.js frontend/src/lib/browserVerificationInput.test.mjs
git commit -m "Add browser verification input helpers"
```

## Task 2: Wire Pointer Events Into Browser Verification Page

**Files:**
- Modify: `frontend/src/pages/BrowserVerificationPage.vue`
- Modify: `frontend/src/lib/routerShape.test.mjs`

- [ ] **Step 1: Write failing source-shape tests for Pointer Events**

Append this test to `frontend/src/lib/routerShape.test.mjs`:

```js
test("browser verification page uses pointer events for drag mapping", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");

  assert.match(source, /@pointerdown\\.prevent="handlePointerDown"/);
  assert.match(source, /@pointermove\\.prevent="handlePointerMove"/);
  assert.match(source, /@pointerup\\.prevent="handlePointerUp"/);
  assert.match(source, /@pointercancel\\.prevent="handlePointerCancel"/);
  assert.match(source, /setPointerCapture/);
  assert.match(source, /releasePointerCapture/);
  assert.match(source, /suppressNextClick/);
  assert.doesNotMatch(source, /@mousedown\\.prevent="sendPointerCommand\\('mousedown'/);
  assert.doesNotMatch(source, /@mousemove="handleMouseMove"/);
  assert.doesNotMatch(source, /let mouseMoveSentAt = 0/);
});
```

- [ ] **Step 2: Run source-shape tests to verify failure**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs
```

Expected: FAIL because `BrowserVerificationPage.vue` still uses mouse events.

- [ ] **Step 3: Update the template events and dynamic frame style**

In `frontend/src/pages/BrowserVerificationPage.vue`, replace the frame event attributes with:

```vue
          :style="viewerFrameStyle"
          @click="handleClick"
          @pointerdown.prevent="handlePointerDown"
          @pointermove.prevent="handlePointerMove"
          @pointerup.prevent="handlePointerUp"
          @pointercancel.prevent="handlePointerCancel"
          @lostpointercapture="handlePointerCancel"
          @wheel.prevent="sendWheelCommand"
          @keydown.prevent="sendKeyCommand"
          @paste.prevent="sendPasteCommand"
```

Remove the old `@mousemove`, `@mousedown`, and `@mouseup` bindings.

- [ ] **Step 4: Import helpers and replace mouse-only state**

In `BrowserVerificationPage.vue`, add this import below the existing imports:

```js
import {
  createDragState,
  frameAspectRatio,
  isDragClickSuppressed,
  mapFramePointToViewport,
  markPointerMoveSent,
  notePointerMove,
  shouldSendPointerMove,
} from "../lib/browserVerificationInput";
```

Replace:

```js
let mouseMoveSentAt = 0;
```

with:

```js
let activePointerState = null;
let suppressNextClick = false;
```

Add this computed value after `visibleMessageText`:

```js
const viewerFrameStyle = computed(() => ({
  "--browser-verification-aspect-ratio": frameAspectRatio(session.value?.viewport),
}));
```

- [ ] **Step 5: Replace coordinate helper and pointer command handlers**

Replace `commandCoordinates()` with:

```js
function commandCoordinates(event) {
  const rect = viewerRef.value?.getBoundingClientRect();
  return mapFramePointToViewport(event, rect, session.value?.viewport || {});
}
```

Replace `sendPointerCommand()` and `handleMouseMove()` with:

```js
function sendPointerCommand(type, event) {
  viewerRef.value?.focus();
  void sendInput({ type, ...commandCoordinates(event) });
}

function handleClick(event) {
  if (suppressNextClick) {
    suppressNextClick = false;
    return;
  }
  sendPointerCommand("click", event);
}

function handlePointerDown(event) {
  viewerRef.value?.focus();
  const coordinates = commandCoordinates(event);
  activePointerState = createDragState({
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    now: Date.now(),
  });
  suppressNextClick = false;
  try {
    event.currentTarget?.setPointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture is best effort.
  }
  void sendInput({ type: "mousedown", ...coordinates });
}

function handlePointerMove(event) {
  if (!activePointerState) {
    return;
  }
  const now = Date.now();
  notePointerMove(activePointerState, {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    now,
  });
  if (!shouldSendPointerMove({ state: activePointerState, pointerId: event.pointerId, now, dragging: true })) {
    return;
  }
  markPointerMoveSent(activePointerState, now);
  void sendInput({ type: "mousemove", ...commandCoordinates(event) });
}

function finishPointerDrag(event, cancelled = false) {
  if (!activePointerState || activePointerState.pointerId !== event.pointerId) {
    return;
  }
  const state = activePointerState;
  if (!cancelled) {
    void sendInput({ type: "mousemove", ...commandCoordinates(event) });
  }
  void sendInput({ type: "mouseup", ...commandCoordinates(event) });
  try {
    event.currentTarget?.releasePointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture release is best effort.
  }
  suppressNextClick = isDragClickSuppressed(state);
  activePointerState = null;
  scheduleScreenshotRefresh(80);
}

function handlePointerUp(event) {
  finishPointerDrag(event, false);
}

function handlePointerCancel(event) {
  finishPointerDrag(event, true);
}
```

Keep `sendWheelCommand`, `sendKeyCommand`, and `sendPasteCommand` unchanged.

- [ ] **Step 6: Make screenshot refresh drag-aware**

At the top of `scheduleScreenshotRefresh(delay = 1200)`, after the `isFinished` guard, add:

```js
  if (activePointerState && delay < 2400) {
    delay = 2400;
  }
```

This slows normal polling while dragging. `finishPointerDrag()` schedules a fast refresh after release.

- [ ] **Step 7: Run source-shape tests**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs
```

Expected: PASS.

- [ ] **Step 8: Run helper and existing frontend tests**

Run:

```bash
node --test frontend/src/lib/browserVerificationInput.test.mjs frontend/src/lib/routerShape.test.mjs frontend/src/lib/browserVerificationWindow.test.mjs
```

Expected: PASS.

- [ ] **Step 9: Commit Pointer Events wiring**

Run:

```bash
git add frontend/src/pages/BrowserVerificationPage.vue frontend/src/lib/routerShape.test.mjs
git commit -m "Use pointer events for browser verification drag"
```

## Task 3: Dynamic Frame Ratio And Drag Styling

**Files:**
- Modify: `frontend/src/style.css`
- Modify: `frontend/src/lib/routerShape.test.mjs`

- [ ] **Step 1: Write failing CSS source-shape test**

Append this test to `frontend/src/lib/routerShape.test.mjs`:

```js
test("browser verification frame uses dynamic aspect ratio and disables touch panning", () => {
  const source = readFileSync(new URL("../style.css", import.meta.url), "utf8");

  assert.match(source, /aspect-ratio:\s*var\(--browser-verification-aspect-ratio,\s*520 \/ 640\)/);
  assert.match(source, /touch-action:\s*none/);
});
```

- [ ] **Step 2: Run CSS source-shape test to verify failure**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs
```

Expected: FAIL because the CSS still uses fixed `aspect-ratio: 520 / 640` and lacks `touch-action: none`.

- [ ] **Step 3: Update verification frame CSS**

In `frontend/src/style.css`, replace the `.browser-verification-frame` aspect ratio block:

```css
.browser-verification-frame {
  position: relative;
  min-height: 420px;
  aspect-ratio: 520 / 640;
  overflow: hidden;
  border: 0;
  border-radius: 0;
  background: var(--surface-strong);
  outline: none;
}
```

with:

```css
.browser-verification-frame {
  position: relative;
  min-height: 420px;
  aspect-ratio: var(--browser-verification-aspect-ratio, 520 / 640);
  overflow: hidden;
  border: 0;
  border-radius: 0;
  background: var(--surface-strong);
  outline: none;
  touch-action: none;
}
```

- [ ] **Step 4: Run frontend source tests**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs frontend/src/lib/browserVerificationInput.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: Vite build succeeds.

- [ ] **Step 6: Commit styling change**

Run:

```bash
git add frontend/src/style.css frontend/src/lib/routerShape.test.mjs
git commit -m "Match browser verification frame to screenshot ratio"
```

## Task 4: Backend Safety Verification

**Files:**
- No planned source changes.
- Test: `tests/test_browser_verification.py`, `tests/test_browser_verification_api.py`

- [ ] **Step 1: Run backend browser verification tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py tests/test_browser_verification_api.py -q
```

Expected: all tests pass. Current baseline after v0.8.14 is 41 tests.

- [ ] **Step 2: Run frontend tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all frontend `node:test` tests pass.

- [ ] **Step 3: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: build succeeds.

- [ ] **Step 4: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 5: Inspect final scope**

Run:

```bash
git diff --stat origin/main..HEAD
git diff --name-only origin/main..HEAD
```

Expected changed files are limited to:

```text
docs/superpowers/specs/2026-05-31-browser-verification-drag-accuracy-design.md
docs/superpowers/plans/2026-05-31-browser-verification-drag-accuracy.md
frontend/src/lib/browserVerificationInput.js
frontend/src/lib/browserVerificationInput.test.mjs
frontend/src/lib/routerShape.test.mjs
frontend/src/pages/BrowserVerificationPage.vue
frontend/src/style.css
```

If the user has not explicitly said `推送` in the current turn, do not update `VERSION` or README release notes.

## Self-Review Checklist

- Spec coverage:
  - Pointer Events are covered in Task 2.
  - Pointer capture and lost/cancel behavior are covered in Task 2.
  - Dense drag movement and hover throttle are covered in Task 1 and Task 2.
  - Drag click suppression is covered in Task 1 and Task 2.
  - Drag-aware screenshot refresh is covered in Task 2.
  - Dynamic viewport aspect ratio is covered in Task 1 and Task 3.
  - Backend command contract remains unchanged and is verified in Task 4.
  - Version/README release note rule is stated in Task 4.
- Placeholder scan:
  - No unresolved placeholder markers or vague catch-all steps remain.
- Type consistency:
  - Helper exports are `mapFramePointToViewport`, `frameAspectRatio`, `createDragState`, `notePointerMove`, `shouldSendPointerMove`, `markPointerMoveSent`, and `isDragClickSuppressed`.
  - Vue handlers are `handlePointerDown`, `handlePointerMove`, `handlePointerUp`, `handlePointerCancel`, and `handleClick`.
