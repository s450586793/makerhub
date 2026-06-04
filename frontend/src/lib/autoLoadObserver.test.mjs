import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createAutoLoadObserver,
  loadMoreTriggerElement,
} from "./autoLoadObserver.js";

function nextTurn() {
  return new Promise((resolve) => {
    setImmediate(resolve);
  });
}

test("loadMoreTriggerElement resolves a single load-more element", () => {
  const element = {
    getBoundingClientRect: () => ({ top: 0, bottom: 20 }),
  };

  assert.equal(loadMoreTriggerElement({ value: element }), element);
});

test("loadMoreTriggerElement resolves v-for ref arrays", () => {
  const element = {
    getBoundingClientRect: () => ({ top: 0, bottom: 20 }),
  };

  assert.equal(loadMoreTriggerElement({ value: [null, element] }), element);
});

test("createAutoLoadObserver observes the resolved trigger and loads when it is already visible", async () => {
  const element = {
    getBoundingClientRect: () => ({ top: 100, bottom: 140 }),
  };
  const observed = [];
  let loads = 0;
  class FakeIntersectionObserver {
    constructor(callback, options) {
      this.callback = callback;
      this.options = options;
    }

    observe(target) {
      observed.push(target);
    }

    disconnect() {}
  }
  const win = {
    innerHeight: 600,
    requestAnimationFrame: (callback) => {
      callback();
      return 1;
    },
    IntersectionObserver: FakeIntersectionObserver,
  };

  const observer = createAutoLoadObserver({
    triggerRef: { value: [null, element] },
    canLoad: () => true,
    isLoading: () => false,
    load: () => {
      loads += 1;
    },
    nextTick: () => Promise.resolve(),
    win: () => win,
  });

  observer.ensure();
  await nextTurn();

  assert.deepEqual(observed, [element]);
  assert.equal(loads, 1);
});
