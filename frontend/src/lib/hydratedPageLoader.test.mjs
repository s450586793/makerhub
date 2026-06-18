import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveHydratedLightPhase } from "./hydratedPageLoader.js";

test("hydrated loader defers light payload when full hydration has no stable view", () => {
  const decision = resolveHydratedLightPhase({
    hydrateFull: true,
    incomingItems: [{ key: "source-1" }],
    hasStableView: false,
  });

  assert.deepEqual(decision, {
    renderLight: false,
    hydrateFull: true,
    hydrateImmediately: true,
  });
});

test("hydrated loader renders light payload when a stable view can be preserved", () => {
  const decision = resolveHydratedLightPhase({
    hydrateFull: true,
    incomingItems: [{ key: "source-1" }],
    hasStableView: true,
  });

  assert.deepEqual(decision, {
    renderLight: true,
    hydrateFull: true,
    hydrateImmediately: false,
  });
});

test("hydrated loader renders light payload when full hydration is not requested", () => {
  const decision = resolveHydratedLightPhase({
    hydrateFull: false,
    incomingItems: [{ key: "source-1" }],
    hasStableView: false,
  });

  assert.deepEqual(decision, {
    renderLight: true,
    hydrateFull: false,
    hydrateImmediately: false,
  });
});

test("hydrated loader does not defer empty light payloads", () => {
  const decision = resolveHydratedLightPhase({
    hydrateFull: true,
    incomingItems: [],
    hasStableView: false,
  });

  assert.deepEqual(decision, {
    renderLight: true,
    hydrateFull: true,
    hydrateImmediately: false,
  });
});
