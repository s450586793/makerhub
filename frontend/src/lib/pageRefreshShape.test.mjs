import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const logsPageSource = readFileSync(new URL("../pages/LogsPage.vue", import.meta.url), "utf8");
const tasksPageSource = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");

test("LogsPage uses shared page refresh controller for auto tracking", () => {
  assert.match(logsPageSource, /createPageRefreshController/);
  assert.doesNotMatch(logsPageSource, /window\.setInterval\(load,\s*5000\)/);
  assert.match(logsPageSource, /logRefreshController/);
});

test("TasksPage uses shared page refresh controller for state events", () => {
  assert.match(tasksPageSource, /createPageRefreshController/);
  assert.doesNotMatch(tasksPageSource, /function refreshFromStateEvent/);
  assert.match(tasksPageSource, /tasksRefreshController/);
});
