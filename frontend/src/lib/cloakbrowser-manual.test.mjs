import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync, spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import https from "node:https";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline";
import puppeteer from "puppeteer-core";

import {
  createManualRequestPolicy,
  isManualPageUrl,
  normalizeManualEvents,
  dispatchManualEvents,
  createManualVerification,
} from "../../../app/services/cloakbrowser_manual.mjs";

test("manual verification keeps top navigation inside the selected platform", () => {
  assert.equal(isManualPageUrl("https://makerworld.com.cn/zh/models/123", "cn"), true);
  assert.equal(isManualPageUrl("https://bambulab.cn/zh/sign-in", "cn"), true);
  for (const url of ["https://makerworld.com/", "https://makerworld.com.cn.evil.test/", "http://makerworld.com.cn/", "https://user@makerworld.com.cn/", "https://makerworld.com.cn:444/", "file:///tmp/a", "javascript:alert(1)"]) {
    assert.equal(isManualPageUrl(url, "cn"), false, url);
  }
});

test("manual input validates a complete batch before sending any events", async () => {
  const calls = [];
  const page = { mouse: { move: async (...args) => calls.push(args) } };
  await assert.rejects(dispatchManualEvents(page, [
    { type: "pointermove", x: 12, y: 30 },
    { type: "pointerdown", x: Infinity, y: 30 },
  ], 1080, 780));
  assert.deepEqual(calls, []);
  assert.throws(() => normalizeManualEvents(Array(81).fill({ type: "key", key: "Enter" }), 1080, 780));
  assert.throws(() => normalizeManualEvents([{ type: "key", key: "Control+F12" }], 1080, 780));
  assert.throws(() => normalizeManualEvents([{ type: "pointerup", x: -1, y: 3 }], 1080, 780));
  assert.throws(() => normalizeManualEvents([{ type: "pointermove", x: 2, y: 3, delay_ms: 51 }], 1080, 780));
  assert.throws(() => normalizeManualEvents([{ type: "text", text: "a".repeat(2001) }], 1080, 780));
});

test("manual input preserves drag order, text and modifier release", async () => {
  const calls = [];
  const page = {
    mouse: Object.fromEntries(["move", "down", "up", "wheel"].map(name => [name, async (...args) => calls.push([name, ...args])])),
    keyboard: Object.fromEntries(["down", "up", "press", "insertText"].map(name => [name, async (...args) => calls.push([name, ...args])])),
  };
  await dispatchManualEvents(page, [
    { type: "pointerdown", x: 20, y: 40 },
    { type: "pointermove", x: 80, y: 40 },
    { type: "pointerup", x: 100, y: 40 },
    { type: "key", key: "Ctrl+A" },
    { type: "text", text: "测试" },
    { type: "wheel", delta_y: 150 },
  ], 1080, 780);
  assert.deepEqual(calls, [
    ["move", 20, 40], ["down", { button: "left" }], ["move", 80, 40],
    ["move", 100, 40], ["up", { button: "left" }],
    ["down", "Control"], ["press", "a"], ["up", "Control"],
    ["insertText", "测试"], ["wheel", { deltaY: 150 }],
  ]);
});

test("manual download guard requires an explicit grant and real captcha continuation", () => {
  const policy = createManualRequestPolicy("cn", "123");
  const url = "https://makerworld.com.cn/api/v1/design-service/instance/123/f3mf?type=download&fileType=3mf";
  const request = { url, method: "GET", headers: {} };
  assert.equal(policy.allow(request), false);
  policy.authorize();
  assert.equal(policy.allow({ ...request, method: "OPTIONS" }), true);
  assert.equal(policy.allow({ ...request, url: url.replace("123", "999") }), false);
  assert.equal(policy.allow({ ...request, url: url.replace("com.cn", "com") }), false);
  assert.equal(policy.allow(request), true);
  assert.equal(policy.allow(request), false);
  policy.observe({ status_code: 418, payload: { captchaId: "fixture" } });
  assert.equal(policy.allow(request), false);
  assert.equal(policy.allow({ ...request, headers: { "x-bbl-captcha-result": "answer" } }), true);
  policy.observe({ status_code: 418, payload: { captchaId: "fixture-retry" } });
  assert.equal(policy.allow({ ...request, headers: { "x-bbl-captcha-result": "retry" } }), true);
  policy.observe({ status_code: 200, payload: { url: "https://files.test/file.3mf" } });
  assert.equal(policy.allow({ ...request, headers: { "x-bbl-captcha-result": "duplicate" } }), false);
  assert.equal(policy.allow({ url: "https://files.test/file.3mf", method: "GET" }), false);
});

