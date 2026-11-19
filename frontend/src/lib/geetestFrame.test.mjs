import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import puppeteer from "puppeteer-core";

const module = await import("./geetestFrame.js").catch(() => ({}));
const challenge = { challenge_id: "challenge-1", captcha_id: "11111111111111111111111111111111" };
const proof = { captcha_id: challenge.captcha_id, lot_number: "lot", pass_token: "pass", gen_time: "123", captcha_output: "output" };

function harness(patch = {}) {
  assert.equal(typeof module.createGeetestFrame, "function", "isolated SDK host must be implemented");
  const handlers = new Map();
  const timers = new Map();
  const callbacks = [];
  const attributes = {};
  const frame = { contentWindow: {}, setAttribute: (key, value) => { attributes[key] = value; }, remove() { this.removed = true; } };
  const container = { appendChild: (value) => assert.equal(value, frame) };
  const windowObject = { addEventListener: (name, fn) => handlers.set(name, fn), removeEventListener: (name, fn) => { if (handlers.get(name) === fn) handlers.delete(name); } };
  let timerId = 0;
  const host = module.createGeetestFrame({ container, challenge, windowObject, documentObject: { createElement: () => frame },
    onReady: () => callbacks.push(["ready"]), onSuccess: (value) => callbacks.push(["success", value]),
    onError: (value) => callbacks.push(["error", value]), onClose: () => callbacks.push(["close"]),
    schedule: (callback) => { timers.set(++timerId, callback); return timerId; }, unschedule: (id) => timers.delete(id), ...patch });
  const nonce = frame.srcdoc.match(/"nonce":"([a-f0-9]+)"/)[1];
  const send = (type, payload = {}, event = {}) => handlers.get("message")?.({ origin: "null", source: frame.contentWindow,
    data: { channel: "makerhub-geetest", nonce, type, ...payload }, ...event });
  return { host, frame, attributes, handlers, callbacks, timers, nonce, send };
}

test("SDK lives in an opaque sandbox with a fixed official script and a unique nonce", () => {
  const first = harness();
  const second = harness();
  assert.equal(first.attributes.sandbox, "allow-scripts");
  assert.equal(first.attributes.referrerpolicy, "no-referrer");
  assert.match(first.frame.srcdoc, /https:\/\/static\.geetest\.com\/v4\/gt4\.js/);
  assert.match(first.frame.srcdoc, /initGeetest4/);
  assert.match(first.frame.srcdoc, /getValidate/);
  assert.match(first.frame.srcdoc, /showCaptcha/);
  assert.notEqual(first.nonce, second.nonce);
  first.host.destroy();
  second.host.destroy();
});

test("messages require the exact source, opaque origin, channel and nonce", () => {
  const page = harness();
  page.send("ready", {}, { source: {} });
  page.send("ready", {}, { origin: "https://evil.test" });
  page.send("ready", { nonce: "stale" });
  page.send("ready", { channel: "other" });
  assert.deepEqual(page.callbacks, []);
  page.send("ready");
  assert.deepEqual(page.callbacks, [["ready"]]);
  assert.equal(page.timers.size, 0);
  page.host.destroy();
});

test("only the current captcha proof succeeds once and private or extra SDK fields are discarded", () => {
  const page = harness();
  page.send("success", { proof: { ...proof, captcha_id: "other" } });
  assert.equal(page.callbacks[0][0], "error");
  const valid = harness();
  valid.send("success", { proof: { ...proof, private: "discard" } });
  valid.send("success", { proof });
  assert.deepEqual(valid.callbacks, [["success", proof]]);
  assert.equal(valid.timers.size, 0);
  valid.host.destroy();
});

test("destroy and timeout detach the frame and invalidate late callbacks", () => {
  const page = harness();
  const listener = page.handlers.get("message");
  page.host.destroy();
  listener({ origin: "null", source: page.frame.contentWindow, data: { channel: "makerhub-geetest", nonce: page.nonce, type: "success", proof } });
  assert.equal(page.frame.removed, true);
  assert.equal(page.handlers.size, 0);
  assert.equal(page.timers.size, 0);
  assert.deepEqual(page.callbacks, []);
  const timeout = harness();
  [...timeout.timers.values()][0]();
  assert.match(timeout.callbacks[0][1], /超时/);
  assert.equal(timeout.frame.removed, true);
});

