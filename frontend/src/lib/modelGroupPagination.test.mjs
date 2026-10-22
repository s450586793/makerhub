import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("../pages/ModelLibraryGroupPage.vue", import.meta.url), "utf8");

function componentFunction(name, nextName) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(`function ${nextName}(`, start);
  assert.ok(start >= 0 && end > start, `Cannot find ${name}`);
  return source.slice(start, end).replace(/async\s*$/, "");
}

function makeHarness({ items, page = 3, firstPage = 1, pageSize = 8, serverItems, fetchOverride, deleteRequest }) {
  const calls = [];
  const events = [];
  const payload = { value: {
    items,
    page,
    loaded_from_page: firstPage,
    page_size: pageSize,
    count: items.length,
    filtered_total: 40,
    has_more: true,
  } };
  const view = { value: { title: "本地整理" } };
  const loadingMore = { value: false };
  const needsRefresh = { value: false };
  const status = { value: "" };
  const route = { fullPath: "/models/source/local/local-organizer?nav_context=organizer" };
  const fetchPage = async (requestedPage, options) => {
    calls.push(requestedPage);
    if (fetchOverride) return fetchOverride(requestedPage, options);
    const start = (requestedPage - 1) * pageSize;
    return {
      view: view.value,
      items: serverItems.slice(start, start + pageSize),
      page: requestedPage,
      page_size: pageSize,
      filtered_total: serverItems.length,
      source_counts: { all: serverItems.length },
      has_more: start + pageSize < serverItems.length,
    };
  };
  const noop = () => {};
  const create = new Function(
    "payload", "view", "fetchPage", "syncFiltersFromRoute", "rememberGroupList", "nextTick",
    "ensureObserver", "disconnectObserver", "loadingMore", "PAGE_SIZE",
    "loaded", "routePage", "scrollToRouteAnchor", "findModel", "window", "apiRequest", "deleting", "status",
    "captureModelListAnchor", "restoreModelListAnchor", "route", "needsRefresh",
    `let requestToken = 0;
     let groupListAbortController = null;
     ${componentFunction("mergeUniqueModelItems", "routePage")}
     async ${componentFunction("load", "reloadVisiblePages")}
     async ${componentFunction("reloadVisiblePages", "loadMore")}
     async ${componentFunction("loadMore", "disconnectObserver")}
     async ${componentFunction("deleteOne", "restoreOne")}
     return { reload: reloadVisiblePages, load, loadMore, deleteOne, invalidate: () => { requestToken += 1; } };`,
  );
  const anchor = { modelDir: "model-11", top: 340, scrollY: 1500 };
  const methods = create(
    payload, view, fetchPage, noop, noop, async () => {},
    () => events.push("observe"), () => events.push("disconnect"), loadingMore, pageSize,
    { value: true }, () => page, async () => {},
    (modelDir) => payload.value.items.find((item) => item.model_dir === modelDir),
    { confirm: () => true }, async () => {
      events.push("delete-request");
      return deleteRequest ? deleteRequest() : { success: true };
    }, { value: false }, status,
    () => { events.push("capture-anchor"); return anchor; },
    async (value) => { assert.equal(value, anchor); events.push("restore-anchor"); }, route, needsRefresh,
  );
  return { ...methods, payload, calls, events, route, needsRefresh, status };
}

const models = (from, to) => Array.from({ length: to - from + 1 }, (_, index) => ({
  model_dir: `model-${from + index}`,
}));
const dirs = (items) => items.map((item) => item.model_dir);

test("refresh after deleting a model preserves all loaded pages and fills the shifted boundary", async () => {
  const serverItems = models(1, 40).filter((item) => item.model_dir !== "model-10");
  const harness = makeHarness({ items: models(1, 24), serverItems });

  await harness.reload();

  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 24)));
  assert.equal(harness.payload.value.count, 24);
  assert.equal(harness.payload.value.page, 3);
  assert.deepEqual(harness.calls, [1, 2, 3]);
});

test("deleting the only item on the last loaded page moves pagination to the last populated page", async () => {
  const harness = makeHarness({ items: models(1, 17), serverItems: models(1, 16) });

  await harness.reload();

  assert.deepEqual(dirs(harness.payload.value.items), dirs(models(1, 16)));
  assert.equal(harness.payload.value.page, 2);
  assert.equal(harness.payload.value.has_more, false);
  assert.deepEqual(harness.calls, [1, 2]);
});

