import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { subscribeStateEvents } from "./stateEvents.js";

const originalEventSource = globalThis.EventSource;
const originalWindow = globalThis.window;

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
    for (const listener of this.listeners.get(type) || []) {
      listener({
        type,
        data: JSON.stringify(payload),
        lastEventId: String(payload.id || ""),
      });
    }
  }
}

afterEach(() => {
  globalThis.EventSource = originalEventSource;
  globalThis.window = originalWindow;
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
