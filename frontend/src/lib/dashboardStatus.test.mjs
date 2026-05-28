import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dashboardStatusAction,
  dashboardStatusElementKind,
  needsBrowserVerification,
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

test("historical 3MF source health issue routes to tasks page", () => {
  const card = {
    key: "cn",
    title: "国内站",
    route: "/tasks",
    action_label: "进入任务页",
    checks: [{ state: "historical_3mf_issue" }],
  };

  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "route",
    label: "进入任务页",
    to: "/tasks",
  });
});

test("verification status card exposes a browser verification action", () => {
  const card = {
    key: "global",
    action_label: "去验证",
    url: "https://makerworld.com",
    checks: [{ state: "verification_required" }],
  };

  assert.equal(needsBrowserVerification(card), true);
  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "browser-verification",
    label: "去验证",
  });
});
