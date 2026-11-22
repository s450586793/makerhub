import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { parse as babelParse } from "@babel/parser";
import { compileScript, compileStyle, parse } from "@vue/compiler-sfc";
import puppeteer from "puppeteer-core";
import { computed, nextTick, ref } from "vue";

const source = readFileSync(new URL("../components/AccountVerificationDialog.vue", import.meta.url), "utf8");
const descriptor = parse(source).descriptor;
const script = descriptor.scriptSetup.content;
const setup = babelParse(script, { sourceType: "module" }).program.body.filter((node) => node.type !== "ImportDeclaration")
  .map((node) => script.slice(node.start, node.end)).join("\n");
const challenge = { challenge_id: "challenge-1", captcha_id: "captcha-1" };

function dialog() {
  const calls = [];
  const emitted = [];
  const watchers = [];
  const frames = [];
  const listeners = new Map();
  let changed;
  let unmount;
  let focusRestored = 0;
  let closeFocused = 0;
  const props = { visible: true, platform: "cn", action: { endpoint: "/api/config/online-accounts/cn/verification" } };
  const controller = {
    open: (action) => calls.push({ operation: "open", action }), close: () => calls.push({ operation: "close" }),
    complete: (challengeId, proof) => calls.push({ operation: "complete", challengeId, proof }),
  };
  const dependencies = {
    computed, ref, nextTick,
    watch: (getter, callback) => { watchers.push({ getter, callback }); },
    onBeforeUnmount: (callback) => { unmount = callback; }, defineProps: () => props,
    defineEmits: () => (...event) => emitted.push(event),
    apiRequest: () => assert.fail("dialog delegates networking to its controller"),
    createManualVerificationController: (options) => { changed = options.onChange; return controller; },
    createGeetestFrame: (options) => {
      const frame = { options, destroyed: false, destroy() { this.destroyed = true; } };
      frames.push(frame);
      return frame;
    },
    document: { activeElement: { focus: () => { focusRestored++; } },
      addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener: (name) => listeners.delete(name) },
  };
  const page = new Function(...Object.keys(dependencies), setup + `
    return { state, panel, closeButton, captchaHost, sdkLoading, sdkError, requestClose, restart, handleDialogKeydown };
  `)(...Object.values(dependencies));
  page.captchaHost.value = {};
  page.closeButton.value = { focus() { closeFocused++; } };
  page.panel.value = { contains: (element) => element === page.closeButton.value };
  async function sync() {
    for (const watcher of watchers) {
      const value = typeof watcher.getter === "function" ? watcher.getter() : watcher.getter.value;
      const same = Array.isArray(value) ? value.every((entry, index) => entry === watcher.previous?.[index]) : value === watcher.previous;
      if (!same) {
        const previous = watcher.previous;
        watcher.previous = value;
        await watcher.callback(value, previous);
      }
    }
  }
  async function setState(patch = {}) {
    changed({ session_id: "manual_cn", platform: "cn", status: "challenge", loading: false, completed: false, challenge, ...patch });
    await sync();
  }
  return { ...page, calls, emitted, frames, props, listeners, setState, sync, unmount: () => unmount(), restored: () => focusRestored, closeFocused: () => closeFocused };
}

test("dialog mounts only the current official captcha and submits SDK proof automatically", async () => {
  const page = dialog();
  await page.setState();
  assert.equal(page.frames.length, 1);
  assert.deepEqual(page.frames[0].options.challenge, challenge);
  const proof = { captcha_id: "captcha-1", lot_number: "lot" };
  page.frames[0].options.onSuccess(proof);
  assert.deepEqual(page.calls.at(-1), { operation: "complete", challengeId: "challenge-1", proof });
  assert.equal(page.emitted.length, 0);
  await page.setState({ status: "verifying", challenge: null });
  assert.equal(page.frames[0].destroyed, true);
  assert.doesNotMatch(descriptor.template.content, /刷新画面|验证完成|发送文字|pointerdown|remoteImage/);
  page.unmount();
});

test("replacing or closing a challenge destroys its SDK and rejects late callbacks", async () => {
  const page = dialog();
  await page.setState();
  const first = page.frames[0];
  await page.setState({ challenge: { ...challenge, challenge_id: "challenge-2" } });
  assert.equal(first.destroyed, true);
  first.options.onSuccess({ captcha_id: "old" });
  assert.equal(page.calls.some((call) => call.operation === "complete"), false);
  const second = page.frames[1];
  page.requestClose();
  second.options.onSuccess({ captcha_id: "late" });
  assert.equal(second.destroyed, true);
  assert.equal(page.calls.some((call) => call.operation === "complete"), false);
  assert.deepEqual(page.emitted, [["close"]]);
  page.unmount();
});

