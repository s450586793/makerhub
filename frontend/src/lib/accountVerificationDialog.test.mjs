import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { parse as babelParse } from "@babel/parser";
import { compileScript, compileStyle, parse } from "@vue/compiler-sfc";
import puppeteer from "puppeteer-core";
import { computed, nextTick, ref } from "vue";

import { verificationImageUrl, verificationKeyboardInput, verificationPointerPoint } from "./manualVerification.js";

const source = readFileSync(new URL("../components/AccountVerificationDialog.vue", import.meta.url), "utf8");
const descriptor = parse(source).descriptor;
const script = descriptor.scriptSetup.content;
const setup = babelParse(script, { sourceType: "module" }).program.body
  .filter((node) => node.type !== "ImportDeclaration")
  .map((node) => script.slice(node.start, node.end)).join("\n");

function dialog() {
  const calls = [];
  const emitted = [];
  let changed;
  let watcher;
  let unmount;
  const props = { visible: true, platform: "cn", action: { endpoint: "/api/config/online-accounts/cn/verification" } };
  const controller = {
    open: (action) => calls.push({ operation: "open", action }),
    close: () => calls.push({ operation: "close" }),
    sendEvents: (events) => calls.push({ operation: "input", events }),
    finish: () => calls.push({ operation: "finish" }),
    refresh: () => calls.push({ operation: "refresh" }),
  };
  const dependencies = {
    computed, ref, nextTick, verificationImageUrl, verificationKeyboardInput, verificationPointerPoint,
    watch: (_source, callback) => { watcher = callback; },
    onBeforeUnmount: (callback) => { unmount = callback; },
    defineProps: () => props,
    defineEmits: () => (...event) => emitted.push(event),
    apiRequest: () => assert.fail("the dialog should delegate networking to its controller"),
    createManualVerificationController: (options) => { changed = options.onChange; return controller; },
    document: { activeElement: null, addEventListener() {}, removeEventListener() {} },
  };
  const page = new Function(...Object.keys(dependencies), setup + `
    return { state, canInteract, viewport, remoteImage, panel, closeButton, text, inputError, zoom, panMode, imageStyle, setZoom, handlePointerDown, handlePointerMove, handlePointerUp, handleWheel, handleRemoteKeydown, handlePaste, sendText, finish, handleDialogKeydown };
  `)(...Object.values(dependencies));
  const captured = [];
  const released = [];
  let focused = 0;
  page.viewport.value = { scrollLeft: 0, scrollTop: 0, scrollWidth: 1200, scrollHeight: 800, clientWidth: 600, clientHeight: 400, focus() { focused++; }, setPointerCapture: (id) => captured.push(id), releasePointerCapture: (id) => released.push(id) };
  page.remoteImage.value = { getBoundingClientRect: () => ({ left: 20, top: 30, width: 600, height: 400 }) };
  const setState = (patch = {}) => changed({ session_id: "manual_cn", platform: "cn", status: "challenge", image: "aQ==", width: 1200, height: 800, completed: false, ...patch });
  setState();
  return { ...page, calls, emitted, captured, released, props, setState, focusCount: () => focused, watcher: (...args) => watcher(...args), unmount: () => unmount() };
}

function pointer(patch = {}) {
  return { pointerId: 1, pointerType: "touch", button: 0, clientX: 320, clientY: 230, timeStamp: 100, preventDefault() {}, ...patch };
}

test("dialog forwards a touch drag in viewport coordinates and releases pointer capture once", () => {
  const page = dialog();
  page.handlePointerDown(pointer());
  page.handlePointerMove(pointer({ pointerId: 2 }));
  page.handlePointerMove(pointer({ clientX: 400, clientY: 250, timeStamp: 108 }));
  page.handlePointerMove(pointer({ clientX: 500, clientY: 250, timeStamp: 120 }));
  page.handlePointerUp(pointer({ clientX: 700, clientY: 250, timeStamp: 160 }));
  page.handlePointerUp(pointer());
  assert.deepEqual(page.calls.map((call) => call.events), [
    [{ type: "pointerdown", x: 600, y: 400, delay_ms: 0 }],
    [{ type: "pointermove", x: 960, y: 440, delay_ms: 20 }],
    [{ type: "pointerup", x: 1199, y: 440, delay_ms: 40 }],
  ]);
  assert.deepEqual(page.captured, [1]);
  assert.deepEqual(page.released, [1]);
  assert.equal(page.focusCount(), 1);
});

test("finishing a held drag sends pointerup before the finish operation", () => {
  const page = dialog();
  page.handlePointerDown(pointer());
  page.finish();
  assert.equal(page.calls[1].events[0].type, "pointerup");
  assert.deepEqual({ x: page.calls[1].events[0].x, y: page.calls[1].events[0].y }, { x: 600, y: 400 });
  assert.ok(page.calls[1].events[0].delay_ms >= 0 && page.calls[1].events[0].delay_ms <= 50);
  assert.deepEqual(page.calls[2], { operation: "finish" });
});