test("SDK failures and close are terminal and do not expose upstream errors", () => {
  const failed = harness();
  failed.send("error", { error: "sensitive upstream detail" });
  assert.deepEqual(failed.callbacks, [["error", "验证码加载失败，请重试。"]]);
  failed.send("success", { proof });
  assert.equal(failed.callbacks.length, 1);
  const closed = harness();
  closed.send("close");
  assert.deepEqual(closed.callbacks, [["close"]]);
  assert.equal(closed.frame.removed, true);
});

test("untrusted SDK servers and malformed challenge configuration are rejected", () => {
  for (const config of [{ ...challenge, api_servers: ["evil.test"] }, { ...challenge, static_servers: ["static.geetest.com@evil.test"] }, { ...challenge, captcha_id: "</script>" }, { captcha_id: "captcha-1" }]) {
    assert.throws(() => harness({ challenge: config }), /验证配置/);
  }
  const page = harness({ challenge: { ...challenge, api_servers: ["gcaptcha4.geetest.com"], static_servers: ["static.geetest.com"] } });
  assert.match(page.frame.srcdoc, /apiServers/);
  assert.match(page.frame.srcdoc, /staticServers/);
  page.host.destroy();
});

test("a hidden frame waits for layout and displays a full-size challenge when revealed", {
  skip: !process.env.MAKERHUB_TEST_CHROMIUM && "Set MAKERHUB_TEST_CHROMIUM to run Chromium integration tests",
  timeout: 30000,
}, async (t) => {
  const server = createServer((_request, response) => {
    response.setHeader("Content-Type", "text/html");
    response.end('<!doctype html><html><head><style>iframe{display:block;width:100%;height:100%;border:0}</style></head><body><div id="captcha" style="display:none;height:440px"></div></body></html>');
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const browser = await puppeteer.launch({ executablePath: process.env.MAKERHUB_TEST_CHROMIUM,
    headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  t.after(() => browser.close());
  const source = readFileSync(new URL("./geetestFrame.js", import.meta.url), "utf8").replace("export function createGeetestFrame", "function createGeetestFrame");
  for (const width of [384, 278]) {
    const page = await browser.newPage();
    t.after(() => page.close().catch(() => {}));
    await page.setRequestInterception(true);
    page.on("request", (request) => {
      if (request.url() !== "https://static.geetest.com/v4/gt4.js") { void request.continue(); return; }
      void request.respond({ contentType: "application/javascript", body: `
        window.sdkScriptLoaded = true;
        window.initGeetest4 = (config, ready) => {
          const box = document.createElement("div");
          box.className = "geetest_box";
          box.style.cssText = "width:" + (340 * config.rem) + "px;height:" + (386 * config.rem) + "px";
          document.querySelector("#captcha").appendChild(box);
          ready({ onReady(fn) { queueMicrotask(fn); }, onSuccess() {}, onError() {}, onClose() {}, showCaptcha() {} });
        };
      ` });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.addScriptTag({ content: source + "\nwindow.createGeetestFrame = createGeetestFrame;" });
    await page.evaluate(({ width, challenge }) => {
      const container = document.querySelector("#captcha");
      container.style.width = width + "px";
      window.sdkEvents = [];
      window.sdkHost = window.createGeetestFrame({ container, challenge,
        onReady: () => window.sdkEvents.push("ready"), onError: (message) => window.sdkEvents.push(message) });
    }, { width, challenge });
    const frame = page.frames().find((value) => value.parentFrame() === page.mainFrame());
    await frame.waitForFunction(() => window.sdkScriptLoaded === true, { polling: 50, timeout: 5000 });
    assert.deepEqual(await page.evaluate(() => window.sdkEvents), [], "hidden layout must not initialize a tiny challenge");
    await page.$eval("#captcha", (element) => { element.style.display = "block"; });
    await page.waitForFunction(() => window.sdkEvents.length > 0);
    assert.deepEqual(await page.evaluate(() => window.sdkEvents), ["ready"]);
    const bounds = await frame.$eval(".geetest_box", (element) => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height }));
    assert.ok(bounds.width >= width * 0.85, `${width}px container: challenge uses the available width`);
    assert.ok(bounds.width <= width && bounds.height <= 440, "challenge stays inside the frame");
    await page.evaluate(() => window.sdkHost.destroy());
    assert.equal(await page.$("iframe"), null);
    await page.close();
  }
});

for (const { name, config } of [
  { name: "official-demo", config: { captcha_id: "54088bb07d2df3c46b79f80300b0abbe" } },
  { name: "makerworld", config: { captcha_id: "dbebe0fd427a657ff2cc126106718105",
    api_servers: ["gcaptcha4.geetest.com", "gcaptcha4.geevisit.com", "gcaptcha4.gsensebot.com"],
    static_servers: ["static.geetest.com", "static.geevisit.com"] } },
]) test(`official SDK actually reaches ready in an opaque iframe without access to the application origin (${name})`, {
  skip: (!process.env.MAKERHUB_TEST_CHROMIUM || !process.env.MAKERHUB_TEST_GEETEST) && "Set MAKERHUB_TEST_CHROMIUM and MAKERHUB_TEST_GEETEST for the live SDK check",
  timeout: 60000,
}, async (t) => {
  const server = createServer((_request, response) => {
    response.setHeader("Content-Type", "text/html");
    response.end('<!doctype html><html><head><style>iframe{width:100%;height:100%;border:0}</style></head><body><div id="captcha" style="width:278px;height:440px"></div></body></html>');
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const browser = await puppeteer.launch({ executablePath: process.env.MAKERHUB_TEST_CHROMIUM,
    headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const failures = [];
  page.on("requestfailed", (request) => { const url = new URL(request.url()); failures.push(`${url.origin}${url.pathname}: ${request.failure()?.errorText}`); });
  page.on("pageerror", (error) => failures.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const source = readFileSync(new URL("./geetestFrame.js", import.meta.url), "utf8").replace("export function createGeetestFrame", "function createGeetestFrame");
  await page.addScriptTag({ content: source + "\nwindow.createGeetestFrame = createGeetestFrame;" });
  await page.evaluate(({ name, config }) => {
    window.sdkEvents = [];
    window.sdkHost = window.createGeetestFrame({ container: document.querySelector("#captcha"),
      challenge: { challenge_id: name, ...config },
      onReady: () => window.sdkEvents.push("ready"), onSuccess: () => window.sdkEvents.push("success"),
      onError: (message) => window.sdkEvents.push(message), onClose: () => window.sdkEvents.push("close") });
  }, { name, config });
  await page.waitForFunction(() => window.sdkEvents.length > 0, { timeout: 40000 });
  const events = await page.evaluate(() => window.sdkEvents);
  assert.equal(events[0], "ready", `official SDK event: ${JSON.stringify(events)}`);
  const frame = page.frames().find((value) => value.parentFrame() === page.mainFrame());
  assert.ok(frame, "official captcha sandbox exists");
  const isolated = await frame.evaluate(() => {
    try { void parent.document; return false; } catch { return true; }
  });
  assert.equal(isolated, true, "third-party SDK cannot inspect app cookies or DOM");
  await frame.waitForFunction(() => {
    const bounds = document.querySelector(".geetest_box")?.getBoundingClientRect();
    const title = document.querySelector(".geetest_title");
    return bounds && bounds.width > 100 && bounds.height > 100 && title && Number(getComputedStyle(title).opacity) >= 0.99;
  }, { timeout: 15000 }).catch(async (error) => {
    t.diagnostic(JSON.stringify({ failures, events: await page.evaluate(() => window.sdkEvents), classes: await frame.$eval(".geetest_captcha", (element) => element.className) }));
    throw error;
  });
  const bounds = await frame.$eval(".geetest_box", (element) => {
    const { x, y, width, height } = element.getBoundingClientRect(); return { x, y, width, height };
  });
  assert.ok(bounds.width >= 240, "official challenge stays large enough to operate on a narrow phone");
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 278, "official challenge fits a 320px phone's inner panel");
  assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= 440, "official challenge fits the iframe height");
  assert.deepEqual(failures, [], "official SDK resources and scripts load without errors");
  if (process.env.MAKERHUB_GEETEST_SCREENSHOT) await page.screenshot({ path: process.env.MAKERHUB_GEETEST_SCREENSHOT.replace(/\.png$/, `-${name}.png`) });
  await page.evaluate(() => window.sdkHost.destroy());
  assert.equal(await page.$("iframe"), null);
});
