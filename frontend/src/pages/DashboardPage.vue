<template>
  <template v-if="loading">
    <section class="surface empty-state">
      <h2>正在加载首页数据</h2>
      <p>请稍候。</p>
    </section>
  </template>

  <template v-else>
    <section class="page-intro dashboard-hero">
      <div class="dashboard-hero__copy">
        <span class="eyebrow">首页</span>
        <h1>自动化工作台</h1>
        <p>把归档队列、订阅、本地整理和远端刷新放在同一个首页里，先看概览，再进入对应页面处理细节。</p>
      </div>
      <div class="dashboard-hero__actions">
        <RouterLink class="button button-secondary" to="/tasks">归档任务</RouterLink>
        <RouterLink class="button button-secondary" to="/subscriptions">订阅</RouterLink>
        <RouterLink class="button button-secondary" to="/organizer">本地整理</RouterLink>
        <RouterLink class="button button-secondary" to="/remote-refresh">远端刷新</RouterLink>
      </div>
    </section>

    <section class="stats-grid">
      <article v-for="card in payload.stats" :key="card.label" class="surface stat-card">
        <span>{{ card.label }}</span>
        <strong>{{ card.value }}</strong>
        <small>{{ card.hint }}</small>
      </article>
    </section>

    <section class="dashboard-automation-grid">
      <article class="surface section-card automation-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">订阅概览</span>
            <h2>作者 / 收藏夹订阅</h2>
          </div>
          <div class="automation-card__header-actions">
            <span :class="['count-pill', automation.subscriptions.running_count ? 'count-pill--warn' : 'count-pill--ok']">
              {{ automation.subscriptions.running_count || 0 }} 同步中
            </span>
            <RouterLink class="section-link" to="/subscriptions">进入订阅页</RouterLink>
          </div>
        </div>

        <div class="automation-card__stats automation-card__stats--four">
          <div class="summary-box">
            <strong>订阅总数</strong>
            <span>{{ automation.subscriptions.count || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>启用中</strong>
            <span>{{ automation.subscriptions.enabled_count || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>同步中</strong>
            <span>{{ automation.subscriptions.running_count || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>源端删除标记</strong>
            <span>{{ automation.subscriptions.deleted_marked_count || 0 }}</span>
          </div>
        </div>

        <div class="automation-card__split">
          <div class="automation-card__block">
            <div class="automation-card__block-head">
              <strong>最近执行</strong>
            </div>
            <div v-if="automation.subscriptions.recent_items.length" class="automation-card__list">
              <article
                v-for="item in automation.subscriptions.recent_items"
                :key="`${item.id}-${item.last_run_at}`"
                class="automation-card__item"
              >
                <div class="automation-card__item-head">
                  <strong>{{ item.name || item.url || "未命名订阅" }}</strong>
                  <span :class="['count-pill', subscriptionPillClass(item)]">{{ subscriptionStatusLabel(item) }}</span>
                </div>
                <p>{{ item.last_message || "等待下一轮同步。" }}</p>
                <div class="automation-card__meta">
                  <span>{{ subscriptionModeLabel(item.mode) }}</span>
                  <span>上次运行 {{ formatDateTime(item.last_run_at) }}</span>
                </div>
              </article>
            </div>
            <p v-else class="empty-copy">当前没有订阅执行记录。</p>
          </div>

          <div class="automation-card__block">
            <div class="automation-card__block-head">
              <strong>下一轮计划</strong>
            </div>
            <div v-if="automation.subscriptions.next_items.length" class="automation-card__list">
              <article
                v-for="item in automation.subscriptions.next_items"
                :key="`${item.id}-${item.next_run_at}`"
                class="automation-card__item"
              >
                <div class="automation-card__item-head">
                  <strong>{{ item.name || item.url || "未命名订阅" }}</strong>
                  <span class="count-pill">{{ subscriptionModeLabel(item.mode) }}</span>
                </div>
                <p>{{ item.url || "未记录源链接。" }}</p>
                <div class="automation-card__meta">
                  <span>下次运行 {{ formatDateTime(item.next_run_at) }}</span>
                </div>
              </article>
            </div>
            <p v-else class="empty-copy">当前没有已启用的计划任务。</p>
          </div>
        </div>
      </article>

      <article class="surface section-card automation-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">远端刷新概览</span>
            <h2>评论 / 附件 / 打印配置增量同步</h2>
          </div>
          <div class="automation-card__header-actions">
            <span :class="['count-pill', remoteRefreshPillClass(automation.remote_refresh)]">
              {{ remoteRefreshStatusLabel(automation.remote_refresh.status, automation.remote_refresh.running) }}
            </span>
            <RouterLink class="section-link" to="/remote-refresh">进入远端刷新页</RouterLink>
          </div>
        </div>

        <div class="automation-card__stats automation-card__stats--four">
          <div class="summary-box">
            <strong>单轮数量</strong>
            <span>{{ automation.remote_refresh.batch_size || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>可刷新总数</strong>
            <span>{{ automation.remote_refresh.last_eligible_total || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>剩余待刷</strong>
            <span>{{ automation.remote_refresh.last_remaining_total || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>上轮失败</strong>
            <span>{{ automation.remote_refresh.last_batch_failed || 0 }}</span>
          </div>
        </div>

        <div class="automation-card__timeline automation-card__timeline--three">
          <span>
            <strong>下次运行</strong>
            <em>{{ formatDateTime(automation.remote_refresh.next_run_at) }}</em>
          </span>
          <span>
            <strong>上次运行</strong>
            <em>{{ formatDateTime(automation.remote_refresh.last_run_at) }}</em>
          </span>
          <span>
            <strong>上次成功</strong>
            <em>{{ formatDateTime(automation.remote_refresh.last_success_at) }}</em>
          </span>
        </div>

        <div class="automation-card__stats automation-card__stats--three">
          <div class="summary-box">
            <strong>本轮成功</strong>
            <span>{{ automation.remote_refresh.last_batch_succeeded || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>本轮总处理</strong>
            <span>{{ automation.remote_refresh.last_batch_total || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>缺 Cookie 跳过</strong>
            <span>{{ automation.remote_refresh.last_skipped_missing_cookie || 0 }}</span>
          </div>
        </div>

        <div class="automation-card__message">
          <strong>最近状态</strong>
          <p>{{ automation.remote_refresh.last_message || "当前还没有远端刷新执行记录。" }}</p>
        </div>
      </article>

      <article class="surface section-card automation-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">本地整理概览</span>
            <h2>监控 `/app/local` 的 3MF 导入</h2>
          </div>
          <div class="automation-card__header-actions">
            <span :class="['count-pill', automation.organizer.active_count ? 'count-pill--warn' : 'count-pill--ok']">
              {{ automation.organizer.active_count || 0 }} 活跃
            </span>
            <RouterLink class="section-link" to="/organizer">进入本地整理页</RouterLink>
          </div>
        </div>

        <div class="automation-card__stats automation-card__stats--four">
          <div class="summary-box">
            <strong>候选 3MF</strong>
            <span>{{ automation.organizer.detected_total || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>运行中</strong>
            <span>{{ automation.organizer.running_count || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>排队中</strong>
            <span>{{ automation.organizer.queued_count || 0 }}</span>
          </div>
          <div class="summary-box">
            <strong>模式</strong>
            <span>{{ automation.organizer.move_files ? "移动" : "复制" }}</span>
          </div>
        </div>

        <div class="automation-card__timeline automation-card__timeline--two">
          <span>
            <strong>扫描目录</strong>
            <em>{{ automation.organizer.source_dir || "/app/local" }}</em>
          </span>
          <span>
            <strong>归档目录</strong>
            <em>{{ automation.organizer.target_dir || "/app/archive" }}</em>
          </span>
        </div>

        <div class="automation-card__block">
          <div class="automation-card__block-head">
            <strong>最近整理记录</strong>
          </div>
          <div v-if="automation.organizer.items.length" class="automation-card__list">
            <article
              v-for="item in automation.organizer.items"
              :key="item.id || item.fingerprint || item.source_path || item.title"
              class="automation-card__item"
            >
              <div class="automation-card__item-head">
                <strong>{{ item.title || item.file_name || "未命名文件" }}</strong>
                <span :class="['count-pill', organizerPillClass(item.status)]">{{ organizerStatusLabel(item.status) }}</span>
              </div>
              <p>{{ item.message || item.model_dir || item.target_path || "等待整理。" }}</p>
              <div class="automation-card__meta">
                <span>{{ item.model_dir || item.source_path || "-" }}</span>
                <span>{{ formatDateTime(item.updated_at) }}</span>
              </div>
            </article>
          </div>
          <p v-else class="empty-copy">当前没有本地整理记录。</p>
        </div>
      </article>
    </section>

    <section class="dashboard-layout">
      <article class="surface section-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">系统状态</span>
            <h2>连接与代理状态</h2>
          </div>
          <RouterLink class="section-link" to="/settings?tab=connections">去设置</RouterLink>
        </div>
        <div class="status-list">
          <div v-for="item in payload.system_status" :key="item.title" class="status-item">
            <div>
              <strong>{{ item.title }}</strong>
              <span>{{ item.status }}</span>
            </div>
            <span :class="['status-dot', item.enabled && 'is-ok']"></span>
          </div>
        </div>
      </article>

      <article class="surface section-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">任务摘要</span>
            <h2>当前归档情况</h2>
          </div>
          <RouterLink class="section-link" to="/tasks">展开任务页</RouterLink>
        </div>
        <div class="summary-stack">
          <div class="summary-box">
            <strong>运行中任务</strong>
            <span>{{ payload.task_summary?.running?.length || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>排队中任务</strong>
            <span>{{ payload.task_summary?.queued_count || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>待补 3MF</strong>
            <span>{{ payload.task_summary?.missing_3mf_count || 0 }} 项</span>
          </div>
        </div>

        <div class="dashboard-summary-columns">
          <div class="dashboard-summary-group">
            <h3>最近失败</h3>
            <div v-if="payload.task_summary?.recent_failures?.length" class="summary-list">
              <div
                v-for="item in payload.task_summary.recent_failures"
                :key="item.id || item.title || item.url"
                class="summary-list__item"
              >
                <strong>{{ item.title || item.url || "未命名任务" }}</strong>
                <span>{{ item.message || "失败原因未记录" }}</span>
              </div>
            </div>
            <p v-else class="empty-copy">最近没有失败任务。</p>
          </div>

          <div class="dashboard-summary-group">
            <h3>待补 3MF</h3>
            <div v-if="payload.task_summary?.missing_3mf?.length" class="summary-list">
              <div
                v-for="item in payload.task_summary.missing_3mf"
                :key="`${item.model_id}-${item.instance_id}-${item.title}`"
                class="summary-list__item"
              >
                <strong>{{ item.title || item.model_id || "未命名模型" }}</strong>
                <span>{{ item.message || "等待重新下载 3MF" }}</span>
              </div>
            </div>
            <p v-else class="empty-copy">当前没有待补 3MF 记录。</p>
          </div>
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

function subscriptionModeLabel(value) {
  const normalized = String(value || "").trim();
  const mapping = {
    author_upload: "作者页",
    collection_models: "收藏夹",
    collection_detail: "合集页",
  };
  return mapping[normalized] || "订阅";
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

function subscriptionPillClass(item) {
  if (item?.running) {
    return "count-pill--warn";
  }
  if (item?.last_error_at) {
    return "count-pill--danger";
  }
  if (item?.last_run_at) {
    return "count-pill--ok";
  }
  return "";
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

function organizerStatusLabel(value) {
  const normalized = String(value || "").trim().toLowerCase();
  const mapping = {
    pending: "待处理",
    queued: "排队中",
    running: "运行中",
    success: "已完成",
    completed: "已完成",
    failed: "失败",
    skipped: "跳过",
  };
  return mapping[normalized] || "未记录";
}

function organizerPillClass(value) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "running" || normalized === "queued" || normalized === "pending") {
    return "count-pill--warn";
  }
  if (normalized === "failed") {
    return "count-pill--danger";
  }
  if (normalized === "success" || normalized === "completed") {
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
