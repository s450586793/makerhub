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

test("createPageRefreshScheduler can keep an existing timer while coalescing schedules", () => {
  const timers = createTimerHarness();
  const calls = [];
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => calls.push(reason),
    delayMs: 300,
    resetExistingTimer: false,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("first");
  scheduler.schedule("second");

  assert.deepEqual(timers.cleared, []);
  assert.equal(timers.scheduled.length, 1);
  timers.scheduled[0].fn();
  assert.deepEqual(calls, ["second"]);
});

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

test("createPageRefreshScheduler can refresh immediately and clear pending timers", async () => {
  const timers = createTimerHarness();
  const calls = [];
  const scheduler = createPageRefreshScheduler({
    refresh: (reason) => calls.push(reason),
    delayMs: 300,
    isHidden: () => false,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  });

  scheduler.schedule("state-event");
  await scheduler.refreshNow("visibility-resumed");

  assert.deepEqual(timers.cleared, [1]);
  assert.deepEqual(calls, ["visibility-resumed"]);
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
