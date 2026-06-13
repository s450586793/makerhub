import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { apiRequest } from "./api.js";

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.window = originalWindow;
});

test("apiRequest rejects successful API responses that contain an HTML page", async () => {
  globalThis.fetch = async () => new Response("<!doctype html><html><body>index</body></html>", {
    status: 200,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });

  await assert.rejects(
    apiRequest("/api/subscriptions"),
    /接口返回了网页页面/,
  );
});

test("apiRequest still accepts successful JSON API responses", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

  assert.deepEqual(await apiRequest("/api/subscriptions"), { ok: true });
});