test("zoom centers the enlarged image and moving the picture never sends remote pointer events", async () => {
  const page = dialog();
  await page.setZoom(2);
  assert.equal(page.imageStyle.value.width, "1200px");
  assert.equal(page.viewport.value.scrollLeft, 300);
  assert.equal(page.viewport.value.scrollTop, 200);
  page.panMode.value = true;
  page.handlePointerDown(pointer());
  page.handlePointerMove(pointer({ clientX: 220, clientY: 180 }));
  page.handlePointerUp(pointer({ clientX: 220, clientY: 180 }));
  assert.equal(page.viewport.value.scrollLeft, 400);
  assert.equal(page.viewport.value.scrollTop, 250);
  assert.equal(page.calls.length, 0);
  assert.deepEqual(page.captured, [1]);
  assert.deepEqual(page.released, [1]);
  await page.setZoom(1);
  assert.equal(page.panMode.value, false);
  assert.deepEqual(page.imageStyle.value, {});
});

test("an enlarged scrolled picture maps remote clicks from its current bounds", async () => {
  const page = dialog();
  await page.setZoom(2);
  page.remoteImage.value = { getBoundingClientRect: () => ({ left: -280, top: -170, width: 1200, height: 800 }) };
  page.handlePointerDown(pointer());
  assert.deepEqual(page.calls[0].events, [{ type: "pointerdown", x: 600, y: 400, delay_ms: 0 }]);
});

test("terminal session states disable screenshot input", () => {
  const page = dialog();
  for (const status of ["error", "failed", "cancelled", "expired"]) {
    page.setState({ status });
    assert.equal(page.canInteract.value, false);
    page.handlePointerDown(pointer());
  }
  page.setState({ status: "completed", completed: true });
  page.handlePointerDown(pointer());
  assert.equal(page.calls.length, 0);
});

test("dialog normalizes wheel units and forwards keyboard and pasted text", () => {
  const page = dialog();
  page.handleWheel(pointer({ deltaY: 3, deltaMode: 1 }));
  let prevented = 0;
  page.handleRemoteKeydown({ key: "Enter", preventDefault() { prevented++; }, stopPropagation() {} });
  page.handlePaste({ preventDefault() { prevented++; }, clipboardData: { getData: () => "验证码" } });
  assert.deepEqual(page.calls.map((call) => call.events), [
    [{ type: "wheel", x: 600, y: 400, delta_y: 48 }],
    [{ type: "key", key: "Enter" }],
    [{ type: "text", text: "验证码" }],
  ]);
  assert.equal(prevented, 2);
  page.handlePaste({ preventDefault() {}, clipboardData: { getData: () => "a".repeat(2001) } });
  assert.match(page.inputError.value, /2000/);
  assert.equal(page.calls.length, 3);
});

test("Escape and unmount close the remote session, while composition Escape is ignored", () => {
  const page = dialog();
  page.handleDialogKeydown({ key: "Escape", isComposing: true });
  assert.equal(page.calls.length, 0);
  page.handleDialogKeydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(page.emitted, [["close"]]);
  page.unmount();
  assert.deepEqual(page.calls, [{ operation: "close" }, { operation: "close" }]);
});

