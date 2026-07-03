import assert from "node:assert/strict";
import { test } from "node:test";

import { accountMessageText, accountStatusClass, accountStatusLabel } from "./accountStatus.js";

test("account probe http error is warning when saved profile exists", () => {
  const item = {
    platform: "cn",
    status: "http_error",
    display_name: "艾斯",
    account_id: "2024907479",
    message: "国内账号测试失败，暂时无法确认 Cookie 是否可用。",
  };

  assert.equal(accountStatusLabel(item), "读取受限");
  assert.equal(accountStatusClass(item), "is-warning");
  assert.equal(
    accountMessageText(item),
    "国内账号已保存，账号资料或来源同步可读取；后台检测暂时无法确认账号探针。",
  );
});

test("account probe http error is warning when source sync is usable", () => {
  const item = {
    platform: "global",
    status: "http_error",
    message: "国际账号测试失败，暂时无法确认 Cookie 是否可用。",
  };
  const context = {
    sourceSync: {
      last_status: "success",
      account_name: "艾斯",
      account_uid: "2073587493",
    },
  };

  assert.equal(accountStatusLabel(item, context), "读取受限");
  assert.equal(accountStatusClass(item, context), "is-warning");
  assert.equal(
    accountMessageText(item, context),
    "国际账号已保存，账号资料或来源同步可读取；后台检测暂时无法确认账号探针。",
  );
});

test("account probe http error stays expired without account evidence", () => {
  const item = {
    platform: "cn",
    status: "http_error",
    message: "国内账号测试失败，暂时无法确认 Cookie 是否可用。",
  };

  assert.equal(accountStatusLabel(item), "连接异常");
  assert.equal(accountStatusClass(item), "is-expired");
  assert.equal(accountMessageText(item), "国内账号测试失败，暂时无法确认 Cookie 是否可用。");
});

test("account auth required stays expired even when profile exists", () => {
  const item = {
    platform: "global",
    status: "auth_required",
    display_name: "艾斯",
    message: "国际 Cookie 失效，请重新获取并保存 Cookie。",
  };

  assert.equal(accountStatusLabel(item), "Cookie 失效");
  assert.equal(accountStatusClass(item), "is-expired");
  assert.equal(accountMessageText(item), "国际 Cookie 失效，请重新获取并保存 Cookie。");
});