test("the native captcha close button cancels the dialog and restores the entry focus", async () => {
  const page = dialog();
  await page.sync();
  await page.setState();
  const frame = page.frames[0];
  frame.options.onClose();
  assert.equal(frame.destroyed, true);
  assert.deepEqual(page.emitted, [["close"]]);
  assert.equal(page.calls.at(-1).operation, "close");
  assert.equal(page.sdkError.value, "");
  frame.options.onSuccess({ captcha_id: "late" });
  assert.equal(page.calls.some((call) => call.operation === "complete"), false);
  page.props.visible = false;
  await page.sync();
  assert.equal(page.restored(), 1);
  page.unmount();
});

test("SDK errors expose retry and Escape restores focus while composition Escape is ignored", async () => {
  const page = dialog();
  await page.sync();
  await page.setState();
  page.frames[0].options.onError("验证码加载失败，请重试。");
  assert.match(page.sdkError.value, /加载失败/);
  page.handleDialogKeydown({ key: "Escape", isComposing: true });
  assert.equal(page.emitted.length, 0);
  page.handleDialogKeydown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(page.emitted, [["close"]]);
  page.props.visible = false;
  await page.sync();
  assert.equal(page.restored(), 1);
  page.unmount();
});

test("focus returning from the isolated iframe cannot escape to the underlying page", async () => {
  const page = dialog();
  await page.sync();
  const before = page.closeFocused();
  assert.equal(typeof page.listeners.get("focusin"), "function");
  page.listeners.get("focusin")({ target: {} });
  assert.equal(page.closeFocused(), before + 1);
  page.listeners.get("focusin")({ target: page.closeButton.value });
  assert.equal(page.closeFocused(), before + 1);
  page.unmount();
  assert.equal(page.listeners.size, 0);
});

