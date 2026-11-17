import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { parse as babelParse } from "@babel/parser";
import { parse } from "@vue/compiler-sfc";
import { computed, ref } from "vue";

import * as dashboardStatus from "./dashboardStatus.js";
import * as browserSession from "./browserSession.js";
import { createHydratedResource } from "./useHydratedResource.js";
import {
  dashboardStatusAction,
  dashboardStatusActions,
  dashboardStatusElementKind,
  getSourceRefreshDisplayTotals,
  shouldShowDashboardStatusDetail,
  normalizeRuntimeStatusLabel,
  runtimeTaskAction,
} from "./dashboardStatus.js";

const dashboardSource = readFileSync(new URL("../pages/DashboardPage.vue", import.meta.url), "utf8");
const dashboardScript = parse(dashboardSource).descriptor.scriptSetup.content;
const dashboardAst = babelParse(dashboardScript, { sourceType: "module" });
const dashboardSetup = dashboardAst.program.body
  .filter((node) => node.type !== "ImportDeclaration")
  .map((node) => dashboardScript.slice(node.start, node.end)).join("\n");

const browserAction = {
  kind: "api", label: "打开指纹浏览器", endpoint: "/api/config/online-accounts/global/browser/open",
  method: "POST", opens_browser: true,
};
const verifiedAction = {
  kind: "api", label: "已验证", endpoint: "/api/tasks/missing-3mf/verification-verified",
  method: "POST", body: { platform: "global" },
};

function sourceCard(recovered = false) {
  return {
    key: "global", title: "国际站", state: recovered ? "ok" : "verification_required",
    account_status: recovered ? "ok" : "verification_required",
    three_mf_gate: recovered ? "open" : "verification_required",
    status: recovered ? "可归档" : "需要浏览器确认", checks: [],
    actions: recovered ? [browserAction] : [browserAction, verifiedAction],
  };
}

