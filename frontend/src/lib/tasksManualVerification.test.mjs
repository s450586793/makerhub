import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");

test("missing 3MF verification action is an external link", () => {
  assert.match(source, /href="missingVerificationHref\(item\)"/);
  assert.match(source, />\s*访问源页面\s*</);
  assert.doesNotMatch(source, /api\/browser-verification\/sessions/);
  assert.doesNotMatch(source, /startBrowserVerification/);
  assert.doesNotMatch(source, /browserVerificationPath/);
  assert.doesNotMatch(source, /browserVerificationWindow/);
});

test("task page can infer a MakerWorld homepage when model URL is missing", () => {
  assert.match(source, /function missingVerificationHref\(item\)/);
  assert.match(source, /https:\/\/makerworld\.com\.cn/);
  assert.match(source, /https:\/\/makerworld\.com/);
});