test("only the native captcha is shown and stays inside light, dark, mobile and short screens", {
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
  const styles = [readFileSync(new URL("../../../app/static/css/app.css", import.meta.url), "utf8"),
    readFileSync(new URL("../style.css", import.meta.url), "utf8").replace(/^@import[^;]+;/gm, ""),
    compileStyle({ source: descriptor.styles[0].content, id: "data-v-verification-layout", scoped: true }).code].join("\n");
  const frameSource = readFileSync(new URL("./geetestFrame.js", import.meta.url), "utf8")
    .replace("export function createGeetestFrame", "function createGeetestFrame");
  const sdkFixture = `
    window.initGeetest4 = (options, ready) => {
      const scale = options.rem === undefined ? 1 : options.rem;
      const style = document.createElement("style");
      style.textContent = "#captcha{display:grid;place-items:center}.geetest_box{display:flex;flex-direction:column;gap:12px;box-sizing:border-box;overflow:hidden;background:#e8ece8;color:#17251e}.geetest_window{display:block;flex:1;min-height:0;width:100%}.geetest_slider{box-sizing:border-box;flex:0 0 32px;width:100%;margin:0}";
      document.head.appendChild(style);
      const box = document.createElement("section");
      box.className = "geetest_box";
      box.style.width = (340 * scale) + "px";
      box.style.height = (386 * scale) + "px";
      box.innerHTML = '<svg class="geetest_window" viewBox="0 0 340 320" role="img" aria-label="验证码题图"><rect width="340" height="320" fill="#bcd6c5"/><path d="M0 260 90 140 160 220 250 90 340 230V320H0Z" fill="#467c65"/><circle cx="76" cy="70" r="28" fill="#f4d890"/></svg><input class="geetest_slider" type="range" min="0" max="100" value="0" aria-label="拖动滑块">';
      document.querySelector("#captcha").appendChild(box);
      ready({ onReady(fn) { queueMicrotask(fn); }, onSuccess() {}, onError() {}, onClose() {}, showCaptcha() {} });
    };
  `;
  const screenshotDir = process.env.MAKERHUB_DIALOG_SCREENSHOT_DIR;
  if (screenshotDir) mkdirSync(screenshotDir, { recursive: true });
  for (const { name, width, height, theme } of [
    { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
    { name: "mobile-dark", width: 390, height: 844, theme: "dark" },
    { name: "mobile-narrow", width: 320, height: 568, theme: "dark" },
    { name: "mobile-short", width: 390, height: 360, theme: "light" },
  ]) {
    const page = await browser.newPage();
    t.after(() => page.close().catch(() => {}));
    await page.setViewport({ width, height });
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (request.url() === "https://static.geetest.com/v4/gt4.js") {
        void request.respond({ contentType: "application/javascript", body: sdkFixture });
      } else {
        void request.abort();
      }
    });
    await page.setContent(`<!doctype html><html data-theme="${theme}"><head><style>${styles}</style></head><body><button id="origin">去验证</button><div id="fixture"></div></body></html>`);
    await page.focus("#origin");
    await page.addScriptTag({ path: fileURLToPath(new URL("../../node_modules/vue/dist/vue.global.prod.js", import.meta.url)) });
    await page.addScriptTag({ content: `
      const { ${vueImports} } = Vue;
      const apiRequest = () => { throw new Error("networking is mocked for layout verification"); };
      const createManualVerificationController = ({ onChange }) => {
        window.setDialogState = onChange;
        return { open() { onChange({ status: "preparing", loading: true, completed: false, challenge: null }); }, close() {}, complete() {} };
      };
      ${frameSource}
      ${component}
      Dialog.__scopeId = "data-v-verification-layout";
      Vue.createApp({ render: () => Vue.h(Dialog, { visible: true, platform: "cn" }) }).mount("#fixture");
    ` });
    await page.waitForSelector(".account-verification-dialog__panel");
    const loading = await page.$eval(".account-verification-dialog__status", (panel) => ({
      width: panel.getBoundingClientRect().width, height: panel.getBoundingClientRect().height,
      closeFocused: document.activeElement.textContent === "关闭",
      background: getComputedStyle(panel).backgroundColor, color: getComputedStyle(panel).color,
    }));
    assert.ok(loading.width <= 420 && loading.width <= width - 16, `${name}: compact width`);
    assert.ok(loading.height < 230, `${name}: no reserved browser canvas`);
    assert.equal(loading.closeFocused, true);
    assert.notEqual(loading.background, loading.color, `${name}: status theme is readable`);
    await page.evaluate((challenge) => window.setDialogState({ session_id: "layout", platform: "cn", status: "challenge",
      loading: false, completed: false, challenge }), { ...challenge, captcha_id: "11111111111111111111111111111111" });
    await page.waitForSelector("iframe");
    const frame = page.frames().find((value) => value.parentFrame() === page.mainFrame());
    assert.ok(frame, `${name}: isolated captcha frame exists`);
    await frame.waitForSelector(".geetest_box");
    await page.waitForFunction(() => document.querySelector(".account-verification-dialog__captcha")?.getAttribute("aria-busy") === "false");
    const loaded = await page.$eval(".account-verification-dialog__panel", (panel) => {
      const bounds = panel.getBoundingClientRect();
      return { width: bounds.width, height: bounds.height, top: bounds.top, bottom: bounds.bottom,
        frameWidth: panel.querySelector("iframe").getBoundingClientRect().width,
        frameHeight: panel.querySelector("iframe").getBoundingClientRect().height,
        background: getComputedStyle(panel).backgroundColor, borderWidth: getComputedStyle(panel).borderWidth };
    });
    assert.equal(loaded.frameWidth, width, `${name}: native overlay fills viewport width`);
    assert.equal(loaded.frameHeight, height, `${name}: native overlay fills viewport height`);
    assert.equal(loaded.background, "rgba(0, 0, 0, 0)", `${name}: no application panel background`);
    assert.equal(loaded.borderWidth, "0px", `${name}: no application panel border`);
    assert.equal(await page.$(".account-verification-dialog__status"), null, `${name}: no duplicate title or explanation`);
    const captcha = await frame.$eval(".geetest_box", (box) => ({
      width: box.getBoundingClientRect().width, top: box.getBoundingClientRect().top, bottom: box.getBoundingClientRect().bottom,
      imageWidth: box.querySelector(".geetest_window").getBoundingClientRect().width,
    }));
    assert.ok(captcha.width <= 340 && captcha.width <= loaded.frameWidth - 16, `${name}: native challenge fits width`);
    assert.ok(captcha.imageWidth >= captcha.width * 0.85, `${name}: challenge image remains readable`);
    if (width <= 390) assert.ok(captcha.width >= 240, `${name}: mobile challenge is not reduced to a thumbnail`);
    assert.ok(captcha.top >= 8 && captcha.bottom <= height - 8, `${name}: native challenge fits height`);
    await frame.focus(".geetest_slider");
    await page.keyboard.press("ArrowRight");
    assert.ok(await frame.$eval(".geetest_slider", (slider) => Number(slider.value)) > 0, `${name}: captcha slider accepts keyboard input`);
    assert.ok(loaded.top >= 0 && loaded.bottom <= height, `${name}: fits viewport`);
    if (screenshotDir) await page.screenshot({ path: join(screenshotDir, `${name}-geetest.png`) });
    await page.evaluate(() => window.setDialogState({ status: "failed", loading: false, completed: false, message: "请重试。", challenge: null }));
    await page.waitForFunction(() => document.querySelector(".account-verification-dialog__actions")?.textContent.includes("重试"));
    assert.equal(await page.$("iframe"), null);
  }
});
