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

test("LogsPage keeps advanced troubleshooting filters out of the user toolbar", () => {
  assert.doesNotMatch(logsPageSource, /aria-label="日志文件"/);
  assert.doesNotMatch(logsPageSource, /aria-label="日志事件"/);
  assert.doesNotMatch(logsPageSource, /aria-label="每页条数"/);
  assert.match(logsPageSource, /query\.set\("file"/);
  assert.match(logsPageSource, /if \(filters\.event\) query\.set\("event"/);
  assert.match(logsPageSource, /query\.set\("limit"/);
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
  assert.match(remoteRefreshPageSource, /apiRequest\("\/api\/source-refresh"\)/);
  assert.match(remoteRefreshPageSource, /apiRequest\("\/api\/source-refresh\/run"/);
  assert.doesNotMatch(remoteRefreshPageSource, /apiRequest\("\/api\/remote-refresh"\)/);
  assert.doesNotMatch(remoteRefreshPageSource, /apiRequest\("\/api\/remote-refresh\/run"/);
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
  assert.match(dashboardPageSource, /getSourceRefreshDisplayTotals/);
  assert.match(dashboardPageSource, /source_refresh/);
  assert.match(dashboardPageSource, /sourceRefreshActiveRun/);
  assert.match(dashboardPageSource, /sourceRefreshDisplayTotals/);
  assert.match(dashboardPageSource, /"dashboard"/);
  assert.match(dashboardPageSource, /"source_refresh_queue"/);
  assert.match(dashboardPageSource, /"source_refresh_runs"/);
});

test("DashboardPage renders a light snapshot before full dashboard hydration", () => {
  assert.match(dashboardPageSource, /apiRequest\("\/api\/dashboard\/light"\)/);
  assert.match(dashboardPageSource, /refreshFullDashboard/);
  assert.match(dashboardPageSource, /scheduleFullDashboardHydration/);
  assert.match(dashboardPageSource, /requestIdleCallback|setTimeout/);
});

test("RemoteRefreshPage explains active run progress from resumable batch state", () => {
  assert.match(remoteRefreshPageSource, /hasActiveRun\.value/);
  assert.match(remoteRefreshPageSource, /activeRun\.value\.completed_total/);
  assert.match(remoteRefreshPageSource, /activeRun\.value\.remaining_total/);
  assert.match(remoteRefreshPageSource, /当前批次计划处理/);
});

test("OrganizerPage uses shared page refresh controller for organize task refresh", () => {
  assert.match(organizerPageSource, /createPageRefreshController/);
  assert.doesNotMatch(organizerPageSource, /function syncTaskTimer/);
  assert.match(organizerPageSource, /organizerRefreshController/);
});

test("OrganizerPage does not block first paint on source library payload", () => {
  assert.match(organizerPageSource, /async function refreshSourceLibrary/);
  assert.match(organizerPageSource, /void refreshSourceLibrary/);
  assert.match(organizerPageSource, /apiRequest\("\/api\/tasks\/light"\)/);
  assert.doesNotMatch(organizerPageSource, /Promise\.all\(requests\)/);
});

test("TasksPage renders a light task snapshot before full task hydration", () => {
  assert.match(tasksPageSource, /apiRequest\("\/api\/tasks\/light"\)/);
  assert.match(tasksPageSource, /refreshFullTasks/);
  assert.match(tasksPageSource, /void refreshFullTasks/);
});

test("TasksPage renders grouped archive queue display items", () => {
  assert.match(tasksPageSource, /archive_queue_display/);
  assert.match(tasksPageSource, /archiveQueueForDisplay/);
  assert.match(tasksPageSource, /visibleActiveTasks = computed\(\(\) => \(archiveQueueForDisplay\.value\.active/);
  assert.match(tasksPageSource, /visibleQueuedTasks = computed\(\(\) => \(archiveQueueForDisplay\.value\.queued/);
  assert.doesNotMatch(tasksPageSource, /visibleActiveTasks = computed\(\(\) => payload\.value\.archive_queue\.active/);
  assert.doesNotMatch(tasksPageSource, /visibleQueuedTasks = computed\(\(\) => payload\.value\.archive_queue\.queued/);
});

test("SettingsPage uses shared page refresh controller for system update state", () => {
  assert.match(settingsPageSource, /createPageRefreshController/);
  assert.match(settingsPageSource, /settingsRefreshController/);
  assert.match(settingsPageSource, /accountCodeTimer/);
});

test("SettingsPage derives synced online account source counts from current subscriptions", () => {
  assert.match(settingsPageSource, /accountSyncedSourceCounts/);
  assert.match(settingsPageSource, /accountSourceOverview/);
  assert.match(settingsPageSource, /onlineAccountOverview/);
  assert.match(settingsPageSource, /const subscriptionItems = Array\.isArray\(config\.value\?\.subscriptions\)/);
  assert.match(settingsPageSource, /syncStateAccount\.account_avatar_url/);
  assert.match(settingsPageSource, /accountSourceStats\(\s*inventoryByPlatform\[item\.platform\],\s*syncStateByPlatform\[item\.platform\],\s*subscriptionItems,\s*item\.platform,/);
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

test("ModelsPage restores deep pages with a single include-until-page request", () => {
  assert.match(modelsPageSource, /apiRequest\("\/api\/models\/light/);
  assert.match(modelsPageSource, /refreshFullModelList/);
  assert.match(modelsPageSource, /scheduleFullModelHydration/);
  assert.match(modelsPageSource, /requestIdleCallback|setTimeout/);
  assert.match(modelsPageSource, /includeUntilPage/);
  assert.match(modelsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(modelsPageSource, /for \(let page = append \? nextPage : 1; page <= nextPage;/);
});

test("ModelsPage uses the shared hydrated light-phase decision", () => {
  assert.match(modelsPageSource, /resolveHydratedLightPhase/);
  assert.match(modelsPageSource, /hasStableModelListView/);
  assert.match(modelsPageSource, /await refreshFullModelList\(\{ refresh \}\)/);
});

test("SubscriptionsPage restores deep pages with a single include-until-page request", () => {
  assert.match(subscriptionsPageSource, /apiRequest\("\/api\/subscriptions\/light/);
  assert.match(subscriptionsPageSource, /refreshFullSubscriptions/);
  assert.match(subscriptionsPageSource, /scheduleFullSubscriptionsHydration/);
  assert.match(subscriptionsPageSource, /requestIdleCallback|setTimeout/);
  assert.match(subscriptionsPageSource, /includeUntilPage/);
  assert.match(subscriptionsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(subscriptionsPageSource, /for \(let page = 1; page <= pagesToLoad;/);
});

test("OrganizerPage refreshes source cards through a light source-library snapshot", () => {
  assert.match(organizerPageSource, /apiRequest\("\/api\/source-library\/light"\)/);
  assert.match(organizerPageSource, /refreshFullSourceLibrary/);
  assert.match(organizerPageSource, /scheduleFullSourceLibraryHydration/);
  assert.match(organizerPageSource, /requestIdleCallback|setTimeout/);
});

test("SettingsPage renders from light config before background diagnostics", () => {
  assert.match(settingsPageSource, /refreshLightConfig/);
  assert.match(settingsPageSource, /refreshSettingsDiagnostics/);
  assert.match(settingsPageSource, /void refreshSettingsDiagnostics\(\)/);
});
