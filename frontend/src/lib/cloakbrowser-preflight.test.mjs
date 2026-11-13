import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import puppeteer from "puppeteer-core";

import { coordinateThreeMfAuthorization } from "../../../app/services/cloakbrowser_bridge.mjs";

const helpers = await import("../../../app/services/cloakbrowser_preflight.mjs").catch((error) => {
  if (error.code !== "ERR_MODULE_NOT_FOUND") throw error;
  return {};
});

function helper(name) {
  assert.equal(typeof helpers[name], "function", `${name} must be exported`);
  return helpers[name];
}

function fixture({
  modelId = 2347709,
  instances = [{ id: 2658384 }, { id: 2658379 }, { id: 2658388 }],
  hash = "#profileId-2658384",
  loginVisible = false,
  authenticated = true,
  designFields = {},
} = {}) {
  const page = new EventEmitter();
  let url = `https://makerworld.com.cn/zh/models/${modelId}${hash}`;
  const design = { id: modelId, instances, ...designFields };
  const login = { innerText: "Log In", textContent: "Log In",
    getBoundingClientRect: () => ({ width: loginVisible ? 80 : 0, height: 24 }),
    getAttribute: () => "", hasAttribute: () => false };
  page.url = () => url;
  page.goto = async (target) => { url = target; };
  page.setRequestInterception = async () => {};
  page.evaluate = async (callback, input) => JSON.parse(JSON.stringify(runInNewContext(`(${callback.toString()})(input)`, {
    input,
    location: new URL(url),
    document: {
      querySelector: (selector) => selector === "#__NEXT_DATA__"
        ? { textContent: JSON.stringify({ props: { pageProps: { design } }, token: "private-hydration-token" }) }
        : null,
      querySelectorAll: () => [login],
    },
    getComputedStyle: () => ({ display: "block", visibility: "visible", opacity: "1" }),
  })));
  const context = { cookies: async () => authenticated
    ? [{ name: "token", value: "private-cookie-token", domain: ".makerworld.com.cn" }] : [] };
  return { page, context, design, setUrl: (value) => { url = value; } };
}

const modelUrl = "https://makerworld.com.cn/zh/models/2347709";

test("preflight uses live three-instance data and never revives the stale fourth instance", async () => {
  const prepare = helper("prepareThreeMfDownload");
  const { page, context } = fixture();
  let closed = false;
  const preflight = await prepare({ platform: "cn", modelUrl, context, autoVerifyChallenges: false }, {
    withTemporaryPage: async (_browser, _context, callback) => {
      try { return await callback(page); }
      finally { closed = true; }
    },
    ensurePageVerification: async (_page, options) => {
      assert.equal(options.allowInteraction, false);
      assert.ok(options.timeoutMs <= 5000);
      return { completed: true, detected: false };
    },
  });
  assert.equal(preflight.ready, true);
  assert.equal(preflight.authenticated, true);
  assert.deepEqual(preflight.model.instances.map((item) => item.id), ["2658384", "2658379", "2658388"]);
  assert.equal(preflight.model.instances.some((item) => item.id === "3584010"), false);
  assert.equal(closed, true);
  assert.equal(JSON.stringify(preflight).includes("private"), false);
});

test("enabled preflight verification shares the existing bounded solver while blocking authorization requests", async () => {
  const prepare = helper("prepareThreeMfDownload");
  const { page, context } = fixture();
  let navigations = 0;
  let blocked = 0;
  let forwarded = 0;
  const navigate = page.goto;
  page.goto = async (...args) => { navigations++; await navigate(...args); };
  const result = await prepare({ platform: "cn", modelUrl, context }, {
    withTemporaryPage: async (_browser, _context, callback) => callback(page),
    ensurePageVerification: async (_page, options) => {
      assert.equal(options.allowInteraction, true);
      assert.equal(options.timeoutMs, 50000);
      assert.equal(options.maxAttempts, 2);
      for (const path of ["/api/v1/design-service/instance/2658384/f3mf", "/private-fixture.3mf"]) {
        page.emit("request", { url: () => `https://makerworld.com.cn${path}`,
          abort: async () => { blocked++; }, continue: async () => { forwarded++; } });
      }
      return { completed: true, verification: { attempted: true, attempts: 1 } };
    },
  });
  assert.equal(result.ready, true);
  assert.equal(blocked, 2);
  assert.equal(forwarded, 0);
  assert.equal(navigations, 1);
});

