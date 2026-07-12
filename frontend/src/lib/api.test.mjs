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
