import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createPagePerformanceTracker,
  normalizePagePerformancePayload,
  recordApiDuration,
  resetPerformanceStateForTests,
} from "./performance.js";

test("recordApiDuration tracks API count, slow count, and max duration", () => {
  resetPerformanceStateForTests();
  recordApiDuration("/api/models", 120);
  recordApiDuration("/api/dashboard", 900);
  const payload = normalizePagePerformancePayload({
    page: "models",
    route: "/models?page=2",
    durationMs: 1400,
  });
  assert.equal(payload.api_count, 2);
  assert.equal(payload.slow_api_count, 1);
  assert.equal(payload.max_api_duration_ms, 900);
});

test("createPagePerformanceTracker reports only slow page loads", async () => {
  resetPerformanceStateForTests();
  const calls = [];
  const tracker = createPagePerformanceTracker({
    page: "models",
    route: () => "/models",
    now: (() => {
      const values = [0, 1400];
      return () => {
        return values.shift() ?? 1400;
      };
    })(),
    request: async (path, options) => calls.push([path, options]),
  });

  await tracker.finish();

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/performance/events");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].redirectOn401, false);
  assert.equal(calls[0][1].body.page, "models");
});
