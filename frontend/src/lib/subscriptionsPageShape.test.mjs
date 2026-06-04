import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { normalizeSubscriptionsPayload } from "./subscriptions.js";

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
        page_size: 24,
        has_more: true,
      },
    ],
  });

  assert.equal(payload.sections[0].total, 25);
  assert.equal(payload.sections[0].page, 1);
  assert.equal(payload.sections[0].page_size, 24);
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
        page_size: "24",
        has_more: 1,
      },
    ],
  });

  assert.deepEqual(payload.sections[0].items, []);
  assert.equal(payload.sections[0].count, 0);
  assert.equal(payload.sections[0].total, 7);
  assert.equal(payload.sections[0].page, 2);
  assert.equal(payload.sections[0].page_size, 24);
  assert.equal(payload.sections[0].has_more, true);
});

test("subscriptions page requests paged subscription payloads and exposes load more", () => {
  assert.match(pageSource, /PAGE_SIZE\s*=\s*24/);
  assert.match(pageSource, /routePage\(/);
  assert.match(pageSource, /page_size/);
  assert.match(pageSource, /loadMoreSubscriptionSources/);
  assert.match(pageSource, />\s*加载更多\s*</);
  assert.match(pageSource, /全选(?:当前)?已加载/);
});
