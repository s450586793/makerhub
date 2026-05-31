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
