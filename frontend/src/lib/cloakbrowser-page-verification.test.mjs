import assert from "node:assert/strict";
import vm from "node:vm";
import { test } from "node:test";

import * as verification from "../../../app/services/cloakbrowser_verification.mjs";

function statePage({ title = "MakerWorld", marker = false, iframeUrls = [], geetest = false } = {}) {
  const visible = {
    getBoundingClientRect: () => ({ width: 300, height: 70 }),
    checkVisibility: () => true,
  };
  return {
    evaluate: async (callback) => vm.runInNewContext(`(${callback.toString()})()`, {
      URL,
      location: { href: "https://makerworld.com/zh" },
      getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
      document: {
        title,
        querySelector: (selector) => {
          if (selector.includes("challenge-form")) return marker ? visible : null;
          if (selector.includes("geetest")) return geetest ? visible : null;
          return null;
        },
        querySelectorAll: (selector) => {
          if (selector.includes("iframe")) return iframeUrls.map((src) => ({ ...visible, src }));
          if (selector.includes("geetest")) return geetest ? [{ ...visible, querySelector: () => null }] : [];
          return [];
        },
      },
    }),
  };
}

function initialState(kind = "cloudflare") {
  return { detected: true, kind };
}

function clearedState() {
  return { detected: false, kind: "none" };
}

test("page verification detection requires challenge evidence rather than an error page title", async () => {
  const state = await verification.inspectPageVerificationState(statePage({ title: "403 Forbidden" }));
  assert.equal(state.detected, false);
  assert.equal(state.kind, "none");
  const cf = await verification.inspectPageVerificationState(statePage({ title: "Just a moment...", marker: true }));
  assert.equal(cf.detected, true);
  assert.equal(cf.kind, "cloudflare");
});

test("page verification rejects lookalike Turnstile origins and credentials", async () => {
  for (const url of [
    "https://challenges.cloudflare.com.evil.test/turnstile",
    "https://challenges.cloudflare.com@evil.test/turnstile",
    "http://challenges.cloudflare.com/turnstile",
    "https://challenges.cloudflare.com:8443/turnstile",
    "https://user@challenges.cloudflare.com/turnstile",
  ]) {
    const result = await verification.inspectPageVerificationState(statePage({ iframeUrls: [url] }));
    assert.equal(result.detected, false, url);
  }
  const result = await verification.inspectPageVerificationState(statePage({
    iframeUrls: ["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"],
  }));
  assert.equal(result.detected, true);
  assert.equal(result.kind, "cloudflare");
});

test("ordinary page bypasses verification without solver or clicks", async () => {
  let interactions = 0;
  const result = await verification.ensurePageVerification({}, {
    detectPageState: async () => clearedState(),
    verificationAdapter: async () => { interactions++; },
    passiveWaitMs: 0,
  });
  assert.equal(result.completed, true);
  assert.equal(result.detected, false);
  assert.equal(result.reason, "no_challenge");
  assert.equal(interactions, 0);
});

test("page verification waits for passive completion before dispatching interaction", async () => {
  let inspections = 0;
  let interactions = 0;
  const result = await verification.ensurePageVerification({}, {
    detectPageState: async () => ++inspections > 1 ? clearedState() : initialState(),
    verificationAdapter: async () => { interactions++; },
    passiveWaitMs: 100,
    sleep: async () => {},
  });
  assert.equal(result.completed, true);
  assert.equal(result.detected, true);
  assert.equal(result.kind, "cloudflare");
  assert.equal(interactions, 0);
});

test("page verification attempts a supported graphical challenge with the remaining timeout", async () => {
  let clear = false;
  let attempts = 0;
  const result = await verification.ensurePageVerification({}, {
    timeoutMs: 1000,
    passiveWaitMs: 0,
    detectPageState: async () => clear ? clearedState() : initialState("geetest4"),
    verificationAdapter: async (_page, options) => {
      attempts++;
      assert.ok(options.timeoutMs > 0 && options.timeoutMs <= 1000);
      assert.ok(options.signal instanceof AbortSignal);
      clear = true;
      return { attempted: true, completed: true, provider: "geetest4", challenge_type: "slider", attempts: 1, reason: "completed" };
    },
  });
  assert.equal(result.completed, true);
  assert.equal(result.manual_required, false);
  assert.equal(result.kind, "geetest4");
  assert.equal(attempts, 1);
});

