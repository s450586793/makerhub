import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { apiRequest } from "./api.js";
import {
  normalizePagePerformancePayload,
  resetPerformanceStateForTests,
} from "./performance.js";
import * as performanceMetrics from "./performance.js";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;
const originalPerformance = globalThis.performance;

function installClock(initialValue = 0) {
  let value = initialValue;
  Object.defineProperty(globalThis, "performance", {
    configurable: true,
    writable: true,
    value: { now: () => value },
  });
  return {
    set(nextValue) {
      value = nextValue;
    },
  };
}

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
  Object.defineProperty(globalThis, "performance", {
    configurable: true,
    writable: true,
    value: originalPerformance,
  });
  resetPerformanceStateForTests();
});

test("apiRequest rejects successful API responses that contain an HTML page", async () => {
  globalThis.fetch = async () => new Response("<!doctype html><html><body>index</body></html>", {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });

  await assert.rejects(
    apiRequest("/api/subscriptions"),
    /\/api\/subscriptions.*HTTP 200.*接口返回了网页页面/,
  );
});

test("apiRequest includes API path and status when an error detail contains HTML", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    detail: "<html><body>proxy login</body></html>",
  }), {
    status: 502,
    headers: { "Content-Type": "application/json" },
  });

  await assert.rejects(
    apiRequest("/api/config/online-accounts/login"),
    /\/api\/config\/online-accounts\/login.*HTTP 502.*接口返回了网页页面/,
  );
});

test("apiRequest classifies an HTML 401 response before redirecting to login", async () => {
  const redirects = [];
  globalThis.window = {
    location: {
      pathname: "/settings",
      search: "",
      hash: "",
      assign: (target) => redirects.push(target),
    },
  };
  globalThis.fetch = async () => new Response("<!doctype html><html><body>proxy login</body></html>", {
    status: 401,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });

  await assert.rejects(
    apiRequest("/api/config/online-accounts/login", { method: "POST" }),
    /\/api\/config\/online-accounts\/login.*HTTP 401.*接口返回了网页页面/,
  );
  assert.deepEqual(redirects, []);
});

test("apiRequest includes API path and status for JSON errors", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "upstream timeout" }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });

  await assert.rejects(
    apiRequest("/api/config/online-accounts/login", { method: "POST", redirectOn401: false }),
    /\/api\/config\/online-accounts\/login.*HTTP 503.*upstream timeout/,
  );
});

test("apiRequest still accepts successful JSON API responses", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

  assert.deepEqual(await apiRequest("/api/subscriptions"), { ok: true });
});

test("apiRequest forwards an AbortSignal to fetch", async () => {
  const controller = new AbortController();
  let requestOptions;
  globalThis.fetch = async (_path, options) => {
    requestOptions = options;
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await apiRequest("/api/tasks/light", { signal: controller.signal });

  assert.equal(requestOptions.signal, controller.signal);
});

test("apiRequest records TTFB at response headers and includes JSON parsing in total time", async () => {
  const clock = installClock();
  resetPerformanceStateForTests();
  globalThis.fetch = async () => {
    clock.set(40);
    return {
      status: 200,
      ok: true,
      headers: new Headers({
        "Content-Type": "application/json",
        "Content-Length": "512",
      }),
      json: async () => {
        clock.set(115);
        return { ok: true };
      },
    };
  };

  await apiRequest("/api/models?token=secret");

  const recordedMetrics = typeof performanceMetrics.getApiPerformanceMetricsForTests === "function"
    ? performanceMetrics.getApiPerformanceMetricsForTests()
    : [];
  assert.deepEqual(recordedMetrics, [{
    ttfb_ms: 40,
    body_parse_ms: 75,
    total_ms: 115,
    response_bytes: 512,
    status: 200,
  }]);
  const pagePayload = normalizePagePerformancePayload({
    page: "models",
    route: "/models",
    durationMs: 1300,
  });
  assert.equal(pagePayload.max_ttfb_ms, 40);
  assert.equal(pagePayload.max_parse_ms, 75);
  assert.equal(pagePayload.max_total_ms, 115);
  assert.equal(pagePayload.max_api_duration_ms, 115);
});

test("apiRequest records total time and status when JSON parsing fails", async () => {
  const clock = installClock();
  resetPerformanceStateForTests();
  globalThis.fetch = async () => {
    clock.set(20);
    return {
      status: 502,
      ok: false,
      headers: new Headers({ "Content-Type": "application/json" }),
      json: async () => {
        clock.set(85);
        throw new SyntaxError("invalid JSON");
      },
    };
  };

  await assert.rejects(
    apiRequest("/api/subscriptions", { redirectOn401: false }),
    /invalid JSON/,
  );

  const recordedMetrics = typeof performanceMetrics.getApiPerformanceMetricsForTests === "function"
    ? performanceMetrics.getApiPerformanceMetricsForTests()
    : [];
  assert.deepEqual(recordedMetrics, [{
    ttfb_ms: 20,
    body_parse_ms: 65,
    total_ms: 85,
    response_bytes: 0,
    status: 502,
  }]);
});
