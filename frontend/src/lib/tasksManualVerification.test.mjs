import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");

test("missing 3MF verification action is an external link", () => {
  const routeToken = ["browser", "verification"].join("-");
  const startToken = ["start", "Browser", "Verification"].join("");
  const pathToken = ["browser", "Verification", "Path"].join("");
  const windowToken = ["browser", "Verification", "Window"].join("");

  assert.match(source, /href="missingVerificationHref\(item\)"/);
  assert.match(source, />\s*访问源页面\s*</);
  assert.equal(source.includes(["api", routeToken, "sessions"].join("/")), false);
  assert.equal(source.includes(startToken), false);
  assert.equal(source.includes(pathToken), false);
  assert.equal(source.includes(windowToken), false);
});

test("task page can infer a MakerWorld homepage when model URL is missing", () => {
  assert.match(source, /function missingVerificationHref\(item\)/);
  assert.match(source, /https:\/\/makerworld\.com\.cn/);
  assert.match(source, /https:\/\/makerworld\.com/);
});

test("task page distinguishes Cloudflare from manual verification status", () => {
  assert.match(source, /normalized === "verification_required"\) return "需要验证"/);
  assert.match(source, /normalized === "cloudflare"\) return "Cloudflare 校验"/);
});
