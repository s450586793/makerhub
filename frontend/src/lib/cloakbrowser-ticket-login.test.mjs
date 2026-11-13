import assert from "node:assert/strict";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import puppeteer from "puppeteer-core";

const helpers = await import("../../../app/services/cloakbrowser_login.mjs");
const ticketUrl = "https://makerworld.com/api/sign-in/ticket?to=https%3A%2F%2Fmakerworld.com%2Fzh&ticket=private-fixture-ticket";

function helper(name) {
  assert.equal(typeof helpers[name], "function", `${name} must be exported`);
  return helpers[name];
}

function fixture({ mode = "confirmation", authenticated = false, challenge = false, challengeAfterContinue = false } = {}) {
  let url = "https://makerworld.com/en";
  let state = authenticated ? "authenticated" : "anonymous";
  let challengeVisible = challenge;
  let clicks = 0;
  const navigations = [];
  const cookies = [{ name: "token", value: "bambu-fixture-token", domain: ".bambulab.com" },
    { name: "token", value: authenticated ? "makerworld-fixture-token" : "", domain: ".makerworld.com" }];
  const visible = { display: "block", visibility: "visible", opacity: "1" };
  const button = (text, click = () => {}) => ({ innerText: text, textContent: text,
    getBoundingClientRect: () => ({ width: 100, height: 30 }), hasAttribute: () => false, click });
  const page = {
    url: () => url,
    goto: async (target) => {
      navigations.push(target);
      url = target.startsWith("https://makerworld.com/api/sign-in/ticket") ? "https://makerworld.com/en" : target;
      state = target.startsWith("https://bambulab.com/") ? mode : state;
    },
    evaluate: async (fn, input) => {
      const buttons = state === "confirmation" ? [button("Sign Out"), button("Continue", () => {
        clicks += 1;
        url = "https://makerworld.com/en";
        state = "authenticated";
        challengeVisible = challengeAfterContinue;
        cookies[1].value = "makerworld-fixture-token";
      })] : state === "anonymous" ? [button("Log In")] : [];
      const document = {
        title: challengeVisible ? "Just a moment" : "MakerWorld", body: {}, readyState: "complete",
        querySelector: (selector) => selector.includes("#challenge-form") && challengeVisible ? {}
          : selector.startsWith("input") && state === "form" ? {} : null,
        querySelectorAll: (selector) => selector.includes("button") ? buttons : [],
      };
      return runInNewContext(`(${fn.toString()})(input)`, {
        document, location: { href: url }, getComputedStyle: () => visible,
        window: { getComputedStyle: () => visible }, URL, input,
      });
    },
  };
  return { page, context: { cookies: async () => cookies }, cookies, navigations, clicks: () => clicks,
    setChallenge: (value) => { challengeVisible = value; } };
}

test("official ticket predicate rejects the other platform, callbacks and credential lookalikes", () => {
  const accepts = helper("isMakerWorldTicketUrl");
  assert.equal(accepts(ticketUrl, "global"), true);
  for (const target of [ticketUrl.replace("makerworld.com/", "makerworld.com.evil/"),
    ticketUrl.replace("https://", "https://user@"), ticketUrl.replace("ticket=private-fixture-ticket", "ticket="),
    ticketUrl.replace("makerworld.com%2Fzh", "evil.test%2Fzh")]) {
    assert.equal(accepts(target, "global"), false);
  }
  assert.equal(accepts(ticketUrl, "cn"), false);
});

test("empty MakerWorld token plus a Bambu token does not authenticate an anonymous homepage", async () => {
  const { page, context } = fixture();
  const result = await helper("readMakerWorldAuthenticationState")(context, page, "global");
  assert.equal(result.authenticated, false);
  assert.equal(result.authentication_reason, "auth_required");
  assert.equal(JSON.stringify(result).includes("fixture-token"), false);
});

test("failed direct ticket login uses one official signed-in Continue and verifies the resulting MakerWorld session", async () => {
  const current = fixture();
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000,
  });
  assert.equal(result.authenticated, true);
  assert.equal(result.authentication_reason, "ready");
  assert.equal(result.login_fallback_attempted, true);
  assert.equal(current.clicks(), 1);
  assert.equal(current.navigations.length, 2);
  const fallback = new URL(current.navigations[1]);
  assert.equal(fallback.origin, "https://bambulab.com");
  assert.equal(helpers.isBambuLoginConfirmationUrl(fallback.href, "global"), true);
  assert.equal(JSON.stringify(result).includes("private-fixture-ticket"), false);
});

test("successful direct ticket login needs no SSO fallback", async () => {
  const current = fixture({ authenticated: true });
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000,
  });
  assert.equal(result.authenticated, true);
  assert.equal(result.login_fallback_attempted, false);
  assert.equal(current.navigations.length, 1);
  assert.equal(current.clicks(), 0);
});

