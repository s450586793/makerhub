import assert from "node:assert/strict";
import { test } from "node:test";

import { accountOperationalView } from "./accountStatus.js";

test("cookie invalid remains a relogin state after source sync succeeds", () => {
  const view = accountOperationalView({
    state: "cookie_invalid",
    label: "需要重新登录",
    tone: "danger",
    message: "国内站 3MF 下载需要重新登录。",
    action: "login",
  });

  assert.deepEqual(view, {
    label: "需要重新登录",
    statusClass: "is-expired",
    message: "国内站 3MF 下载需要重新登录。",
    action: "login",
  });
});

test("archive-ready account remains a neutral operational state", () => {
  assert.deepEqual(accountOperationalView({
    label: "可归档",
    tone: "ok",
    message: "国际站 3MF 下载可用。",
    action: "none",
  }), {
    label: "可归档",
    statusClass: "",
    message: "国际站 3MF 下载可用。",
    action: "none",
  });
});
