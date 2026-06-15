import assert from "node:assert/strict";
import { test } from "node:test";

import { accountSourceOverview, accountSyncedSourceCounts } from "./accountSourceStats.js";

test("account synced author count comes from current subscriptions, not stale sync state", () => {
  const followedAuthors = Array.from({ length: 36 }, (_, index) => ({
    uid: String(1000 + index),
    handle: `maker${index}`,
    url: `https://makerworld.com.cn/zh/@maker${index}/upload`,
  }));
  const subscriptions = followedAuthors.slice(0, 25).map((author, index) => ({
    id: `sub-${index}`,
    url: author.url,
    mode: "author_upload",
    enabled: true,
  }));

  const counts = accountSyncedSourceCounts(
    { followed_authors: followedAuthors, imported_sources: [] },
    subscriptions,
    "cn",
  );

  assert.equal(counts.followedAuthors, 25);
});

test("account synced collection count is zero when account reports collections but no URLs were discovered", () => {
  const counts = accountSyncedSourceCounts(
    { followed_collections: [], imported_sources: [] },
    [
      {
        id: "default-favorites",
        url: "https://makerworld.com.cn/zh/@s450586793/collections/models",
        mode: "collection_models",
      },
    ],
    "cn",
  );

  assert.equal(counts.followedCollections, 0);
});

test("account synced default favorite count matches saved default favorite subscription", () => {
  const counts = accountSyncedSourceCounts(
    {
      default_favorites: {
        url: "https://makerworld.com.cn/zh/@s450586793/collections/models",
      },
    },
    [
      {
        id: "default-favorites",
        url: "https://makerworld.com.cn/zh/@s450586793/collections/models",
        mode: "collection_models",
      },
    ],
    "cn",
  );

  assert.equal(counts.defaultFavorites, 1);
});

test("account synced counts fall back to current platform subscriptions when inventory lists are stale", () => {
  const counts = accountSyncedSourceCounts(
    { followed_authors: [], followed_collections: [], imported_sources: [] },
    [
      {
        id: "cn-author",
        url: "https://makerworld.com.cn/zh/@maker/upload",
        mode: "author_upload",
      },
      {
        id: "global-author",
        url: "https://makerworld.com/zh/@globalmaker/upload",
        mode: "author_upload",
      },
      {
        id: "cn-default",
        url: "https://makerworld.com.cn/zh/@s450586793/collections/models",
        mode: "collection_models",
      },
    ],
    "cn",
  );

  assert.equal(counts.followedAuthors, 1);
  assert.equal(counts.followedCollections, 0);
  assert.equal(counts.defaultFavorites, 1);
});

test("account synced default favorites fallback ignores followed collection subscriptions", () => {
  const counts = accountSyncedSourceCounts(
    { followed_authors: [], followed_collections: [], imported_sources: [] },
    [
      {
        id: "cn-collection",
        url: "https://makerworld.com.cn/zh/collections/518732-test",
        mode: "collection_models",
      },
    ],
    "cn",
  );

  assert.equal(counts.defaultFavorites, 0);
  assert.equal(counts.followedCollections, 0);
});

test("account source overview separates account synced sources from full subscription library total", () => {
  const accountItems = [
    { sourceTotal: 39, sourceSyncedTotal: 38, sourcePendingTotal: 1 },
    { sourceTotal: 10, sourceSyncedTotal: 10, sourcePendingTotal: 0 },
  ];
  const subscriptions = Array.from({ length: 75 }, (_, index) => ({
    id: `sub-${index}`,
    url: `https://makerworld.com.cn/zh/@maker${index}/upload`,
    mode: "author_upload",
  }));

  const overview = accountSourceOverview(accountItems, subscriptions);

  assert.equal(overview.subscriptionTotal, 75);
  assert.equal(overview.accountReportedTotal, 49);
  assert.equal(overview.accountSyncedTotal, 48);
  assert.equal(overview.otherTotal, 27);
  assert.equal(overview.pendingTotal, 1);
});
