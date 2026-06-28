import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dashboardStatusAction,
  dashboardStatusActions,
  dashboardStatusElementKind,
  getSourceRefreshDisplayTotals,
  shouldShowDashboardStatusDetail,
  normalizeRuntimeStatusLabel,
  runtimeTaskAction,
} from "./dashboardStatus.js";

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

test("verification status card opens the platform homepage", () => {
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
    {
      kind: "api",
      label: "已验证",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "global" },
    },
  ]);
});

test("cn verification status card submits verified retry for cn platform", () => {
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
    {
      kind: "api",
      label: "已验证",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "cn" },
    },
  ]);
});

test("dashboard status card uses explicit backend actions before inferred recovery actions", () => {
  const card = {
    key: "global",
    action_label: "手动过 CF",
    url: "https://makerworld.com",
    state: "verification_required",
    actions: [
      {
        kind: "external",
        label: "手动过 CF",
        href: "https://makerworld.com",
      },
      {
        kind: "api",
        label: "重新检测",
        endpoint: "/api/config/online-accounts/global/test",
        method: "POST",
        body: {},
      },
    ],
  };

  assert.deepEqual(dashboardStatusActions(card), [
    {
      kind: "external",
      label: "手动过 CF",
      href: "https://makerworld.com",
    },
    {
      kind: "api",
      label: "重新检测",
      endpoint: "/api/config/online-accounts/global/test",
      method: "POST",
      body: {},
    },
  ]);
});

test("verification retry action tolerates localized source status text", () => {
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
    {
      kind: "api",
      label: "已验证",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "cn" },
    },
  ]);
});

test("cookie invalid source card submits cookie refresh retry", () => {
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
    {
      kind: "api",
      label: "已更新 Cookie",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "global" },
    },
  ]);
});

test("auth required source card submits cookie refresh retry", () => {
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
    {
      kind: "api",
      label: "已更新 Cookie",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "cn" },
    },
  ]);
});

test("cookie status text without state submits cookie refresh retry", () => {
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
    {
      kind: "api",
      label: "已更新 Cookie",
      endpoint: "/api/tasks/missing-3mf/verification-verified",
      method: "POST",
      body: { platform: "global" },
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
