import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createScopedRefreshScheduler,
  matchesStateEventRules,
  normalizeScopes,
  shouldHandleStateEvent,
} from "./stateRefresh.js";

test("normalizeScopes keeps only non-empty scope strings", () => {
  assert.deepEqual(normalizeScopes("archive_queue"), ["archive_queue"]);
  assert.deepEqual(
    normalizeScopes(["archive_queue", "", null, "missing_3mf"]),
    ["archive_queue", "missing_3mf"],
  );
});

test("shouldHandleStateEvent filters by scope and optional type", () => {
  assert.equal(
    shouldHandleStateEvent(
      { type: "state.changed", scope: "archive_queue" },
      ["archive_queue"],
    ),
    true,
  );
  assert.equal(
    shouldHandleStateEvent(
      { type: "state.changed", payload: { scope: "missing_3mf" } },
      ["missing_3mf"],
    ),
    true,
  );
  assert.equal(
    shouldHandleStateEvent(
      { type: "state.changed", scope: "remote_refresh_state" },
      ["archive_queue"],
    ),
    false,
  );
  assert.equal(
    shouldHandleStateEvent(
      { type: "archive.completed", scope: "archive_queue" },
      ["archive_queue"],
      ["state.changed"],
    ),
    false,
  );
});

test("shouldHandleStateEvent allows wildcard scope", () => {
  assert.equal(
    shouldHandleStateEvent({ type: "state.changed", scope: "anything" }, ["*"]),
    true,
  );
});

test("event rules match scope and type as rule pairs", () => {
  const eventRules = [
    { scopes: ["subscriptions_state", "source_library"], types: ["state.changed", "source_library.changed"] },
    { scopes: ["archive_queue"], types: ["archive.completed", "archive.failed"] },
  ];

  assert.equal(matchesStateEventRules({ type: "state.changed", scope: "subscriptions_state" }, eventRules), true);
  assert.equal(matchesStateEventRules({ type: "archive.completed", scope: "archive_queue" }, eventRules), true);
  assert.equal(matchesStateEventRules({ type: "state.changed", scope: "archive_queue" }, eventRules), false);
  assert.equal(matchesStateEventRules({ type: "archive.completed", scope: "source_library" }, eventRules), false);
});

test("scoped refresh scheduler prefers event rules over broad filters", () => {
  const received = [];
  const scheduled = [];
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue", "source_library"],
    eventRules: [
      { scopes: ["source_library"], types: ["source_library.changed"] },
      { scopes: ["archive_queue"], types: ["archive.completed", "archive.failed"] },
    ],
    debounceMs: 0,
    callback: (event) => received.push(event),
    setTimeoutFn: (fn) => {
      scheduled.push(fn);
      return scheduled.length;
    },
    clearTimeoutFn: () => {},
  });

  scheduler.handleEvent({ type: "state.changed", scope: "archive_queue" });
  scheduler.handleEvent({ type: "archive.completed", scope: "archive_queue" });
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  assert.deepEqual(received, [{ type: "archive.completed", scope: "archive_queue" }]);
});

test("createScopedRefreshScheduler debounces matching events", () => {
  const scheduled = [];
  const cleared = [];
  const received = [];
  let nextTimerId = 1;
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue"],
    debounceMs: 50,
    callback: (event) => received.push(event),
    isHidden: () => false,
    setTimeoutFn: (fn, ms) => {
      const timer = { id: nextTimerId, fn, ms };
      nextTimerId += 1;
      scheduled.push(timer);
      return timer.id;
    },
    clearTimeoutFn: (timerId) => cleared.push(timerId),
  });

  scheduler.handleEvent({ type: "state.changed", scope: "archive_queue", id: 1 });
  scheduler.handleEvent({ type: "state.changed", scope: "archive_queue", id: 2 });

  assert.deepEqual(cleared, [1]);
  assert.equal(scheduled.length, 2);
  scheduled.at(-1).fn();
  assert.deepEqual(received, [{ type: "state.changed", scope: "archive_queue", id: 2 }]);
});

test("createScopedRefreshScheduler defers while hidden and refreshes on visible", () => {
  const scheduled = [];
  const received = [];
  let hidden = true;
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue"],
    debounceMs: 10,
    callback: (event) => received.push(event),
    isHidden: () => hidden,
    setTimeoutFn: (fn) => {
      scheduled.push(fn);
      return scheduled.length;
    },
    clearTimeoutFn: () => {},
  });

  scheduler.handleEvent({ type: "state.changed", scope: "archive_queue" });
  assert.equal(scheduled.length, 0);
  assert.deepEqual(received, []);

  hidden = false;
  scheduler.handleVisibilityChange();
  assert.equal(scheduled.length, 1);
  scheduled[0]();
  assert.deepEqual(received, [{ type: "visibility.resumed", scope: "visibility" }]);
});

test("createScopedRefreshScheduler clears pending timer on dispose", () => {
  const cleared = [];
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue"],
    callback: () => {},
    setTimeoutFn: () => 42,
    clearTimeoutFn: (timerId) => cleared.push(timerId),
  });

  scheduler.handleEvent({ type: "state.changed", scope: "archive_queue" });
  scheduler.dispose();

  assert.deepEqual(cleared, [42]);
});

test("createScopedRefreshScheduler forwards a completion event without an extra debounce delay", () => {
  const scheduled = [];
  const received = [];
  const scheduler = createScopedRefreshScheduler({
    scopes: ["archive_queue"],
    debounceMs: 0,
    callback: (event) => received.push(event),
    isHidden: () => false,
    setTimeoutFn: (fn, ms) => {
      scheduled.push({ fn, ms });
      return scheduled.length;
    },
    clearTimeoutFn: () => {},
  });

  scheduler.handleEvent({ type: "archive.completed", scope: "archive_queue" });

  assert.equal(scheduled.length, 1);
  assert.equal(scheduled[0].ms, 0);
  scheduled[0].fn();
  assert.deepEqual(received, [{ type: "archive.completed", scope: "archive_queue" }]);
});