test("refresh honors the loaded page segment when a list was opened directly on a deep page", async () => {
  const serverItems = models(1, 32).filter((item) => item.model_dir !== "model-20");
  const harness = makeHarness({ items: models(17, 24), firstPage: 3, serverItems });

  await harness.reload();

  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(16, 24)));
  assert.equal(harness.payload.value.loaded_from_page, 3);
  assert.deepEqual(harness.calls, [3]);
});

test("an emptied directly opened last page falls back to the previous populated page", async () => {
  const harness = makeHarness({ items: models(17, 17), firstPage: 3, serverItems: models(1, 16) });

  await harness.reload();

  assert.deepEqual(dirs(harness.payload.value.items), dirs(models(9, 16)));
  assert.equal(harness.payload.value.loaded_from_page, 2);
  assert.equal(harness.payload.value.page, 2);
  assert.equal(harness.payload.value.has_more, false);
  assert.deepEqual(harness.calls, [3, 2]);
});

test("an empty group resets pagination without leaving a phantom next page", async () => {
  const harness = makeHarness({ items: models(1, 1), page: 1, serverItems: [] });

  await harness.reload();

  assert.deepEqual(harness.payload.value.items, []);
  assert.equal(harness.payload.value.count, 0);
  assert.equal(harness.payload.value.page, 1);
  assert.equal(harness.payload.value.loaded_from_page, 1);
  assert.equal(harness.payload.value.has_more, false);
});

test("refresh uses the page size returned by the API instead of assuming eight items", async () => {
  const serverItems = models(1, 12).filter((item) => item.model_dir !== "model-2");
  const harness = makeHarness({ items: models(1, 9), pageSize: 3, serverItems });

  await harness.reload();

  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 9)));
  assert.equal(harness.payload.value.page_size, 3);
  assert.deepEqual(harness.calls, [1, 2, 3]);
});

test("a superseded refresh never commits partial or stale pages", async () => {
  let finish;
  const originalItems = models(1, 24);
  const harness = makeHarness({
    items: originalItems,
    fetchOverride: () => new Promise((resolve) => { finish = resolve; }),
  });

  const pending = harness.reload();
  harness.invalidate();
  finish({ items: models(1, 8), page: 1, page_size: 8, has_more: true, filtered_total: 40 });
  await pending;

  assert.equal(harness.payload.value.items, originalItems);
  assert.equal(harness.payload.value.count, 24);
});

test("a slow load-more response cannot append stale items while a deletion is pending", async () => {
  let finishAppend;
  let finishDelete;
  const originalItems = models(1, 24);
  const serverItems = models(1, 40).filter((item) => item.model_dir !== "model-10");
  const harness = makeHarness({
    items: originalItems,
    serverItems,
    fetchOverride: async (page) => {
      if (page === 4) return new Promise((resolve) => { finishAppend = resolve; });
      return {
        items: serverItems.slice((page - 1) * 8, page * 8),
        page, page_size: 8, filtered_total: serverItems.length, has_more: true,
      };
    },
    deleteRequest: () => new Promise((resolve) => { finishDelete = resolve; }),
  });

  const pendingAppend = harness.load({ append: true });
  const pendingDelete = harness.deleteOne("model-10");
  finishAppend({ items: models(25, 32), page: 4, page_size: 8, has_more: true, filtered_total: 40 });
  await pendingAppend;
  assert.equal(harness.payload.value.items, originalItems);
  finishDelete({ success: true });
  await pendingDelete;

  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 24)));
  assert.equal(harness.payload.value.page, 3);
});

test("deleting restores the adjacent card position after replacing the refreshed pages", async () => {
  const harness = makeHarness({ items: models(1, 24), serverItems: models(1, 40) });

  await harness.deleteOne("model-10");

  assert.ok(harness.events.indexOf("capture-anchor") >= 0);
  assert.ok(harness.events.indexOf("capture-anchor") < harness.events.indexOf("delete-request"));
  assert.ok(harness.events.indexOf("restore-anchor") > harness.events.indexOf("delete-request"));
});

test("a deletion finishing after navigation cannot refresh the next route", async () => {
  let finishDelete;
  const harness = makeHarness({
    items: models(1, 24),
    serverItems: models(1, 40),
    deleteRequest: () => new Promise((resolve) => { finishDelete = resolve; }),
  });

  const pending = harness.deleteOne("model-10");
  harness.route.fullPath = "/models/source/local/another-source";
  finishDelete({ success: true });
  await pending;

  assert.deepEqual(harness.calls, []);
  assert.ok(!harness.events.includes("restore-anchor"));
});

