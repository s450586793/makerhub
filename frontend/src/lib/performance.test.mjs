import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createPagePerformanceTracker,
  normalizePagePerformancePayload,
  recordApiDuration,
  resetPerformanceStateForTests,
} from "./performance.js";
import * as performanceMetrics from "./performance.js";

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

test("createPagePerformanceTracker records final data and explicit enrichment milestones", async () => {
  resetPerformanceStateForTests();
  const calls = [];
  const tracker = createPagePerformanceTracker({
    page: "models",
    route: () => "/models?token=secret",
    now: (() => {
      const values = [0, 1250, 1650, 1800];
      return () => values.shift() ?? 1800;
    })(),
    request: async (path, options) => calls.push([path, options]),
  });

  assert.equal(typeof performanceMetrics.recordApiRequestMetrics, "function");
  performanceMetrics.recordApiRequestMetrics("/api/models?token=secret", {
    ttfbMs: 180,
    bodyParseMs: 40,
    totalMs: 220,
    responseBytes: 1024,
    status: 200,
  });
  assert.equal(typeof tracker.markDataReady, "function");
  assert.equal(typeof tracker.markEnrichmentReady, "function");
  tracker.markDataReady();
  tracker.markEnrichmentReady();
  const payload = await tracker.finish();

  assert.equal(payload.duration_ms, 1250);
  assert.equal(payload.data_ready_ms, 1250);
  assert.equal(payload.enrichment_ready_ms, 1650);
  assert.equal(payload.max_ttfb_ms, 180);
  assert.equal(payload.max_parse_ms, 40);
  assert.equal(payload.max_total_ms, 220);
  assert.equal(calls[0][1].body.route, "/models");
});

test("separate enrichment tracker excludes idle time and emits an independent event", async () => {
  resetPerformanceStateForTests();
  const calls = [];
  const firstLoad = createPagePerformanceTracker({
    page: "tasks",
    now: (() => {
      const values = [0, 1300, 1300];
      return () => values.shift() ?? 1300;
    })(),
    request: async (path, options) => calls.push([path, options]),
  });

  firstLoad.markDataReady();
  await firstLoad.finish();

  const enrichment = createPagePerformanceTracker({
    page: "tasks",
    eventKind: "enrichment",
    now: (() => {
      const values = [12000, 13600, 13600];
      return () => values.shift() ?? 13600;
    })(),
    request: async (path, options) => calls.push([path, options]),
  });

  enrichment.markEnrichmentReady();
  await enrichment.finish();

  assert.equal(calls.length, 2);
  assert.equal(calls.filter(([, options]) => options.body.event_kind === "page").length, 1);
  assert.equal(calls[1][1].body.event_kind, "enrichment");
  assert.equal(calls[1][1].body.duration_ms, 1600);
  assert.equal(calls[1][1].body.data_ready_ms, 0);
  assert.equal(calls[1][1].body.enrichment_ready_ms, 1600);
});
