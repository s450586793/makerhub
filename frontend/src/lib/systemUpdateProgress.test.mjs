import assert from "node:assert/strict";
import { test } from "node:test";

import { systemUpdateProgressState } from "./systemUpdateProgress.js";

test("system update progress maps active pulling phase", () => {
  const state = systemUpdateProgressState({
    status: "running",
    phase: "pulling",
    message: "正在拉取最新镜像，服务稍后会短暂重启。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.failed, false);
  assert.equal(state.progress, 25);
  assert.equal(state.label, "正在拉取镜像");
  assert.equal(state.message, "正在拉取最新镜像，服务稍后会短暂重启。");
  assert.equal(state.percentText, "25%");
  assert.equal(state.variant, "running");
});

test("system update progress maps web update phase", () => {
  const state = systemUpdateProgressState({
    status: "running",
    phase: "updating_web",
    message: "API 镜像已拉取完成，正在更新 Web 前端容器。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.progress, 30);
  assert.equal(state.label, "正在更新 Web");
  assert.equal(state.message, "API 镜像已拉取完成，正在更新 Web 前端容器。");
});

test("system update progress forces completed status to 100 percent", () => {
  const state = systemUpdateProgressState({
    status: "succeeded",
    phase: "completed",
    message: "系统已重新启动，当前版本 v0.9.34。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, false);
  assert.equal(state.completed, true);
  assert.equal(state.progress, 100);
  assert.equal(state.label, "更新完成");
  assert.equal(state.percentText, "100%");
  assert.equal(state.variant, "success");
});

test("system update progress keeps failed phase location", () => {
  const state = systemUpdateProgressState({
    status: "failed",
    phase: "starting",
    message: "等待新容器恢复超时。",
    last_error: "等待新容器恢复超时。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.failed, true);
  assert.equal(state.progress, 96);
  assert.equal(state.label, "等待服务恢复");
  assert.equal(state.message, "等待新容器恢复超时。");
  assert.equal(state.variant, "failed");
});

test("system update progress handles pending startup restart wait", () => {
  const state = systemUpdateProgressState({
    status: "pending_startup",
    phase: "starting",
    message: "",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.progress, 96);
  assert.equal(state.label, "等待服务恢复");
  assert.equal(state.message, "等待服务恢复");
});

test("system update progress hides idle without message", () => {
  const state = systemUpdateProgressState({
    status: "idle",
    phase: "idle",
    message: "",
  });

  assert.equal(state.visible, false);
  assert.equal(state.progress, 0);
  assert.equal(state.variant, "idle");
});

test("system update progress shows unknown active phase at midpoint", () => {
  const state = systemUpdateProgressState({
    status: "running",
    phase: "checking_network",
    message: "正在检查网络。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.progress, 50);
  assert.equal(state.label, "正在更新");
  assert.equal(state.message, "正在检查网络。");
});
