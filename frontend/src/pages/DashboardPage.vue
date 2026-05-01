<template>
  <template v-if="loading">
    <section class="page-intro dashboard-hero dashboard-hero--compact dashboard-hero--loading">
      <div class="dashboard-hero__copy">
        <span class="eyebrow">首页</span>
        <h1>工作台</h1>
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
        <h1>工作台</h1>
      </div>

      <div class="dashboard-hero__status-grid">
        <component
          v-for="item in payload.system_status"
          :key="item.key || item.title"
          :is="item.url ? 'a' : 'div'"
          :class="[
            'dashboard-hero__status',
            item.tone && `is-${item.tone}`,
            item.url && 'dashboard-hero__status--link',
          ]"
          :href="item.url || undefined"
          :target="item.url ? '_blank' : undefined"
          :rel="item.url ? 'noreferrer noopener' : undefined"
        >
          <strong>{{ item.title }}</strong>
          <div class="dashboard-hero__status-line">
            <span :class="['dashboard-hero__status-dot', item.tone && `is-${item.tone}`]" />
            <span class="dashboard-hero__status-text">{{ item.status }}</span>
          </div>
          <span v-if="item.detail" class="dashboard-hero__status-detail">{{ item.detail }}</span>
          <span v-if="item.url && item.action_label" class="dashboard-hero__status-action">{{ item.action_label }}</span>
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
                {{ automation.organizer.source_dir || "/app/local" }}
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
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { apiRequest } from "../lib/api";
import { subscribeArchiveCompletion } from "../lib/archiveEvents";
import { formatServerDateTime } from "../lib/helpers";


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
let refreshTimer = null;
let unsubscribeArchiveEvents = null;
let refreshWhenVisible = false;

function clearRefreshTimer() {
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh() {
  clearRefreshTimer();
  if (typeof window === "undefined" || document.hidden || !hasActiveWork.value) {
    return;
  }
  refreshTimer = window.setTimeout(() => {
    void load();
  }, 10000);
}

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
    scheduleRefresh();
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
    clearRefreshTimer();
    return;
  }
  const shouldRefresh = refreshWhenVisible;
  refreshWhenVisible = false;
  if (shouldRefresh || hasActiveWork.value) {
    void load();
    return;
  }
  scheduleRefresh();
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

watch(hasActiveWork, () => {
  scheduleRefresh();
});

onMounted(async () => {
  await load({ initial: true });
  unsubscribeArchiveEvents = subscribeArchiveCompletion(handleArchiveCompleted);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onBeforeUnmount(() => {
  clearRefreshTimer();
  if (typeof unsubscribeArchiveEvents === "function") {
    unsubscribeArchiveEvents();
    unsubscribeArchiveEvents = null;
  }
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
