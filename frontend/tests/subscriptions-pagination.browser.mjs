import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const executablePath = process.env.CHROMIUM_EXECUTABLE;
assert.ok(executablePath, "Set CHROMIUM_EXECUTABLE to a local Chromium executable");

test("subscription pagination remains stable through refresh and navigation", async (t) => {
  const server = await createServer({
    root,
    configFile: `${root}vite.config.js`,
    server: { host: "127.0.0.1", port: 0 },
    logLevel: "error",
  });
  await server.listen();
  t.after(() => server.close());
  const browser = await puppeteer.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const requests = [];
  const revisions = new Map();
  let failNextRequest = false;
  const bitmap = await readFile(new URL("../../app/static/img/makerhub-logo.png", import.meta.url));
  await page.evaluateOnNewDocument(() => {
    delete window.IntersectionObserver;
    const sources = new Set();
    window.EventSource = class {
      listeners = new Map();
      constructor() { sources.add(this); }
      addEventListener(type, callback) { this.listeners.set(type, callback); }
      close() { sources.delete(this); }
    };
    let eventId = 0;
    window.refreshSubscriptions = () => {
      for (const source of sources) {
        source.listeners.get("state.changed")?.({
          type: "state.changed",
          data: JSON.stringify({ id: ++eventId, scope: "subscriptions_state", type: "state.changed" }),
        });
      }
    };
  });
  await page.setRequestInterception(true);
  page.on("request", async (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/static/") || url.pathname.startsWith("/test-cover/")) {
      await request.respond({ status: 200, contentType: "image/png", body: bitmap });
      return;
    }
    if (!url.pathname.startsWith("/api/")) {
      await request.continue();
      return;
    }
    let payload = {};
    let status = 200;
    if (url.pathname === "/api/bootstrap") {
      payload = { app_version: "0.20.1", session: { authenticated: true, username: "test" } };
    } else if (url.pathname === "/api/system/version") {
      payload = { app_version: "0.20.1" };
    } else if (url.pathname === "/api/subscriptions/light") {
      const currentPage = Number(url.searchParams.get("page"));
      const pageSize = Number(url.searchParams.get("page_size"));
      requests.push({ page: currentPage, pageSize, limit: url.searchParams.get("limit") });
      if (failNextRequest) {
        failNextRequest = false;
        status = 503;
        payload = { detail: "Temporary test failure" };
      } else {
        const revision = revisions.get(currentPage) || 0;
        const start = (currentPage - 1) * pageSize;
        const items = Array.from({ length: Math.max(0, Math.min(pageSize, 25 - start)) }, (_, index) => {
          const id = start + index + 1;
          return {
            key: `source-${id}`,
            kind: "author",
            card_kind: "author",
            title: `Source ${id}${revision ? ` v${revision}` : ""}`,
            model_dirs: [`model-${id}`],
            preview_snapshot_url: `/test-cover/${id}-${revision}.png`,
          };
        });
        payload = {
          count: 25,
          summary: { enabled: 25, running: 0 },
          sections: [{ key: "subscription_sources", items, total: 25, page: currentPage, page_size: pageSize, has_more: start + items.length < 25 }],
        };
      }
    } else if (url.pathname.startsWith("/api/logs")) {
      payload = { entries: [], files: [], facets: {}, count: 0 };
    }
    await request.respond({ status, contentType: "application/json", body: JSON.stringify(payload) });
  });
  const base = `http://127.0.0.1:${server.httpServer.address().port}`;
  const titles = () => page.$$eval(".source-library-card__title", (nodes) => nodes.map((node) => node.textContent.trim()));
  const waitCount = (count) => page.waitForFunction((expected) => document.querySelectorAll(".source-library-card").length === expected, {}, count);
  const refresh = async (currentPage, revision) => {
    revisions.set(currentPage, revision);
    await page.evaluate(() => window.refreshSubscriptions());
    await page.waitForFunction((expected) => [...document.querySelectorAll(".source-library-card__title")].some((node) => node.textContent.endsWith(expected)), {}, `v${revision}`);
  };
  const navigate = (path) => page.evaluate(async (nextPath) => {
    const { default: router } = await import("/src/router.js");
    await router.push(nextPath);
  }, path);

  await page.goto(`${base}/subscriptions`);
  await waitCount(8);
  await page.click(".list-loader-anchor button");
  await waitCount(16);
  await page.click(".list-loader-anchor button");
  await waitCount(24);

  await t.test("a background refresh preserves all three loaded pages", async () => {
    const before = await titles();
    const requestCount = requests.length;
    await refresh(3, 1);
    assert.equal((await titles()).length, 24);
    assert.deepEqual((await titles()).slice(0, 16), before.slice(0, 16));
    assert.deepEqual(requests.slice(requestCount).map((request) => request.page), [3]);
  });

  await t.test("returning through the sidebar restores the loaded page and cards", async () => {
    await navigate("/logs");
    await page.waitForSelector(".source-library-card", { hidden: true });
    const response = page.waitForResponse((item) => item.url().includes("/api/subscriptions/light"));
    await navigate("/subscriptions");
    await response;
    await waitCount(24);
    assert.equal((await titles())[0], "Source 1");
    assert.equal(new URL(page.url()).searchParams.get("page"), "3");
  });

  await t.test("refreshing a one-card last page preserves the preceding 24 cards", async () => {
    await page.click(".list-loader-anchor button");
    await waitCount(25);
    await refresh(4, 2);
    assert.equal((await titles()).length, 25);
    assert.equal((await titles())[0], "Source 1");
    assert.equal((await titles()).at(-1), "Source 25 v2");
  });

  await t.test("a failed background refresh keeps the visible cards", async () => {
    const before = await titles();
    failNextRequest = true;
    const response = page.waitForResponse((item) => item.url().includes("/api/subscriptions/light") && item.status() === 503);
    await page.evaluate(() => window.refreshSubscriptions());
    await response;
    assert.deepEqual(await titles(), before);
  });

  await t.test("an explicit page replaces a previously expanded cached list", async () => {
    await navigate("/logs");
    await page.waitForSelector(".source-library-card", { hidden: true });
    const requestCount = requests.length;
    await navigate("/subscriptions?page=2");
    await waitCount(8);
    assert.equal((await titles())[0], "Source 9");
    assert.equal(new URL(page.url()).searchParams.get("page"), "2");
    assert.deepEqual(requests.slice(requestCount).map((request) => request.page), [2]);
    await refresh(2, 3);
    assert.equal((await titles()).length, 8);
    assert.equal((await titles())[0], "Source 9 v3");
  });

  await t.test("opening a deep link retains only the pages loaded from that link", async () => {
    await page.goto(`${base}/subscriptions?page=3`);
    await waitCount(8);
    assert.equal((await titles())[0], "Source 17 v1");
    await page.click(".list-loader-anchor button");
    await waitCount(9);
    await refresh(4, 4);
    assert.equal((await titles()).length, 9);
    assert.equal((await titles())[0], "Source 17 v1");
    assert.equal((await titles()).at(-1), "Source 25 v4");
  });

  assert.ok(requests.every((request) => request.pageSize === 8 && request.limit === null));
  assert.deepEqual(errors, []);
});
