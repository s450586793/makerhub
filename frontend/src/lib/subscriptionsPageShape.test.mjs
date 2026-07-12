import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  mergeSubscriptionSourcesForLightRefresh,
  normalizeSubscriptionsPayload,
} from "./subscriptions.js";

const pageSource = readFileSync(new URL("../pages/SubscriptionsPage.vue", import.meta.url), "utf8");

test("subscription payload normalization preserves section pagination metadata", () => {
  const payload = normalizeSubscriptionsPayload({
    sections: [
      {
        key: "subscription_sources",
        items: [{ key: "source-1" }],
        count: 1,
        total: 25,
        page: 1,
        page_size: 8,
        has_more: true,
      },
    ],
  });

  assert.equal(payload.sections[0].total, 25);
  assert.equal(payload.sections[0].page, 1);
  assert.equal(payload.sections[0].page_size, 8);
  assert.equal(payload.sections[0].has_more, true);
});

test("subscription payload normalization repairs malformed section paging fields", () => {
  const payload = normalizeSubscriptionsPayload({
    sections: [
      {
        key: "subscription_sources",
        items: null,
        total: "7",
        page: "2",
        page_size: "8",
        has_more: 1,
      },
    ],
  });

  assert.deepEqual(payload.sections[0].items, []);
  assert.equal(payload.sections[0].count, 0);
  assert.equal(payload.sections[0].total, 7);
  assert.equal(payload.sections[0].page, 2);
  assert.equal(payload.sections[0].page_size, 8);
  assert.equal(payload.sections[0].has_more, true);
});

test("light subscription refresh preserves existing full card visuals", () => {
  const fullCard = {
    key: "author:mw:alice",
    title: "Alice",
    preview_models: [
      { model_dir: "model-a", title: "Model A", cover_url: "/archive/model-a/cover.webp" },
    ],
    preview_snapshot_url: "/api/source-library/snapshots/alice.webp?v=full",
    model_dirs: ["model-a"],
    model_count: 1,
    stats: [{ label: "模型", value: 1 }],
    recent_summary: "最近归档 Model A",
  };
  const currentSection = {
    key: "subscription_sources",
    items: [fullCard],
    count: 1,
    total: 1,
    page: 1,
    page_size: 8,
    has_more: false,
  };
  const lightSection = {
    key: "subscription_sources",
    items: [
      {
        key: "author:mw:alice",
        title: "Alice",
        preview_models: [],
        preview_snapshot_url: "",
        model_dirs: [],
        model_count: 0,
        stats: [{ label: "模型", value: 0 }],
        recent_summary: "",
      },
    ],
    count: 1,
    total: 1,
    page: 1,
    page_size: 8,
    has_more: false,
  };

  const merged = mergeSubscriptionSourcesForLightRefresh(currentSection, lightSection);

  assert.equal(merged.items[0].preview_snapshot_url, "/api/source-library/snapshots/alice.webp?v=full");
  assert.deepEqual(merged.items[0].preview_models, fullCard.preview_models);
  assert.deepEqual(merged.items[0].model_dirs, ["model-a"]);
  assert.deepEqual(merged.items[0].stats, fullCard.stats);
  assert.equal(merged.items[0].recent_summary, "最近归档 Model A");
});

test("light subscription refresh still shows newly discovered cards", () => {
  const merged = mergeSubscriptionSourcesForLightRefresh(
    {
      key: "subscription_sources",
      items: [{ key: "author:mw:alice", preview_snapshot_url: "/snapshot/alice.webp" }],
    },
    {
      key: "subscription_sources",
      items: [
        { key: "author:mw:alice", preview_snapshot_url: "" },
        { key: "collection:mw:new", title: "New Collection", preview_snapshot_url: "" },
      ],
      total: 2,
      page: 1,
      page_size: 8,
      has_more: false,
    },
  );

  assert.equal(merged.items.length, 2);
  assert.equal(merged.items[0].preview_snapshot_url, "/snapshot/alice.webp");
  assert.equal(merged.items[1].title, "New Collection");
});

test("subscriptions page requests eight-card pages and auto-loads more", () => {
  assert.match(pageSource, /PAGE_SIZE\s*=\s*8/);
  assert.match(pageSource, /routePage\(/);
  assert.match(pageSource, /page_size/);
  assert.match(pageSource, /loadMoreSubscriptionSources/);
  assert.match(pageSource, /IntersectionObserver/);
  assert.match(pageSource, /下拉到底自动加载下一页/);
  assert.match(pageSource, /正在加载更多订阅来源/);
  assert.match(pageSource, /createAutoLoadObserver/);
  assert.match(pageSource, /triggerRef:\s*loadMoreTrigger/);
  assert.match(pageSource, /load:\s*loadMoreSubscriptionSources/);
  assert.match(pageSource, />\s*加载更多\s*</);
  assert.match(pageSource, /全选(?:当前)?已加载/);
});

test("subscriptions page leaves initial loading when the first request fails", () => {
  assert.match(pageSource, /initialLoadFailed\s*=\s*ref\(false\)/);
  assert.match(pageSource, /initialLoadFailed\.value\s*=\s*true/);
  assert.match(pageSource, /v-else-if="initialLoadFailed"/);
  assert.match(pageSource, /重试/);
});

test("subscriptions page accepts the light projection as the final first response", () => {
  assert.doesNotMatch(pageSource, /shouldDeferLightSubscriptionCards|refreshFullSubscriptions/);
  assert.match(pageSource, /if \(!initialLoaded\.value && currentToken === requestToken\) \{\s*initialLoadFailed\.value = true;/s);
});
