import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { submitLogin } from "./loginFlow.js";

const loginPageSource = readFileSync(new URL("../pages/LoginPage.vue", import.meta.url), "utf8");
const appStateSource = readFileSync(new URL("./appState.js", import.meta.url), "utf8");

test("login success uses SPA navigation without blocking on bootstrap", () => {
  assert.equal(loginPageSource.includes("bootstrapApp"), false);
  assert.equal(loginPageSource.includes("window.location.assign"), false);
  assert.match(loginPageSource, /submitLogin/);
});

test("appState exposes login session hydration for successful login responses", () => {
  assert.match(appStateSource, /export function applyLoginSession/);
  assert.match(appStateSource, /appState\.ready = true/);
  assert.match(appStateSource, /authenticated: true/);
  assert.match(appStateSource, /kind: "session"/);
});

test("submitLogin authenticates, hydrates session, and navigates with router.replace", async () => {
  const calls = [];
  const replaced = [];
  let resolveRefresh;
  const refreshTriggered = new Promise((resolve) => {
    resolveRefresh = resolve;
  });
  const loginPayload = {
    username: "admin",
    default_password: true,
  };
  const sessionUpdates = [];
  const refreshCalls = [];

  const result = await submitLogin({
    username: "admin",
    password: "secret",
    next: "/models",
    request: async (path, options) => {
      calls.push([path, options]);
      return loginPayload;
    },
    applySession: (payload) => {
      sessionUpdates.push(payload);
      return { authenticated: true, username: payload.username };
    },
    router: {
      replace: async (target) => {
        replaced.push(target);
      },
    },
    refresh: async () => {
      refreshCalls.push("refresh");
      resolveRefresh();
    },
  });
  await refreshTriggered;

  assert.equal(result, loginPayload);
  assert.deepEqual(calls, [[
    "/api/auth/login",
    {
      method: "POST",
      body: {
        username: "admin",
        password: "secret",
      },
      redirectOn401: false,
    },
  ]]);
  assert.deepEqual(sessionUpdates, [loginPayload]);
  assert.deepEqual(replaced, ["/models"]);
  assert.deepEqual(refreshCalls, ["refresh"]);
});
