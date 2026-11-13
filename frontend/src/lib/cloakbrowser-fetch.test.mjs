import assert from "node:assert/strict";
import http from "node:http";
import { test } from "node:test";
import puppeteer from "puppeteer-core";

let fetchInBusinessPage;
try {
  ({ fetchInBusinessPage } = await import("../../../app/services/cloakbrowser_fetch.mjs"));
} catch (error) {
  if (error.code !== "ERR_MODULE_NOT_FOUND") throw error;
}

test("browser fetch exposes an injectable browser-only GET implementation", () => {
  assert.equal(typeof fetchInBusinessPage, "function");
});

test("fetch URL identity normalizes uppercase hosts, default TLS ports, dot segments, and fragments", async () => {
  let pageUrl = "about:blank";
  const session = { on() {}, off() {}, detach: async () => {},
    send: async (method) => method === "Page.getFrameTree" ? { frameTree: { frame: { id: "main" } } } : {},
  };
  const page = {
    target: () => ({ createCDPSession: async () => session }),
    goto: async (url) => { pageUrl = url; return { status: () => 200, headers: () => ({}) }; },
    url: () => pageUrl,
    evaluate: async (_script, options) => ({ status_code: 200, url: options.url,
      content_type: "application/json", headers: {}, text: "{}" }),
  };
  const result = await fetchInBusinessPage({
    context: { cookies: async () => [] }, targetUrl: "https://MAKERWORLD.com:443/api/../api/items#ignored",
  }, {
    withTemporaryPage: async (_browser, _context, callback) => callback(page),
    isAllowedBrowserFetchUrl: (url) => new URL(url).origin === "https://makerworld.com",
    cleanCookie: (cookie) => cookie, cleanFetchHeaders: () => ({}),
    headersWithBrowserAuth: (headers) => headers,
    hostnameMatchesDomains: () => true, platformDomains: () => ["makerworld.com"],
    makerWorldHomeUrl: () => "https://makerworld.com/zh",
    inspectPageVerificationState: async () => ({ detected: false }),
  });
  assert.equal(result.url, "https://makerworld.com/api/items");
});

const executablePath = process.env.MAKERHUB_TEST_CHROMIUM;