test("an already queued load-more callback cannot supersede an in-flight refresh", async () => {
  let finishFirstPage;
  const serverItems = models(1, 40).filter((item) => item.model_dir !== "model-10");
  const harness = makeHarness({
    items: models(1, 24),
    fetchOverride: async (page) => {
      if (page === 1) return new Promise((resolve) => { finishFirstPage = resolve; });
      return {
        items: serverItems.slice((page - 1) * 8, page * 8),
        page, page_size: 8, filtered_total: serverItems.length, has_more: true,
      };
    },
  });

  const pending = harness.reload();
  await harness.loadMore();
  finishFirstPage({ items: serverItems.slice(0, 8), page: 1, page_size: 8, filtered_total: serverItems.length, has_more: true });
  await pending;

  assert.deepEqual(harness.calls, [1, 2, 3]);
  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 24)));
  assert.equal(harness.payload.value.page, 3);
});

test("load-more waits while a deletion request is pending", async () => {
  let finishDelete;
  const harness = makeHarness({
    items: models(1, 24), serverItems: models(1, 40),
    deleteRequest: () => new Promise((resolve) => { finishDelete = resolve; }),
  });

  const pending = harness.deleteOne("model-10");
  await harness.loadMore();
  assert.deepEqual(harness.calls, []);
  finishDelete({ success: true });
  await pending;
});

test("a failed refresh after successful deletion retries loaded boundaries before appending more", async () => {
  let failPageTwo = true;
  const serverItems = models(1, 40).filter((item) => item.model_dir !== "model-10");
  const harness = makeHarness({
    items: models(1, 24),
    fetchOverride: async (page) => {
      if (page === 2 && failPageTwo) {
        failPageTwo = false;
        throw new Error("fixture timeout");
      }
      return {
        items: serverItems.slice((page - 1) * 8, page * 8),
        page, page_size: 8, filtered_total: serverItems.length, has_more: true,
      };
    },
  });

  await harness.deleteOne("model-10");
  assert.equal(harness.needsRefresh.value, true);
  assert.match(harness.status.value, /已删除.*列表刷新失败/);
  assert.equal(harness.payload.value.page, 3);
  await harness.loadMore();

  assert.deepEqual(harness.calls, [1, 2, 1, 2, 3]);
  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 24)));
  assert.equal(harness.needsRefresh.value, false);
  await harness.loadMore();
  assert.equal(harness.calls.at(-1), 4);
  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 32)));
});

test("a failed initial load releases its controller so cached results can still load more", async () => {
  let failInitialPage = true;
  const serverItems = models(1, 40);
  const harness = makeHarness({
    items: models(1, 24),
    fetchOverride: async (page) => {
      if (page === 3 && failInitialPage) {
        failInitialPage = false;
        throw new Error("fixture timeout");
      }
      return {
        items: serverItems.slice((page - 1) * 8, page * 8),
        page, page_size: 8, filtered_total: serverItems.length, has_more: true,
      };
    },
  });

  await assert.rejects(harness.load(), /fixture timeout/);
  await harness.loadMore();

  assert.deepEqual(harness.calls, [3, 4]);
  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems.slice(0, 32)));
});

test("the last loaded page can retry a failed refresh even when there are no more pages", async () => {
  let failPageTwo = true;
  const serverItems = models(1, 24).filter((item) => item.model_dir !== "model-10");
  const harness = makeHarness({
    items: models(1, 24),
    fetchOverride: async (page) => {
      if (page === 2 && failPageTwo) {
        failPageTwo = false;
        throw new Error("fixture timeout");
      }
      return {
        items: serverItems.slice((page - 1) * 8, page * 8),
        page, page_size: 8, filtered_total: serverItems.length, has_more: page * 8 < serverItems.length,
      };
    },
  });
  harness.payload.value.has_more = false;

  await harness.deleteOne("model-10");
  await harness.loadMore();

  assert.deepEqual(harness.calls, [1, 2, 1, 2, 3]);
  assert.deepEqual(dirs(harness.payload.value.items), dirs(serverItems));
  assert.equal(harness.payload.value.has_more, false);
  assert.equal(harness.needsRefresh.value, false);
});

test("repeated refresh failures leave the existing observer intact without triggering their own retry loop", async () => {
  const harness = makeHarness({
    items: models(1, 24),
    fetchOverride: async () => { throw new Error("fixture offline"); },
  });

  await assert.rejects(harness.reload(), /fixture offline/);
  assert.deepEqual(harness.events, []);
  await harness.loadMore();

  assert.deepEqual(harness.events, []);
  assert.equal(harness.needsRefresh.value, true);
  assert.equal(harness.calls.length, 2);
});