function snapshot(card = sourceCard()) {
  return { system_status: [card], stats: [], task_summary: {} };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function dashboardPage({ read = () => snapshot(), act = async () => ({ message: "操作已完成。" }), openBrowser = () => ({ closed: false, location: { replace() {} }, close() {} }) } = {}) {
  let mounted;
  let unmounted;
  let subscription;
  let subscribed = false;
  const dependencies = {
    ...dashboardStatus,
    ...browserSession,
    ref, computed, createHydratedResource,
    onMounted: (callback) => { mounted = callback; },
    onBeforeUnmount: (callback) => { unmounted = callback; },
    apiRequest: (endpoint, options) => endpoint === "/api/dashboard/light" ? read(options) : act(endpoint, options),
    createPagePerformanceTracker: () => ({ markDataReady() {}, finish() {} }),
    formatServerDateTime: (value) => value,
    subscribeStateRefresh: (_scopes, callback, options = {}) => {
      subscription = { callback, options };
      subscribed = true;
      return () => { subscribed = false; };
    },
    window: {
      location: { protocol: "http:", hostname: "makerhub.test" },
      open: openBrowser,
    },
    document: { hidden: false, addEventListener() {}, removeEventListener() {} },
    console: { error() {} },
  };
  const page = new Function(...Object.keys(dependencies), dashboardSetup + `
    return { load, loading, payload, runStatusAction, statusActionMessage, isStatusActionBusy, verificationDialog, closeVerificationDialog, handleVerificationCompleted };
  `)(...Object.values(dependencies));
  return {
    ...page,
    mount: () => mounted(),
    unmount: () => unmounted(),
    changed: () => { if (subscribed) subscription.callback(); },
    ready: () => { if (subscribed) subscription.options.onReady?.(); },
  };
}

async function flushDashboard() {
  await new Promise((resolve) => setImmediate(resolve));
}

test("dashboard opens manual verification inside its dialog without opening another browser or submitting twice", async () => {
  const requests = [];
  const action = { kind: "api", label: "去验证", endpoint: "/api/config/online-accounts/global/verification", opens_verification: true };
  const card = { ...sourceCard(), actions: [action] };
  const page = dashboardPage({
    read: () => snapshot(card),
    act: async (...args) => requests.push(args),
    openBrowser: () => assert.fail("manual verification must stay in the MakerHub dialog"),
  });
  await page.mount();
  const normalized = dashboardStatusActions(card)[0];
  assert.equal(normalized.opens_verification, true);
  await page.runStatusAction(card, normalized);
  const firstDialog = page.verificationDialog.value;
  await page.runStatusAction(card, normalized);
  assert.equal(page.verificationDialog.value, firstDialog);
  assert.equal(page.verificationDialog.value.visible, true);
  assert.equal(page.verificationDialog.value.platform, "global");
  assert.equal(page.verificationDialog.value.action.endpoint, action.endpoint);
  assert.equal(requests.length, 0);
  page.closeVerificationDialog();
  assert.equal(page.verificationDialog.value.visible, false);
});

test("dashboard verification dialog switches platform and accepts only confirmed completion", async () => {
  let reads = 0;
  const globalAction = { kind: "api", label: "去验证", endpoint: "/api/config/online-accounts/global/verification", opens_verification: true };
  const globalCard = { ...sourceCard(), actions: [globalAction] };
  const cnAction = { ...globalAction, endpoint: globalAction.endpoint.replace("/global/", "/cn/") };
  const cnCard = { ...globalCard, key: "cn", actions: [cnAction] };
  const page = dashboardPage({ read: () => { reads++; return { system_status: [globalCard, cnCard] }; } });
  await page.mount();
  await page.runStatusAction(globalCard, globalAction);
  await page.runStatusAction(cnCard, cnAction);
  assert.equal(page.verificationDialog.value.platform, "cn");
  const before = reads;
  page.handleVerificationCompleted({ status: "authorized", completed: false });
  assert.equal(reads, before);
  assert.equal(page.statusActionMessage(cnCard), "");
  page.handleVerificationCompleted({ completed: true, message: "下载授权已恢复。" });
  await flushDashboard();
  assert.equal(page.statusActionMessage(cnCard), "下载授权已恢复。");
  assert.equal(page.statusActionMessage(globalCard), "");
  assert.ok(reads > before);
  page.closeVerificationDialog();
  page.handleVerificationCompleted({ completed: true, message: "晚到结果。" });
  assert.equal(page.statusActionMessage(cnCard), "下载授权已恢复。");
});

test("dashboard feedback disappears when its action is removed after recovery", async () => {
  let current = snapshot();
  const page = dashboardPage({ read: () => current, act: async () => { throw new Error("HTTP 502"); } });
  await page.mount();
  await page.runStatusAction(sourceCard(), verifiedAction);
  await flushDashboard();
  assert.equal(page.statusActionMessage(sourceCard()), "HTTP 502");
  current = snapshot(sourceCard(true));
  await page.load();
  assert.equal(page.statusActionMessage(current.system_status[0]), "");
});

test("dashboard keeps a valid action failure and pending state across unchanged health refreshes", async () => {
  const action = deferred();
  const page = dashboardPage({ act: () => action.promise });
  await page.mount();
  const running = page.runStatusAction(sourceCard(), browserAction);
  await page.load();
  assert.equal(page.isStatusActionBusy(sourceCard(), browserAction), true);
  action.reject(new Error("HTTP 502"));
  await running;
  await page.load();
  assert.equal(page.statusActionMessage(sourceCard(), browserAction), "HTTP 502");
  assert.equal(page.isStatusActionBusy(sourceCard(), browserAction), false);
});

test("dashboard ignores a late action failure after the same browser action recovers", async () => {
  let current = snapshot();
  const action = deferred();
  const page = dashboardPage({ read: () => current, act: () => action.promise });
  await page.mount();
  const running = page.runStatusAction(sourceCard(), browserAction);
  current = snapshot(sourceCard(true));
  await page.load();
  action.reject(new Error("HTTP 502"));
  await running;
  await flushDashboard();
  assert.equal(page.statusActionMessage(current.system_status[0]), "");
  assert.equal(page.isStatusActionBusy(current.system_status[0], browserAction), false);
});

test("dashboard card shows the later successful action instead of an earlier action error", async () => {
  const page = dashboardPage({ act: async (endpoint) => {
    if (endpoint === verifiedAction.endpoint) throw new Error("HTTP 502");
    return { message: "浏览器已打开。" };
  } });
  await page.mount();
  await page.runStatusAction(sourceCard(), verifiedAction);
  await flushDashboard();
  await page.runStatusAction(sourceCard(), browserAction);
  await flushDashboard();
  assert.equal(page.statusActionMessage(sourceCard()), "浏览器已打开。");
  assert.equal(page.statusActionMessage(sourceCard(), verifiedAction), "HTTP 502");
});

test("dashboard card shows the latest failure while retaining earlier valid action feedback", async () => {
  const page = dashboardPage({ act: async (endpoint) => {
    throw new Error(endpoint === browserAction.endpoint ? "浏览器启动失败。" : "验证提交失败。");
  } });
  await page.mount();
  await page.runStatusAction(sourceCard(), browserAction);
  await flushDashboard();
  await page.runStatusAction(sourceCard(), verifiedAction);
  await flushDashboard();
  assert.equal(page.statusActionMessage(sourceCard()), "验证提交失败。");
  assert.equal(page.statusActionMessage(sourceCard(), browserAction), "浏览器启动失败。");
});

test("dashboard shows a pending action failure when it finishes after another action succeeds", async () => {
  const verification = deferred();
  const page = dashboardPage({ act: (endpoint) => endpoint === verifiedAction.endpoint
    ? verification.promise : Promise.resolve({ message: "浏览器已打开。" }) });
  await page.mount();
  const running = page.runStatusAction(sourceCard(), verifiedAction);
  await page.runStatusAction(sourceCard(), browserAction);
  await flushDashboard();
  assert.equal(page.isStatusActionBusy(sourceCard(), verifiedAction), true);
  assert.equal(page.statusActionMessage(sourceCard()), "浏览器已打开。");
  verification.reject(new Error("验证提交失败。"));
  await running;
  await flushDashboard();
  assert.equal(page.statusActionMessage(sourceCard()), "验证提交失败。");
  assert.equal(page.statusActionMessage(sourceCard(), browserAction), "浏览器已打开。");
});

test("dashboard action completion preserves failures on another source card", async () => {
  const cnAction = { ...browserAction, endpoint: "/api/config/online-accounts/cn/browser/open" };
  const cnCard = { ...sourceCard(), key: "cn", title: "国内站", actions: [cnAction] };
  const page = dashboardPage({
    read: () => ({ system_status: [sourceCard(), cnCard] }),
    act: async (endpoint) => {
      if (endpoint === cnAction.endpoint) throw new Error("国内站浏览器启动失败。");
      return { message: "国际站浏览器已打开。" };
    },
  });
  await page.mount();
  await page.runStatusAction(cnCard, cnAction);
  await flushDashboard();
  await page.runStatusAction(sourceCard(), browserAction);
  await flushDashboard();
  assert.equal(page.statusActionMessage(sourceCard()), "国际站浏览器已打开。");
  assert.equal(page.statusActionMessage(cnCard), "国内站浏览器启动失败。");
});

test("dashboard refreshes backend status after an action fails", async () => {
  let current = snapshot();
  const page = dashboardPage({ read: () => current, act: async () => {
    current = snapshot(sourceCard(true));
    throw new Error("HTTP 502");
  } });
  await page.mount();
  await page.runStatusAction(sourceCard(), verifiedAction);
  await flushDashboard();
  assert.equal(page.payload.value.system_status[0].status, "可归档");
  assert.equal(page.statusActionMessage(page.payload.value.system_status[0]), "");
});

test("dashboard subscribes before the first response so a concurrent recovery is retained", async () => {
  const first = deferred();
  let initial = true;
  const page = dashboardPage({ read: () => {
    if (initial) {
      initial = false;
      return first.promise;
    }
    return snapshot(sourceCard(true));
  } });
  const mounting = page.mount();
  page.changed();
  await flushDashboard();
  first.resolve(snapshot());
  await mounting;
  assert.equal(page.payload.value.system_status[0].status, "可归档");
});

test("dashboard refreshes after SSE readiness closes the initial connection gap", async () => {
  let current = snapshot();
  const page = dashboardPage({ read: () => current });
  await page.mount();
  current = snapshot(sourceCard(true));
  page.ready();
  assert.equal(page.loading.value, false);
  await flushDashboard();
  assert.equal(page.payload.value.system_status[0].status, "可归档");
});

test("dashboard keeps the initial skeleton until the ready refresh completes after aborting the first request", async () => {
  const first = deferred();
  const recovery = deferred();
  let initial = true;
  const page = dashboardPage({ read: ({ signal }) => {
    if (initial) {
      initial = false;
      signal.addEventListener("abort", () => first.reject(Object.assign(new Error("Aborted"), { name: "AbortError" })));
      return first.promise;
    }
    return recovery.promise;
  } });
  const mounting = page.mount();
  page.ready();
  await mounting;
  assert.equal(page.loading.value, true);
  recovery.resolve(snapshot(sourceCard(true)));
  await flushDashboard();
  assert.equal(page.loading.value, false);
  assert.equal(page.payload.value.system_status[0].status, "可归档");
});

test("dashboard source health card body is not an external link", () => {
  const card = {
    key: "cn",
    title: "国内站",
    url: "https://makerworld.com.cn",
    action_label: "打开官网",
    checks: [{ state: "ok" }],
  };

  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "external",
    label: "打开官网",
    href: "https://makerworld.com.cn",
  });
});

