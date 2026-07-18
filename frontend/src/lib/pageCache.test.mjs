import assert from "node:assert/strict";
import { test } from "node:test";

import {
  getPageCache,
  resetPageCacheForTests,
  setPageCache,
} from "./pageCache.js";

test("page cache evicts least recently used entries at its capacity", () => {
  assert.equal(typeof resetPageCacheForTests, "function");
  resetPageCacheForTests();
  for (let index = 0; index < 33; index += 1) {
    setPageCache(`item-${index}`, { index });
  }

  assert.equal(getPageCache("item-0"), null);
  assert.deepEqual(getPageCache("item-32"), { index: 32 });
});
