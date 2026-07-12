import assert from "node:assert/strict";
import { test } from "node:test";

import { createHydratedResource } from "./useHydratedResource.js";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

test("hydrated resource only commits the latest load response", async () => {
  const requests = [];
  const committed = [];
  const resource = createHydratedResource({
    load: ({ key }) => {
      const request = deferred();
      requests.push({ key, request });
      return request.promise;
    },
    onData: (value, state) => committed.push({ value, revision: state.revision }),
  });

  const first = resource.load({ key: "first" });
  const second = resource.load({ key: "second" });
  requests[1].request.resolve({ key: "second" });
  assert.deepEqual(await second, { key: "second" });
  requests[0].request.resolve({ key: "first" });

  assert.equal(await first, undefined);
  assert.deepEqual(committed, [{ value: { key: "second" }, revision: 2 }]);
  assert.equal(resource.revision, 2);
});

test("hydrated resource cancellation is silent", async () => {
  const errors = [];
  const resource = createHydratedResource({
    load: ({ signal }) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
    }),
    onError: (error) => errors.push(error),
  });

  const pending = resource.load();
  resource.cancel();

  assert.equal(await pending, undefined);
  assert.deepEqual(errors, []);
  assert.equal(resource.revision, 2);
});

test("hydrated resource only enriches on an explicit call", async () => {
  const phases = [];
  const committed = [];
  const resource = createHydratedResource({
    load: async () => {
      phases.push("load");
      return { items: ["light"] };
    },
    enrich: async (current) => {
      phases.push("enrich");
      return { items: [...current.items, "detail"] };
    },
    merge: (current, incoming) => ({ ...current, ...incoming }),
    onData: (value, state) => committed.push({ value, phase: state.phase }),
  });

  await resource.load();
  assert.deepEqual(phases, ["load"]);
  assert.deepEqual(resource.value, { items: ["light"] });

  await resource.enrich();
  assert.deepEqual(phases, ["load", "enrich"]);
  assert.deepEqual(resource.value, { items: ["light", "detail"] });
  assert.deepEqual(committed.map((item) => item.phase), ["load", "enrich"]);
});

test("hydrated resource reads and updates an optional cache", async () => {
  let cached = { items: ["cached"] };
  const committed = [];
  const resource = createHydratedResource({
    cache: {
      get: () => cached,
      set: (value) => {
        cached = value;
      },
    },
    load: async () => ({ items: ["fresh"] }),
    onData: (value, state) => committed.push({ value, phase: state.phase }),
  });

  assert.deepEqual(resource.value, { items: ["cached"] });
  await resource.load();

  assert.deepEqual(cached, { items: ["fresh"] });
  assert.deepEqual(committed, [{ value: { items: ["fresh"] }, phase: "load" }]);
});
