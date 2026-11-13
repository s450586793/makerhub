import assert from "node:assert/strict";
import { test } from "node:test";

import { coordinateThreeMfAuthorization } from "../../../app/services/cloakbrowser_bridge.mjs";

function authorizationFixture({ captcha = false, secondResponse = null } = {}) {
  const calls = { clicks: 0, waiters: [], order: [] };
  const response = {
    url: () => "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
    request: () => ({ method: () => "GET" }),
    status: () => captcha ? 418 : 200,
    text: async () => JSON.stringify(captcha
      ? { captchaId: "fixture-captcha" }
      : { name: "fixture.3mf", url: "https://download.example.test/fixture.3mf" }),
  };
  const page = {
    waitForResponse: (matcher, options) => {
      calls.order.push("waiter");
      calls.waiters.push(options);
      if (calls.waiters.length === 1) {
        assert.equal(matcher(response), true);
        return Promise.resolve(response);
      }
      if (secondResponse) {
        assert.equal(matcher(secondResponse), true);
        return Promise.resolve(secondResponse);
      }
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

test("authorization response matching accepts only GET and POST business requests", async () => {
  const { page, options } = authorizationFixture();
  const waitForResponse = page.waitForResponse;
  let matcher;
  page.waitForResponse = (responseMatcher, waiterOptions) => {
    matcher = responseMatcher;
    return waitForResponse(responseMatcher, waiterOptions);
  };
  assert.equal((await coordinateThreeMfAuthorization(page, {
    ...options, inputAutoVerify: false,
    pageVerificationAdapter: async () => ({ detected: false, completed: true, kind: "none" }),
  })).status_code, 200);
  const response = { url: () => "https://api.bambulab.cn/v1/design-service/instance/123/f3mf" };
  for (const method of ["GET", "POST"]) {
    assert.equal(matcher({ ...response, request: () => ({ method: () => method }) }), true, method);
  }
  for (const method of ["OPTIONS", "HEAD", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT", "get", "", null, undefined, 1]) {
    assert.equal(matcher({ ...response, request: () => ({ method: () => method }) }), false, String(method));
  }
  const unavailableRequests = [
    { name: "missing request", response },
    { name: "null request", response: { ...response, request: () => null } },
    { name: "missing method", response: { ...response, request: () => ({}) } },
    { name: "throwing request", response: { ...response, request: () => { throw new Error("request unavailable"); } } },
    { name: "throwing method", response: { ...response, request: () => ({ method: () => { throw new Error("method unavailable"); } }) } },
  ];
  for (const unavailable of unavailableRequests) {
    assert.equal(matcher(unavailable.response), false, unavailable.name);
  }
  for (const url of [
    "https://api.bambulab.cn/v1/design-service/instance/456/f3mf",
    "https://untrusted.example.test/v1/design-service/instance/123/f3mf",
    "https://api.bambulab.cn/v1/design-service/instance/123/other",
  ]) {
    assert.equal(matcher({ url: () => url, request: () => ({ method: () => "GET" }) }), false, url);
  }
});

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
  assert.equal(result.authorization_diagnostics.stage, "not_required");
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, false);
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
  assert.equal(result.authorization_diagnostics.stage, "preflight");
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, false);
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
  assert.equal(result.authorization_diagnostics.stage, "post_verification");
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, true);
  assert.equal(result.authorization_diagnostics.post_verification_budget_ms, adapterBudget);
  assert.ok(result.authorization_diagnostics.preflight_verification_ms >= 30);
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
  assert.deepEqual(result.authorization_diagnostics, {
    preflight_verification_ms: result.authorization_diagnostics.preflight_verification_ms,
    post_verification_budget_ms: 0,
    post_verification_adapter_started: false,
    stage: "post_budget_exhausted",
  });
  assert.ok(result.authorization_diagnostics.preflight_verification_ms >= 15);
});

test("an unrecognized post-click challenge is distinct from an exhausted preflight budget", async () => {
  const { page, options, calls } = authorizationFixture({ captcha: true });
  const frame = { $: async () => null, $$: async () => [] };
  page.mainFrame = () => frame;
  page.frames = () => [frame];
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    verificationTimeoutMs: 120,
    pageVerificationAdapter: async () => ({ detected: false, completed: true, kind: "none" }),
  });
  assert.deepEqual(result.verification, {
    attempted: false, completed: false, provider: "unknown", challenge_type: "unknown", attempts: 0, reason: "timeout",
  });
  assert.equal(result.authorization_diagnostics.stage, "post_verification");
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, true);
  assert.ok(result.authorization_diagnostics.post_verification_budget_ms > 0);
  assert.ok(result.authorization_diagnostics.post_verification_budget_ms <= 120);
  assert.equal(calls.clicks, 1);
  assert.equal(result.status_code, 418);
  assert.equal(Object.hasOwn(result.payload, "url"), false);
});