test("preflight verification budgets stay bounded when explicitly configured", async () => {
  const prepare = helper("prepareThreeMfDownload");
  const { page, context } = fixture();
  const result = await prepare({ platform: "cn", modelUrl, context, autoVerifyChallenges: true,
    verificationTimeoutMs: 90000, verificationMaxAttempts: 9 }, {
    withTemporaryPage: async (_browser, _context, callback) => callback(page),
    ensurePageVerification: async (_page, options) => {
      assert.equal(options.timeoutMs, 50000);
      assert.equal(options.maxAttempts, 2);
      return { completed: false, verification: { attempted: true, attempts: 2, reason: "attempts_exhausted" } };
    },
  });
  assert.equal(result.reason, "verification_required");
});

test("preflight rejects both absent session cookies and a visible login action", async () => {
  const prepare = helper("prepareThreeMfDownload");
  for (const options of [{ authenticated: false }, { loginVisible: true }]) {
    const { page, context } = fixture(options);
    const result = await prepare({ platform: "cn", modelUrl, context }, {
      withTemporaryPage: async (_browser, _context, callback) => callback(page),
      ensurePageVerification: async () => ({ completed: true }),
    });
    assert.equal(result.ready, false);
    assert.equal(result.authenticated, false);
    assert.equal(result.reason, "auth_required");
  }
});

test("preflight anchors hydration to the exact model and platform", async () => {
  const read = helper("readThreeMfModelState");
  const { page } = fixture({ modelId: 1 });
  assert.equal((await read(page, "cn", modelUrl)).reason, "model_mismatch");
  const other = fixture();
  other.setUrl("https://makerworld.com/zh/models/2347709");
  assert.equal((await read(other.page, "cn", modelUrl)).reason, "model_mismatch");
});

test("preflight bounds instances and projects only download restriction semantics", async () => {
  const read = helper("readThreeMfModelState");
  const { page } = fixture({
    instances: Array.from({ length: 105 }, (_, index) => ({ id: index + 1, token: "private-instance-token" })),
    designFields: { token: "private-design-token", threeMfDownloadAllowed: false,
      paidSetting: { crowdfunding: true, token: "private-paid-token" },
      licenseDescriptionInfo: { content: "SDFL-PPO", token: "private-license-token" } },
  });
  const result = await read(page, "cn", modelUrl);
  assert.equal(result.model.instances.length, 100);
  assert.equal(result.model.threeMfDownloadAllowed, false);
  assert.equal(result.model.paidSetting.crowdfunding, true);
  assert.match(JSON.stringify(result.model.licenseDescriptionInfo), /sdfl-ppo/i);
  assert.equal(JSON.stringify(result).includes("private"), false);
});

test("disabled preflight verification stays passive and returns only bounded verification", async () => {
  const prepare = helper("prepareThreeMfDownload");
  const { page, context } = fixture();
  const result = await prepare({ platform: "cn", modelUrl, context, autoVerifyChallenges: false }, {
    withTemporaryPage: async (_browser, _context, callback) => callback(page),
    ensurePageVerification: async (_page, options) => {
      assert.equal(options.allowInteraction, false);
      return { completed: false, detected: true, kind: "cloudflare",
        verification: { attempted: false, completed: false, reason: "interaction_disabled", token: "private" } };
    },
  });
  assert.equal(result.reason, "verification_required");
  assert.equal(result.ready, false);
  assert.equal(JSON.stringify(result).includes("private"), false);
});

