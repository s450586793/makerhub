import assert from "node:assert/strict";
import { test } from "node:test";

import { createManualVerificationController } from "./manualVerification.js";

const endpoint = "/api/config/online-accounts/cn/verification";
const challenge = (patch = {}) => ({ challenge_id: "challenge-1", captcha_id: "captcha-1", ...patch });
const proof = { captcha_id: "captcha-1", lot_number: "lot", pass_token: "token", gen_time: "123", captcha_output: "output" };
const session = (patch = {}) => ({ session_id: "manual_cn", platform: "cn", status: "challenge", message: "请完成验证。", completed: false, challenge: challenge(), ...patch });

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }

function harness(respond = async () => session(), options = {}) {
  const calls = [];
  const changes = [];
  const completed = [];
  const timers = new Map();
  const signals = [];
  let timerId = 0;
  const controller = createManualVerificationController({
    request: async (path, options) => {
      const { signal, ...visibleOptions } = options;
      calls.push({ path, ...visibleOptions }); signals.push(signal);
      return respond(path, options);
    },
    onChange: (value) => changes.push(value),
    onComplete: (value) => completed.push(value),
    schedule: (callback, delay) => { callback.delay = delay; timers.set(++timerId, callback); return timerId; },
    unschedule: (id) => timers.delete(id),
    ...options,
  });
  function tick(delay) {
    const timer = [...timers].find(([, callback]) => callback.delay === delay);
    assert.ok(timer, `expected pending timer with delay ${delay}`);
    timers.delete(timer[0]); timer[1]();
  }
  return { controller, calls, changes, completed, timers, signals, tick, state: () => changes.at(-1) };
}

test("a ready challenge waits for SDK callbacks without image or status polling", async () => {
  const page = harness();
  await page.controller.open({ endpoint });
  assert.equal(page.state().challenge.challenge_id, "challenge-1");
  assert.equal(page.state().loading, false);
  assert.equal(page.timers.size, 0);
  assert.deepEqual(page.calls, [{ path: endpoint, method: "POST", body: {} }]);
  await page.controller.close();
});

test("preparing and verifying poll a single lightweight GET and stop when the challenge is ready", async () => {
  const status = deferred();
  const page = harness(async (path) => path === endpoint ? session({ status: "preparing", challenge: null }) : status.promise);
  await page.controller.open({ endpoint });
  assert.equal(page.timers.size, 1);
  const first = page.controller.refresh();
  const second = page.controller.refresh();
  await flush();
  assert.deepEqual(page.calls.at(-1), { path: `${endpoint}/manual_cn`, method: "GET" });
  assert.equal(page.calls.length, 2);
  status.resolve(session());
  await Promise.all([first, second]);
  assert.equal(page.timers.size, 0);
  await page.controller.close();
});

test("SDK success submits the official proof once and only server completion emits success", async () => {
  const submitted = deferred();
  let checked = false;
  const page = harness(async (path) => {
    if (path.endsWith("/complete")) return submitted.promise;
    return checked ? session({ status: "completed", completed: true, challenge: null }) : session();
  });
  await page.controller.open({ endpoint });
  const first = page.controller.complete("challenge-1", proof);
  const duplicate = page.controller.complete("challenge-1", proof);
  await flush();
  assert.equal(page.calls.filter((call) => call.path.endsWith("/complete")).length, 1);
  assert.deepEqual(page.calls.at(-1).body, { challenge_id: "challenge-1", proof });
  assert.equal(page.completed.length, 0);
  submitted.resolve(session({ status: "verifying", challenge: null }));
  await Promise.all([first, duplicate]);
  assert.equal(page.timers.size, 1);
  checked = true;
  await page.controller.refresh();
  assert.equal(page.completed.length, 1);
  assert.equal(page.timers.size, 0);
  await page.controller.complete("challenge-1", proof);
  assert.equal(page.completed.length, 1);
  await page.controller.close();
});

test("a replaced challenge rejects stale SDK callbacks", async () => {
  const page = harness(async (_path, options) => options.method === "GET" ? session({ challenge: challenge({ challenge_id: "challenge-2" }) }) : session());
  await page.controller.open({ endpoint });
  await page.controller.refresh();
  await page.controller.complete("challenge-1", proof);
  assert.equal(page.calls.some((call) => call.path.endsWith("/complete")), false);
  await page.controller.complete("challenge-2", proof);
  assert.equal(page.calls.filter((call) => call.path.endsWith("/complete")).length, 1);
  await page.controller.close();
});