test("ticket hydration clearing an injected token cannot be mistaken for a completed login", async () => {
  const current = fixture({ authenticated: true });
  const navigate = current.page.goto;
  current.page.goto = async (target) => {
    await navigate(target);
    if (target === ticketUrl) setTimeout(() => { current.cookies[1].value = ""; }, 20);
  };
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000,
  });
  assert.equal(result.authenticated, true);
  assert.equal(result.login_fallback_attempted, true);
  assert.equal(current.clicks(), 1);
});

test("ordinary Bambu sign-in forms remain available without submitting credentials", async () => {
  const current = fixture({ mode: "form" });
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000,
  });
  assert.equal(result.authenticated, false);
  assert.equal(result.authentication_reason, "auth_required");
  assert.equal(result.login_fallback_attempted, true);
  assert.equal(current.navigations.length, 2);
  assert.equal(current.clicks(), 0);
});

test("verification pages and untrusted ticket targets do not start an SSO fallback", async () => {
  const current = fixture({ challenge: true });
  const complete = helper("completeMakerWorldTicketSession");
  const challenged = await complete(current.context, current.page, "global", { targetUrl: ticketUrl, timeoutMs: 3000 });
  assert.equal(challenged.authenticated, false);
  assert.equal(challenged.authentication_reason, "verification_required");
  assert.equal(challenged.login_fallback_attempted, false);
  assert.equal(current.navigations.length, 1);
  const invalid = fixture();
  const rejected = await complete(invalid.context, invalid.page, "global", { targetUrl: "https://evil.test/", timeoutMs: 3000 });
  assert.equal(rejected.authenticated, false);
  assert.equal(invalid.navigations.length, 0);
});

test("ticket login reuses enabled page verification without navigating the one-time ticket twice", async () => {
  const current = fixture({ challenge: true });
  const calls = [];
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000, autoVerifyChallenges: true,
    ensurePageVerification: async (_page, options) => {
      calls.push(options);
      assert.equal(options.allowInteraction, true);
      assert.ok(options.timeoutMs > 0 && options.timeoutMs < 3000);
      assert.equal(options.maxAttempts, 2);
      current.setChallenge(false);
      return { completed: true, verification: { attempted: true, attempts: 1 } };
    },
  });
  assert.equal(result.authenticated, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(current.navigations.filter((url) => url === ticketUrl).length, 1);
});

test("the SSO landing challenge receives only the remaining provider attempt", async () => {
  const current = fixture({ challenge: true, challengeAfterContinue: true });
  const limits = [];
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000, autoVerifyChallenges: true,
    ensurePageVerification: async (_page, options) => {
      limits.push(options.maxAttempts);
      current.setChallenge(false);
      return { completed: true, verification: { attempted: true, attempts: 1 } };
    },
  });
  assert.equal(result.authenticated, true);
  assert.deepEqual(limits, [2, 1]);
  assert.equal(current.navigations.filter((url) => url === ticketUrl).length, 1);
});

test("disabled verification and a shared two-attempt budget prevent further provider interactions", async () => {
  const complete = helper("completeMakerWorldTicketSession");
  const disabled = fixture({ challenge: true });
  const result = await complete(disabled.context, disabled.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000, autoVerifyChallenges: false,
    ensurePageVerification: async () => assert.fail("disabled verification cannot call the provider"),
  });
  assert.equal(result.authentication_reason, "verification_required");
  const current = fixture({ challenge: true });
  const navigate = current.page.goto;
  current.page.goto = async (target) => {
    await navigate(target);
    if (target.startsWith("https://bambulab.com/")) current.setChallenge(true);
  };
  let providerCalls = 0;
  const exhausted = await complete(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 3000, autoVerifyChallenges: true,
    ensurePageVerification: async (_page, options) => {
      providerCalls++;
      assert.equal(options.maxAttempts, 2);
      current.setChallenge(false);
      return { completed: true, verification: { attempted: true, attempts: 2 } };
    },
  });
  assert.equal(exhausted.authentication_reason, "verification_required");
  assert.equal(exhausted.login_fallback_attempted, true);
  assert.equal(providerCalls, 1);
});

test("the login deadline aborts an active provider before the helper returns", async () => {
  const current = fixture({ challenge: true });
  let signal;
  let cancelled = false;
  const started = performance.now();
  const result = await helper("completeMakerWorldTicketSession")(current.context, current.page, "global", {
    targetUrl: ticketUrl, timeoutMs: 1150, autoVerifyChallenges: true,
    ensurePageVerification: async (_page, options) => {
      signal = options.signal;
      await new Promise((resolve) => signal.addEventListener("abort", () => { cancelled = true; resolve(); }, { once: true }));
      return { completed: false, verification: { attempted: true, attempts: 1 } };
    },
  });
  assert.equal(result.authenticated, false);
  assert.equal(signal.aborted, true);
  assert.equal(cancelled, true);
  assert.ok(performance.now() - started < 1800);
  assert.equal(current.navigations.length, 1);
});