test("stale or mismatched selected instances prevent the download click", async () => {
  const check = helper("assertThreeMfTarget");
  for (const [instanceId, code] of [["3584010", "MAKERHUB_TARGET_UNAVAILABLE"], ["2658379", "MAKERHUB_TARGET_MISMATCH"]]) {
    const { page } = fixture();
    let clicks = 0;
    const result = await coordinateThreeMfAuthorization(page, {
      authorizationTimeout: 100,
      assertTargetReady: () => check(page, { platform: "cn", modelUrl, instanceId, timeoutMs: 1 }),
      findButton: async () => ({ click: async () => { clicks++; }, dispose: async () => {} }),
    });
    assert.equal(result.status_code, 409);
    assert.equal(result.payload.code, code);
    assert.equal(clicks, 0);
  }
});

test("a valid target hash can settle without selecting a different instance", async () => {
  const check = helper("assertThreeMfTarget");
  const { page, setUrl } = fixture();
  setTimeout(() => setUrl(`${modelUrl}#profileId-2658379`), 5);
  await check(page, { platform: "cn", modelUrl, instanceId: "2658379", timeoutMs: 250 });
});

test("target is checked again after finding the button and before any mouse input", async () => {
  const check = helper("assertThreeMfTarget");
  const { page, design } = fixture();
  page.waitForResponse = (_matcher, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
  });
  let clicks = 0;
  const result = await coordinateThreeMfAuthorization(page, {
    assertTargetReady: () => check(page, { platform: "cn", modelUrl, instanceId: "2658384", timeoutMs: 0 }),
    findButton: async () => {
      design.instances = [{ id: 2658379 }];
      return { click: async () => { clicks++; }, dispose: async () => {} };
    },
  });
  assert.equal(result.payload.code, "MAKERHUB_TARGET_UNAVAILABLE");
  assert.equal(clicks, 0);
});

test("blocked authorization aborts response waiting with a bounded target mismatch", async () => {
  const install = helper("installThreeMfAuthorizationGuard");
  const { page } = fixture();
  const guard = await install(page, { platform: "cn", instanceId: "2658384" });
  page.waitForResponse = (_matcher, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
  });
  let aborted = false;
  const result = await coordinateThreeMfAuthorization(page, {
    authorizationGuardSignal: guard.signal,
    findButton: async () => ({ dispose: async () => {}, click: async () => {
      page.emit("request", { url: () => "https://makerworld.com.cn/api/v1/design-service/instance/3584010/f3mf?token=private",
        abort: async () => { aborted = true; }, continue: async () => assert.fail("wrong instance must not be sent") });
    } }),
  });
  assert.equal(aborted, true);
  assert.deepEqual(result, { status_code: 409, payload: { code: "MAKERHUB_TARGET_MISMATCH" } });
  assert.equal(JSON.stringify(result).includes("private"), false);
  await guard.dispose();
});

test("preflight failures never expose raw exception text or leave request listeners", async () => {
  const prepare = helper("prepareThreeMfDownload");
  const { page, context } = fixture();
  page.goto = async () => { throw new Error("private-cookie-token"); };
  const result = await prepare({ platform: "cn", modelUrl, context }, {
    withTemporaryPage: async (_browser, _context, callback) => callback(page),
  });
  assert.deepEqual(result, { ready: false, authenticated: false, reason: "preflight_failed", model: null });
  assert.equal(page.listenerCount("request"), 0);
});

test("authorization request guard blocks other instances and preflight blocks all authorization", async () => {
  const install = helper("installThreeMfAuthorizationGuard");
  for (const readOnly of [false, true]) {
    const { page } = fixture();
    const guard = await install(page, { platform: "cn", instanceId: "2658384", readOnly });
    const observed = [];
    for (const [id, authorization] of [["2658384", true], ["3584010", true], ["other", false]]) {
      const request = {
        url: () => authorization ? `https://makerworld.com.cn/api/v1/design-service/instance/${id}/f3mf` : "https://makerworld.com.cn/api/details",
        abort: async () => { observed.push(`blocked:${id}`); },
        continue: async () => { observed.push(`allowed:${id}`); },
      };
      page.emit("request", request);
    }
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(observed, [readOnly ? "blocked:2658384" : "allowed:2658384", "blocked:3584010", "allowed:other"]);
    assert.equal(guard.signal.aborted, !readOnly);
    await guard.dispose();
    assert.equal(page.listenerCount("request"), 0);
  }
});