test("historical 3MF source health issue does not add homepage retry action", () => {
  const card = {
    key: "global",
    title: "国际站",
    url: "https://makerworld.com",
    action_label: "打开官网",
    checks: [{ state: "historical_3mf_issue" }],
  };

  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "external",
    label: "打开官网",
    href: "https://makerworld.com",
  });
  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com",
    },
  ]);
});

test("verification status card only opens the platform homepage", () => {
  const card = {
    key: "global",
    action_label: "访问主页",
    url: "https://makerworld.com",
    checks: [{ state: "verification_required" }],
  };

  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "external",
    label: "访问主页",
    href: "https://makerworld.com",
  });
  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "访问主页",
      href: "https://makerworld.com",
    },
  ]);
});

test("cn verification status card does not infer verified retry action", () => {
  const card = {
    key: "cn",
    action_label: "打开官网",
    url: "https://makerworld.com.cn",
    state: "verification_required",
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com.cn",
    },
  ]);
});

test("verification status card keeps explicit verified retry action from backend", () => {
  const card = {
    key: "global",
    action_label: "打开官网",
    url: "https://makerworld.com",
    state: "verification_required",
    actions: [
      {
        kind: "external",
        label: "打开官网",
        href: "https://makerworld.com",
      },
      {
        kind: "api",
        label: "已验证",
        endpoint: "/api/tasks/missing-3mf/verification-verified",
        method: "POST",
        body: { platform: "global" },
      },
    ],
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com",
    },
    {
      kind: "api",
      label: "已验证",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "global" },
    },
  ]);
});

