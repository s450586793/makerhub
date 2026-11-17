import assert from "node:assert/strict";
import { test } from "node:test";

import { createManualVerificationController, verificationImageUrl, verificationKeyboardInput, verificationPointerPoint } from "./manualVerification.js";

const endpoint = "/api/config/online-accounts/cn/verification";
const session = (patch = {}) => ({ session_id: "manual_cn", platform: "cn", status: "challenge", message: "请完成验证。", image: "aQ==", width: 1200, height: 800, completed: false, ...patch });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

function harness(respond = async () => session()) {
  const calls = [];
  const changes = [];
  const completed = [];
  const timers = new Map();
  let timerId = 0;
  const controller = createManualVerificationController({
    request: async (path, options) => {
      calls.push({ path, ...options });
      return respond(path, options);
    },
    onChange: (value) => changes.push(value),
    onComplete: (value) => completed.push(value),
    schedule: (callback) => { timers.set(++timerId, callback); return timerId; },
    unschedule: (id) => timers.delete(id),
  });
  return { controller, calls, changes, completed, timers, state: () => changes.at(-1) };
}

test("manual verification maps a scaled image to viewport coordinates and clamps captured drags", () => {
  const bounds = { left: 20, top: 50, width: 600, height: 400 };
  assert.deepEqual(verificationPointerPoint({ clientX: 320, clientY: 250 }, bounds, 1200, 800), { x: 600, y: 400 });
  assert.deepEqual(verificationPointerPoint({ clientX: -20, clientY: 900 }, bounds, 1200, 800), { x: 0, y: 799 });
  assert.equal(verificationPointerPoint({ clientX: NaN, clientY: 0 }, bounds, 1200, 800), null);
  assert.equal(verificationPointerPoint({ clientX: 0, clientY: 0 }, { ...bounds, width: 0 }, 1200, 800), null);
});

test("manual verification renders only inline JPEG data and rejects external image URLs", () => {
  assert.equal(verificationImageUrl("aQ=="), "data:image/jpeg;base64,aQ==");
  for (const value of ["https://example.test/image", "javascript:alert(1)", "data:image/svg+xml;base64,aQ==", "<img>", ""]) {
    assert.equal(verificationImageUrl(value), "");
  }
});

test("remote keyboard input preserves typed characters, shortcuts and navigation without forwarding composition", () => {
  assert.deepEqual(verificationKeyboardInput({ key: "A", shiftKey: true }), { type: "text", text: "A" });
  assert.deepEqual(verificationKeyboardInput({ key: "中" }), { type: "text", text: "中" });
  assert.deepEqual(verificationKeyboardInput({ key: "a", ctrlKey: true }), { type: "key", key: "Control+a" });
  assert.deepEqual(verificationKeyboardInput({ key: "Tab", shiftKey: true }), { type: "key", key: "Shift+Tab" });
  assert.deepEqual(verificationKeyboardInput({ key: "Backspace" }), { type: "key", key: "Backspace" });
  for (const event of [{ key: "Process" }, { key: "Dead" }, { key: "Shift", shiftKey: true }, { key: "Escape" }, { key: "a", isComposing: true }]) {
    assert.equal(verificationKeyboardInput(event), null);
  }
});

test("opening a session publishes its challenge and polls a single frame at a time", async () => {
  const frame = deferred();
  const page = harness(async (path) => path.endsWith("/frame") ? frame.promise : session());
  await page.controller.open({ endpoint });
  assert.equal(page.state().image, "aQ==");
  assert.equal(page.timers.size, 1);
  const first = page.controller.refresh();
  const second = page.controller.refresh();
  await flush();
  assert.equal(page.calls.filter((call) => call.path.endsWith("/frame")).length, 1);
  assert.equal(page.timers.size, 0);
  frame.resolve(session({ image: "bQ==" }));
  await Promise.all([first, second]);
  assert.equal(page.state().image, "bQ==");
  assert.equal(page.timers.size, 1);
  await page.controller.close();
});

