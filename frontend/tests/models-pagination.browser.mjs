import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const executablePath = process.env.CHROMIUM_EXECUTABLE;
assert.ok(executablePath, "Set CHROMIUM_EXECUTABLE to a local Chromium executable");

test("model library keeps the browsing list stable until an explicit refresh", async (t) => {
  const server = await createServer({ root, configFile: `${root}vite.config.js`, server: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
  await server.listen();
  t.after(() => server.close());
  const browser = await puppeteer.launch({ executablePath, headless: true, args: ["--no-sandbox"] });
  t.after(() => browser.close());
  const base = `http://127.0.0.1:${server.httpServer.address().port}`;
  const bitmap = await readFile(new URL("../../app/static/img/makerhub-logo.png", import.meta.url));

  async function fixture(t, path = "/models", total = 60) {
    const page = await browser.newPage();
    t.after(() => page.close());
    await page.setViewport({ width: 1440, height: 600 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let models = Array.from({ length: total }, (_, index) => ({ model_dir: `model-${index + 1}`, title: `Model ${index + 1}`, source: "cn", local_flags: {}, stats: {} }));
    let failNext = false;
    const requests = [];
    await page.evaluateOnNewDocument(() => {
      const observers = new Set();
      window.IntersectionObserver = class {
        constructor(callback) { this.callback = callback; }
        observe(target) { this.target = target; observers.add(this); }
        disconnect() { observers.delete(this); }
        unobserve() { observers.delete(this); }
      };
      window.loadModelPage = () => {
        for (const observer of [...observers]) {
          if (observer.target?.classList.contains("list-loader-anchor")) observer.callback([{ target: observer.target, isIntersecting: true }]);
        }
      };
      const sources = new Set();
      let id = 0;
      window.EventSource = class {
        listeners = new Map();
        constructor() { sources.add(this); }
        addEventListener(type, callback) { this.listeners.set(type, callback); }
        close() { sources.delete(this); }
      };
      window.completeArchive = () => {
        for (const source of sources) source.listeners.get("archive.completed")?.({ type: "archive.completed", data: JSON.stringify({ id: ++id, type: "archive.completed", scope: "archive_queue", payload: {} }) });
      };
      window.hasArchiveListener = () => [...sources].some((source) => source.listeners.has("archive.completed"));
    });
    await page.setRequestInterception(true);
    page.on("request", async (request) => {
      const url = new URL(request.url());
      if (url.pathname.startsWith("/static/")) return request.respond({ status: 200, contentType: "image/png", body: bitmap });
      if (!url.pathname.startsWith("/api/")) return request.continue();
      let payload = {};
      let status = 200;
      if (url.pathname === "/api/bootstrap") payload = { app_version: "0.20.5", session: { authenticated: true, username: "test" } };
      else if (url.pathname === "/api/system/version") payload = { app_version: "0.20.5" };
      else if (url.pathname === "/api/models/light") {
        const currentPage = Number(url.searchParams.get("page"));
        const size = Number(url.searchParams.get("page_size"));
        requests.push({ page: currentPage, pageSize: size, q: url.searchParams.get("q") });
        const filtered = url.searchParams.get("q") ? models.filter((model) => model.title.includes(url.searchParams.get("q"))) : models;
        const start = (currentPage - 1) * size;
        payload = { items: filtered.slice(start, start + size), count: size, page: currentPage, page_size: size, total: models.length, filtered_total: filtered.length, source_counts: { all: models.length, cn: models.length }, has_more: start + size < filtered.length, tags: [] };
        if (failNext) { failNext = false; status = 503; payload = { detail: "Temporary test failure" }; }
      } else if (url.pathname.startsWith("/api/logs")) payload = { entries: [], files: [], facets: {}, count: 0 };
      await request.respond({ status, contentType: "application/json", body: JSON.stringify(payload) });
    });
    await page.goto(base + path);
    await page.waitForSelector(".model-grid .gallery-card");
    await page.waitForFunction(() => window.hasArchiveListener());
    const ids = () => page.$$eval(".model-grid [data-model-dir]", (nodes) => nodes.map((node) => node.dataset.modelDir));
    const navigate = (path) => page.evaluate(async (path) => { const { default: router } = await import("/src/router.js"); await router.push(path); }, path);
    const append = async (count) => {
      await page.evaluate(() => window.loadModelPage());
      await page.waitForFunction((count) => document.querySelectorAll(".model-grid .gallery-card").length === count, {}, count);
    };
    const insert = () => { models = [{ model_dir: "new-model", title: "New model", source: "cn", local_flags: {}, stats: {} }, ...models.slice(0, -1)]; };
    t.after(() => assert.deepEqual(errors, []));
    return { page, ids, navigate, append, insert, requests, fail: () => { failNext = true; } };
  }

  await t.test("an archive event does not replace the cards being read", async (t) => {
    const { page, ids, insert, requests } = await fixture(t);
    const before = await ids();
    const requestCount = requests.length;
    insert();
    await page.evaluate(() => { for (let index = 0; index < 5; index++) window.completeArchive(); });
    await page.waitForFunction(() => document.querySelector(".model-library-refresh")?.textContent.includes("有更新") || document.querySelector('[data-model-dir="new-model"]'));
    assert.deepEqual(await ids(), before);
    assert.equal(requests.length, requestCount);
    await page.click(".model-library-refresh");
    await page.waitForSelector('[data-model-dir="new-model"]');
    assert.equal((await ids())[0], "new-model");
  });

  await t.test("background updates retain all three loaded pages", async (t) => {
    const { page, ids, append } = await fixture(t);
    await append(24);
    await append(36);
    const before = await ids();
    await page.evaluate(() => window.completeArchive());
    await page.waitForFunction(() => document.querySelector(".model-library-refresh")?.textContent.includes("有更新") || document.querySelectorAll(".model-grid .gallery-card").length === 12);
    assert.deepEqual(await ids(), before);
  });

  await t.test("returning to the anchored list keeps the cached cards after revalidation", async (t) => {
    const { page, ids, navigate, insert } = await fixture(t, "/models?anchor=model-3");
    const before = await ids();
    await navigate("/logs");
    insert();
    await navigate("/models?anchor=model-3");
    await page.waitForFunction(() => document.querySelector(".model-library-refresh")?.textContent.includes("有更新") || document.querySelector('[data-model-dir="new-model"]'));
    assert.deepEqual(await ids(), before);
    assert.equal(new URL(page.url()).searchParams.get("anchor"), "model-3");
  });

  await t.test("browser back retains the loaded range and the last page in the URL", async (t) => {
    const { page, ids, append, navigate, requests } = await fixture(t);
    await append(24);
    await append(36);
    const before = await ids();
    assert.equal(new URL(page.url()).searchParams.get("page"), "3");
    await navigate("/logs");
    const count = requests.length;
    await page.goBack();
    await page.waitForFunction(() => document.querySelectorAll(".model-grid .gallery-card").length === 36 && window.hasArchiveListener());
    assert.deepEqual(await ids(), before);
    assert.deepEqual(requests.slice(count).map((request) => request.page), [3]);
  });

  await t.test("revalidation retains earlier cards when the last page contains only two models", async (t) => {
    const { page, ids, append, navigate } = await fixture(t, "/models?page=4", 50);
    await append(14);
    const before = await ids();
    await navigate("/logs");
    await navigate("/models?page=5&anchor=model-49");
    await page.waitForFunction(() => document.querySelectorAll(".model-grid .gallery-card").length === 14 && window.hasArchiveListener());
    assert.deepEqual(await ids(), before);
    assert.equal((await ids())[0], "model-37");
  });

  await t.test("explicit filters replace the cached query and a failed refresh keeps its cards", async (t) => {
    const { page, ids, navigate, fail } = await fixture(t);
    await navigate("/models?q=Model%205");
    await page.waitForFunction(() => [...document.querySelectorAll(".gallery-card__title")].every((node) => node.textContent.includes("Model 5")));
    const before = await ids();
    assert.equal(before[0], "model-5");
    fail();
    await page.click(".model-library-refresh");
    await page.waitForFunction(() => document.querySelector(".form-status")?.textContent.includes("Temporary test failure"));
    assert.deepEqual(await ids(), before);
  });
});