test("browser-managed source card preserves the browser login action", () => {
  const card = {
    key: "cn",
    action_label: "打开指纹浏览器",
    actions: [{
      kind: "api",
      label: "打开指纹浏览器",
      endpoint: "/api/config/online-accounts/cn/browser/open",
      method: "POST",
      opens_browser: true,
    }],
  };

  assert.deepEqual(dashboardStatusActions(card), [{
    kind: "api",
    label: "打开指纹浏览器",
    endpoint: "/api/config/online-accounts/cn/browser/open",
    method: "POST",
    opens_browser: true,
  }]);
});

test("localized verification source status text does not infer retry action", () => {
  const card = {
    key: "cn",
    action_label: "打开官网",
    url: "https://makerworld.com.cn",
    status: "需要验证",
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com.cn",
    },
  ]);
});

test("cookie invalid source card only opens the platform homepage", () => {
  const card = {
    key: "global",
    action_label: "打开官网",
    url: "https://makerworld.com",
    state: "cookie_invalid",
    status: "Cookie 异常",
    detail: "国际区下载 3MF 需要有效登录态；如果最近出现验证页，请更新 global Cookie / token，必要时补充 cf_clearance。",
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com",
    },
  ]);
});

test("auth required source card does not infer cookie refresh retry", () => {
  const card = {
    key: "cn",
    action_label: "打开官网",
    url: "https://makerworld.com.cn",
    state: "auth_required",
    status: "Cookie 失效",
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com.cn",
    },
  ]);
});