test("a missing initial image loads a frame immediately", async () => {
  const page = harness(async (path) => path === endpoint ? session({ image: "" }) : session());
  await page.controller.open({ endpoint });
  assert.deepEqual(page.calls.map((call) => call.path), [endpoint, `${endpoint}/manual_cn/frame`]);
  assert.equal(page.state().loading, false);
  assert.equal(page.state().image, "aQ==");
  await page.controller.close();
});

test("canceling while create is pending deletes the late-created session without publishing it", async () => {
  const creation = deferred();
  const page = harness(async (path) => path === endpoint ? creation.promise : {});
  const opening = page.controller.open({ endpoint });
  await flush();
  const closing = page.controller.close();
  const count = page.changes.length;
  creation.resolve(session());
  await opening;
  await closing;
  assert.equal(page.changes.length, count);
  assert.deepEqual(page.calls.at(-1), { path: `${endpoint}/manual_cn`, method: "DELETE" });
  assert.equal(page.timers.size, 0);
});

test("switching platforms invalidates late responses and cleans the old session", async () => {
  const cn = deferred();
  const globalEndpoint = endpoint.replace("/cn/", "/global/");
  const page = harness(async (path) => path === endpoint ? cn.promise : path === globalEndpoint ? session({ session_id: "manual_global", platform: "global" }) : {});
  const first = page.controller.open({ endpoint });
  await flush();
  const second = page.controller.open({ endpoint: globalEndpoint });
  await flush();
  assert.equal(page.calls.some((call) => call.path === globalEndpoint), false);
  cn.resolve(session());
  await first;
  await second;
  assert.equal(page.state().platform, "global");
  assert.equal(page.state().session_id, "manual_global");
  assert.ok(page.calls.some((call) => call.method === "DELETE" && call.path === `${endpoint}/manual_cn`));
  await page.controller.close();
});

test("reopening waits for the previous session deletion to release its browser lease", async () => {
  const deletion = deferred();
  const page = harness(async (_path, options) => options.method === "DELETE" ? deletion.promise : session());
  await page.controller.open({ endpoint });
  const second = page.controller.open({ endpoint });
  await flush();
  assert.equal(page.calls.filter((call) => call.path === endpoint).length, 1);
  deletion.resolve({});
  await second;
  assert.equal(page.calls.filter((call) => call.path === endpoint).length, 2);
  await page.controller.close();
});

test("closing invalidates in-flight frames and cannot emit a late success", async () => {
  const frame = deferred();
  const page = harness(async (path) => path.endsWith("/frame") ? frame.promise : session());
  await page.controller.open({ endpoint });
  const loading = page.controller.refresh();
  await flush();
  await page.controller.close();
  const count = page.changes.length;
  frame.resolve(session({ completed: true }));
  await loading;
  assert.equal(page.changes.length, count);
  assert.equal(page.completed.length, 0);
  assert.equal(page.timers.size, 0);
});

test("drag input stays ordered across requests and finish waits for the final pointer release", async () => {
  const firstInput = deferred();
  let inputs = 0;
  const page = harness(async (path) => {
    if (path.endsWith("/input") && inputs++ === 0) return firstInput.promise;
    return session({ completed: path.endsWith("/finish") });
  });
  await page.controller.open({ endpoint });
  const down = page.controller.sendEvents([{ type: "pointerdown", x: 20, y: 30 }]);
  await flush();
  page.controller.sendEvents([{ type: "pointermove", x: 40, y: 30, delay_ms: 16 }]);
  page.controller.sendEvents([{ type: "pointermove", x: 70, y: 30, delay_ms: 20 }]);
  page.controller.sendEvents([{ type: "pointerup", x: 70, y: 30, delay_ms: 25 }]);
  const finishing = page.controller.finish();
  await flush();
  assert.equal(page.calls.filter((call) => call.path.endsWith("/input")).length, 1);
  assert.equal(page.calls.some((call) => call.path.endsWith("/finish")), false);
  firstInput.resolve(session());
  await down;
  await finishing;
  const operations = page.calls.filter((call) => call.path.endsWith("/input") || call.path.endsWith("/finish"));
  assert.deepEqual(operations.map((call) => call.path.split("/").at(-1)), ["input", "input", "finish"]);
  assert.deepEqual(operations[1].body.events, [{ type: "pointermove", x: 40, y: 30, delay_ms: 16 }, { type: "pointermove", x: 70, y: 30, delay_ms: 20 }, { type: "pointerup", x: 70, y: 30, delay_ms: 25 }]);
  assert.equal(page.completed.length, 1);
  await page.controller.close();
});

