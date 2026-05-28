import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildModelDetailRoute,
  getStoredModelReturnState,
  inferModelReturnContext,
  storeModelReturnState,
} from "./modelNavigation.js";

test("model detail route omits return query parameters", () => {
  assert.deepEqual(
    buildModelDetailRoute("/models/mwcn2553383", {
      returnTo: "/models?page=2&anchor=MW_2553383_Long_Title",
      returnLabel: "返回",
      returnContext: "subscriptions",
    }),
    {
      path: "/models/mwcn2553383",
    },
  );
});

test("return state is stored per short detail path", () => {
  const state = new Map();
  const storage = {
    getItem: (key) => state.get(key) || null,
    setItem: (key, value) => state.set(key, value),
  };

  storeModelReturnState(storage, "/models/mwcn2553383", {
    returnTo: "/models?page=2&anchor=MW_2553383_Long_Title",
    returnContext: "subscriptions",
  });

  assert.deepEqual(getStoredModelReturnState(storage, "/models/mwcn2553383"), {
    returnTo: "/models?page=2&anchor=MW_2553383_Long_Title",
    returnContext: "subscriptions",
  });
});

test("return context is inferred from source library paths", () => {
  assert.equal(inferModelReturnContext("/models/source/author/some-key"), "subscriptions");
  assert.equal(inferModelReturnContext("/models/source/local/local-organizer"), "organizer");
  assert.equal(inferModelReturnContext("/models/state/local"), "organizer");
});

test("external return paths are ignored", () => {
  const state = new Map();
  const storage = {
    getItem: (key) => state.get(key) || null,
    setItem: (key, value) => state.set(key, value),
  };

  assert.equal(storeModelReturnState(storage, "/models/mwg42", {
    returnTo: "https://makerworld.com",
  }), false);
  assert.deepEqual(getStoredModelReturnState(storage, "/models/mwg42"), {});
});
