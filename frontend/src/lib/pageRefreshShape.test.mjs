import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const logsPageSource = readFileSync(new URL("../pages/LogsPage.vue", import.meta.url), "utf8");
const tasksPageSource = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");
const remoteRefreshPageSource = readFileSync(new URL("../pages/RemoteRefreshPage.vue", import.meta.url), "utf8");
const organizerPageSource = readFileSync(new URL("../pages/OrganizerPage.vue", import.meta.url), "utf8");
const settingsPageSource = readFileSync(new URL("../pages/SettingsPage.vue", import.meta.url), "utf8");

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
