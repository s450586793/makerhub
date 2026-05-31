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
  assert.doesNotMatch(source, /section-card__header/);
  assert.doesNotMatch(source, /browser-verification-actions/);
  assert.doesNotMatch(source, /RouterLink/);
  assert.doesNotMatch(source, /panelHeading/);
  assert.doesNotMatch(source, /cancelSession/);
  assert.doesNotMatch(source, /cancelling/);
  assert.doesNotMatch(source, />返回任务</);
  assert.doesNotMatch(source, />刷新</);
  assert.doesNotMatch(source, />取消</);
  assert.doesNotMatch(source, />平台</);
  assert.doesNotMatch(source, />状态</);
  assert.doesNotMatch(source, />截图</);
  assert.doesNotMatch(source, />模型 ID</);
  assert.doesNotMatch(source, />配置</);
  assert.doesNotMatch(source, />Captcha</);
  assert.match(source, /visibleMessageText/);
});

test("browser verification page uses pointer events for drag mapping", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");
  assert.match(source, /@pointerdown\.prevent="handlePointerDown"/);
  assert.match(source, /@pointermove\.prevent="handlePointerMove"/);
  assert.match(source, /@pointerup\.prevent="handlePointerUp"/);
  assert.match(source, /@pointercancel\.prevent="handlePointerCancel"/);
  assert.match(source, /setPointerCapture/);
  assert.match(source, /releasePointerCapture/);
  assert.match(source, /suppressNextClick/);
  assert.doesNotMatch(source, /@mousedown\.prevent="sendPointerCommand\('mousedown'/);
  assert.doesNotMatch(source, /@mousemove="handleMouseMove"/);
  assert.doesNotMatch(source, /let mouseMoveSentAt = 0/);
});

test("browser verification page serializes drag input commands", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");

  assert.match(source, /let inputQueue = Promise\.resolve\(\)/);
  assert.match(source, /function postInput\(payload\)/);
  assert.match(source, /inputQueue = inputQueue\.then\(\s*\(\) => postInput\(payload\),\s*\(\) => postInput\(payload\),\s*\)/);
});

test("browser verification drag release uses the last tracked pointer position", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");

  assert.match(source, /function pointerStateCoordinates\(state\)/);
  assert.match(source, /clientX:\s*state\.lastX/);
  assert.match(source, /clientY:\s*state\.lastY/);
  assert.match(source, /const releaseCoordinates = cancelled \? pointerStateCoordinates\(state\) : commandCoordinates\(event\)/);
  assert.match(source, /type: "mouseup", \.\.\.releaseCoordinates/);
});