test("closing during create deletes the late session without publishing or mounting its challenge", async () => {
  const creation = deferred();
  const page = harness(async (path) => path === endpoint ? creation.promise : {});
  const opening = page.controller.open({ endpoint });
  await flush();
  const closing = page.controller.close();
  const count = page.changes.length;
  creation.resolve(session());
  await Promise.all([opening, closing]);
  await flush();
  assert.equal(page.changes.length, count);
  assert.deepEqual(page.calls.at(-1), { path: `${endpoint}/manual_cn`, method: "DELETE" });
  assert.equal(page.timers.size, 0);
});

test("switching platforms waits for old cleanup and ignores late proof responses", async () => {
  const submitted = deferred();
  const deletion = deferred();
  const globalEndpoint = endpoint.replace("/cn/", "/global/");
  const page = harness(async (path, options) => {
    if (path.endsWith("/complete")) return submitted.promise;
    if (options.method === "DELETE" && path.startsWith(endpoint)) return deletion.promise;
    return path === globalEndpoint ? session({ session_id: "manual_global", platform: "global" }) : session();
  });
  await page.controller.open({ endpoint });
  const submit = page.controller.complete("challenge-1", proof);
  await flush();
  const switching = page.controller.open({ endpoint: globalEndpoint });
  await flush();
  assert.equal(page.calls.some((call) => call.path === globalEndpoint), false);
  deletion.resolve({});
  await switching;
  submitted.resolve(session({ status: "completed", completed: true }));
  await submit;
  assert.equal(page.state().platform, "global");
  assert.equal(page.completed.length, 0);
  await page.controller.close();
});

test("terminal and failed requests stop polling and allow a fresh session", async () => {
  for (const status of ["failed", "cancelled", "expired"]) {
    const page = harness(async () => session({ status, challenge: null }));
    await page.controller.open({ endpoint });
    assert.equal(page.state().loading, false);
    assert.equal(page.timers.size, 0);
    await page.controller.complete("challenge-1", proof);
    assert.equal(page.calls.length, 1);
    await page.controller.close();
  }
  const page = harness(async (path) => {
    if (path.endsWith("/complete")) throw new Error("验证已过期，请重试。");
    return session();
  });
  await page.controller.open({ endpoint });
  await page.controller.complete("challenge-1", proof);
  assert.equal(page.state().status, "failed");
  assert.match(page.state().error, /已过期/);
  assert.equal(page.timers.size, 0);
  await page.controller.open({ endpoint });
  assert.equal(page.state().status, "challenge");
  assert.equal(page.state().error, "");
  await page.controller.close();
});

test("invalid endpoints and mismatched responses fail without exposing a challenge", async () => {
  const invalid = harness();
  await invalid.controller.open({ endpoint: "https://evil.test/verification" });
  assert.equal(invalid.calls.length, 0);
  assert.equal(invalid.state().status, "failed");
  const mismatch = harness(async () => session({ platform: "global" }));
  await mismatch.controller.open({ endpoint });
  assert.equal(mismatch.state().status, "failed");
  assert.equal(mismatch.state().challenge, null);
  await mismatch.controller.close();
});

test("a hung create is cancelled on close so reopening can proceed and late distinct sessions are cleaned", async () => {
  const creation = deferred();
  let creates = 0;
  const page = harness(async (path) => path === endpoint && creates++ === 0 ? creation.promise : session({ session_id: "new_session" }));
  const opening = page.controller.open({ endpoint });
  await flush();
  assert.equal(page.signals[0]?.aborted, false);
  const closing = page.controller.close();
  await flush();
  assert.equal(page.signals[0].aborted, true);
  await closing;
  await page.controller.open({ endpoint });
  assert.equal(page.state().session_id, "new_session");
  creation.resolve(session({ session_id: "late_session" }));
  await opening;
  await flush();
  assert.ok(page.calls.some((call) => call.path === `${endpoint}/late_session` && call.method === "DELETE"));
  assert.equal(page.state().session_id, "new_session");
  await page.controller.close();
  assert.equal(page.timers.size, 0);
});

