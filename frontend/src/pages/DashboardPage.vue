<template>
  <div class="dashboard-page">
    <template v-if="loading">
      <section class="page-intro dashboard-hero dashboard-hero--compact dashboard-hero--loading">
        <div class="dashboard-hero__copy">
          <span class="eyebrow">首页</span>
        </div>

        <div class="dashboard-hero__status-grid">
          <div v-for="item in dashboardLoadingStatusCards" :key="item" class="dashboard-hero__status dashboard-loading-card">
            <span class="dashboard-loading-line dashboard-loading-line--title" />
            <span class="dashboard-loading-line" />
          </div>
        </div>
      </section>

      <section class="stats-grid">
        <article v-for="item in dashboardLoadingStatCards" :key="item" class="surface stat-card dashboard-loading-card">
          <span class="dashboard-loading-line dashboard-loading-line--label" />
          <strong class="dashboard-loading-line dashboard-loading-line--value" />
          <small class="dashboard-loading-line" />
        </article>
      </section>

      <section class="surface section-card dashboard-panel">
        <div class="dashboard-automation-cards">
          <article v-for="item in dashboardLoadingMiniCards" :key="item" class="dashboard-mini-card dashboard-loading-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-loading-line dashboard-loading-line--label" />
                <h3 class="dashboard-loading-line dashboard-loading-line--heading" />
              </div>
              <span class="dashboard-loading-line dashboard-loading-line--pill" />
            </div>

            <div class="dashboard-mini-card__stats">
              <div v-for="stat in dashboardLoadingStatColumns" :key="stat" class="dashboard-mini-card__stat">
                <span class="dashboard-loading-line dashboard-loading-line--label" />
                <strong class="dashboard-loading-line dashboard-loading-line--value" />
              </div>
            </div>

            <div class="dashboard-mini-card__meta">
              <span>
                <strong class="dashboard-loading-line dashboard-loading-line--label" />
                <span class="dashboard-loading-line" />
              </span>
              <span>
                <strong class="dashboard-loading-line dashboard-loading-line--label" />
                <span class="dashboard-loading-line" />
              </span>
            </div>
          </article>
        </div>
      </section>
    </template>

    <template v-else>
      <section class="page-intro dashboard-hero dashboard-hero--compact">
        <div class="dashboard-hero__copy">
          <span class="eyebrow">首页</span>
        </div>

        <div class="dashboard-hero__status-grid">
          <component
            v-for="item in payload.system_status"
            :key="item.key || item.title"
            :is="dashboardStatusElementKind(item)"
            :class="[
              'dashboard-hero__status',
              item.tone && `is-${item.tone}`,
            ]"
          >
            <strong>{{ item.title }}</strong>
            <div class="dashboard-hero__status-line">
              <span :class="['dashboard-hero__status-dot', item.tone && `is-${item.tone}`]" />
              <span class="dashboard-hero__status-text">{{ item.status }}</span>
            </div>
            <span v-if="shouldShowDashboardStatusDetail(item)" class="dashboard-hero__status-detail">{{ item.detail }}</span>
            <div v-if="item.checks?.length" class="dashboard-hero__check-list">
              <span
                v-for="check in item.checks"
                :key="`${item.key || item.title}-${check.source || check.label}`"
                :class="['dashboard-hero__check', check.tone && `is-${check.tone}`]"
                :title="check.detail || undefined"
              >
                <span>{{ check.label }}</span>
                <strong>{{ check.status }}</strong>
              </span>
            </div>
            <div v-if="dashboardStatusActions(item).length" class="dashboard-hero__status-actions">
              <template
                v-for="action in dashboardStatusActions(item)"
                :key="`${item.key || item.title}-${action.kind}-${action.label}`"
              >
                <RouterLink
                  v-if="action.kind === 'route'"
                  class="dashboard-hero__status-action dashboard-hero__status-action--link"
                  :to="action.to"
                >
                  {{ action.label }}
                </RouterLink>
                <a
                  v-else-if="action.kind === 'external'"
                  class="dashboard-hero__status-action dashboard-hero__status-action--link"
                  :href="action.href"
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  {{ action.label }}
                </a>
                <button
                  v-else-if="action.kind === 'api'"
                  class="dashboard-hero__status-action dashboard-hero__status-action--button"
                  type="button"
                  :disabled="isStatusActionBusy(item, action)"
                  :title="statusActionMessage(item, action) || undefined"
                  @click="runStatusAction(item, action)"
                >
                  {{ isStatusActionBusy(item, action) ? "提交中" : action.label }}
                </button>
              </template>
            </div>
            <span v-if="statusActionMessage(item)" class="dashboard-hero__status-feedback">
              {{ statusActionMessage(item) }}
            </span>
          </component>
        </div>
      </section>

      <section class="stats-grid">
        <article v-for="card in payload.stats" :key="card.label" class="surface stat-card">
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <small>{{ card.hint }}</small>
        </article>
      </section>

      <section class="surface section-card dashboard-panel">
        <div class="dashboard-automation-cards">
          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">归档任务</span>
                <h3>当前队列</h3>
              </div>
              <span :class="['count-pill', (payload.task_summary?.running?.length || 0) || (payload.task_summary?.queued_count || 0) ? 'count-pill--warn' : 'count-pill--ok']">
                {{ (payload.task_summary?.running?.length || 0) + (payload.task_summary?.queued_count || 0) }} 活跃
              </span>
            </div>

            <div class="dashboard-mini-card__stats">
              <div class="dashboard-mini-card__stat">
                <span>运行中</span>
                <strong>{{ payload.task_summary?.running?.length || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>排队中</span>
                <strong>{{ payload.task_summary?.queued_count || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>待补 3MF</span>
                <strong>{{ payload.task_summary?.missing_3mf_count || 0 }}</strong>
              </div>
            </div>

            <div class="dashboard-mini-card__meta">
              <span>
                <strong>最近失败</strong>
                {{ taskFailureText }}
              </span>
              <span>
                <strong>待补 3MF</strong>
                {{ taskMissingText }}
              </span>
            </div>

            <div class="dashboard-mini-card__footer">
              <RouterLink class="section-link" to="/tasks">进入任务页</RouterLink>
            </div>
          </article>

          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">订阅库</span>
                <h3>作者与收藏夹</h3>
              </div>
              <span :class="['count-pill', automation.subscriptions.running_count ? 'count-pill--warn' : 'count-pill--ok']">
                {{ automation.subscriptions.running_count || 0 }} 同步中
              </span>
            </div>

            <div class="dashboard-mini-card__stats">
              <div class="dashboard-mini-card__stat">
                <span>总数</span>
                <strong>{{ automation.subscriptions.count || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>启用</span>
                <strong>{{ automation.subscriptions.enabled_count || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>删除标记</span>
                <strong>{{ automation.subscriptions.deleted_marked_count || 0 }}</strong>
              </div>
            </div>

            <div class="dashboard-mini-card__meta">
              <span>
                <strong>下次运行</strong>
                {{ nextSubscriptionItem ? formatDateTime(nextSubscriptionItem.next_run_at) : "未安排" }}
              </span>
              <span>
                <strong>最近结果</strong>
                {{ subscriptionRecentText }}
              </span>
            </div>

            <div class="dashboard-mini-card__footer">
              <RouterLink class="section-link" to="/subscriptions">进入订阅库</RouterLink>
            </div>
          </article>

          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">源端刷新</span>
                <h3>增量同步</h3>
              </div>
              <span :class="['count-pill', remoteRefreshPillClass(automation.remote_refresh)]">
                {{ remoteRefreshStatusLabel(automation.remote_refresh.status, automation.remote_refresh.running) }}
              </span>
            </div>

            <div class="dashboard-mini-card__stats">
              <div class="dashboard-mini-card__stat">
                <span>本轮计划</span>
                <strong>{{ automation.remote_refresh.last_batch_total || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>可刷</span>
                <strong>{{ automation.remote_refresh.last_eligible_total || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>剩余</span>
                <strong>{{ automation.remote_refresh.last_remaining_total || 0 }}</strong>
              </div>
            </div>

            <div class="dashboard-mini-card__meta">
              <span>
                <strong>上次运行</strong>
                {{ formatDateTime(automation.remote_refresh.last_run_at) }}
              </span>
              <span>
                <strong>最近状态</strong>
                {{ automation.remote_refresh.last_message || "当前没有源端刷新记录。" }}
              </span>
            </div>

            <div class="dashboard-mini-card__footer">
              <RouterLink class="section-link" to="/remote-refresh">进入源端刷新页</RouterLink>
            </div>
          </article>

          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">本地库</span>
                <h3>3MF 与本地状态</h3>
              </div>
              <span :class="['count-pill', automation.organizer.active_count ? 'count-pill--warn' : 'count-pill--ok']">
                {{ automation.organizer.active_count || 0 }} 活跃
              </span>
            </div>

            <div class="dashboard-mini-card__stats">
              <div class="dashboard-mini-card__stat">
                <span>候选</span>
                <strong>{{ automation.organizer.detected_total || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>运行</span>
                <strong>{{ automation.organizer.running_count || 0 }}</strong>
              </div>
              <div class="dashboard-mini-card__stat">
                <span>排队</span>
                <strong>{{ automation.organizer.queued_count || 0 }}</strong>
              </div>
            </div>

            <div class="dashboard-mini-card__meta">
              <span>
                <strong>扫描目录</strong>
                {{ automation.organizer.source_dir || "/app/data/local" }}
              </span>
              <span>
                <strong>最近记录</strong>
                {{ organizerRecentText }}
              </span>
            </div>

            <div class="dashboard-mini-card__footer">
              <RouterLink class="section-link" to="/organizer">进入本地库</RouterLink>
            </div>
          </article>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import { apiRequest } from "../lib/api";
import {
  dashboardStatusActions,
  dashboardStatusElementKind,
  shouldShowDashboardStatusDetail,
} from "../lib/dashboardStatus";
import { formatServerDateTime } from "../lib/helpers";
import { subscribeStateRefresh } from "../lib/stateEvents";


const defaultAutomationOverview = {
  subscriptions: {
    count: 0,
    enabled_count: 0,
    running_count: 0,
    deleted_marked_count: 0,
    recent_items: [],
    next_items: [],
  },
  remote_refresh: {
    enabled: false,
    status: "idle",
    running: false,
    last_batch_total: 0,
    last_batch_succeeded: 0,
    last_batch_failed: 0,
    last_eligible_total: 0,
    last_remaining_total: 0,
    last_skipped_missing_cookie: 0,
    next_run_at: "",
    last_run_at: "",
    last_success_at: "",
    last_message: "",
  },
  organizer: {
    source_dir: "",
    target_dir: "",
    move_files: true,
    detected_total: 0,
    running_count: 0,
    queued_count: 0,
    active_count: 0,
    items: [],
  },
};

const loading = ref(true);
const dashboardLoadingStatusCards = [1, 2, 3];
const dashboardLoadingStatCards = [1, 2, 3, 4];
const dashboardLoadingMiniCards = [1, 2, 3, 4];
const dashboardLoadingStatColumns = [1, 2, 3];
const payload = ref({
  stats: [],
  system_status: [],
  automation_overview: defaultAutomationOverview,
  task_summary: {
    running: [],
    queued_count: 0,
    recent_failures: [],
    missing_3mf_count: 0,
    missing_3mf: [],
  },
});
const statusActionState = ref({});

const automation = computed(() => payload.value?.automation_overview || defaultAutomationOverview);
const latestFailureItem = computed(() => payload.value?.task_summary?.recent_failures?.[0] || null);
const latestMissingItem = computed(() => payload.value?.task_summary?.missing_3mf?.[0] || null);
const latestSubscriptionItem = computed(() => automation.value?.subscriptions?.recent_items?.[0] || null);
const nextSubscriptionItem = computed(() => automation.value?.subscriptions?.next_items?.[0] || null);
const latestOrganizerItem = computed(() => automation.value?.organizer?.items?.[0] || null);
const taskFailureText = computed(() => {
  const item = latestFailureItem.value;
  if (!item) {
    return "当前没有失败记录。";
  }
  const title = item.title || item.url || "未命名任务";
  const message = item.message || "失败原因未记录";
  return `${title} · ${message}`;
});
const taskMissingText = computed(() => {
  const item = latestMissingItem.value;
  if (!item) {
    return "当前没有待补 3MF。";
  }
  const title = item.title || item.model_id || "未命名模型";
  const message = item.message || "等待重新下载 3MF";
  return `${title} · ${message}`;
});
const subscriptionRecentText = computed(() => {
  const item = latestSubscriptionItem.value;
  if (!item) {
    return "最近没有执行记录。";
  }
  return item.last_message || `${subscriptionStatusLabel(item)} · ${formatDateTime(item.last_run_at)}`;
});
const organizerRecentText = computed(() => {
  const item = latestOrganizerItem.value;
  if (!item) {
    return "当前没有整理记录。";
  }
  return item.message || item.model_dir || item.source_path || "最近一条记录已完成。";
});
const hasActiveWork = computed(() => Boolean(
  (payload.value.task_summary?.running?.length || 0)
  || Number(payload.value.task_summary?.queued_count || 0)
  || Number(automation.value?.subscriptions?.running_count || 0)
  || Boolean(automation.value?.remote_refresh?.running)
  || Number(automation.value?.organizer?.active_count || 0)
));

let requestInFlight = false;
let unsubscribeStateRefresh = null;
let refreshWhenVisible = false;

async function load({ initial = false } = {}) {
  if (requestInFlight) {
    return;
  }
  requestInFlight = true;
  if (initial) {
    loading.value = true;
  }
  try {
    payload.value = await apiRequest("/api/dashboard");
  } catch (error) {
    console.error("首页数据刷新失败", error);
  } finally {
    loading.value = false;
    requestInFlight = false;
  }
}

function statusActionKey(item, action = {}) {
  return [
    item?.key || item?.title || "status",
    action.kind || "",
    action.endpoint || action.href || action.to || action.label || "",
  ].join(":");
}

function statusActionMessage(item, action = {}) {
  const key = statusActionKey(item, action);
  if (statusActionState.value[key]?.message) {
    return statusActionState.value[key].message;
  }
  if (action && (action.kind || action.endpoint || action.href || action.to || action.label)) {
    return "";
  }
  const prefix = `${item?.key || item?.title || "status"}:`;
  const entry = Object.entries(statusActionState.value).find(([entryKey, value]) => (
    entryKey.startsWith(prefix) && value?.message
  ));
  return entry?.[1]?.message || "";
}

function isStatusActionBusy(item, action) {
  return Boolean(statusActionState.value[statusActionKey(item, action)]?.busy);
}

async function runStatusAction(item, action) {
  if (!action?.endpoint || isStatusActionBusy(item, action)) {
    return;
  }
  const key = statusActionKey(item, action);
  statusActionState.value = {
    ...statusActionState.value,
    [key]: { busy: true, message: "" },
  };
  try {
    const result = await apiRequest(action.endpoint, {
      method: action.method || "POST",
      body: action.body || {},
    });
    statusActionState.value = {
      ...statusActionState.value,
      [key]: { busy: false, message: result?.message || "重试已提交。" },
    };
    void load();
  } catch (error) {
    statusActionState.value = {
      ...statusActionState.value,
      [key]: {
        busy: false,
        message: error instanceof Error ? error.message : "操作提交失败。",
      },
    };
  }
}

function handleArchiveCompleted() {
  if (document.hidden) {
    refreshWhenVisible = true;
    return;
  }
  void load();
}

function handleVisibilityChange() {
  if (document.hidden) {
    return;
  }
  const shouldRefresh = refreshWhenVisible;
  refreshWhenVisible = false;
  if (shouldRefresh || hasActiveWork.value) {
    void load();
  }
}

function formatDateTime(value, fallback = "未安排") {
  return formatServerDateTime(value, { fallback });
}

function subscriptionStatusLabel(item) {
  if (item?.running) {
    return "同步中";
  }
  if (item?.last_error_at) {
    return "异常";
  }
  if (item?.last_run_at) {
    return "已执行";
  }
  return "待执行";
}

function remoteRefreshStatusLabel(status, running) {
  if (running || status === "running") {
    return "刷新中";
  }
  const normalized = String(status || "").trim();
  const mapping = {
    idle: "空闲",
    disabled: "已停用",
    error: "异常",
  };
  return mapping[normalized] || "空闲";
}

function remoteRefreshPillClass(item) {
  if (item?.running || item?.status === "running") {
    return "count-pill--warn";
  }
  if (item?.status === "error") {
    return "count-pill--danger";
  }
  if (item?.enabled) {
    return "count-pill--ok";
  }
  return "";
}

onMounted(async () => {
  await load({ initial: true });
  unsubscribeStateRefresh = subscribeStateRefresh(
    ["archive_queue", "missing_3mf", "organize_tasks", "subscriptions_state", "remote_refresh_state"],
    handleArchiveCompleted,
  );
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onBeforeUnmount(() => {
  if (typeof unsubscribeStateRefresh === "function") {
    unsubscribeStateRefresh();
    unsubscribeStateRefresh = null;
  }
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
