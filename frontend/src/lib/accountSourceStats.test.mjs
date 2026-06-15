import assert from "node:assert/strict";
import { test } from "node:test";

import { accountSyncedSourceCounts } from "./accountSourceStats.js";

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
