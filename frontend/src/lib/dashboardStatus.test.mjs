import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dashboardStatusAction,
  dashboardStatusActions,
  dashboardStatusElementKind,
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

test("historical 3MF source health issue opens the platform homepage", () => {
  const card = {
    key: "global",
    title: "国际站",
    url: "https://makerworld.com",
    action_label: "访问主页",
    checks: [{ state: "historical_3mf_issue" }],
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
      label: "重试 3MF",
      endpoint: "/api/tasks/missing-3mf/retry-all",
      method: "POST",
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
      label: "重试 3MF",
      endpoint: "/api/tasks/missing-3mf/retry-all",
      method: "POST",
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