test("a visible Log In, an expired token or an unfinished callback cannot satisfy the login postcondition", async () => {
  const readState = helper("readMakerWorldAuthenticationState");
  const current = fixture();
  current.cookies[1].value = "nonempty-but-logged-out";
  assert.equal((await readState(current.context, current.page, "global")).authenticated, false);
  const expired = fixture({ authenticated: true });
  expired.cookies[1].expires = Date.now() / 1000 - 10;
  assert.equal((await readState(expired.context, expired.page, "global")).authenticated, false);
  const callback = fixture({ authenticated: true });
  callback.page.url = () => ticketUrl;
  assert.equal((await readState(callback.context, callback.page, "global")).authenticated, false);
});

test("session cookie checks reject malformed expiry, unrelated domains and paths", () => {
  const hasSession = helpers.hasMakerWorldSessionCookie;
  const token = { name: "token", value: "fixture-token", domain: ".makerworld.com", path: "/" };
  for (const expires of [0, true, "4102444800", NaN, Infinity, -2]) {
    assert.equal(hasSession([{ ...token, expires }], "global"), false);
  }
  for (const domain of [".bambulab.com", ".com", ".notmakerworld.com", ".makerworld.com.evil"]) {
    assert.equal(hasSession([{ ...token, domain }], "global"), false);
  }
  assert.equal(hasSession([{ ...token, expires: -1 }], "global"), true);
  assert.equal(hasSession([{ ...token, path: "/zh" }], "global", "/en"), false);
  assert.equal(hasSession([{ ...token, path: "/en" }], "global", "/english"), false);
  assert.equal(hasSession([{ ...token, path: "/en" }], "global", "/en/models/123"), true);
});

test("ticket fallback works through real Chromium navigation and never requests a 3MF", {
  skip: !process.env.MAKERHUB_TEST_CHROMIUM && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  const complete = helper("completeMakerWorldTicketSession");
  const browser = await puppeteer.launch({ executablePath: process.env.MAKERHUB_TEST_CHROMIUM,
    headless: true, args: ["--no-sandbox", "--disable-background-networking"] });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const context = page.browserContext();
  const requests = [];
  let mode = "confirmation";
  await context.setCookie({ name: "token", value: "", domain: ".makerworld.com", path: "/", secure: true },
    { name: "token", value: "bambu-fixture-token", domain: ".bambulab.com", path: "/", secure: true });
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const url = new URL(request.url());
    requests.push({ host: url.hostname, path: url.pathname, navigation: request.isNavigationRequest() });
    if (url.pathname.includes("/f3mf")) {
      void request.abort();
    } else if (url.hostname === "bambulab.com") {
      const body = mode === "form" ? `<!doctype html><form onsubmit="window.submissions=(window.submissions||0)+1;return false">
        <input type="email"><input type="password"><button>Log In</button><button type="button">Continue</button></form>`
        : `<!doctype html><button>Sign Out</button>
          <button onclick="location.href='https://makerworld.com/en?fixture_success=1'">Continue</button>`;
      void request.respond({ status: 200, contentType: "text/html", body });
    } else if (url.pathname === "/api/sign-in/ticket") {
      void request.respond({ status: 302, headers: { location: "https://makerworld.com/en" } });
    } else if (url.searchParams.get("fixture_success") === "1") {
      void request.respond({ status: 200, headers: { "set-cookie": "token=makerworld-fixture-token; Domain=.makerworld.com; Path=/; Secure; SameSite=Lax" },
        contentType: "text/html", body: "<!doctype html><button>My account</button>" });
    } else {
      void request.respond({ status: 200, contentType: "text/html", body: mode === "challenge"
        ? "<!doctype html><title>Just a moment</title><div id='challenge-form'></div>"
        : "<!doctype html><button>Log In</button>" });
    }
  });
  const result = await complete(context, page, "global", { targetUrl: ticketUrl, timeoutMs: 10000 });
  assert.equal(result.authenticated, true);
  assert.equal(result.login_fallback_attempted, true);
  assert.equal(requests.filter((item) => item.host === "bambulab.com" && item.navigation).length, 1);
  assert.equal(requests.some((item) => item.path.includes("/f3mf")), false);

  await t.test("a normal sign-in form stays unsigned and is never submitted", async () => {
    mode = "form";
    await context.setCookie({ name: "token", value: "", domain: ".makerworld.com", path: "/", secure: true });
    const result = await complete(context, page, "global", { targetUrl: ticketUrl, timeoutMs: 10000 });
    assert.equal(result.authenticated, false);
    assert.equal(result.authentication_reason, "auth_required");
    assert.equal(result.login_fallback_attempted, true);
    assert.equal(await page.evaluate(() => window.submissions || 0), 0);
  });

  await t.test("a real challenge page stays passive without a fallback navigation", async () => {
    mode = "challenge";
    const previousFallbacks = requests.filter((item) => item.host === "bambulab.com" && item.navigation).length;
    const result = await complete(context, page, "global", { targetUrl: ticketUrl, timeoutMs: 10000 });
    assert.equal(result.authenticated, false);
    assert.equal(result.authentication_reason, "verification_required");
    assert.equal(result.login_fallback_attempted, false);
    assert.equal(requests.filter((item) => item.host === "bambulab.com" && item.navigation).length, previousFallbacks);
    assert.equal(requests.some((item) => item.path.includes("/f3mf")), false);
  });
});
