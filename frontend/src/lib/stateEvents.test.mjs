import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { subscribeStateEvents, subscribeStateRefresh } from "./stateEvents.js";

const originalEventSource = globalThis.EventSource;
const originalWindow = globalThis.window;
const originalDocument = globalThis.document;

class FakeEventSource {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    this.closed = false;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.closed = true;
  }

  emit(type, payload) {
    this.emitRaw(type, JSON.stringify(payload), String(payload.id || ""));
  }

  emitRaw(type, data, lastEventId = "") {
    for (const listener of this.listeners.get(type) || []) {
      listener({
        type,
        data,
        lastEventId,
      });
    }
  }
}

function installFakeBrowser() {
  const timers = new Map();
  let nextTimerId = 1;
  globalThis.EventSource = FakeEventSource;
  globalThis.window = {
    clearTimeout: (id) => timers.delete(id),
    setTimeout: (callback, delay) => {
      const id = nextTimerId++;
      timers.set(id, { callback, delay });
      return id;
    },
  };
  return timers;
}

afterEach(() => {
  globalThis.EventSource = originalEventSource;
  globalThis.window = originalWindow;
  globalThis.document = originalDocument;
  FakeEventSource.instances = [];
});

test("account health events reach subscribers", () => {
  globalThis.EventSource = FakeEventSource;
  globalThis.window = {
    clearTimeout,
    setTimeout,
  };

  const received = [];
  const unsubscribe = subscribeStateEvents((event) => received.push(event), ["account_health"]);
  const source = FakeEventSource.instances[0];

  source.emit("account_health.changed", {
    id: 1,
    type: "account_health.changed",
    scope: "account_health",
  });
  unsubscribe();

  assert.equal(source.url, "/api/events/state?scope=account_health");
  assert.deepEqual(received, [{
    id: 1,
    type: "account_health.changed",
    scope: "account_health",
  }]);
});

test("ready events only notify opted-in subscribers and stop after unsubscribe", () => {
  const timers = installFakeBrowser();
  const received = [];
  const ready = [];
  const unsubscribeReady = subscribeStateEvents((event) => received.push(event), ["account_health"], {
    onReady: (event) => ready.push(event),
  });
  const unsubscribeDefault = subscribeStateEvents((event) => received.push(event), ["archive_queue"]);

  try {
    FakeEventSource.instances.at(-1).emit("ready", { last_event_id: 10 });

    assert.deepEqual(ready, [{ last_event_id: 10, type: "ready" }]);
    assert.deepEqual(received, []);
    assert.equal(timers.size, 0);

    unsubscribeReady();
    FakeEventSource.instances.at(-1).emit("ready", { last_event_id: 11 });

    assert.equal(ready.length, 1);
    assert.deepEqual(received, []);
    FakeEventSource.instances.at(-1).emit("archive.completed", { id: 12, scope: "archive_queue" });
    assert.deepEqual(received, [{ id: 12, scope: "archive_queue", type: "archive.completed" }]);
  } finally {
    unsubscribeReady();
    unsubscribeDefault();
  }
});

test("ready callbacks run again when the state event stream reconnects", () => {
  const timers = installFakeBrowser();
  const received = [];
  const ready = [];
  const unsubscribe = subscribeStateEvents((event) => received.push(event), ["account_health"], {
    onReady: (event) => ready.push(event),
  });

  try {
    const source = FakeEventSource.instances.at(-1);
    source.emit("ready", { last_event_id: 40 });
    source.emit("account_health.changed", { id: 41, scope: "account_health" });
    source.onerror();

    assert.equal(source.closed, true);
    assert.equal(timers.size, 1);
    const [timerId, timer] = [...timers][0];
    assert.equal(timer.delay, 5000);
    timers.delete(timerId);
    timer.callback();

    const reconnected = FakeEventSource.instances.at(-1);
    assert.equal(new URL(reconnected.url, "https://makerhub.test").searchParams.get("last_event_id"), "41");
    assert.equal(ready.length, 1);
    reconnected.emit("ready", { last_event_id: 41 });

    assert.deepEqual(ready, [
      { last_event_id: 40, type: "ready" },
      { last_event_id: 41, type: "ready" },
    ]);
    assert.deepEqual(received, [{ id: 41, scope: "account_health", type: "account_health.changed" }]);
  } finally {
    unsubscribe();
  }
});

test("state refresh forwards ready callbacks without changing scoped debounce", () => {
  const timers = installFakeBrowser();
  const visibilityListeners = new Set();
  globalThis.document = {
    hidden: false,
    addEventListener: (_type, handler) => visibilityListeners.add(handler),
    removeEventListener: (_type, handler) => visibilityListeners.delete(handler),
  };
  const ready = [];
  const refreshed = [];
  const unsubscribe = subscribeStateRefresh(["account_health"], (event) => refreshed.push(event), {
    debounceMs: 20,
    onReady: (event) => ready.push(event),
  });

  try {
    const source = FakeEventSource.instances.at(-1);
    source.emit("ready", { last_event_id: 50 });

    assert.deepEqual(ready, [{ last_event_id: 50, type: "ready" }]);
    assert.deepEqual(refreshed, []);
    assert.equal(timers.size, 0);

    source.emit("state.changed", { id: 51, scope: "archive_queue" });
    assert.equal(timers.size, 0);
    source.emit("account_health.changed", { id: 52, scope: "account_health" });
    assert.equal(timers.size, 1);
    const [timerId, timer] = [...timers][0];
    assert.equal(timer.delay, 20);
    timers.delete(timerId);
    timer.callback();

    assert.deepEqual(refreshed, [{ id: 52, scope: "account_health", type: "account_health.changed" }]);
  } finally {
    unsubscribe();
  }
  assert.equal(visibilityListeners.size, 0);
  assert.equal(timers.size, 0);
});

test("malformed ready data does not notify subscribers or prevent the next ready", (context) => {
  installFakeBrowser();
  const parseErrors = context.mock.method(console, "error", () => {});
  const ready = [];
  const received = [];
  const unsubscribe = subscribeStateEvents((event) => received.push(event), ["account_health"], {
    onReady: (event) => ready.push(event),
  });

  try {
    const source = FakeEventSource.instances.at(-1);
    source.emitRaw("ready", "<html>upstream unavailable</html>");
    source.emitRaw("ready", "null");
    source.emitRaw("ready", "[]");

    assert.deepEqual(ready, []);
    assert.deepEqual(received, []);
    assert.equal(parseErrors.mock.callCount(), 1);

    source.emit("ready", { last_event_id: 60 });
    assert.deepEqual(ready, [{ last_event_id: 60, type: "ready" }]);
    assert.deepEqual(received, []);
  } finally {
    unsubscribe();
  }
});
