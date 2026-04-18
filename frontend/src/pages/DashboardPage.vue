<template>
  <template v-if="loading">
    <section class="surface empty-state">
      <h2>正在加载首页数据</h2>
      <p>请稍候。</p>
    </section>
  </template>

  <template v-else>
    <section class="page-intro dashboard-hero dashboard-hero--compact">
      <div class="dashboard-hero__copy">
        <span class="eyebrow">首页</span>
        <h1>工作台</h1>
        <p>首页只保留总览。需要处理细节时，再进入任务、订阅、本地整理或远端刷新页面。</p>
      </div>

      <div class="dashboard-hero__status-grid">
        <div
          v-for="item in payload.system_status"
          :key="item.title"
          :class="['dashboard-hero__status', item.enabled && 'is-ok']"
        >
          <strong>{{ item.title }}</strong>
          <span>{{ item.status }}</span>
        </div>
      </div>
    </section>

    <section class="stats-grid">
      <article v-for="card in payload.stats" :key="card.label" class="surface stat-card">
        <span>{{ card.label }}</span>
        <strong>{{ card.value }}</strong>
        <small>{{ card.hint }}</small>
      </article>
    </section>

    <section class="dashboard-focus-grid">
      <article class="surface section-card dashboard-panel">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">任务摘要</span>
            <h2>当前任务</h2>
          </div>
          <RouterLink class="section-link" to="/tasks">进入任务页</RouterLink>
        </div>

        <div class="summary-stack">
          <div class="summary-box">
            <strong>运行中</strong>
            <span>{{ payload.task_summary?.running?.length || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>排队中</strong>
            <span>{{ payload.task_summary?.queued_count || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>待补 3MF</strong>
            <span>{{ payload.task_summary?.missing_3mf_count || 0 }} 项</span>
          </div>
        </div>

        <div class="dashboard-notices">
          <article v-if="latestFailureItem" class="dashboard-notice dashboard-notice--error">
            <span class="dashboard-notice__label">最近失败</span>
            <strong>{{ latestFailureItem.title || latestFailureItem.url || "未命名任务" }}</strong>
            <p>{{ latestFailureItem.message || "失败原因未记录" }}</p>
          </article>

          <article v-if="latestMissingItem" class="dashboard-notice">
            <span class="dashboard-notice__label">待补 3MF</span>
            <strong>{{ latestMissingItem.title || latestMissingItem.model_id || "未命名模型" }}</strong>
            <p>{{ latestMissingItem.message || "等待重新下载 3MF" }}</p>
          </article>

          <p v-if="!latestFailureItem && !latestMissingItem" class="empty-copy">当前没有需要优先处理的异常项。</p>
        </div>
      </article>

      <article class="surface section-card dashboard-panel">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">自动化概览</span>
            <h2>订阅 / 刷新 / 整理</h2>
          </div>
        </div>

        <div class="dashboard-automation-cards">
          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">订阅</span>
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
              <RouterLink class="section-link" to="/subscriptions">进入订阅页</RouterLink>
            </div>
          </article>

          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">远端刷新</span>
                <h3>增量同步</h3>
              </div>
              <span :class="['count-pill', remoteRefreshPillClass(automation.remote_refresh)]">
                {{ remoteRefreshStatusLabel(automation.remote_refresh.status, automation.remote_refresh.running) }}
              </span>
            </div>

            <div class="dashboard-mini-card__stats">
              <div class="dashboard-mini-card__stat">
                <span>单轮</span>
                <strong>{{ automation.remote_refresh.batch_size || 0 }}</strong>
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
                {{ automation.remote_refresh.last_message || "当前没有远端刷新记录。" }}
              </span>
            </div>

            <div class="dashboard-mini-card__footer">
              <RouterLink class="section-link" to="/remote-refresh">进入远端刷新页</RouterLink>
            </div>
          </article>

          <article class="dashboard-mini-card">
            <div class="dashboard-mini-card__head">
              <div>
                <span class="dashboard-mini-card__eyebrow">本地整理</span>
                <h3>3MF 导入</h3>
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
              <RouterLink class="section-link" to="/organizer">进入本地整理页</RouterLink>
            </div>
          </article>
        </div>
      </article>
    </section>
  </template>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { apiRequest } from "../lib/api";
import { subscribeArchiveCompletion } from "../lib/archiveEvents";
import { parseServerDate } from "../lib/helpers";


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
    batch_size: 0,
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
  const date = parseServerDate(value);
  if (!date) {
    return fallback;
  }
  return date.toLocaleString("zh-CN", {
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
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