test("disabled post-click verification never claims that its adapter started", async () => {
  const { page, options, calls } = authorizationFixture({ captcha: true });
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    inputAutoVerify: false,
    pageVerificationAdapter: async () => ({ detected: false, completed: true, kind: "none" }),
    verificationAdapter: async () => assert.fail("post-click verification is disabled"),
  });
  assert.equal(result.status_code, 418);
  assert.equal(result.authorization_diagnostics.stage, "verification_disabled");
  assert.equal(result.authorization_diagnostics.post_verification_budget_ms, 0);
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, false);
  assert.equal(calls.clicks, 1);
});

test("authorization diagnostics retain only bounded internal fields without expanding verification limits", async () => {
  for (const [requestedAttempts, expectedAttempts] of [[99, 2], [-4, 1]]) {
    const { page, options, calls } = authorizationFixture({ captcha: true });
    let postBudget;
    const result = await coordinateThreeMfAuthorization(page, {
      ...options,
      verificationTimeoutMs: 1_000_000,
      verificationMaxAttempts: requestedAttempts,
      pageVerificationAdapter: async (_page, { timeoutMs, maxAttempts }) => {
        assert.ok(timeoutMs <= 50_000);
        assert.equal(maxAttempts, expectedAttempts);
        return { detected: false, completed: true, kind: "none", token: "must-not-leak" };
      },
      verificationAdapter: async (_page, { timeoutMs, maxAttempts }) => {
        assert.ok(timeoutMs > 0 && timeoutMs <= 50_000);
        assert.equal(maxAttempts, expectedAttempts);
        postBudget = timeoutMs;
        return { attempted: false, completed: false, reason: "timeout", token: "must-not-leak" };
      },
    });
    const diagnostics = result.authorization_diagnostics;
    assert.deepEqual(Object.keys(diagnostics).sort(), ["post_verification_adapter_started",
      "post_verification_budget_ms", "preflight_verification_ms", "stage"]);
    assert.ok(Number.isInteger(diagnostics.preflight_verification_ms));
    assert.ok(diagnostics.preflight_verification_ms >= 0 && diagnostics.preflight_verification_ms <= 50_000);
    assert.equal(diagnostics.post_verification_budget_ms, postBudget);
    assert.equal(diagnostics.post_verification_adapter_started, true);
    assert.equal(diagnostics.stage, "post_verification");
    assert.equal(JSON.stringify(result).includes("must-not-leak"), false);
    assert.equal(calls.clicks, 1);
    assert.equal(Object.hasOwn(result.payload, "url"), false);
  }
});

test("a page-generated signed response still cancels the adapter and records its actual start", async () => {
  const secondResponse = {
    url: () => "https://api.bambulab.cn/v1/design-service/instance/123/f3mf",
    request: () => ({ method: () => "GET" }),
    status: () => 200,
    text: async () => {
      await new Promise(resolve => setTimeout(resolve, 5));
      return JSON.stringify({ name: "fixture.3mf", url: "https://download.example.test/fixture.3mf" });
    },
  };
  const { page, options, calls } = authorizationFixture({ captcha: true, secondResponse });
  let cancelled = false;
  const result = await coordinateThreeMfAuthorization(page, {
    ...options,
    pageVerificationAdapter: async () => ({ detected: false, completed: true, kind: "none" }),
    verificationAdapter: async (_page, { signal }) => await new Promise(resolve => {
      signal.addEventListener("abort", () => {
        cancelled = true;
        resolve({ attempted: false, completed: false, reason: "aborted" });
      }, { once: true });
    }),
  });
  assert.equal(cancelled, true);
  assert.equal(result.status_code, 200);
  assert.equal(result.payload.url, "https://download.example.test/fixture.3mf");
  assert.equal(result.verification.completed, true);
  assert.equal(result.authorization_diagnostics.stage, "page_response");
  assert.equal(result.authorization_diagnostics.post_verification_adapter_started, true);
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
