<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">源端刷新</span>
      <h1>源端刷新配置</h1>
      <p>控制远端评论、附件、打印配置与源端删除标记的分批同步节奏。</p>
    </div>
    <div class="intro-stats remote-refresh-intro-stats">
      <div class="intro-stat">
        <span>当前状态</span>
        <strong>{{ formatRemoteRefreshStatus(remoteRefreshState.status) }}</strong>
      </div>
      <div class="intro-stat">
        <span>本轮计划</span>
        <strong>{{ remoteRefreshState.last_batch_total || 0 }}</strong>
      </div>
      <div class="intro-stat">
        <span>可刷新总数</span>
        <strong>{{ remoteRefreshState.last_eligible_total || 0 }}</strong>
      </div>
      <div class="intro-stat">
        <span>剩余待刷</span>
        <strong>{{ remoteRefreshState.last_remaining_total || 0 }}</strong>
      </div>
    </div>
  </section>

  <section class="surface">
    <form class="settings-form token-card" @submit.prevent="saveRemoteRefresh">
      <div class="settings-grid settings-grid--three">
        <label class="field-card">
          <span>启用源端刷新</span>
          <button
            :class="['subscription-switch', remoteRefreshForm.enabled && 'is-on']"
            type="button"
            :disabled="saving"
            @click="remoteRefreshForm.enabled = !remoteRefreshForm.enabled"
          >
            <span class="subscription-switch__track" aria-hidden="true">
              <span class="subscription-switch__thumb"></span>
            </span>
            <span class="subscription-switch__label">{{ remoteRefreshForm.enabled ? "启用中" : "已停用" }}</span>
          </button>
          <small class="archive-form__hint">默认开启。仅对模型库内已有远端来源链接的模型做增量刷新。</small>
        </label>
        <label class="field-card">
          <span>Cron</span>
          <CronField
            v-model="remoteRefreshForm.cron"
            placeholder="0 0 * * *"
            dialog-title="设置源端刷新 Cron"
          />
        </label>
      </div>
      <p class="archive-form__hint">
        启用后会按计划刷新库内全部可刷新的远端模型，自动增量同步评论、附件与打印配置。
        源端刷新不直接下载 3MF，新发现的打印配置会进入新增 3MF 下载队列。
      </p>
      <div class="settings-grid settings-grid--three">
        <label class="field-card">
          <span>下次运行</span>
          <strong>{{ formatDateTime(remoteRefreshState.next_run_at) }}</strong>
        </label>
        <label class="field-card">
          <span>上次运行</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_run_at) }}</strong>
        </label>
        <label class="field-card">
          <span>上次结果</span>
          <strong>{{ remoteRefreshState.last_message || "-" }}</strong>
        </label>
      </div>
      <div class="form-footer">
        <div class="settings-inline-actions">
          <button class="button button-primary" type="submit" :disabled="saving">
            {{ saving ? "保存中..." : "保存源端刷新设置" }}
          </button>
          <button class="button button-secondary" type="button" :disabled="loading" @click="refreshStateManually">
            刷新状态
          </button>
        </div>
        <span class="form-status">{{ status }}</span>
      </div>
    </form>
  </section>

  <section class="remote-refresh-layout">
    <article class="surface section-card remote-refresh-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">批次摘要</span>
          <h2>最近一轮源端刷新</h2>
        </div>
        <span :class="['count-pill', remoteRefreshState.running ? 'count-pill--warn' : 'count-pill--ok']">
          {{ remoteRefreshState.last_batch_succeeded || 0 }} 成功 / {{ remoteRefreshState.last_batch_failed || 0 }} 失败
        </span>
      </div>

      <div class="remote-refresh-stats">
        <div class="remote-refresh-stat">
          <span>本轮计划</span>
          <strong>{{ remoteRefreshState.last_batch_total || 0 }}</strong>
        </div>
        <div class="remote-refresh-stat">
          <span>可刷新总数</span>
          <strong>{{ remoteRefreshState.last_eligible_total || 0 }}</strong>
        </div>
        <div class="remote-refresh-stat">
          <span>缺 Cookie 跳过</span>
          <strong>{{ remoteRefreshState.last_skipped_missing_cookie || 0 }}</strong>
        </div>
        <div class="remote-refresh-stat">
          <span>本地/非单模型跳过</span>
          <strong>{{ remoteRefreshState.last_skipped_local_or_invalid || 0 }}</strong>
        </div>
      </div>

      <div class="remote-refresh-times">
        <div class="remote-refresh-time">
          <span>上次成功</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_success_at) }}</strong>
        </div>
        <div class="remote-refresh-time">
          <span>上次异常</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_error_at) }}</strong>
        </div>
        <div class="remote-refresh-time">
          <span>当前任务</span>
          <strong>{{ hasCurrentItem ? (remoteRefreshState.current_item.title || "刷新中") : "空闲" }}</strong>
        </div>
      </div>

      <p class="archive-form__hint remote-refresh-note">{{ batchExplanation }}</p>

      <div v-if="hasCurrentItem" class="remote-refresh-current">
        <div class="remote-refresh-current__head">
          <strong>{{ remoteRefreshState.current_item.title || "未命名模型" }}</strong>
          <span>{{ remoteRefreshState.current_item.progress || 0 }}%</span>
        </div>
        <div class="progress-bar"><span :style="{ width: `${remoteRefreshState.current_item.progress || 0}%` }"></span></div>
        <p>{{ remoteRefreshState.current_item.message || "源端刷新进行中" }}</p>
      </div>
    </article>

    <article class="surface section-card remote-refresh-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">刷新记录</span>
          <h2>最近源端刷新历史</h2>
        </div>
        <div class="remote-refresh-history__toolbar">
          <div class="remote-refresh-history__filters">
            <button
              v-for="option in historyFilterOptions"
              :key="option.value"
              :class="['remote-refresh-history__filter', historyFilter === option.value && 'is-active']"
              type="button"
              @click="applyHistoryFilter(option.value)"
            >
              {{ option.label }}
            </button>
          </div>
          <span class="count-pill">{{ filteredHistory.length }} / {{ recentHistory.length }} 条</span>
        </div>
      </div>

      <div v-if="visibleHistory.length" class="remote-refresh-history">
        <article
          v-for="item in visibleHistory"
          :key="item.id || `${item.title}-${item.updated_at}`"
          class="remote-refresh-history__item"
        >
          <div class="remote-refresh-history__head">
            <div class="remote-refresh-history__title">
              <strong>{{ item.title || item.url || "未命名模型" }}</strong>
              <span class="remote-refresh-history__time">{{ formatDateTime(item.updated_at) }}</span>
            </div>
            <span :class="['remote-refresh-history__status', `is-${historyStatusClass(item.status)}`]">
              {{ historyStatusLabel(item.status) }}
            </span>
          </div>

          <div v-if="historyChangeLabels(item).length" class="remote-refresh-history__chips">
            <span
              v-for="label in historyChangeLabels(item)"
              :key="`${item.id}-${label}`"
              class="remote-refresh-history__chip"
            >
              {{ label }}
            </span>
          </div>

          <p class="remote-refresh-history__message">{{ item.message || "未记录变化摘要。" }}</p>

          <div class="remote-refresh-history__links">
            <RouterLink
              v-if="historyModelDir(item)"
              class="section-link"
              :to="encodeModelPath(historyModelDir(item))"
            >
              查看本地详情
            </RouterLink>
            <a
              v-if="item.url"
              class="section-link"
              :href="item.url"
              target="_blank"
              rel="noreferrer"
            >
              打开源链接
            </a>
          </div>
        </article>

        <div v-if="filteredHistory.length > historyVisibleLimit" class="task-list-footer">
          <button class="button button-secondary button-small" type="button" @click="historyVisibleLimit += HISTORY_PAGE_SIZE">
            加载更多
          </button>
        </div>
      </div>

      <p v-else class="empty-copy">{{ emptyHistoryText }}</p>
    </article>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { RouterLink } from "vue-router";

