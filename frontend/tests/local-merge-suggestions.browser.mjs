import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import puppeteer from "puppeteer-core";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../", import.meta.url));
const executablePath = process.env.CHROMIUM_EXECUTABLE;
assert.ok(executablePath, "Set CHROMIUM_EXECUTABLE to a local Chromium executable");
const { version } = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const routePath = "/models/source/local/local-organizer";
const endpoint = "/api/source-library/sources/local/local-organizer";
const screenshotRoot = fileURLToPath(new URL("../../.workflow/.scratchpad/local-merge-suggestions-20261027/", import.meta.url));
const groupA = "fixture:机甲模型 A / 分件";
const groupB = "fixture:发动机模型 B";
const modelKey = (kind, index) => `local${{ A: 1, B: 2, X: 3 }[kind]}${String(index).padStart(3, "0")}`;
const model = (kind, index) => ({
  short_key: modelKey(kind, index),
  model_dir: `LOCAL_${kind}_${String(index).padStart(2, "0")}`,
  title: `整理模型 ${kind} - 分件 ${index}`,
  detail_path: `/models/${modelKey(kind, index)}`,
  source: "local",
  author: { name: "浏览器回归" },
  tags: ["fixture"],
  stats: {},
  local_flags: {},
  gallery: [], instances: [], attachments: [], comments: [],
});
const groups = new Map([
  [groupA, Array.from({ length: 11 }, (_, index) => model("A", index + 1))],
  [groupB, Array.from({ length: 10 }, (_, index) => model("B", index + 1))],
]);
const outside = Array.from({ length: 6 }, (_, index) => model("X", index + 1));
const allModels = [...outside, ...groups.get(groupA), ...groups.get(groupB)];
const suggestions = [...groups].map(([key, items]) => ({
  key, title: key === groupA ? "机甲模型 A" : "发动机模型 B", count: items.length,
  model_dirs: items.map((item) => item.model_dir), items: items.slice(0, 3),
}));