const executablePath = process.env.MAKERHUB_TEST_CHROMIUM;

test("preflight and target checks use real Chromium DOM with local fixtures", {
  skip: !executablePath && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  const browser = await puppeteer.launch({ executablePath, headless: true,
    args: ["--no-sandbox", "--disable-background-networking"] });
  t.after(() => browser.close());
  async function localPage(html) {
    const page = await browser.newPage();
    t.after(() => page.close().catch(() => {}));
    await page.setRequestInterception(true);
    const serve = (request) => void request.respond({ status: 200, contentType: "text/html", body: "<body></body>" })
      .catch(() => {});
    page.on("request", serve);
    await page.goto(`${modelUrl}#profileId-2658384`);
    await page.setRequestInterception(false);
    page.off("request", serve);
    await page.setContent(html);
    return page;
  }
  const hydration = `<script id="__NEXT_DATA__" type="application/json">${JSON.stringify({ props: { pageProps: {
    design: { id: 2347709, token: "private", instances: [{ id: 2658384 }, { id: 2658379 }, { id: 2658388 }] },
  } } })}</script><span class="primaryButton">Download 3MF</span>`;

  await t.test("real hydration excludes the stale fourth configuration and preflight stays read-only", async () => {
    const page = await localPage(hydration);
    const context = page.browserContext();
    await context.setCookie({ name: "token", value: "private-cookie-token", domain: ".makerworld.com.cn", path: "/", secure: true });
    page.goto = async () => null;
    const result = await helpers.prepareThreeMfDownload({ context, platform: "cn", modelUrl }, {
      withTemporaryPage: async (_browser, _context, callback) => {
        try { return await callback(page); }
        finally { await page.close(); }
      },
    });
    assert.equal(result.ready, true);
    assert.deepEqual(result.model.instances.map((item) => item.id), ["2658384", "2658379", "2658388"]);
    assert.equal(JSON.stringify(result).includes("private"), false);
  });

  await t.test("enabled verification cannot send a real 3MF authorization request", async () => {
    const page = await localPage(hydration);
    const context = page.browserContext();
    await context.setCookie({ name: "token", value: "private-cookie-token", domain: ".makerworld.com.cn", path: "/", secure: true });
    page.goto = async () => null;
    let blocked = false;
    const result = await helpers.prepareThreeMfDownload({ context, platform: "cn", modelUrl, autoVerifyChallenges: true }, {
      withTemporaryPage: async (_browser, _context, callback) => {
        try { return await callback(page); }
        finally { await page.close(); }
      },
      ensurePageVerification: async (_page, options) => {
        assert.equal(options.allowInteraction, true);
        assert.equal(options.maxAttempts, 2);
        blocked = await page.evaluate(async () => {
          try { await fetch("/api/v1/design-service/instance/2658384/f3mf"); return false; }
          catch { return true; }
        });
        return { completed: true, verification: { attempted: true, attempts: 1 } };
      },
    });
    assert.equal(result.ready, true);
    assert.equal(blocked, true);
  });

  await t.test("missing target and fallback hash both stop before real download input", async () => {
    const page = await localPage(hydration);
    for (const instanceId of ["3584010", "2658379"]) {
      const result = await coordinateThreeMfAuthorization(page, {
        assertTargetReady: () => helpers.assertThreeMfTarget(page, { platform: "cn", modelUrl, instanceId, timeoutMs: 1 }),
        findButton: async () => assert.fail("stale or different selected instances cannot reach the download button"),
      });
      assert.equal(result.status_code, 409);
    }
  });

  await t.test("request interception stops a real wrong-instance fetch before any remote response", async () => {
    const page = await localPage(hydration);
    const guard = await helpers.installThreeMfAuthorizationGuard(page, { platform: "cn", instanceId: "2658384" });
    const blocked = await page.evaluate(async () => {
      try { await fetch("/api/v1/design-service/instance/3584010/f3mf"); return false; }
      catch { return true; }
    });
    assert.equal(blocked, true);
    assert.equal(guard.signal.reason.code, "MAKERHUB_TARGET_MISMATCH");
    await page.close();
    await guard.dispose();
  });
});
