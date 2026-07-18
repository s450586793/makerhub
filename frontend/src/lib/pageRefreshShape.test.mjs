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
const keepAlivePageSource = readFileSync(new URL("./useKeepAlivePage.js", import.meta.url), "utf8");

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

test("TasksPage throttles state events from the first event in each refresh window", () => {
  assert.match(tasksPageSource, /delayMs:\s*1000/);
  assert.match(tasksPageSource, /debounceMs:\s*0/);
  assert.match(tasksPageSource, /resetExistingTimer:\s*false/);
});

test("RemoteRefreshPage uses shared page refresh controller for throttled refresh", () => {
  assert.match(remoteRefreshPageSource, /createPageRefreshController/);
  assert.doesNotMatch(remoteRefreshPageSource, /function scheduleRefresh/);
  assert.match(remoteRefreshPageSource, /remoteRefreshController/);
  assert.match(remoteRefreshPageSource, /apiRequest\("\/api\/source-refresh"/);
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

test("DashboardPage treats the light snapshot as its final initial projection", () => {
  assert.match(dashboardPageSource, /apiRequest\("\/api\/dashboard\/light"/);
  assert.match(dashboardPageSource, /createHydratedResource/);
  assert.doesNotMatch(dashboardPageSource, /scheduleFullDashboardHydration|requestIdleCallback/);
  assert.doesNotMatch(dashboardPageSource, /load\(\{ initial: true, hydrateFull: true \}\)/);
});

test("final light resource loaders forward cancellation signals to apiRequest", () => {
  assert.match(dashboardPageSource, /load: \(\{ signal \}\) => apiRequest\("\/api\/dashboard\/light", \{ signal \}\)/);
  assert.match(modelsPageSource, /fetchPage\(page, options = \{\}\).*apiRequest\([^\n]+, \{ signal: options\.signal \}\)/s);
  assert.match(modelsPageSource, /load: \(\{ page, requestOptions, signal \}\) => fetchPage\(page, \{ \.\.\.requestOptions, signal \}\)/);
  assert.match(organizerPageSource, /load: \(\{ signal \}\) => apiRequest\("\/api\/source-library\/light", \{ signal \}\)/);
  assert.match(subscriptionsPageSource, /fetchSubscriptionsPage\(page = 1, options = \{\}\).*apiRequest\([^\n]+, \{ signal: options\.signal \}\)/s);
  assert.match(subscriptionsPageSource, /load: \(\{ page, requestOptions, signal \}\) => fetchSubscriptionsPage\(page, \{ \.\.\.requestOptions, signal \}\)/);
  assert.match(tasksPageSource, /load: \(\{ signal \}\) => apiRequest\("\/api\/tasks\/light", \{ signal \}\)/);
  assert.match(tasksPageSource, /enrich: \(_current, \{ signal \}\) => apiRequest\("\/api\/tasks", \{ signal \}\)/);
  assert.match(remoteRefreshPageSource, /load: \(\{ signal \}\) => apiRequest\("\/api\/source-refresh", \{ signal \}\)/);
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

test("OrganizerPage projects source cards and task state from one response", () => {
  assert.match(organizerPageSource, /sourceLibraryPayload\.value = \{/);
  assert.match(organizerPageSource, /organizerTasks\.value = response\?\.organize_tasks/);
  assert.doesNotMatch(organizerPageSource, /Promise\.all\(|refreshSourceLibrary/);
});

test("TasksPage only enriches task details explicitly", () => {
  assert.match(tasksPageSource, /apiRequest\("\/api\/tasks\/light"/);
  assert.match(tasksPageSource, /createHydratedResource/);
  assert.match(tasksPageSource, /refreshFullTasks/);
  assert.match(tasksPageSource, /\.enrich\(/);
  assert.doesNotMatch(tasksPageSource, /load\(\{ hydrateFull: true \}\)/);
});

test("TasksPage renders grouped archive queue display items", () => {
  assert.match(tasksPageSource, /archive_queue_display/);
  assert.match(tasksPageSource, /archiveQueueForDisplay/);
  assert.match(tasksPageSource, /visibleActiveTasks = computed\(\(\) => \(archiveQueueForDisplay\.value\.active/);
  assert.match(tasksPageSource, /visibleQueuedTasks = computed\(\(\) => \(archiveQueueForDisplay\.value\.queued/);
  assert.doesNotMatch(tasksPageSource, /visibleActiveTasks = computed\(\(\) => payload\.value\.archive_queue\.active/);
  assert.doesNotMatch(tasksPageSource, /visibleQueuedTasks = computed\(\(\) => payload\.value\.archive_queue\.queued/);
});

test("TasksPage renders archive subtask progress inside queue items", () => {
  assert.match(tasksPageSource, /archive-subtasks/);
  assert.match(tasksPageSource, /archiveSubtasks\(item\)/);
  assert.match(tasksPageSource, /archiveSubtaskStatusLabel/);
  assert.match(tasksPageSource, /metadata.*media.*attachments.*comments.*three_mf.*finalize/s);
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
  assert.match(settingsPageSource, /const sourceInventory = inventoryByPlatform\[item\.platform\]/);
  assert.match(settingsPageSource, /const sourceSync = syncStateByPlatform\[item\.platform\]/);
  assert.match(settingsPageSource, /accountSourceStats\(\s*sourceInventory,\s*sourceSync,\s*subscriptionItems,\s*item\.platform,/);
  assert.match(settingsPageSource, /const accountHealthByPlatform = config\.value\?\.account_health \|\| \{\}/);
  assert.match(settingsPageSource, /accountOperationalView\(operational\)/);
  assert.match(settingsPageSource, /shouldShowBrowserSession\(mergedItem, operational\)/);
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

test("ModelsPage restores deep pages with one final light request", () => {
  assert.match(modelsPageSource, /apiRequest\("\/api\/models\/light/);
  assert.match(modelsPageSource, /createHydratedResource/);
  assert.doesNotMatch(modelsPageSource, /scheduleFullModelHydration|requestIdleCallback/);
  assert.match(modelsPageSource, /includeUntilPage/);
  assert.match(modelsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(modelsPageSource, /for \(let page = append \? nextPage : 1; page <= nextPage;/);
});

test("ModelsPage renders its final light response without a full fallback", () => {
  assert.match(modelsPageSource, /function renderLightModelListResponse/);
  assert.match(modelsPageSource, /renderLightModelListResponse\(response, nextPage\)/);
  assert.doesNotMatch(modelsPageSource, /resolveHydratedLightPhase|refreshFullModelList/);
});

test("SubscriptionsPage restores deep pages with one final light request", () => {
  assert.match(subscriptionsPageSource, /apiRequest\("\/api\/subscriptions\/light/);
  assert.match(subscriptionsPageSource, /createHydratedResource/);
  assert.doesNotMatch(subscriptionsPageSource, /scheduleFullSubscriptionsHydration|requestIdleCallback/);
  assert.match(subscriptionsPageSource, /includeUntilPage/);
  assert.match(subscriptionsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(subscriptionsPageSource, /for \(let page = 1; page <= pagesToLoad;/);
});

test("OrganizerPage loads tasks and source cards from one light source-library request", () => {
  assert.match(organizerPageSource, /apiRequest\("\/api\/source-library\/light"/);
  assert.match(organizerPageSource, /createHydratedResource/);
  assert.doesNotMatch(organizerPageSource, /apiRequest\("\/api\/tasks\/light"\)/);
  assert.doesNotMatch(organizerPageSource, /refreshConfig|scheduleFullSourceLibraryHydration|requestIdleCallback/);
});

test("SettingsPage renders from light config before background diagnostics", () => {
  assert.match(settingsPageSource, /refreshLightConfig/);
  assert.match(settingsPageSource, /refreshSettingsDiagnostics/);
  assert.match(settingsPageSource, /void refreshSettingsDiagnostics\(\)/);
});

test("keep-alive lifecycle helper pauses cached pages without duplicate cleanup", () => {
  assert.match(keepAlivePageSource, /onActivated/);
  assert.match(keepAlivePageSource, /onDeactivated/);
  assert.match(keepAlivePageSource, /onBeforeUnmount/);
  assert.match(keepAlivePageSource, /const active = ref\(false\)/);
  assert.match(keepAlivePageSource, /onActivate/);
  assert.match(keepAlivePageSource, /onDeactivate/);
});

test("cached work pages cancel their live work while deactivated", () => {
  for (const source of [
    modelsPageSource,
    subscriptionsPageSource,
    organizerPageSource,
    remoteRefreshPageSource,
    tasksPageSource,
  ]) {
    assert.match(source, /useKeepAlivePage/);
    assert.match(source, /onDeactivate: deactivatePage/);
  }

  assert.match(modelsPageSource, /function deactivatePage\(\)[\s\S]*?modelsResource\.cancel\(\)[\s\S]*?disconnectObserver\(\)/);
  assert.match(subscriptionsPageSource, /function deactivatePage\(\)[\s\S]*?subscriptionsResource\.cancel\(\)[\s\S]*?disconnectObserver\(\)/);
  assert.match(modelsPageSource, /loadMoreAbortController\?\.abort\(\)/);
  assert.match(subscriptionsPageSource, /loadMoreAbortController\?\.abort\(\)/);
  assert.match(organizerPageSource, /function deactivatePage\(\)[\s\S]*?organizerResource\.cancel\(\)[\s\S]*?stopOrganizerRefreshController\(\)/);
  assert.match(remoteRefreshPageSource, /function deactivatePage\(\)[\s\S]*?remoteRefreshResource\.cancel\(\)[\s\S]*?stopRemoteRefreshController\(\)/);
  assert.match(tasksPageSource, /function deactivatePage\(\)[\s\S]*?tasksResource\.cancel\(\)[\s\S]*?stopTasksRefreshController\(\)/);
});

test("state refresh controllers are gated by their cached page activity", () => {
  for (const source of [organizerPageSource, remoteRefreshPageSource, tasksPageSource]) {
    assert.match(source, /isActive: \(\) => pageActive\.value/);
  }
});
