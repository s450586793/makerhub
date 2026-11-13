import assert from "node:assert/strict";
import { test } from "node:test";

import { coordinateThreeMfAuthorization } from "../../../app/services/cloakbrowser_bridge.mjs";

function authorizationFixture({ captcha = false } = {}) {
  const calls = { clicks: 0, waiters: [], order: [] };
  const response = {
    url: () => "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
    status: () => captcha ? 418 : 200,
    text: async () => JSON.stringify(captcha
      ? { captchaId: "fixture-captcha" }
      : { name: "fixture.3mf", url: "https://download.example.test/fixture.3mf" }),
  };
  const page = {
    waitForResponse: (_matcher, options) => {
      calls.order.push("waiter");
      calls.waiters.push(options);
      if (calls.waiters.length === 1) return Promise.resolve(response);
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
      });
    },
  };
  const options = {
    instanceId: "123",
    authorizationTimeout: 1000,
    inputAutoVerifyChallenges: true,
    inputAutoVerify: true,
    findButton: async () => ({
      click: async () => { calls.clicks++; },
      dispose: async () => {},
    }),
  };
  return { page, options, calls };
}

test("3MF preflight finishes before arming authorization and clicks Download only once", async () => {
  const { page, options, calls } = authorizationFixture();
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    verificationMaxAttempts: 1,
    pageVerificationAdapter: async (_page, verificationOptions) => {
      calls.order.push("preflight");
      assert.equal(verificationOptions.allowInteraction, true);
      assert.equal(verificationOptions.maxAttempts, 1);
      return { detected: true, completed: true, kind: "cloudflare" };
    },
  });
  assert.equal(calls.order[0], "preflight");
  assert.equal(calls.clicks, 1);
  assert.equal(result.status_code, 200);
});

test("3MF unresolved preflight returns manual Cloudflare state without authorization", async () => {
  const { page, options, calls } = authorizationFixture();
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    pageVerificationAdapter: async () => ({
      detected: true, completed: false, manual_required: true, kind: "cloudflare",
      verification: { attempted: true, completed: false, attempts: 2, reason: "timeout" },
    }),
  });
  assert.equal(result.status_code, 403);
  assert.equal(result.payload.code, "MAKERHUB_CLOUDFLARE");
  assert.equal(calls.clicks, 0);
  assert.equal(calls.waiters.length, 0);
  assert.equal(result.verification.completed, false);
});

test("disabling preflight interaction preserves passive checks independently of 3MF verification", async () => {
  const { page, options } = authorizationFixture();
  let checked = false;
  await coordinateThreeMfAuthorization(page, {
    ...options,
    inputAutoVerifyChallenges: false,
    pageVerificationAdapter: async (_page, { allowInteraction }) => {
      checked = true;
      assert.equal(allowInteraction, false);
      return { detected: false, completed: true, kind: "none" };
    },
  });
  assert.equal(checked, true);
});

test("preflight and post-click verification share one time budget", async () => {
  const { page, options, calls } = authorizationFixture({ captcha: true });
  let adapterBudget;
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    verificationTimeoutMs: 100,
    verificationMaxAttempts: 1,
    pageVerificationAdapter: async () => {
      await new Promise(resolve => setTimeout(resolve, 35));
      return { detected: true, completed: true, kind: "cloudflare" };
    },
    verificationAdapter: async (_page, { timeoutMs, maxAttempts }) => {
      adapterBudget = timeoutMs;
      assert.equal(maxAttempts, 1);
      return { attempted: true, completed: false, reason: "unsupported_challenge" };
    },
  });
  assert.ok(adapterBudget > 0 && adapterBudget <= 75, `remaining budget: ${adapterBudget}`);
  assert.ok(calls.waiters[1].timeout <= 75);
  assert.equal(calls.clicks, 1);
  assert.equal(result.status_code, 418);
});

test("an exhausted preflight budget cannot start a second verification stage", async () => {
  const { page, options, calls } = authorizationFixture({ captcha: true });
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    verificationTimeoutMs: 10,
    pageVerificationAdapter: async () => {
      await new Promise(resolve => setTimeout(resolve, 20));
      return { detected: true, completed: true, kind: "cloudflare" };
    },
    verificationAdapter: async () => assert.fail("verification budget already exhausted"),
  });
  assert.equal(result.status_code, 418);
  assert.equal(result.verification.completed, false);
  assert.equal(result.verification.reason, "timeout");
  assert.equal(calls.clicks, 1);
});

test("3MF stops before automatic interaction if navigation left the platform", async () => {
  const { page, options, calls } = authorizationFixture();
  await assert.rejects(coordinateThreeMfAuthorization(page, {
    ...options,
    assertPageAllowed: () => { throw new Error("model page redirected outside allowed domains"); },
    pageVerificationAdapter: async () => assert.fail("outside platform page must not be verified"),
  }), /outside allowed domains/);
  assert.equal(calls.clicks, 0);
  assert.equal(calls.waiters.length, 0);
});

test("3MF rechecks the page after preflight navigation and before Download", async () => {
  const { page, options, calls } = authorizationFixture();
  let allowed = true;
  await assert.rejects(coordinateThreeMfAuthorization(page, {
    ...options,
    assertPageAllowed: () => {
      if (!allowed) throw new Error("model page redirected outside allowed domains");
    },
    pageVerificationAdapter: async () => {
      allowed = false;
      return { detected: true, completed: true, kind: "cloudflare" };
    },
  }), /outside allowed domains/);
  assert.equal(calls.clicks, 0);
});