test("a successful token alone never claims the page challenge was cleared", async () => {
  const result = await verification.ensurePageVerification({}, {
    passiveWaitMs: 0,
    detectPageState: async () => initialState(),
    verificationAdapter: async () => ({ completed: true, provider: "turnstile", reason: "completed" }),
  });
  assert.equal(result.completed, false);
  assert.equal(result.manual_required, true);
  assert.equal(result.reason, "challenge_unchanged");
});

test("disabled automatic interaction still returns a structured manual state", async () => {
  let attempts = 0;
  const result = await verification.ensurePageVerification({}, {
    allowInteraction: false,
    passiveWaitMs: 0,
    detectPageState: async () => initialState(),
    verificationAdapter: async () => { attempts++; },
  });
  assert.equal(result.completed, false);
  assert.equal(result.manual_required, true);
  assert.equal(result.verification.attempted, false);
  assert.equal(attempts, 0);
});

test("hanging challenge discovery and interactions remain bounded by one overall deadline", async () => {
  for (const hangDiscovery of [true, false]) {
    const started = performance.now();
    const result = await verification.ensurePageVerification({}, {
      timeoutMs: 30,
      passiveWaitMs: 0,
      detectPageState: async () => hangDiscovery ? new Promise(() => {}) : initialState(),
      verificationAdapter: async () => new Promise(() => {}),
    });
    assert.equal(result.completed, false);
    assert.equal(result.reason, "timeout");
    assert.ok(performance.now() - started < 500);
  }
});

test("page verification honors an already cancelled signal without any interaction", async () => {
  let calls = 0;
  const controller = new AbortController();
  controller.abort();
  const result = await verification.ensurePageVerification({}, {
    signal: controller.signal,
    detectPageState: async () => { calls++; return initialState(); },
  });
  assert.equal(result.reason, "aborted");
  assert.equal(result.completed, false);
  assert.equal(calls, 0);
});

test("challenge navigation can replace its execution context during passive verification", async () => {
  let calls = 0;
  const result = await verification.ensurePageVerification({}, {
    timeoutMs: 500,
    passiveWaitMs: 100,
    sleep: async () => {},
    detectPageState: async () => {
      calls++;
      if (calls === 1) return initialState();
      if (calls === 2) throw new Error("Execution context was destroyed, most likely because of a navigation.");
      return clearedState();
    },
    verificationAdapter: async () => { throw new Error("passive verification must not click"); },
  });
  assert.equal(result.completed, true);
  assert.equal(result.manual_required, false);
  assert.equal(calls, 3);
});

function turnstileFixture({ frameUrl, frameBox = { x: 100, y: 60, width: 300, height: 70 }, checkbox = null } = {}) {
  const widget = {
    evaluate: async (_callback, operation) => operation === "frame-src" ? frameUrl : true,
    boundingBox: async () => frameBox,
  };
  const child = {
    url: () => frameUrl,
    $: async (selector) => selector.includes("checkbox") ? checkbox : null,
    $$: async () => [],
    frameElement: async () => widget,
  };
  widget.contentFrame = async () => child;
  const main = {
    url: () => "https://makerworld.com/zh",
    $: async (selector) => selector.includes("challenges.cloudflare.com") ? widget : null,
    $$: async () => [],
  };
  const events = [];
  return {
    events,
    page: {
      mainFrame: () => main,
      frames: () => [main, child],
      createCDPSession: async () => ({
        send: async (method, event) => {
          if (method === "DOM.getDocument") {
            return { root: { nodeId: 1, backendNodeId: 1, nodeType: 9,
              nodeName: "#document", localName: "", nodeValue: "", children: [] } };
          }
          assert.equal(method, "Input.dispatchMouseEvent");
          events.push(event);
          return {};
        },
        detach: async () => {},
      }),
    },
  };
}

test("challenge discovery never takes a checkbox from a lookalike Cloudflare frame", async () => {
  const checkbox = {
    evaluate: async () => true,
    boundingBox: async () => ({ x: 115, y: 80, width: 25, height: 25 }),
  };
  const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com.evil.test/widget", checkbox });
  const challenge = await verification.detectVerificationChallenge(fixture.page);
  assert.equal(challenge, null);
});