import CronField from "../components/CronField.vue";
import { applyConfigPayload, refreshConfig } from "../lib/appState";
import { apiRequest } from "../lib/api";
import { encodeModelPath, formatServerDateTime } from "../lib/helpers";


const HISTORY_PAGE_SIZE = 12;
const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const IDLE_REFRESH_INTERVAL_MS = 60000;
const status = ref("");
const loading = ref(true);
const saving = ref(false);
const historyFilter = ref("changed");
const historyVisibleLimit = ref(HISTORY_PAGE_SIZE);
const remoteRefreshState = ref({});
const remoteRefreshForm = reactive({
  enabled: true,
  cron: "0 0 * * *",
});
const historyFilterOptions = [
  { value: "changed", label: "有远端更新" },
  { value: "all", label: "全部" },
  { value: "issues", label: "异常与跳过" },
];
let refreshTimer = null;
let disposed = false;

const recentHistory = computed(() => {
  const items = remoteRefreshState.value?.recent_items;
  return Array.isArray(items) ? items : [];
});

const filteredHistory = computed(() => {
  if (historyFilter.value === "all") {
    return recentHistory.value;
  }
  if (historyFilter.value === "issues") {
    return recentHistory.value.filter((item) => historyIsIssue(item));
  }
  return recentHistory.value.filter((item) => historyHasRemoteChange(item));
});