test("create timeout reports retry without replay and late cleanup never deletes a newer reused session", async () => {
  const creation = deferred();
  let creates = 0;
  const page = harness(async (path) => path === endpoint && creates++ === 0 ? creation.promise : session({ session_id: "shared_session" }), { requestTimeout: 15000 });
  const opening = page.controller.open({ endpoint });
  await flush();
  page.tick(15000);
  await opening;
  assert.equal(page.state().status, "failed");
  assert.match(page.state().error, /超时.*重试/);
  assert.equal(creates, 1);
  await page.controller.open({ endpoint });
  creation.resolve(session({ session_id: "shared_session" }));
  await flush();
  assert.equal(page.calls.some((call) => call.method === "DELETE"), false);
  assert.equal(page.state().session_id, "shared_session");
  await page.controller.close();
});

test("late create cannot delete an active replacement while its session id is still unknown", async () => {
  const first = deferred();
  const second = deferred();
  let creates = 0;
  const page = harness(async (path) => path === endpoint ? (++creates === 1 ? first.promise : second.promise) : {});
  const opening = page.controller.open({ endpoint });
  await flush();
  await page.controller.close();
  const replacement = page.controller.open({ endpoint });
  await flush();
  first.resolve(session({ session_id: "shared_session" }));
  await opening;
  await flush();
  assert.equal(page.calls.some((call) => call.method === "DELETE"), false);
  second.resolve(session({ session_id: "shared_session" }));
  await replacement;
  assert.equal(page.state().session_id, "shared_session");
  await page.controller.close();
});

test("hung status and proof requests time out without accepting late success or replaying proof", async () => {
  for (const operation of ["status", "proof"]) {
    const pending = deferred();
    const page = harness(async (path, options) => path.endsWith("/complete") || options.method === "GET" ? pending.promise : session());
    await page.controller.open({ endpoint });
    const request = operation === "status" ? page.controller.refresh() : page.controller.complete("challenge-1", proof);
    await flush();
    page.tick(15000);
    await request;
    assert.equal(page.state().status, "failed");
    assert.match(page.state().error, /超时.*重试/);
    assert.equal(page.signals[1].aborted, true);
    pending.resolve(session({ status: "completed", completed: true, challenge: null }));
    await flush();
    assert.equal(page.completed.length, 0);
    await page.controller.complete("challenge-1", proof);
    assert.equal(page.calls.length, 2);
    await page.controller.close();
    assert.equal(page.timers.size, 0);
  }
});

test("a hung DELETE has a finite deadline and cannot permanently block reopening", async () => {
  const deletion = deferred();
  const page = harness(async (_path, options) => options.method === "DELETE" ? deletion.promise : session());
  await page.controller.open({ endpoint });
  const closing = page.controller.close();
  await flush();
  const reopened = page.controller.open({ endpoint });
  await flush();
  assert.equal(page.calls.filter((call) => call.path === endpoint).length, 1);
  page.tick(15000);
  await Promise.all([closing, reopened]);
  assert.equal(page.calls.filter((call) => call.path === endpoint).length, 2);
  assert.equal(page.signals[1].aborted, true);
  const finalClose = page.controller.close();
  await flush();
  page.tick(15000);
  await finalClose;
  assert.equal(page.timers.size, 0);
});

test("a late cleanup already dispatched before reopening is awaited with a finite deadline", async () => {
  const creation = deferred();
  const deletion = deferred();
  let creates = 0;
  const page = harness(async (path, options) => {
    if (options.method === "DELETE") return deletion.promise;
    return path === endpoint && creates++ === 0 ? creation.promise : session({ session_id: "replacement" });
  });
  const opening = page.controller.open({ endpoint });
  await flush();
  await page.controller.close();
  creation.resolve(session({ session_id: "late_session" }));
  await opening;
  await flush();
  assert.ok(page.calls.some((call) => call.method === "DELETE"));
  const reopening = page.controller.open({ endpoint });
  await flush();
  assert.equal(creates, 1);
  page.tick(15000);
  await reopening;
  assert.equal(creates, 2);
  assert.equal(page.state().session_id, "replacement");
  deletion.resolve({});
  await page.controller.close();
  assert.equal(page.timers.size, 0);
});