test("input batches are bounded while keeping all text and key events", async () => {
  const page = harness();
  await page.controller.open({ endpoint });
  const events = Array.from({ length: 177 }, (_, index) => ({ type: "text", text: String(index) }));
  await page.controller.sendEvents(events);
  const batches = page.calls.filter((call) => call.path.endsWith("/input")).map((call) => call.body.events);
  assert.deepEqual(batches.map((batch) => batch.length), [80, 80, 17]);
  assert.deepEqual(batches.flat(), events);
  await page.controller.close();
});

test("an overloaded drag buffer remains bounded and retains its final release", async () => {
  const input = deferred();
  let count = 0;
  const page = harness(async (path) => path.endsWith("/input") && count++ === 0 ? input.promise : session());
  await page.controller.open({ endpoint });
  const sending = page.controller.sendEvents([{ type: "pointerdown", x: 0, y: 20, delay_ms: 0 }]);
  await flush();
  page.controller.sendEvents(Array.from({ length: 900 }, (_, x) => ({ type: "pointermove", x, y: 20, delay_ms: 16 })));
  page.controller.sendEvents([{ type: "pointerup", x: 900, y: 20, delay_ms: 16 }]);
  input.resolve(session());
  await sending;
  const buffered = page.calls.filter((call) => call.path.endsWith("/input")).slice(1).flatMap((call) => call.body.events);
  assert.equal(buffered.length, 600);
  assert.equal(buffered.at(-1).type, "pointerup");
  assert.ok(buffered.slice(0, -1).every((event, index, items) => index === 0 || event.x > items[index - 1].x));
  await page.controller.close();
});

test("verification success requires the server completed flag and is emitted once", async () => {
  let confirmed = false;
  const page = harness(async () => session({ status: "authorized", completed: confirmed }));
  await page.controller.open({ endpoint });
  await page.controller.finish();
  assert.equal(page.completed.length, 0);
  assert.equal(page.state().completed, false);
  confirmed = true;
  await page.controller.refresh();
  await page.controller.finish();
  assert.equal(page.completed.length, 1);
  assert.equal(page.timers.size, 0);
  await page.controller.close();
});

test("mismatched sessions and invalid entry points are rejected", async () => {
  const page = harness(async () => session({ platform: "global" }));
  await page.controller.open({ endpoint });
  assert.equal(page.state().status, "error");
  assert.match(page.state().error, /会话不匹配/);
  assert.equal(page.completed.length, 0);
  await page.controller.close();
  const calls = page.calls.length;
  await page.controller.open({ endpoint: "https://example.test/verification" });
  assert.equal(page.calls.length, calls);
  assert.match(page.state().error, /入口无效/);
  await page.controller.close();
});

test("transient frame errors keep the current image and allow polling to recover", async () => {
  let fail = true;
  const page = harness(async (path) => {
    if (path.endsWith("/frame") && fail) throw new Error("HTTP 502");
    return session();
  });
  await page.controller.open({ endpoint });
  await page.controller.refresh();
  assert.equal(page.state().image, "aQ==");
  assert.equal(page.state().error, "HTTP 502");
  assert.equal(page.timers.size, 1);
  fail = false;
  await page.controller.refresh();
  assert.equal(page.state().error, "");
  await page.controller.close();
});

test("canceling after input begins drops queued events and releases the session", async () => {
  const input = deferred();
  const page = harness(async (path) => path.endsWith("/input") ? input.promise : session());
  await page.controller.open({ endpoint });
  const sending = page.controller.sendEvents([{ type: "pointerdown", x: 10, y: 20 }]);
  await flush();
  page.controller.sendEvents([{ type: "pointerup", x: 10, y: 20 }]);
  await page.controller.close();
  input.resolve(session());
  await sending;
  assert.equal(page.calls.filter((call) => call.path.endsWith("/input")).length, 1);
  assert.equal(page.calls.at(-1).method, "DELETE");
  assert.equal(page.timers.size, 0);
});