const visibleHistory = computed(() => filteredHistory.value.slice(0, historyVisibleLimit.value));

const hasCurrentItem = computed(() => Boolean(remoteRefreshState.value?.current_item?.title || remoteRefreshState.value?.current_item?.id));
const emptyHistoryText = computed(() => {
  if (!recentHistory.value.length) {
    return "还没有源端刷新记录。";
  }
  if (historyFilter.value === "changed") {
    return "当前筛选下没有远端更新记录。";
  }
  if (historyFilter.value === "issues") {
    return "当前筛选下没有异常或跳过记录。";
  }
  return "当前筛选下没有刷新记录。";
});

const batchExplanation = computed(() => {
  const eligibleTotal = Number(remoteRefreshState.value?.last_eligible_total || 0);
  const remainingTotal = Number(remoteRefreshState.value?.last_remaining_total || 0);
  const batchTotal = Number(remoteRefreshState.value?.last_batch_total || 0);
  const successTotal = Number(remoteRefreshState.value?.last_batch_succeeded || 0);
  const failedTotal = Number(remoteRefreshState.value?.last_batch_failed || 0);
  const processedTotal = successTotal + failedTotal;
  const skippedMissingCookie = Number(remoteRefreshState.value?.last_skipped_missing_cookie || 0);
  const skippedLocal = Number(remoteRefreshState.value?.last_skipped_local_or_invalid || 0);

  if (!eligibleTotal) {
    if (skippedMissingCookie > 0) {
      return `当前没有可刷新的模型。已有 ${skippedMissingCookie} 个模型因为缺少对应站点 Cookie 被跳过。`;
    }
    return "当前没有可刷新的远端单模型。只有模型库里已经归档、且带原始 MakerWorld 单模型链接的模型才会参与源端刷新。";
  }

  if (remainingTotal > 0) {
    return `当前可刷新 ${eligibleTotal} 个模型，最近一轮计划处理 ${batchTotal} 个，已完成 ${processedTotal} 个、成功 ${successTotal} 个，仍有 ${remainingTotal} 个待继续刷新。`;
  }

  const skipParts = [];
  if (skippedMissingCookie > 0) {
    skipParts.push(`${skippedMissingCookie} 个缺 Cookie`);
  }
  if (skippedLocal > 0) {
    skipParts.push(`${skippedLocal} 个本地或非单模型来源`);
  }
  const suffix = skipParts.length ? ` 另外还有 ${skipParts.join("，")} 被跳过。` : "";
  return `当前可刷新 ${eligibleTotal} 个模型，最近一轮计划处理 ${batchTotal} 个，已完成 ${processedTotal} 个，剩余 ${remainingTotal} 个。${suffix}`.trim();
});

