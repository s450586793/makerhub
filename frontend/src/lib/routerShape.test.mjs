import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("browser verification route uses standalone light shell", () => {
  const source = readFileSync(new URL("../router.js", import.meta.url), "utf8");
  const standaloneRoutePattern = /path:\s*"\/browser-verification\/:sessionId"[\s\S]*?name:\s*"browser-verification"[\s\S]*?bodyClass:\s*"browser-verification-page"/;
  const shellChildrenBlock = source.match(/path:\s*"\/"[\s\S]*?children:\s*\[([\s\S]*?)\n\s*\],\n\s*},\n\];/)?.[1] || "";

  assert.match(source, standaloneRoutePattern);
  assert.doesNotMatch(shellChildrenBlock, /name:\s*"browser-verification"/);
});

test("browser verification page only renders the validation surface", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");

  assert.doesNotMatch(source, /browser-verification-topbar/);
  assert.doesNotMatch(source, /browser-verification-stats/);
  assert.doesNotMatch(source, />平台</);
  assert.doesNotMatch(source, />状态</);
  assert.doesNotMatch(source, />截图</);
  assert.doesNotMatch(source, />模型 ID</);
  assert.doesNotMatch(source, />配置</);
  assert.doesNotMatch(source, />Captcha</);
});