test("verification dialog stays compact and scrolls tall screenshots on desktop and mobile", {
  skip: !process.env.MAKERHUB_TEST_CHROMIUM && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
}, async (t) => {
  const browser = await puppeteer.launch({ executablePath: process.env.MAKERHUB_TEST_CHROMIUM,
    headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  t.after(() => browser.close());
  const compiled = compileScript(descriptor, { id: "verification-layout", inlineTemplate: true }).content;
  const nodes = babelParse(compiled, { sourceType: "module" }).program.body;
  const vueImports = nodes.filter((node) => node.type === "ImportDeclaration" && node.source.value === "vue")
    .flatMap((node) => node.specifiers.map((item) => `${item.imported.name}: ${item.local.name}`)).join(", ");
  const component = nodes.filter((node) => node.type !== "ImportDeclaration")
    .map((node) => compiled.slice(node.start, node.end)).join("\n").replace("export default", "const Dialog =");
  const styles = [
    readFileSync(new URL("../../../app/static/css/app.css", import.meta.url), "utf8"),
    readFileSync(new URL("../style.css", import.meta.url), "utf8").replace(/^@import[^;]+;/gm, ""),
    compileStyle({ source: descriptor.styles[0].content, id: "data-v-verification-layout", scoped: true }).code,
  ].join("\n");
  const screenshotDir = process.env.MAKERHUB_DIALOG_SCREENSHOT_DIR;
  if (screenshotDir) mkdirSync(screenshotDir, { recursive: true });

  for (const { name, width, height, theme } of [
    { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
    { name: "mobile-dark", width: 390, height: 844, theme: "dark" },
    { name: "mobile-short", width: 390, height: 360, theme: "light" },
  ]) {
    const page = await browser.newPage();
    t.after(() => page.close().catch(() => {}));
    await page.setViewport({ width, height });
    await page.setContent(`<!doctype html><html data-theme="${theme}"><head><style>${styles}</style></head><body><div id="fixture"></div></body></html>`);
    await page.addScriptTag({ path: fileURLToPath(new URL("../../node_modules/vue/dist/vue.global.prod.js", import.meta.url)) });
    await page.addScriptTag({ content: `
      const { ${vueImports} } = Vue;
      const verificationImageUrl = ${verificationImageUrl.toString()};
      const verificationKeyboardInput = ${verificationKeyboardInput.toString()};
      const verificationPointerPoint = ${verificationPointerPoint.toString()};
      const apiRequest = () => { throw new Error("networking is mocked for layout verification"); };
      const createManualVerificationController = ({ onChange }) => {
        window.setDialogState = onChange;
        return { open() { onChange({ loading: true, completed: false }); }, close() {}, refresh() {}, sendEvents() {}, finish() {} };
      };
      ${component}
      Dialog.__scopeId = "data-v-verification-layout";
      Vue.createApp({ render: () => Vue.h(Dialog, { visible: true, platform: "cn" }) }).mount("#fixture");
    ` });
    await page.waitForSelector(".account-verification-dialog__viewport");
    const loading = await page.$eval(".account-verification-dialog__panel", (panel) => ({
      width: panel.getBoundingClientRect().width, height: panel.getBoundingClientRect().height,
      viewportHeight: panel.querySelector(".account-verification-dialog__viewport").getBoundingClientRect().height,
      closeFocused: document.activeElement.textContent === "关闭",
    }));
    assert.ok(loading.width <= 680 && loading.width <= width - 16, `${name}: compact panel width`);
    assert.ok(loading.height < 300, `${name}: loading must not reserve a large browser canvas`);
    assert.ok(loading.viewportHeight <= 90, `${name}: compact loading placeholder`);
    assert.equal(loading.closeFocused, true, `${name}: close stays keyboard-accessible`);
    if (screenshotDir && name !== "mobile-short") await page.screenshot({ path: join(screenshotDir, `${name}-loading.png`) });
    await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      canvas.width = 1200;
      canvas.height = 1800;
      const context = canvas.getContext("2d");
      context.fillStyle = "#f8faf9";
      context.fillRect(0, 0, 1200, 1800);
      context.fillStyle = "#17251e";
      context.font = "32px sans-serif";
      context.fillText("Verification page", 30, 60);
      context.strokeRect(36, 150, 500, 200);
      window.setDialogState({ session_id: "layout", platform: "cn", status: "challenge", loading: false,
        completed: false, width: 1200, height: 1800, image: canvas.toDataURL("image/jpeg").split(",")[1] });
    });
    await page.waitForFunction(() => document.querySelector(".account-verification-dialog__viewport img")?.naturalWidth === 1200);
    const loaded = await page.$eval(".account-verification-dialog__panel", (panel) => {
      const viewport = panel.querySelector(".account-verification-dialog__viewport");
      const image = viewport.querySelector("img");
      const bounds = panel.getBoundingClientRect();
      return { width: bounds.width, top: bounds.top, bottom: bounds.bottom,
        imageWidth: image.getBoundingClientRect().width, imageHeight: image.getBoundingClientRect().height,
        scrollHeight: viewport.scrollHeight, viewportHeight: viewport.clientHeight,
        footerBottom: panel.querySelector("footer").getBoundingClientRect().bottom,
        background: getComputedStyle(panel).backgroundColor, color: getComputedStyle(panel).color };
    });
    assert.ok(loaded.width <= 680, `${name}: image must not enlarge the panel`);
    assert.ok(Math.abs(loaded.imageWidth / loaded.imageHeight - 2 / 3) < 0.001, `${name}: preserve screenshot proportions`);
    assert.ok(loaded.scrollHeight > loaded.viewportHeight, `${name}: tall pages scroll instead of shrinking to fit height`);
    assert.ok(loaded.top >= 8 && loaded.bottom <= height - 8, `${name}: panel fits the screen`);
    assert.ok(loaded.footerBottom <= loaded.bottom, `${name}: action buttons stay visible`);
    assert.notEqual(loaded.background, loaded.color, `${name}: theme keeps readable text`);
    if (screenshotDir) await page.screenshot({ path: join(screenshotDir, `${name}-loaded.png`) });
    await page.$$eval(".account-verification-dialog__zoom button", (buttons) => buttons[2].click());
    await page.waitForFunction((baseWidth) => {
      const image = document.querySelector(".account-verification-dialog__viewport img");
      return image && Math.abs(image.getBoundingClientRect().width - baseWidth * 2) < 1;
    }, {}, loaded.imageWidth);
    assert.ok(await page.$eval(".account-verification-dialog__viewport", (viewport) => viewport.scrollWidth > viewport.clientWidth), `${name}: zoom retains horizontal scrolling`);
    await page.evaluate(() => window.setDialogState({ status: "failed", loading: false, completed: false,
      message: "浏览器仍忙，请稍后重新打开验证。", image: "" }));
    await page.waitForFunction(() => document.querySelector(".account-verification-dialog__viewport")?.textContent.includes("验证页面未能加载。"));
    assert.ok(await page.$eval(".account-verification-dialog__actions", (footer) => footer.textContent.includes("重新打开")), `${name}: a failed wait offers a retry`);
  }
});