function applyPayload(payload) {
  const config = payload?.config || {};
  const state = payload?.state || {};
  remoteRefreshForm.enabled = config.enabled !== false;
  remoteRefreshForm.cron = config.cron || "0 0 * * *";
  remoteRefreshState.value = state;
}

function formatRemoteRefreshStatus(value) {
  const mapping = {
    idle: "空闲",
    running: "运行中",
    error: "异常",
    disabled: "已停用",
  };
  return mapping[String(value || "").trim()] || "空闲";
}

function formatDateTime(value) {
  return formatServerDateTime(value, {
    fallback: "-",
    second: true,
  });
}

function historyStatusLabel(value) {
  const mapping = {
    success: "成功",
    failed: "失败",
    skipped: "跳过",
    source_deleted: "源端已删",
  };
  return mapping[String(value || "").trim()] || "记录";
}

function historyStatusClass(value) {
  const normalized = String(value || "").trim();
  if (normalized === "source_deleted") {
    return "source-deleted";
  }
  if (normalized === "failed") {
    return "failed";
  }
  if (normalized === "skipped") {
    return "skipped";
  }
  return "success";
}

function historyChangeLabels(item) {
  const labels = item?.meta?.change_labels;
  if (!Array.isArray(labels)) {
    return [];
  }
  return labels.filter((label) => String(label || "").trim());
}

function historyHasRemoteChange(item) {
  const statusValue = String(item?.status || "").trim();
  if (statusValue === "source_deleted") {
    return true;
  }
  if (statusValue !== "success") {
    return false;
  }
  const labels = historyChangeLabels(item);
  return labels.some((label) => label !== "已检查，无远端变化");
}

function historyIsIssue(item) {
  return ["failed", "skipped"].includes(String(item?.status || "").trim());
}

function historyModelDir(item) {
  return String(item?.meta?.model_dir || "").trim().replace(/^\/+/, "");
}

function applyHistoryFilter(value) {
  historyFilter.value = value;
  historyVisibleLimit.value = HISTORY_PAGE_SIZE;
}

function clearRefreshTimer() {
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function scheduleRefresh() {
  clearRefreshTimer();
  if (disposed || typeof window === "undefined" || document.hidden) {
    return;
  }
  const interval = remoteRefreshState.value?.running ? ACTIVE_REFRESH_INTERVAL_MS : IDLE_REFRESH_INTERVAL_MS;
  refreshTimer = window.setTimeout(() => {
    void load({ silent: true });
  }, interval);
}

async function load({ silent = false } = {}) {
  let ok = true;
  if (!silent) {
    loading.value = true;
  }
  try {
    const payload = await apiRequest("/api/remote-refresh");
    applyPayload(payload);
  } catch (error) {
    ok = false;
    if (!silent) {
      status.value = error instanceof Error ? error.message : "源端刷新状态加载失败。";
    } else {
      console.error("源端刷新状态刷新失败", error);
    }
  } finally {
    loading.value = false;
    scheduleRefresh();
  }
  return ok;
}

async function saveRemoteRefresh() {
  saving.value = true;
  try {
    const payload = await apiRequest("/api/config/remote-refresh", {
      method: "POST",
      body: {
        enabled: remoteRefreshForm.enabled,
        cron: remoteRefreshForm.cron,
      },
    });
    applyConfigPayload(payload);
    await load({ silent: true });
    status.value = "源端刷新设置已保存。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  } finally {
    saving.value = false;
  }
}

async function refreshStateManually() {
  status.value = "";
  const ok = await load();
  if (ok) {
    status.value = "源端刷新状态已刷新。";
  }
}

function handleVisibilityChange() {
  if (document.hidden) {
    clearRefreshTimer();
    return;
  }
  void load({ silent: true });
}

onMounted(async () => {
  disposed = false;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  await load();
});

onBeforeUnmount(() => {
  disposed = true;
  clearRefreshTimer();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
