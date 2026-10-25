import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const executablePath = process.env.CHROMIUM_EXECUTABLE;
assert.ok(executablePath, "Set CHROMIUM_EXECUTABLE to a local Chromium executable");
const { version } = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const models = [
  { short_key: "local100492", model_dir: "local/related/model-a", title: "本地模型 A", source: "local", id: "" },
  { short_key: "mwcn12345", model_dir: "cn/related/model-b", title: "国内模型 B", source: "cn", id: "12345" },
  { short_key: "mwg98765", model_dir: "global/related/model-c", title: "国际模型 C", source: "global", id: "98765" },
  { short_key: "local100493", model_dir: "local/related/model-d", title: "本地模型 D", source: "local", id: "" },
].map((model, index) => ({
  ...model,
  detail_path: `/models/${model.short_key}`,
  cover_url: `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="${["#315b46", "#7b5832", "#526779", "#674b3e"][index]}"/></svg>`)}`,
  author: { name: "回归测试作者" },
  tags: ["相关模型测试"],
  stats: {},
  local_flags: {},
  gallery: [],
  instances: [],
  attachments: [],
  comments: [],
}));

test("related model navigation loads the route target and preserves it across history and late responses", { timeout: 60000 }, async (t) => {
  const server = await createServer({ root, configFile: `${root}vite.config.js`, server: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
  await server.listen();
  t.after(() => server.close());
  const browser = await puppeteer.launch({ executablePath, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  t.after(() => browser.close());
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;

  async function fixture(t) {
    const page = await browser.newPage();
    t.after(() => page.close());
    await page.setViewport({ width: 1440, height: 1000 });
    page.setDefaultTimeout(8000);
    const errors = [];
    const requests = [];
    const held = new Map();
    const pending = new Map();
    page.on("pageerror", (error) => errors.push(error.message));
    // 事件流不参与本次测试，避免自动重连干扰路由请求。
    await page.evaluateOnNewDocument(() => {
      window.EventSource = class {
        addEventListener() {}
        close() {}
      };
    });
    await page.setRequestInterception(true);
    page.on("request", async (request) => {
      const url = new URL(request.url());
      if (url.protocol === "data:") return request.continue();
      if (url.origin !== origin) {
        errors.push(`Unexpected external request: ${url.origin}`);
        return request.abort();
      }
      if (!url.pathname.startsWith("/api/")) return request.continue();
      let payload = {};
      if (url.pathname === "/api/bootstrap") {
        payload = { app_version: version, session: { authenticated: true, username: "fixture" }, theme_preference: "dark" };
      } else if (url.pathname === "/api/system/version") {
        payload = { app_version: version, github_latest_version: version };
      } else if (url.pathname === "/api/models/light") {
        payload = { items: models };
      } else if (url.pathname.startsWith("/api/models/")) {
        const key = decodeURIComponent(url.pathname.slice("/api/models/".length));
        requests.push(key);
        payload = models.find((model) => model.short_key === key || model.model_dir === key);
        if (!payload) {
          errors.push(`Unexpected model request: ${key}`);
          return request.respond({ status: 404, contentType: "application/json", body: "{}" });
        }
        if (held.has(key)) {
          pending.set(key, request);
          held.get(key)();
          return;
        }
      } else {
        errors.push(`Unexpected API request: ${url.pathname}`);
      }
      return request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
    });

    const assertModel = async (model) => {
      await page.waitForFunction(({ title, cover }) => {
        const image = document.querySelector(".mw-gallery__image");
        return document.querySelector(".mw-head h1")?.textContent === title && image?.src === cover && image.complete && image.naturalWidth > 0;
      }, {}, { title: model.title, cover: model.cover_url });
      assert.equal(new URL(page.url()).pathname, model.detail_path);
      const cachedDir = await page.evaluate(async (key) => {
        const { getPageCache } = await import("/src/lib/pageCache.js");
        return getPageCache(`model-detail:${key}`)?.detail?.model_dir;
      }, model.short_key);
      assert.equal(cachedDir, model.model_dir, "目标短链接的缓存必须对应目标模型");
    };
    const detailRequest = (request) => {
      const pathname = new URL(request.url()).pathname;
      return pathname.startsWith("/api/models/") && pathname !== "/api/models/light";
    };
    const expectRequest = async (model, action) => {
      const [request] = await Promise.all([page.waitForRequest(detailRequest), action()]);
      assert.equal(decodeURIComponent(new URL(request.url()).pathname.slice("/api/models/".length)), model.short_key, "切换详情必须请求新路由，不能使用上一模型的目录");
    };
    const clickRelated = async (model) => {
      const selector = `.mw-related-item[href="${model.detail_path}"]`;
      await page.waitForSelector(selector);
      await expectRequest(model, () => page.click(selector));
      await assertModel(model);
    };
    const navigate = (model) => page.evaluate(async (target) => {
      const { default: router } = await import("/src/router.js");
      await router.push(target);
    }, model.detail_path);
    await page.goto(origin + models[0].detail_path);
    await assertModel(models[0]);
    t.after(() => assert.deepEqual(errors, []));
    return { page, assertModel, expectRequest, clickRelated, navigate, requests, held, pending };
  }

  await t.test("clicking related models updates the request, title, cover and cache, including browser back and forward", async (t) => {
    const { page, assertModel, expectRequest, clickRelated, requests } = await fixture(t);
    await clickRelated(models[3]);
    await clickRelated(models[1]);
    await clickRelated(models[2]);
    await clickRelated(models[0]);
    await expectRequest(models[2], () => page.goBack());
    await assertModel(models[2]);
    await expectRequest(models[0], () => page.goForward());
    await assertModel(models[0]);
    assert.deepEqual(requests, [models[0], models[3], models[1], models[2], models[0], models[2], models[0]].map((model) => model.short_key));
  });

  await t.test("a late response cannot overwrite the newer model or its cache", async (t) => {
    const { page, assertModel, expectRequest, navigate, held, pending } = await fixture(t);
    const slowModel = models[1];
    const ready = new Promise((resolve) => held.set(slowModel.short_key, resolve));
    const selector = `.mw-related-item[href="${slowModel.detail_path}"]`;
    await page.waitForSelector(selector);
    await expectRequest(slowModel, () => page.click(selector));
    await ready;
    await expectRequest(models[2], () => navigate(models[2]));
    await assertModel(models[2]);
    await Promise.all([
      page.waitForResponse((response) => response.url() === `${origin}/api/models/${slowModel.short_key}`),
      pending.get(slowModel.short_key).respond({ status: 200, contentType: "application/json", body: JSON.stringify(slowModel) }),
    ]);
    await page.waitForNetworkIdle({ idleTime: 100 });
    await assertModel(models[2]);
    const staleCache = await page.evaluate(async (key) => {
      const { getPageCache } = await import("/src/lib/pageCache.js");
      return getPageCache(`model-detail:${key}`);
    }, slowModel.short_key);
    assert.equal(staleCache, null, "过期响应不能写入缓存");
  });
});