test("cookie status text without state does not infer cookie refresh retry", () => {
  const card = {
    key: "global",
    action_label: "打开官网",
    url: "https://makerworld.com",
    status: "Cookie 异常",
    detail: "国际区下载 3MF 需要有效登录态；如果最近出现验证页，请更新 global Cookie / token，必要时补充 cf_clearance。",
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "打开官网",
      href: "https://makerworld.com",
    },
  ]);
});

test("source health cards with checks hide duplicated detail", () => {
  assert.equal(shouldShowDashboardStatusDetail({
    detail: "账号连接正常；3MF 下载历史失败待重试。",
    checks: [{ source: "account" }, { source: "download" }],
  }), false);
  assert.equal(shouldShowDashboardStatusDetail({
    detail: "当前已配置代理地址。",
    checks: [],
  }), true);
});

test("runtime status labels distinguish waiting children and blocked verification", () => {
  assert.equal(normalizeRuntimeStatusLabel("waiting_children"), "等待子任务");
  assert.equal(normalizeRuntimeStatusLabel("blocked", "needs_verification"), "需要验证");
});

test("queue repair action points to archive queue repair endpoint", () => {
  assert.deepEqual(runtimeTaskAction({ status: "running", stale: true }), {
    kind: "api",
    label: "修复队列",
    endpoint: "/api/tasks/archive-queue/repair",
    method: "POST",
  });
});

test("blocked verification action opens official homepage", () => {
  assert.deepEqual(runtimeTaskAction({
    status: "blocked",
    blocked_reason: "needs_verification",
    url: "https://makerworld.com",
  }), {
    kind: "external",
    label: "访问主页",
    href: "https://makerworld.com",
  });
});

test("source refresh display totals prefer active source-refresh run", () => {
  assert.deepEqual(getSourceRefreshDisplayTotals({
    remote_refresh: {
      active_run: {
        batch_id: "legacy-batch",
        status: "running",
        candidate_total: 100,
        completed_total: 20,
        remaining_total: 80,
      },
    },
    source_refresh: {
      runs: {
        active_run: {
          run_id: "source-run",
          status: "running",
          candidate_total: 8,
          completed_total: 3,
          remaining_total: 5,
        },
      },
    },
  }), {
    total: 8,
    completed: 3,
    remaining: 5,
    source: "source_refresh",
  });
});

test("source refresh display totals fall back to legacy active run and last summary", () => {
  assert.deepEqual(getSourceRefreshDisplayTotals({
    active_run: {
      batch_id: "legacy-batch",
      status: "resuming",
      candidate_total: 9,
      completed_total: 4,
      remaining_total: 5,
    },
    last_batch_total: 12,
    last_batch_succeeded: 6,
    last_batch_failed: 1,
    last_batch_skipped: 2,
    last_remaining_total: 3,
  }), {
    total: 9,
    completed: 4,
    remaining: 5,
    source: "remote_refresh_active",
  });

  assert.deepEqual(getSourceRefreshDisplayTotals({
    remote_refresh: {
      last_batch_total: 12,
      last_batch_succeeded: 6,
      last_batch_failed: 1,
      last_batch_skipped: 2,
      last_remaining_total: 3,
    },
  }), {
    total: 12,
    completed: 9,
    remaining: 3,
    source: "remote_refresh_summary",
  });
});