test("business-page GET uses local HTTP fixtures without external requests", {
  skip: !executablePath && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  assert.equal(typeof fetchInBusinessPage, "function");
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: ["--no-sandbox", "--disable-background-networking"],
  });
  t.after(() => browser.close());
  const context = await browser.createBrowserContext();
  const manualPage = await context.newPage();
  const requests = [];
  const routes = new Map();
  const server = http.createServer((request, response) => {
    requests.push({ url: request.url, headers: request.headers, method: request.method });
    const route = routes.get(new URL(request.url, "http://fixture").pathname);
    if (route) return route(request, response);
    response.writeHead(200, { "content-type": "text/html" });
    response.end("<title>MakerHub fixture</title><body>Business page</body>");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const ownedPages = [];
  const dependencies = {
    withTemporaryPage: async (_browser, _context, callback) => {
      const page = await context.newPage();
      ownedPages.push(page);
      try { return await callback(page); }
      finally { await page.close(); }
    },
    isAllowedBrowserFetchUrl: (value) => {
      try { return new URL(value).origin === origin; } catch { return false; }
    },
    cleanCookie: (cookie) => cookie,
    platformDomains: () => ["127.0.0.1"],
    hostnameMatchesDomains: (hostname, domains) => domains.includes(hostname.replace(/^\./, "")),
    cleanFetchHeaders: (headers = {}) => Object.fromEntries(Object.entries(headers)
      .filter(([key]) => ["accept", "authorization", "token"].includes(key.toLowerCase()))),
    headersWithBrowserAuth: (headers) => headers,
    makerWorldHomeUrl: () => `${origin}/business`,
    inspectPageVerificationState: async (page) => ({
      detected: (await page.title()) === "Just a moment...",
      kind: "cloudflare",
    }),
    ensurePageVerification: async () => ({ detected: false, completed: true }),
  };
  const input = { browser, context, platform: "cn", targetUrl: `${origin}/api/items`, timeoutMs: 2000 };
  const json = (value, status = 200, headers = {}) => (_request, response) => {
    response.writeHead(status, { "content-type": "application/json", ...headers });
    response.end(typeof value === "string" ? value : JSON.stringify(value));
  };
  const challengeHtml = "<title>Just a moment...</title><div id=challenge-form>cf-chl-test</div>";

  await t.test("complete multi-megabyte JSON is fetched inside the business page with filtered headers", async () => {
    requests.length = 0;
    const text = JSON.stringify({ data: "模型".repeat(500_000) });
    routes.set("/api/items", json(text, 200, { "set-cookie": "private=secret", "retry-after": "7" }));
    const result = await fetchInBusinessPage({ ...input, headers: {
      accept: "application/json", authorization: "Bearer fixture", Cookie: "leak=secret", Host: "outside.test",
    } }, dependencies);
    assert.equal(result.status_code, 200);
    assert.equal(result.text, text);
    assert.equal(result.url, `${origin}/api/items`);
    assert.equal(result.headers["retry-after"], "7");
    assert.equal(result.headers["set-cookie"], undefined);
    const apiRequest = requests.find((request) => request.url === "/api/items");
    assert.equal(apiRequest.method, "GET");
    assert.equal(apiRequest.headers.authorization, "Bearer fixture");
    assert.equal(apiRequest.headers.referer, `${origin}/business`);
    assert.doesNotMatch(apiRequest.headers.cookie || "", /leak/);
    assert.equal(manualPage.isClosed(), false);
    assert.equal(ownedPages.at(-1).isClosed(), true);
  });

  await t.test("invalid destinations are rejected before setting cookies or opening a page", async () => {
    const openedBefore = ownedPages.length;
    await assert.rejects(fetchInBusinessPage({ ...input, targetUrl: "https://outside.test/api", cookies: [
      { name: "token", value: "secret", domain: "outside.test" },
    ] }, dependencies), /invalid browser fetch URL/);
    assert.equal(ownedPages.length, openedBefore);
    assert.equal((await context.cookies()).some((cookie) => cookie.name === "token"), false);
  });

  await t.test("cookies for unrelated domains are discarded even when the requested API is allowed", async () => {
    await fetchInBusinessPage({ ...input, cookies: [
      { name: "foreign_token", value: "secret", domain: "outside.test" },
      { name: "foreign_url", value: "secret", url: "https://outside.test/" },
    ] }, dependencies);
    assert.equal((await context.cookies()).some((cookie) => cookie.name.startsWith("foreign_")), false);
  });

  await t.test("a redirect outside the allowlist is stopped before forwarding authorization", async () => {
    let outsideRequests = 0;
    const outside = http.createServer((_request, response) => {
      outsideRequests += 1;
      response.end("secret received");
    });
    await new Promise((resolve) => outside.listen(0, "127.0.0.1", resolve));
    try {
      routes.set("/api/items", (_request, response) => {
        if (_request.method === "OPTIONS") {
          response.writeHead(204, { "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true", "Access-Control-Allow-Headers": "authorization,token" });
          response.end(); return;
        }
        response.writeHead(302, { location: `http://127.0.0.1:${outside.address().port}/stolen` });
        response.end();
      });
      const localhostOrigin = `http://localhost:${server.address().port}`;
      const redirectDeps = { ...dependencies,
        isAllowedBrowserFetchUrl: (url) => [origin, localhostOrigin].includes(new URL(url).origin),
      };
      for (const targetUrl of [`${origin}/api/items`, `${origin}/api/../api/items`,
        `http://LOCALHOST:${server.address().port}/api/items`]) {
        await assert.rejects(fetchInBusinessPage({ ...input, targetUrl,
          headers: { authorization: "Bearer fixture", token: "fixture" },
        }, redirectDeps), /outside allowed domains/);
        assert.equal(outsideRequests, 0);
      }
      assert.equal(ownedPages.at(-1).isClosed(), true);
    } finally {
      await new Promise((resolve) => outside.close(resolve));
    }
  });

  await t.test("allowlisted redirects still return the complete final response", async () => {
    routes.set("/api/items", (_request, response) => {
      response.writeHead(302, { location: "/api/final" });
      response.end();
    });
    routes.set("/api/final", json({ redirected: true }));
    const result = await fetchInBusinessPage(input, dependencies);
    assert.equal(result.url, `${origin}/api/final`);
    assert.deepEqual(JSON.parse(result.text), { redirected: true });
  });

  await t.test("an allowlisted Bambu-style API honors browser CORS and never falls back outside the page", async () => {
    let corsEnabled = true;
    const apiMethods = [];
    const api = http.createServer((request, response) => {
      apiMethods.push(request.method);
      if (corsEnabled) {
        response.setHeader("Access-Control-Allow-Origin", origin);
        response.setHeader("Access-Control-Allow-Credentials", "true");
        response.setHeader("Access-Control-Allow-Headers", "authorization, token");
        response.setHeader("Access-Control-Allow-Methods", "GET");
      }
      if (request.method === "OPTIONS") { response.writeHead(204); response.end(); return; }
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"bambuApi":true}');
    });
    await new Promise((resolve) => api.listen(0, "127.0.0.1", resolve));
    const apiOrigin = `http://127.0.0.1:${api.address().port}`;
    const deps = { ...dependencies, isAllowedBrowserFetchUrl: (url) => [origin, apiOrigin].includes(new URL(url).origin) };
    try {
      const result = await fetchInBusinessPage({ ...input,
        targetUrl: `${apiOrigin}/v1/items`, headers: { authorization: "Bearer fixture" },
      }, deps);
      assert.deepEqual(JSON.parse(result.text), { bambuApi: true });
      assert.deepEqual(apiMethods, ["OPTIONS", "GET"]);
      apiMethods.length = 0;
      corsEnabled = false;
      await assert.rejects(fetchInBusinessPage({ ...input, targetUrl: `${apiOrigin}/v1/no-cors` }, deps), /CORS\/proxy/);
      assert.deepEqual(apiMethods, ["GET"]);
    } finally {
      await new Promise((resolve) => api.close(resolve));
    }
  });

  await t.test("ordinary 401, 403, and 429 never trigger verification or GET retries", async () => {
    for (const status of [401, 403, 429]) {
      requests.length = 0;
      routes.set("/api/items", json({ message: "Cloudflare is mentioned in this ordinary API error" }, status));
      let interactions = 0;
      const result = await fetchInBusinessPage(input, { ...dependencies,
        ensurePageVerification: async () => { interactions += 1; throw new Error("must not verify"); },
      });
      assert.equal(result.status_code, status);
      assert.equal(interactions, 0);
      assert.equal(requests.filter((request) => request.url === "/api/items").length, 1);
    }
  });

  await t.test("an explicit CF response without CORS headers still reaches same-page verification", async () => {
    let apiCalls = 0;
    let rootCalls = 0;
    const api = http.createServer((request, response) => {
      if (request.method === "OPTIONS") {
        response.writeHead(204, { "Access-Control-Allow-Origin": origin,
          "Access-Control-Allow-Credentials": "true", "Access-Control-Allow-Headers": "authorization" });
        response.end();
        return;
      }
      if (request.url === "/") {
        rootCalls += 1;
        response.writeHead(403, { "content-type": "text/html" }); response.end(challengeHtml);
        return;
      }
      if (request.url !== "/v1/items") { response.writeHead(204); response.end(); return; }
      apiCalls += 1;
      if (apiCalls === 1) {
        response.writeHead(403, { "content-type": "text/html", "cf-mitigated": "challenge" });
        response.end("<html>Browser check</html>");
      } else {
        response.writeHead(200, { "content-type": "application/json" }); response.end('{"cleared":true}');
      }
    });
    await new Promise((resolve) => api.listen(0, "127.0.0.1", resolve));
    const apiOrigin = `http://127.0.0.1:${api.address().port}`;
    try {
      let verifiedPage;
      const result = await fetchInBusinessPage({ ...input, targetUrl: `${apiOrigin}/v1/items`,
        headers: { authorization: "Bearer fixture" },
      }, { ...dependencies,
        isAllowedBrowserFetchUrl: (url) => [origin, apiOrigin].includes(new URL(url).origin),
        ensurePageVerification: async (page) => {
          verifiedPage = page;
          await page.setContent("<title>Verified</title>");
          return { detected: true, completed: true };
        },
      });
      assert.deepEqual(JSON.parse(result.text), { cleared: true });
      assert.equal(apiCalls, 2);
      assert.equal(rootCalls, 1);
      assert.equal(verifiedPage, ownedPages.at(-1));
      apiCalls = 0;
      rootCalls = 0;
      const unresolved = await fetchInBusinessPage({ ...input, targetUrl: `${apiOrigin}/v1/items`,
        headers: { authorization: "Bearer fixture" },
      }, { ...dependencies,
        isAllowedBrowserFetchUrl: (url) => [origin, apiOrigin].includes(new URL(url).origin),
        ensurePageVerification: async () => ({ detected: true, completed: false, manual_required: true }),
      });
      assert.equal(unresolved.status_code, 403);
      assert.match(unresolved.text, /cf-chl-test/);
      assert.equal(unresolved.verification.manual_required, true);
      assert.equal(apiCalls, 1);
      assert.equal(rootCalls, 1);
    } finally {
      await new Promise((resolve) => api.close(resolve));
    }
  });

  await t.test("a CF response is verified in the same owned page and GET is retried only once", async () => {
    requests.length = 0;
    let apiCalls = 0;
    routes.set("/api/items", (_request, response) => {
      apiCalls += 1;
      response.writeHead(apiCalls === 1 ? 403 : 200, { "content-type": apiCalls === 1 ? "text/html" : "application/json" });
      response.end(apiCalls === 1 ? challengeHtml : '{"cleared":true}');
    });
    routes.set("/", (_request, response) => { response.writeHead(200, { "content-type": "text/html" }); response.end(challengeHtml); });
    let verificationPage;
    const result = await fetchInBusinessPage(input, { ...dependencies,
      ensurePageVerification: async (page, options) => {
        verificationPage = page;
        assert.ok(options.timeoutMs > 0 && options.timeoutMs <= 50_000);
        await page.setContent("<title>Verified</title>");
        return { detected: true, completed: true };
      },
    });
    assert.deepEqual(JSON.parse(result.text), { cleared: true });
    assert.equal(apiCalls, 2);
    assert.equal(verificationPage, ownedPages.at(-1));
    assert.equal(result.verification.completed, true);
    routes.delete("/");
  });

  await t.test("a 3MF authorization GET is never automatically repeated", async () => {
    requests.length = 0;
    routes.set("/api/v1/design-service/instance/123/f3mf", (_request, response) => {
      response.writeHead(403, { "content-type": "text/html", "cf-mitigated": "challenge" }); response.end(challengeHtml);
    });
    const result = await fetchInBusinessPage({ ...input, targetUrl: `${origin}/api/v1/design-service/instance/123/f3mf` }, dependencies);
    assert.equal(result.status_code, 403);
    assert.equal(result.verification.manual_required, true);
    assert.equal(result.headers["cf-mitigated"], "challenge");
    assert.equal(requests.filter((request) => request.url.includes("/f3mf")).length, 1);
    assert.equal(requests.filter((request) => request.url === "/").length, 0);
  });

  await t.test("initial and response challenges share one verification time budget", async () => {
    routes.set("/business", (_request, response) => { response.end(challengeHtml); });
    routes.set("/", (_request, response) => { response.end(challengeHtml); });
    routes.set("/api/items", (_request, response) => {
      response.writeHead(403, { "content-type": "text/html" }); response.end(challengeHtml);
    });
    const budgets = [];
    const result = await fetchInBusinessPage({ ...input, verificationTimeoutMs: 500, verificationMaxAttempts: 2 }, {
      ...dependencies,
      ensurePageVerification: async (page, options) => {
        budgets.push(options.timeoutMs);
        await new Promise((resolve) => setTimeout(resolve, 60));
        await page.setContent("<title>Verified</title>");
        return { detected: true, completed: true };
      },
    });
    assert.equal(budgets.length, 2);
    assert.ok(budgets[1] < budgets[0] - 40);
    assert.equal(result.status_code, 403);
    assert.equal(result.verification.manual_required, true);
    assert.equal(result.verification.completed, false);
    routes.delete("/business");
    routes.delete("/");
  });

  await t.test("an unresolved initial challenge returns HTML without consuming an API GET", async () => {
    requests.length = 0;
    routes.set("/business", (_request, response) => { response.writeHead(403, { "content-type": "text/html" }); response.end(challengeHtml); });
    const result = await fetchInBusinessPage(input, { ...dependencies,
      ensurePageVerification: async () => ({ detected: true, completed: false, manual_required: true }),
    });
    assert.equal(result.status_code, 403);
    assert.equal(result.verification.manual_required, true);
    assert.match(result.text, /cf-chl-test/);
    assert.equal(requests.filter((request) => request.url === "/api/items").length, 0);
    routes.delete("/business");
  });

  await t.test("interaction attempts are shared across the initial page and API-response challenge", async () => {
    requests.length = 0;
    routes.set("/business", (_request, response) => { response.end(challengeHtml); });
    routes.set("/", (_request, response) => { response.end(challengeHtml); });
    routes.set("/api/items", (_request, response) => {
      response.writeHead(403, { "content-type": "text/html" }); response.end(challengeHtml);
    });
    const interactionPermissions = [];
    const result = await fetchInBusinessPage({ ...input, allowInteraction: true, verificationMaxAttempts: 2 }, {
      ...dependencies,
      ensurePageVerification: async (page, options) => {
        interactionPermissions.push(options.allowInteraction);
        if (interactionPermissions.length === 1) {
          assert.equal(options.maxAttempts, 2);
          await page.setContent("<title>Verified</title>");
          return { detected: true, completed: true, verification: { attempts: 2 } };
        }
        return { detected: true, completed: false, manual_required: true, verification: { attempts: 0 } };
      },
    });
    assert.deepEqual(interactionPermissions, [true, false]);
    assert.equal(result.verification.manual_required, true);
    assert.equal(requests.filter((request) => request.url === "/api/items").length, 1);
    routes.delete("/business");
    routes.delete("/");
  });

  await t.test("disabled interactions keep the total budget bounded and enabled verification is capped at 50 seconds", async () => {
    routes.set("/business", (_request, response) => { response.end(challengeHtml); });
    routes.set("/api/items", json({ ok: true }));
    const received = [];
    const deps = { ...dependencies, ensurePageVerification: async (page, options) => {
      received.push(options.timeoutMs);
      await page.setContent("<title>Verified</title>");
      return { detected: true, completed: true, verification: { attempts: 0 } };
    } };
    await fetchInBusinessPage({ ...input, allowInteraction: false, verificationTimeoutMs: 120_000 }, deps);
    await fetchInBusinessPage({ ...input, allowInteraction: true, verificationTimeoutMs: 120_000 }, deps);
    assert.ok(received[0] <= 12_000, `disabled interaction budget was ${received[0]}`);
    assert.ok(received[1] <= 50_000, `enabled verification budget was ${received[1]}`);
    routes.delete("/business");
  });

  await t.test("oversized bodies fail instead of returning truncated JSON", async () => {
    routes.set("/api/items", json({ body: "a".repeat(3000) }));
    await assert.rejects(fetchInBusinessPage({ ...input, maxResponseBytes: 1024 }, dependencies), /body exceeds/);
    assert.equal(ownedPages.at(-1).isClosed(), true);
    assert.equal(manualPage.isClosed(), false);
  });

  await t.test("an unfinished response is aborted within the total budget and its page is closed", async () => {
    routes.set("/api/items", (_request, response) => {
      response.writeHead(200, { "content-type": "application/json" });
      response.write('{"pending":');
    });
    const started = Date.now();
    await assert.rejects(fetchInBusinessPage({ ...input, timeoutMs: 1000, totalTimeoutMs: 350 }, dependencies), /timed out/);
    assert.ok(Date.now() - started < 1500);
    assert.equal(ownedPages.at(-1).isClosed(), true);
    assert.equal(manualPage.isClosed(), false);
  });
});
