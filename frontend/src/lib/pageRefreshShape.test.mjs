import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const logsPageSource = readFileSync(new URL("../pages/LogsPage.vue", import.meta.url), "utf8");
const tasksPageSource = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");
const remoteRefreshPageSource = readFileSync(new URL("../pages/RemoteRefreshPage.vue", import.meta.url), "utf8");
const dashboardPageSource = readFileSync(new URL("../pages/DashboardPage.vue", import.meta.url), "utf8");
const organizerPageSource = readFileSync(new URL("../pages/OrganizerPage.vue", import.meta.url), "utf8");
const settingsPageSource = readFileSync(new URL("../pages/SettingsPage.vue", import.meta.url), "utf8");
const modelsPageSource = readFileSync(new URL("../pages/ModelsPage.vue", import.meta.url), "utf8");
const modelGroupPageSource = readFileSync(new URL("../pages/ModelLibraryGroupPage.vue", import.meta.url), "utf8");
const subscriptionsPageSource = readFileSync(new URL("../pages/SubscriptionsPage.vue", import.meta.url), "utf8");
const appShellSource = readFileSync(new URL("../layouts/AppShell.vue", import.meta.url), "utf8");
const appStateSource = readFileSync(new URL("./appState.js", import.meta.url), "utf8");

test("LogsPage uses shared page refresh controller for auto tracking", () => {
  assert.match(logsPageSource, /createPageRefreshController/);
  assert.doesNotMatch(logsPageSource, /window\.setInterval\(load,\s*5000\)/);
  assert.match(logsPageSource, /logRefreshController/);
});

test("LogsPage defaults troubleshooting filters to a recent time window", () => {
  assert.match(logsPageSource, /const DEFAULT_SINCE = "6h"/);
  assert.match(logsPageSource, /label: "近 6 小时错误", level: "error", since: DEFAULT_SINCE/);
  assert.match(logsPageSource, /label: "历史错误", level: "error", since: ALL_TIME_VALUE/);
  assert.match(logsPageSource, /since: DEFAULT_SINCE/);
  assert.match(logsPageSource, /filters\.since !== DEFAULT_SINCE/);
});

test("TasksPage uses shared page refresh controller for state events", () => {
  assert.match(tasksPageSource, /createPageRefreshController/);
  assert.doesNotMatch(tasksPageSource, /function refreshFromStateEvent/);
  assert.match(tasksPageSource, /tasksRefreshController/);
});

test("RemoteRefreshPage uses shared page refresh controller for throttled refresh", () => {
  assert.match(remoteRefreshPageSource, /createPageRefreshController/);
  assert.doesNotMatch(remoteRefreshPageSource, /function scheduleRefresh/);
  assert.match(remoteRefreshPageSource, /remoteRefreshController/);
});

test("RemoteRefreshPage exposes resumable batch state and recovery actions", () => {
  assert.match(remoteRefreshPageSource, /上次尝试/);
  assert.match(remoteRefreshPageSource, /上次完成/);
  assert.match(remoteRefreshPageSource, /最近阻塞/);
  assert.match(remoteRefreshPageSource, /最近中断/);
  assert.match(remoteRefreshPageSource, /继续源端刷新/);
  assert.match(remoteRefreshPageSource, /修复队列状态/);
  assert.match(remoteRefreshPageSource, /stale_archive_queue_detected/);
  assert.match(remoteRefreshPageSource, /\/api\/tasks\/archive-queue\/repair/);
});

test("DashboardPage shows separate source refresh completion fields", () => {
  assert.match(dashboardPageSource, /最近完成/);
  assert.match(dashboardPageSource, /last_completed_at/);
  assert.match(dashboardPageSource, /最近阻塞/);
  assert.match(dashboardPageSource, /last_defer_reason/);
  assert.match(dashboardPageSource, /"dashboard"/);
});

test("OrganizerPage uses shared page refresh controller for organize task refresh", () => {
  assert.match(organizerPageSource, /createPageRefreshController/);
  assert.doesNotMatch(organizerPageSource, /function syncTaskTimer/);
  assert.match(organizerPageSource, /organizerRefreshController/);
});

test("SettingsPage uses shared page refresh controller for system update state", () => {
  assert.match(settingsPageSource, /createPageRefreshController/);
  assert.match(settingsPageSource, /settingsRefreshController/);
  assert.match(settingsPageSource, /accountCodeTimer/);
});

test("AppShell refreshes GitHub version status after navigation is visible", () => {
  assert.match(appStateSource, /export function refreshVersionStatusInBackground/);
  assert.match(appStateSource, /\/api\/system\/version/);
  assert.match(appShellSource, /refreshVersionStatusInBackground/);
  assert.match(appShellSource, /onMounted\(\(\) => \{/);
});

test("bootstrap state hydration does not depend on GitHub version payload", () => {
  const start = appStateSource.indexOf("function applyBootstrap");
  const end = appStateSource.indexOf("export function applyVersionPayload");
  const applyBootstrapBlock = appStateSource.slice(start, end);

  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  assert.match(applyBootstrapBlock, /appVersion/);
  assert.doesNotMatch(applyBootstrapBlock, /githubLatestVersion|github_version|githubUpdateAvailable/);
});

test("primary pages report slow first-load performance without UI changes", () => {
  for (const source of [
    dashboardPageSource,
    modelsPageSource,
    modelGroupPageSource,
    organizerPageSource,
    settingsPageSource,
    subscriptionsPageSource,
    tasksPageSource,
    logsPageSource,
    remoteRefreshPageSource,
  ]) {
    assert.match(source, /createPagePerformanceTracker/);
    assert.match(source, /perf\.finish/);
  }
});