test("manual guard prevents top navigation to arbitrary hosts", () => {
  const policy = createManualRequestPolicy("global", "123");
  assert.equal(policy.allow({ url: "https://evil.test/", method: "GET", mainNavigation: true }), false);
  assert.equal(policy.allow({ url: "https://makerworld.com/zh/models/123", method: "GET", mainNavigation: true }), true);
  assert.equal(policy.allow({ url: "https://challenges.cloudflare.com/turnstile/v0/api.js", method: "GET" }), true);
});

test("wheel positions the pointer over the scrolled element", async () => {
  const calls = [];
  await dispatchManualEvents({ mouse: {
    move: async (...args) => calls.push(["move", ...args]),
    wheel: async data => calls.push(["wheel", data]),
  } }, [{ type: "wheel", x: 80, y: 120, delta_y: 150 }], 1080, 780);
  assert.deepEqual(calls, [["move", 80, 120], ["wheel", { deltaY: 150 }]]);
});

test("manual Chromium page preserves wrong-answer retries and closes only its own target", {
  skip: !process.env.MAKERHUB_TEST_CHROMIUM && "Set MAKERHUB_TEST_CHROMIUM for Chromium integration",
  timeout: 30000,
}, async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "makerhub-manual-test-"));
  let browser;
  let server;
  let manual;
  let child;
  try {
    execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=makerworld.com.cn",
      "-keyout", path.join(directory, "key.pem"), "-out", path.join(directory, "cert.pem")], { stdio: "ignore" });
    const requests = [];
    const html = `<!doctype html><meta charset="utf-8"><title>Manual fixture</title>
      <style>button,input {display:block;margin:15px;width:200px;height:40px}</style>
      <script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"design":{"id":456,"instances":[{"id":123}]}}}}</script>
      <button id="download" class="primaryButton" onclick="download()">Download 3MF</button>
      <div id="captcha" style="display:none"><button id="wrong" onclick="solve(false)">Wrong answer</button>
      <button id="right" onclick="solve(true)">Correct answer</button><input id="answer"></div>
      <a id="popup" target="_blank" href="/api/v1/design-service/instance/999/f3mf">popup</a>
      <script>
        async function request(headers={}) {
          const response = await fetch('/api/v1/design-service/instance/123/f3mf', {headers});
          const body = await response.json();
          document.querySelector('#captcha').style.display = body.url ? 'none' : 'block';
          window.verified = !!body.url;
        }
        function download(){ request().catch(()=>{}); }
        function solve(good){ request({'x-bbl-captcha-result':good?'correct':'wrong'}); }
      </script>`;
    server = https.createServer({ key: await readFile(path.join(directory, "key.pem")), cert: await readFile(path.join(directory, "cert.pem")) }, (request, response) => {
      if (request.url.includes("/f3mf")) {
        requests.push({ url: request.url, answer: request.headers["x-bbl-captcha-result"] });
        const verified = request.headers["x-bbl-captcha-result"] === "correct";
        response.writeHead(verified ? 200 : 418, { "Content-Type": "application/json" });
        response.end(JSON.stringify(verified ? { name: "fixture.3mf", url: "https://makerworld.com.cn/fixture.3mf" } : { captchaId: "fixture" }));
      } else {
        response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
        response.end(html);
      }
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    browser = await puppeteer.launch({ executablePath: process.env.MAKERHUB_TEST_CHROMIUM, headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-networking", "--no-proxy-server", "--ignore-certificate-errors",
        `--host-resolver-rules=MAP makerworld.com.cn:443 127.0.0.1:${server.address().port}`] });
    const context = browser.defaultBrowserContext();
    await context.setCookie({ name: "token", value: "fixture-token", domain: "makerworld.com.cn", path: "/", secure: true });
    const originalPages = await context.pages();
    manual = await createManualVerification(browser, { platform: "cn", model_url: "https://makerworld.com.cn/zh/models/456", instance_id: "123" });
    let frame;
    for (let i = 0; i < 30; i++) {
      frame = await manual.command({ action: "frame" });
      if (frame.status === "ready") break;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.equal(frame.status, "ready");
    assert.equal(frame.width, 1080);
    assert.equal(Buffer.from(frame.image, "base64").subarray(0, 3).toString("hex"), "ffd8ff");
    const page = (await context.pages()).find(item => !originalPages.includes(item));
    const click = async selector => {
      const point = await page.$eval(selector, element => { const rect = element.getBoundingClientRect(); return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 }; });
      return manual.command({ action: "input", events: [{ type: "pointerdown", ...point }, { type: "pointerup", ...point }] });
    };
    await click("#download");
    assert.equal(requests.length, 0, "unreserved manual click must be blocked");
    await click("#popup");
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(requests.length, 0, "popup must be paused before its first authorization request");
    await manual.command({ action: "authorize" });
    await page.waitForSelector("#wrong", { visible: true });
    await click("#wrong");
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal((await manual.command({ action: "frame" })).status, "challenge");
    await click("#right");
    await page.waitForFunction(() => window.verified === true);
    frame = await manual.command({ action: "frame", snapshot: true });
    assert.equal(frame.status, "authorized");
    assert.equal(frame.authorization.status_code, 200);
    assert.equal(frame.snapshot.authenticated, true);
    assert.deepEqual(requests.map(item => item.answer), [undefined, "wrong", "correct"]);
    await manual.close();
    assert.equal(page.isClosed(), true);
    assert.equal(originalPages.every(item => !item.isClosed()), true);
    assert.equal((await context.pages()).length, originalPages.length);
    manual = null;

    child = spawn(process.execPath, [path.resolve("app/services/cloakbrowser_manual.mjs")], { stdio: ["pipe", "pipe", "pipe"] });
    const replies = createInterface({ input: child.stdout });
    const iterator = replies[Symbol.asyncIterator]();
    const command = async payload => {
      child.stdin.write(JSON.stringify(payload) + "\n");
      return JSON.parse((await iterator.next()).value);
    };
    const endpoint = new URL(browser.wsEndpoint());
    await command({ action: "init", auth_token: "fixture", cdp_url: `http://${endpoint.host}`, platform: "cn", model_url: "https://makerworld.com.cn/zh", lifetime_ms: 10000 });
    await command({ action: "frame" });
    child.kill("SIGTERM");
    await new Promise(resolve => child.once("exit", resolve));
    assert.equal((await context.pages()).length, originalPages.length, "SIGTERM cleans the manual page while preserving the profile");

    child = spawn(process.execPath, [path.resolve("app/services/cloakbrowser_manual.mjs")], { stdio: ["pipe", "pipe", "pipe"] });
    const stopDuringInitialization = target => { if (target.type() === "page") child.kill("SIGTERM"); };
    browser.on("targetcreated", stopDuringInitialization);
    try {
      const exited = new Promise(resolve => child.once("exit", resolve));
      child.stdin.write(JSON.stringify({ action: "init", auth_token: "fixture", cdp_url: `http://${endpoint.host}`,
        platform: "cn", model_url: "https://makerworld.com.cn/zh", lifetime_ms: 10000 }) + "\n");
      await exited;
      assert.equal((await context.pages()).length, originalPages.length, "closing during initialization also cleans its newly created page");
    } finally { browser.off("targetcreated", stopDuringInitialization); }
  } finally {
    child?.kill("SIGKILL");
    await manual?.close();
    await browser?.close();
    if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
    await rm(directory, { recursive: true, force: true });
  }
});