test("a visible official Turnstile widget supports a bounded CDP checkbox interaction", async () => {
  const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com/turnstile" });
  const challenge = await verification.detectVerificationChallenge(fixture.page);
  const result = await verification.attemptAutomaticVerification(fixture.page, {
    detectChallenge: async () => challenge,
    fingerprintChallenge: async () => "widget-a",
    isChallengeComplete: async () => fixture.events.some(({ type }) => type === "mouseReleased"),
    turnstileResponseWaitMs: 0,
    timeoutMs: 300,
  });
  assert.equal(result.completed, true);
  const moves = fixture.events.filter(({ type }) => type === "mouseMoved");
  assert.ok(moves.length >= 8);
  assert.deepEqual(fixture.events.filter(({ type }) => type === "mousePressed").map(({ x, y }) => ({ x, y })), [
    { x: 130, y: 95 },
  ]);
  assert.equal(fixture.events.filter(({ type }) => type === "mouseReleased").length, 1);
});

test("Turnstile iframe fallback refuses a hidden or implausibly small widget", async () => {
  for (const frameBox of [null, { x: 0, y: 0, width: 30, height: 30 }]) {
    const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com/turnstile", frameBox });
    const result = await verification.attemptAutomaticVerification(fixture.page, {
      turnstileResponseWaitMs: 0,
      fingerprintChallenge: async () => "small-widget",
      timeoutMs: 35,
    });
    assert.equal(result.completed, false);
    assert.equal(fixture.events.filter(({ type }) => type === "mousePressed").length, 0);
  }
});

test("page verification uses actual Turnstile interaction then checks the page state", async () => {
  const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com/turnstile" });
  const result = await verification.ensurePageVerification(fixture.page, {
    passiveWaitMs: 0,
    turnstileResponseWaitMs: 0,
    fingerprintChallenge: async () => "widget-a",
    detectPageState: async () => fixture.events.some(({ type }) => type === "mouseReleased")
      ? clearedState() : initialState(),
    timeoutMs: 500,
  });
  assert.equal(result.completed, true);
  assert.equal(result.verification.attempted, true);
  assert.equal(result.verification.provider, "turnstile");
  assert.equal(result.verification.attempts, 1);
});

test("configured verification attempt count is clamped to two per page stage", async () => {
  for (const [requested, expected] of [[1, 1], [20, 2]]) {
    const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com/turnstile" });
    const challenge = await verification.detectVerificationChallenge(fixture.page);
    const result = await verification.attemptAutomaticVerification(fixture.page, {
      detectChallenge: async () => ({ ...challenge, version: fixture.events.filter(({ type }) => type === "mousePressed").length }),
      fingerprintChallenge: async (value) => String(value.version),
      isChallengeComplete: async () => false,
      turnstileResponseWaitMs: 0,
      maxAttempts: requested,
      timeoutMs: 500,
    });
    assert.equal(result.attempts, expected);
    assert.equal(result.reason, "attempts_exhausted");
    assert.equal(fixture.events.filter(({ type }) => type === "mousePressed").length, expected);
  }
});

test("page timeout retains the actual number of interactions already dispatched", async () => {
  const fixture = turnstileFixture({ frameUrl: "https://challenges.cloudflare.com/turnstile" });
  const result = await verification.ensurePageVerification(fixture.page, {
    passiveWaitMs: 0,
    turnstileResponseWaitMs: 0,
    fingerprintChallenge: async () => "widget-a",
    detectPageState: async () => initialState(),
    timeoutMs: 120,
  });
  assert.equal(result.completed, false);
  assert.equal(result.verification.attempted, true);
  assert.equal(result.verification.attempts, 1);
  assert.equal(fixture.events.filter(({ type }) => type === "mousePressed").length, 1);
});

test("a CF page that clears after passive wait completes even without a visible widget", async () => {
  const started = performance.now();
  let discoveries = 0;
  const result = await verification.ensurePageVerification({}, {
    timeoutMs: 300,
    passiveWaitMs: 10,
    detectPageState: async () => ({ detected: performance.now() - started < 50, kind: "cloudflare" }),
    detectChallenge: async () => { discoveries++; return null; },
  });
  assert.equal(result.completed, true);
  assert.equal(result.manual_required, false);
  assert.equal(result.verification.attempted, false);
  assert.equal(result.verification.attempts, 0);
  assert.ok(discoveries > 0);
});

test("legacy challenge completion callbacks are only called after a challenge is discovered", async () => {
  let discoveries = 0;
  const challenge = { provider: "turnstile", challenge_type: "checkbox" };
  const result = await verification.attemptAutomaticVerification({}, {
    timeoutMs: 300,
    sleep: async () => {},
    detectChallenge: async () => ++discoveries < 3 ? null : challenge,
    isChallengeComplete: async (_page, current) => {
      assert.equal(current, challenge);
      return true;
    },
  });
  assert.equal(result.completed, true);
  assert.equal(discoveries, 3);
});