test("local merge suggestions browse the complete group before manual selection", { timeout: 180000 }, async (t) => {
  const profile = await mkdtemp(join(tmpdir(), "makerhub-merge-browser-"));
  const server = await createServer({ root, configFile: `${root}vite.config.js`, server: { host: "127.0.0.1", port: 0 }, logLevel: "error" });
  let browser;
  t.after(async () => {
    await browser?.close();
    await server.close();
    await rm(profile, { recursive: true, force: true });
  });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await puppeteer.launch({ executablePath, userDataDir: profile, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });

  async function fixture(t, query = "nav_context=organizer", { ignoreAbort = false } = {}) {
    const page = await browser.newPage();
    t.after(() => page.close());
    await page.setViewport({ width: 1440, height: 900 });
    page.setDefaultTimeout(8000);
    const errors = [];
    const requests = [];
    const held = [];
    const removed = new Set();
    const mutations = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("dialog", async (dialog) => {
      if (dialog.type() !== "confirm") errors.push(`Unexpected dialog: ${dialog.type()}`);
      await dialog.accept();
    });
    await page.evaluateOnNewDocument(({ ignoreAbort, endpoint }) => {
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
      window.EventSource = class {
        listeners = new Map();
        constructor() { sources.add(this); }
        addEventListener(type, callback) { this.listeners.set(type, callback); }
        close() { sources.delete(this); }
      };
      window.completeArchive = () => {
        for (const source of sources) source.listeners.get("archive.completed")?.({
          type: "archive.completed", data: JSON.stringify({ id: Date.now(), type: "archive.completed", scope: "archive_queue", payload: {} }),
        });
      };
      window.hasArchiveListener = () => [...sources].some((source) => source.listeners.has("archive.completed"));
      // 模拟取消后依然送达的旧响应，验证请求身份保护也能阻止旧数据写入。
      if (ignoreAbort) {
        const fetch = window.fetch.bind(window);
        window.fetch = (input, options = {}) => String(input).includes(endpoint)
          ? fetch(input, { ...options, signal: undefined }) : fetch(input, options);
      }
    }, { ignoreAbort, endpoint });
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
      } else if (url.pathname === endpoint) {
        const mergeGroup = url.searchParams.get("merge_group") || "";
        const pageNumber = Number(url.searchParams.get("page") || 1);
        const size = Number(url.searchParams.get("page_size") || 8);
        requests.push({ mergeGroup, page: pageNumber, url: url.toString() });
        const items = (mergeGroup ? groups.get(mergeGroup) || [] : allModels).filter((item) => !removed.has(item.model_dir));
        const start = (pageNumber - 1) * size;
        const availableSuggestions = suggestions.map((suggestion) => {
          const items = groups.get(suggestion.key).filter((item) => !removed.has(item.model_dir));
          return { ...suggestion, count: items.length, model_dirs: items.map((item) => item.model_dir), items: items.slice(0, 3) };
        });
        const active = availableSuggestions.find((suggestion) => suggestion.key === mergeGroup);
        payload = {
          view: { title: "本地整理", subtitle: "本地导入模型", stats: [] },
          items: items.slice(start, start + size), page: pageNumber, page_size: size,
          count: Math.min(size, items.length - start), total: allModels.length, filtered_total: items.length,
          has_more: start + size < items.length, tags: ["fixture"],
          source_counts: { all: allModels.length, local: allModels.length, cn: 0, global: 0 },
          filters: { q: url.searchParams.get("q") || "", tag: url.searchParams.get("tag") || "", source: url.searchParams.get("source") || "all", sort: url.searchParams.get("sort") || "collectDate", merge_group: mergeGroup },
          merge_suggestions: availableSuggestions,
          active_merge_group: active ? { key: active.key, title: active.title, count: active.count } : null,
        };
        const wait = held.find((entry) => !entry.request && entry.group === mergeGroup && entry.page === pageNumber);
        if (wait) {
          wait.request = request;
          wait.payload = payload;
          wait.resolve();
          return;
        }
      } else if (url.pathname === `${endpoint}/tags`) {
        payload = { items: ["fixture"], has_more: false };
      } else if (url.pathname === "/api/models/delete") {
        const body = JSON.parse(request.postData());
        mutations.push({ path: url.pathname, body });
        body.model_dirs.forEach((dir) => removed.add(dir));
        payload = { success: true, message: "测试模型已隐藏" };
      } else if (url.pathname === "/api/local-library/merge") {
        const body = JSON.parse(request.postData());
        mutations.push({ method: request.method(), path: url.pathname, body });
        body.source_model_dirs.forEach((dir) => removed.add(dir));
        payload = { success: true, message: "测试模型已合并" };
      } else if (url.pathname.startsWith("/api/models/")) {
        const key = decodeURIComponent(url.pathname.slice("/api/models/".length));
        payload = allModels.find((item) => item.short_key === key || item.model_dir === key) || { items: [] };
      } else {
        errors.push(`Unexpected API request: ${url.pathname}`);
      }
      await request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
    });
    await page.goto(`${origin}${routePath}?${query}`);
    await page.waitForSelector(".local-merge-suggestion");
    await page.waitForFunction(() => window.hasArchiveListener());
    t.after(() => assert.deepEqual(errors, []));
    const ids = () => page.$$eval(".model-grid [data-model-dir]", (nodes) => nodes.map((node) => node.dataset.modelDir));
    const selectChip = async (key) => {
      const index = suggestions.findIndex((suggestion) => suggestion.key === key);
      await page.click(`.local-merge-suggestion:nth-of-type(${index + 1})`);
    };
    const clickButton = async (text, scope = ".library-group-hero") => {
      for (const button of await page.$$(`${scope} button`)) {
        if (await button.evaluate((node, text) => node.textContent.trim() === text, text)) {
          await button.click();
          return;
        }
      }
      assert.fail(`找不到按钮：${text}`);
    };
    const assertItems = async (expected) => {
      const expectedIds = expected.map((item) => item.model_dir);
      await page.waitForFunction((expected) => {
        const actual = [...document.querySelectorAll(".model-grid [data-model-dir]")].map((node) => node.dataset.modelDir);
        return JSON.stringify(actual) === JSON.stringify(expected);
      }, {}, expectedIds);
      assert.deepEqual(await ids(), expectedIds);
    };
    const assertUnselected = async () => {
      assert.equal(await page.$$eval(".gallery-card.is-selected", (nodes) => nodes.length), 0, "进入或退出分组必须清空原选择");
      assert.equal(await page.$$eval(".filter-actions button", (nodes) => nodes.some((node) => /^(合并|分享)\s+[1-9]/.test(node.textContent.trim()))), false, "不能保留已卸载卡片的隐藏选择");
    };
    const selectionMode = async () => {
      if (!(await page.$(".gallery-card__select-toggle"))) await clickButton("选择");
      await page.waitForSelector(".gallery-card__select-toggle");
    };
    const assertSelected = async (expected) => {
      const actual = await page.$$eval(".gallery-card.is-selected", (nodes) => nodes.map((node) => node.dataset.modelDir));
      assert.deepEqual(actual, expected.map((item) => item.model_dir));
      assert.equal(await page.$$eval(".filter-actions button", (nodes, count) => nodes.some((node) => node.textContent.trim() === `分享 ${count}`), expected.length), true);
    };
    const append = async (expected) => {
      await page.evaluate(() => window.loadModelPage());
      await assertItems(expected);
    };
    const navigate = (target) => page.evaluate(async (target) => {
      const { default: router } = await import("/src/router.js");
      await router.push(target);
    }, target);
    const holdNext = (group, pageNumber = 1) => {
      const wait = { group, page: pageNumber };
      wait.ready = new Promise((resolve) => { wait.resolve = resolve; });
      held.push(wait);
      return {
        ready: wait.ready,
        release: async () => {
          await wait.ready;
          await wait.request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(wait.payload) });
          await page.waitForNetworkIdle({ idleTime: 100 });
        },
      };
    };
    return { page, requests, ids, selectChip, clickButton, assertItems, assertUnselected, selectionMode, assertSelected, append, navigate, holdNext, mutations, remove: (dir) => removed.add(dir) };
  }

  await t.test("deleting a selected group card cannot retain hidden merge selections", async (t) => {
    const { page, mutations, assertItems, assertSelected, assertUnselected } = await fixture(t, new URLSearchParams({ nav_context: "organizer", merge_group: groupA }).toString());
    await assertItems(groups.get(groupA).slice(0, 8));
    for (const item of groups.get(groupA).slice(0, 2)) await page.click(`[data-model-dir="${item.model_dir}"] .gallery-card__select-toggle`);
    await assertSelected(groups.get(groupA).slice(0, 2));
    await page.click(`[data-model-dir="${groups.get(groupA)[0].model_dir}"] .gallery-delete`);
    await assertItems(groups.get(groupA).slice(1, 9));
    assert.deepEqual(mutations, [{ path: "/api/models/delete", body: { model_dirs: [groups.get(groupA)[0].model_dir] } }]);
    await assertUnselected();
  });

  await t.test("a same-route membership refresh clears old selections and closes an outdated merge dialog", async (t) => {
    const { page, remove, clickButton, assertItems, assertUnselected } = await fixture(t, new URLSearchParams({ nav_context: "organizer", merge_group: groupA }).toString());
    for (const item of groups.get(groupA).slice(0, 2)) await page.click(`[data-model-dir="${item.model_dir}"] .gallery-card__select-toggle`);
    await clickButton("合并 2");
    await page.waitForSelector(".local-merge-dialog__panel");
    remove(groups.get(groupA)[0].model_dir);
    await page.evaluate(() => window.completeArchive());
    await assertItems(groups.get(groupA).slice(1, 9));
    await assertUnselected();
    assert.equal(await page.$(".local-merge-dialog__panel"), null, "成员变化后不能保留旧合并确认框");
  });

  await t.test("aborting an older group request cannot write an error into the next group", async (t) => {
    const { page, selectChip, holdNext, assertItems } = await fixture(t);
    const old = holdNext(groupA);
    await selectChip(groupA);
    await old.ready;
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
    await page.waitForNetworkIdle({ idleTime: 100 });
    const status = await page.$$eval(".form-status", (nodes) => nodes.map((node) => node.textContent).join(" "));
    assert.doesNotMatch(status, /aborted|aborterror|signal is aborted|取消了请求/i, "旧请求的取消异常不能污染新组状态");
  });

  await t.test("a new uncached group can paginate while the previous group append is still pending", async (t) => {
    const { page, selectChip, holdNext, assertItems, append } = await fixture(t, new URLSearchParams({ nav_context: "organizer", merge_group: groupA }).toString());
    const old = holdNext(groupA, 2);
    await page.evaluate(() => window.loadModelPage());
    await old.ready;
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
    try {
      await append(groups.get(groupB));
    } finally {
      await old.release();
    }
    await assertItems(groups.get(groupB));
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupB);
  });

  await t.test("browsing a suggestion selects nothing and a manual merge submits only the two chosen models", async (t) => {
    const { page, requests, mutations, ids, selectChip, clickButton, assertSelected, assertItems, assertUnselected } = await fixture(t);
    const initial = await ids();
    assert.ok(!initial.includes(groups.get(groupA)[8].model_dir), "组内后续成员必须原本没有加载");
    await selectChip(groupA);
    await page.waitForNetworkIdle({ idleTime: 100 });
    assert.equal(await page.$$eval(".gallery-card.is-selected", (nodes) => nodes.length), 0, "点击分组只能查看，不能自动勾选任何模型");
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA, "分组上下文必须进入 URL");
    assert.equal(requests.at(-1).mergeGroup, groupA, "必须向服务端请求完整组，不能只过滤已加载卡片");
    assert.equal(requests.at(-1).page, 1);
    assert.deepEqual(await ids(), groups.get(groupA).slice(0, 8).map((item) => item.model_dir));
    assert.match(await page.$eval(".local-merge-filter", (node) => node.textContent), /机甲模型 A/);
    assert.equal(await page.$eval('.local-merge-suggestion[aria-pressed="true"]', (node) => node.textContent.includes("机甲模型 A")), true);
    assert.deepEqual(mutations, [], "点击分组只浏览，不能触发任何删除或合并写入");
    const selected = [groups.get(groupA)[1], groups.get(groupA)[5]];
    for (const item of selected) await page.click(`[data-model-dir="${item.model_dir}"] .gallery-card__select-toggle`);
    await assertSelected(selected);
    await clickButton("合并 2");
    await page.waitForSelector(".local-merge-dialog__panel");
    assert.equal(await page.$$eval(".local-merge-dialog__list li", (nodes) => nodes.length), 2);
    const [targetSelect, coverSelect] = await page.$$(".local-merge-dialog__form select");
    assert.deepEqual(await targetSelect.$$eval("option", (nodes) => nodes.map((node) => node.value)), selected.map((item) => item.model_dir));
    await targetSelect.select(selected[1].model_dir);
    await coverSelect.select(selected[0].model_dir);
    const title = await page.$eval('.local-merge-dialog__form input[type="text"]', (node) => node.value);
    await clickButton("确认合并", ".local-merge-dialog__panel");
    await assertItems(groups.get(groupA).filter((item) => item !== selected[0]).slice(0, 8));
    assert.deepEqual(mutations, [{
      method: "POST",
      path: "/api/local-library/merge",
      body: {
        target_model_dir: selected[1].model_dir,
        source_model_dirs: [selected[0].model_dir],
        title,
        cover_from_model_dir: selected[0].model_dir,
      },
    }]);
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    assert.equal(requests.at(-1).mergeGroup, groupA, "合并后的重载仍须限定当前组");
    await assertUnselected();
    assert.equal(await page.$(".local-merge-dialog__panel"), null);
  });

  await t.test("group pagination includes previously unloaded members and select-all only selects loaded group cards", async (t) => {
    const { page, requests, selectChip, clickButton, assertItems, assertUnselected, selectionMode, assertSelected, append } = await fixture(t);
    await selectionMode();
    await page.click(".gallery-card__select-toggle");
    await assertSelected([outside[0]]);
    await selectChip(groupA);
    await assertItems(groups.get(groupA).slice(0, 8));
    await assertUnselected();
    await selectionMode();
    await page.click(".gallery-card__select-toggle");
    await assertSelected(groups.get(groupA).slice(0, 1));
    const requestCount = requests.length;
    await selectChip(groupA);
    await assertSelected(groups.get(groupA).slice(0, 1));
    assert.equal(requests.length, requestCount, "重复点击当前组不应刷新或清空手动选择");
    await clickButton("全选当前");
    await assertSelected(groups.get(groupA).slice(0, 8));
    await append(groups.get(groupA));
    assert.equal(requests.at(-1).mergeGroup, groupA);
    assert.equal(requests.at(-1).page, 2);
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    await selectionMode();
    await clickButton("全选当前");
    await assertSelected(groups.get(groupA));
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
    await assertUnselected();
    assert.equal(await page.$$eval(".local-merge-suggestion", (nodes) => nodes.length), 2, "分组结果仍须保留完整建议，供继续切换");
    await selectionMode();
    await page.click(".gallery-card__select-toggle");
    await clickButton("返回全部", ".local-merge-filter");
    await assertItems(allModels.slice(0, 8));
    await assertUnselected();
    assert.equal(new URL(page.url()).searchParams.has("merge_group"), false);
  });

  await t.test("entering a group resets page while return-all preserves filters and reset clears everything", async (t) => {
    const query = new URLSearchParams({ nav_context: "organizer", q: "整理", source: "local", tag: "fixture", sort: "likes", page: "2" });
    const { page, requests, selectChip, clickButton, assertItems, assertUnselected, selectionMode } = await fixture(t, query.toString());
    await selectChip(groupA);
    await assertItems(groups.get(groupA).slice(0, 8));
    assert.equal(requests.at(-1).page, 1);
    assert.ok(!new URL(page.url()).searchParams.has("page") || new URL(page.url()).searchParams.get("page") === "1");
    await selectionMode();
    await page.click(".gallery-card__select-toggle");
    await clickButton("返回全部", ".local-merge-filter");
    await assertItems(allModels.slice(0, 8));
    await assertUnselected();
    for (const [key, value] of query) {
      if (key !== "page") assert.equal(new URL(page.url()).searchParams.get(key), value, `返回全部应保留 ${key}`);
    }
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
    await selectionMode();
    await page.click(".gallery-card__select-toggle");
    await clickButton("重置");
    await assertItems(allModels.slice(0, 8));
    await assertUnselected();
    const reset = new URL(page.url());
    for (const key of ["q", "tag", "source", "sort", "merge_group", "page"]) assert.equal(reset.searchParams.has(key), false, `重置应清除 ${key}`);
    assert.equal(reset.searchParams.get("nav_context"), "organizer");
  });

  await t.test("group deep links, reload, detail return and browser history retain their context", async (t) => {
    const query = new URLSearchParams({ nav_context: "organizer", merge_group: groupA });
    const { page, requests, clickButton, assertItems, assertUnselected, append } = await fixture(t, query.toString());
    await assertItems(groups.get(groupA).slice(0, 8));
    await append(groups.get(groupA));
    const target = groups.get(groupA)[8];
    if (await page.$(".gallery-card__select-toggle")) await clickButton("取消选择");
    await page.click(`[data-model-dir="${target.model_dir}"] .gallery-card__title`);
    await page.waitForSelector(".mw-head h1");
    await page.waitForNetworkIdle({ idleTime: 100 });
    const detailUrl = new URL(page.url());
    assert.equal(detailUrl.pathname, target.detail_path);
    const returnTo = new URL(await page.$eval(".mw-head__back", (node) => node.href), origin);
    assert.equal(returnTo.searchParams.get("merge_group"), groupA);
    assert.equal(returnTo.searchParams.get("page"), "2");
    const requestStart = requests.length;
    // 列表滚动会保留短暂详情滚动位置；先把返回箭头移出固定顶栏的遮挡区。
    await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: "instant" }));
    await page.waitForFunction(() => {
      const node = document.querySelector(".mw-head__back");
      if (!node) return false;
      const rect = node.getBoundingClientRect();
      return node.contains(document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2));
    });
    const clickGeometry = await page.$eval(".mw-head__back", (node) => {
      const rect = node.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      return { rect: rect.toJSON(), scrollY: window.scrollY, hit: document.elementFromPoint(x, y)?.outerHTML.slice(0, 500) };
    });
    await page.click(".mw-head__back");
    await page.waitForSelector(".local-merge-filter").catch(async (error) => {
      error.message += `; geometry=${JSON.stringify(clickGeometry)}; return URL=${page.url()}; body=${await page.$eval("body", (node) => node.textContent.slice(-1600))}; requests=${JSON.stringify(requests.slice(requestStart))}`;
      throw error;
    });
    await page.waitForNetworkIdle({ idleTime: 100 });
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    assert.ok((await page.$$eval(".model-grid [data-model-dir]", (nodes) => nodes.map((node) => node.dataset.modelDir))).every((id) => groups.get(groupA).some((item) => item.model_dir === id)));
    assert.ok(requests.slice(requestStart).every((request) => request.mergeGroup === groupA), "详情返回后的重验证也必须限定当前组");
    await assertUnselected();
    await page.goBack();
    await page.waitForSelector(".mw-head h1");
    await page.goForward();
    await page.waitForSelector(".local-merge-filter");
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    const refreshStart = requests.length;
    await page.reload();
    await assertItems(groups.get(groupA).slice(8));
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    assert.ok(requests.slice(refreshStart).length > 0 && requests.slice(refreshStart).every((request) => request.mergeGroup === groupA));
  });

  await t.test("expired group links keep an empty group and a usable return-all action", async (t) => {
    const key = "expired-group-key";
    const { page, requests, clickButton, assertItems } = await fixture(t, new URLSearchParams({ nav_context: "organizer", merge_group: key }).toString());
    await page.waitForSelector(".empty-state");
    assert.equal((await page.$$(".model-grid .gallery-card")).length, 0);
    assert.equal(requests.at(-1).mergeGroup, key);
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), key);
    await clickButton("返回全部", ".local-merge-filter");
    await assertItems(allModels.slice(0, 8));
    assert.equal(new URL(page.url()).searchParams.has("merge_group"), false);
  });

  await t.test("cache is isolated by group and stale responses cannot replace another group or all models", async (t) => {
    const { page, selectChip, clickButton, assertItems, assertUnselected, holdNext, navigate } = await fixture(t, "nav_context=organizer", { ignoreAbort: true });
    await selectChip(groupA);
    await assertItems(groups.get(groupA).slice(0, 8));
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
    const cached = holdNext(groupA);
    await selectChip(groupA);
    await cached.ready;
    await assertItems(groups.get(groupA).slice(0, 8));
    await assertUnselected();
    await cached.release();
    await assertItems(groups.get(groupA).slice(0, 8));
    const stale = holdNext(groupB);
    await selectChip(groupB);
    await stale.ready;
    await navigate(`${routePath}?${new URLSearchParams({ nav_context: "organizer", merge_group: groupA })}`);
    await assertItems(groups.get(groupA).slice(0, 8));
    await stale.release();
    await assertItems(groups.get(groupA).slice(0, 8));
    assert.equal(new URL(page.url()).searchParams.get("merge_group"), groupA);
    const staleAppend = holdNext(groupA, 2);
    await page.evaluate(() => window.loadModelPage());
    await staleAppend.ready;
    await clickButton("返回全部", ".local-merge-filter");
    await assertItems(allModels.slice(0, 8));
    await staleAppend.release();
    await assertItems(allModels.slice(0, 8));
    assert.equal(new URL(page.url()).searchParams.has("merge_group"), false);
    // 再进入 B，确认此前过期响应也没有污染该组自己的缓存。
    await selectChip(groupB);
    await assertItems(groups.get(groupB).slice(0, 8));
  });

  await t.test("the active group works in dark and light themes without horizontal overflow at 375px", async (t) => {
    const { page, selectChip, assertItems } = await fixture(t);
    await selectChip(groupA);
    await assertItems(groups.get(groupA).slice(0, 8));
    await page.setViewport({ width: 375, height: 812 });
    await mkdir(screenshotRoot, { recursive: true });
    for (const theme of ["dark", "light"]) {
      await page.evaluate(async (theme) => {
        const { applyTheme } = await import("/src/lib/theme.js");
        applyTheme(theme);
        window.scrollTo(0, 0);
      }, theme);
      await page.waitForFunction((theme) => document.documentElement.dataset.theme === theme, {}, theme);
      const geometry = await page.evaluate(() => {
        const filter = document.querySelector(".local-merge-filter");
        const bounds = filter.getBoundingClientRect();
        const style = getComputedStyle(filter);
        return { viewport: window.innerWidth, width: document.documentElement.scrollWidth, left: bounds.left, right: bounds.right, color: style.color, background: style.backgroundColor };
      });
      assert.ok(geometry.width <= geometry.viewport + 1, `${theme} 模式存在横向溢出：${JSON.stringify(geometry)}`);
      assert.ok(geometry.left >= -1 && geometry.right <= 376, "当前组提示条应在移动端视口内");
      assert.notEqual(geometry.color, geometry.background, "当前组文字与背景必须有可见差异");
      await page.screenshot({ path: join(screenshotRoot, `merge-group-${theme}-375.png`) });
    }
  });
});
