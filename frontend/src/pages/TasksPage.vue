<template>
  <section class="surface surface--filters page-intro app-page-toolbar" data-tasks-page>
    <div class="app-page-toolbar__copy">
      <span class="eyebrow">任务</span>
      <div class="app-page-toolbar__title-row">
        <h1>任务总览</h1>
      </div>
    </div>
    <div class="intro-stats app-page-toolbar__stats">
      <div class="intro-stat">
        <span>运行中/排队</span>
        <strong>{{ payload.summary.running_or_queued }}</strong>
      </div>
      <div class="intro-stat">
        <span>缺失 3MF</span>
        <strong>{{ payload.summary.missing_3mf_count }}</strong>
      </div>
      <div class="intro-stat">
        <span>整理中</span>
        <strong>{{ payload.summary.organize_count }}</strong>
      </div>
    </div>
  </section>

  <section class="surface section-card">
    <div class="section-card__header">
      <div>
        <span class="eyebrow">归档入口</span>
        <h2>输入链接开始归档</h2>
      </div>
    </div>
    <form class="archive-form" @submit.prevent="submitArchive">
      <input
        v-model.trim="archiveUrl"
        class="archive-form__input"
        type="text"
        placeholder="支持单模型、作者上传页、收藏夹模型页链接"
      >
      <button class="button button-primary" type="submit" :disabled="submittingArchive">
        {{ submittingArchive ? "提交中..." : "开始归档" }}
      </button>
    </form>
    <p class="archive-form__hint">示例：`/zh/models/...`、`/zh/@xxx/upload`、`/zh/@xxx/collections/models`、`/zh/collections/267246-...`</p>
    <span class="form-status">{{ archiveStatus }}</span>
  </section>

  <div
    v-if="archiveSubmitDialog.visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="archive-submit-dialog-title"
    @click="closeArchiveSubmitDialog"
  >
    <div class="submit-dialog__panel" @click.stop>
      <span :class="['submit-dialog__icon', archiveSubmitDialog.variant === 'confirm' && 'submit-dialog__icon--confirm']">
        {{ archiveSubmitDialog.variant === "confirm" ? archiveSubmitDialog.discoveredCount : "✓" }}
      </span>
      <h2 id="archive-submit-dialog-title">{{ archiveSubmitDialog.title }}</h2>
      <p>{{ archiveSubmitDialog.message }}</p>
      <p v-if="archiveSubmitDialog.summary" class="submit-dialog__summary">{{ archiveSubmitDialog.summary }}</p>
      <p
        v-if="archiveSubmitDialog.variant === 'confirm' && archiveSubmitDialog.subscriptionSupported && archiveSubmitDialog.subscriptionName"
        class="submit-dialog__summary"
      >
        归档并订阅将自动添加订阅：{{ archiveSubmitDialog.subscriptionName }}
      </p>
      <div class="submit-dialog__actions">
        <button
          v-if="archiveSubmitDialog.variant === 'confirm'"
          class="button button-secondary"
          type="button"
          :disabled="Boolean(confirmingArchiveMode)"
          @click="closeArchiveSubmitDialog"
        >
          取消
        </button>
        <button
          v-if="archiveSubmitDialog.variant === 'confirm' && archiveSubmitDialog.subscriptionSupported"
          class="button button-secondary"
          type="button"
          :disabled="Boolean(confirmingArchiveMode)"
          @click="submitArchiveFromDialog(false)"
        >
          {{ confirmingArchiveMode === "archive" ? "提交中..." : "仅归档" }}
        </button>
        <button
          class="button button-primary"
          type="button"
          :disabled="Boolean(confirmingArchiveMode)"
          @click="handleArchiveDialogPrimaryAction"
        >
          {{
            archiveSubmitDialog.variant === "confirm"
              ? (
                archiveSubmitDialog.subscriptionSupported
                  ? (confirmingArchiveMode === "archive_and_subscribe" ? "提交中..." : "归档并订阅")
                  : (confirmingArchiveMode === "archive" ? "提交中..." : "确认提交")
              )
              : "知道了"
          }}
        </button>
      </div>
    </div>
  </div>

  <section v-if="runtimeMode" class="surface section-card">
    <div class="section-card__header">
      <div>
        <span class="eyebrow">运行核心</span>
        <h2>批次任务</h2>
      </div>
      <span class="count-pill">{{ runtimeRuns.length }} 个运行 / {{ runtimeBatches.length }} 个批次</span>
    </div>
    <div class="task-columns">
      <div class="task-column">
        <h3>运行</h3>
        <div v-if="runtimeRuns.length">
          <div v-for="run in runtimeRuns" :key="run.run_id" class="task-item">
            <strong>{{ run.message || run.source_url || run.run_id }}</strong>
            <span>{{ runtimeRunLabel(run.status) }}</span>
            <p>总数 {{ run.total || 0 }} · 完成 {{ run.completed || 0 }} · 失败 {{ run.failed || 0 }}</p>
          </div>
        </div>
        <p v-else class="empty-copy">当前没有运行中的批次。</p>
      </div>
      <div class="task-column">
        <h3>批次</h3>
        <div v-if="runtimeBatches.length">
          <div v-for="batch in runtimeBatches" :key="batch.batch_id" class="task-item">
            <strong>{{ batch.message || batch.batch_id }}</strong>
            <span>{{ runtimeRunLabel(batch.status) }}</span>
            <p>总数 {{ batch.total || 0 }} · 完成 {{ batch.completed || 0 }} · 失败 {{ batch.failed || 0 }}</p>
          </div>
        </div>
        <p v-else class="empty-copy">暂无等待执行的批次。</p>
      </div>
      <div class="task-column">
        <h3>失败明细</h3>
        <div v-if="runtimeFailures.length">
          <div
            v-for="failure in runtimeFailures"
            :key="failure.failure_id"
            class="task-item task-item--error"
          >
            <strong>{{ failure.title || failure.model_id || failure.failure_id }}</strong>
            <span>{{ runtimeFailureLabel(failure.status) }}</span>
            <p>{{ failure.message || "等待处理。" }}</p>
          </div>
        </div>
        <p v-else class="empty-copy">暂无失败明细。</p>
      </div>
    </div>
  </section>

  <section v-else class="task-layout">
    <article class="surface section-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">归档队列</span>
          <h2>当前归档任务</h2>
        </div>
        <div class="filter-actions">
          <button
            class="button button-secondary button-small"
            type="button"
            :disabled="repairingArchiveQueue"
            @click="repairArchiveQueue"
          >
            {{ repairingArchiveQueue ? "修复中..." : "修复队列" }}
          </button>
          <span class="count-pill">{{ payload.archive_queue.running_count }} 运行中 / {{ payload.archive_queue.queued_count }} 排队中</span>
        </div>
      </div>
      <span v-if="archiveRepairStatus" class="form-status">{{ archiveRepairStatus }}</span>
      <div class="task-columns">
        <div class="task-column">
          <h3>运行中</h3>
          <div v-if="visibleActiveTasks.length">
            <div
              v-for="item in visibleActiveTasks"
              :key="item.id || item.title"
              class="task-item"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ runtimeStatusLabel(item) }}</span>
              <div v-if="item.progress" class="progress-bar"><span :style="{ width: `${item.progress}%` }"></span></div>
              <p>{{ item.message || "正在执行中" }}</p>
              <div v-if="archiveSubtasks(item).length" class="archive-subtasks">
                <div
                  v-for="subtask in archiveSubtasks(item)"
                  :key="subtask.type"
                  :class="['archive-subtask', `is-${subtask.status || 'pending'}`]"
                >
                  <div class="archive-subtask__head">
                    <span>{{ subtask.label }}</span>
                    <strong>{{ archiveSubtaskStatusLabel(subtask.status) }}</strong>
                  </div>
                  <div v-if="subtask.status === 'running' || subtask.progress" class="archive-subtask__bar">
                    <span :style="{ width: `${subtask.progress || 0}%` }"></span>
                  </div>
                </div>
              </div>
              <a
                v-if="runtimeExternalAction(item)"
                class="button button-secondary button-small"
                :href="runtimeExternalAction(item).href"
                target="_blank"
                rel="noreferrer noopener"
              >
                {{ runtimeExternalAction(item).label }}
              </a>
            </div>
            <div v-if="(archiveQueueForDisplay.active || []).length > activeVisibleLimit" class="task-list-footer">
              <button class="button button-secondary button-small" type="button" @click="activeVisibleLimit += TASKS_PAGE_SIZE">
                加载更多
              </button>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有运行中的归档任务。</p>
        </div>
        <div class="task-column">
          <h3>排队中</h3>
          <div v-if="visibleQueuedTasks.length">
            <div
              v-for="item in visibleQueuedTasks"
              :key="item.id || item.title"
              class="task-item"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ runtimeStatusLabel(item) }}</span>
              <p>{{ item.message || "等待归档" }}</p>
              <div v-if="archiveSubtasks(item).length" class="archive-subtasks">
                <div
                  v-for="subtask in archiveSubtasks(item)"
                  :key="subtask.type"
                  :class="['archive-subtask', `is-${subtask.status || 'pending'}`]"
                >
                  <div class="archive-subtask__head">
                    <span>{{ subtask.label }}</span>
                    <strong>{{ archiveSubtaskStatusLabel(subtask.status) }}</strong>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="(archiveQueueForDisplay.queued || []).length > queuedVisibleLimit" class="task-list-footer">
              <button class="button button-secondary button-small" type="button" @click="queuedVisibleLimit += TASKS_PAGE_SIZE">
                加载更多
              </button>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有排队中的任务。</p>
        </div>
        <div class="task-column">
          <div class="task-column__heading">
            <h3>最近失败</h3>
            <button
              v-if="(archiveQueueForDisplay.recent_failures || []).length"
              class="button button-secondary button-small"
              type="button"
              :disabled="clearingRecentFailures"
              @click="clearRecentFailures"
            >
              {{ clearingRecentFailures ? "清除中..." : "清除" }}
            </button>
          </div>
          <span v-if="recentFailureStatus" class="form-status">{{ recentFailureStatus }}</span>
          <div v-if="visibleFailureTasks.length">
            <div
              v-for="item in visibleFailureTasks"
              :key="item.id || item.title"
              class="task-item task-item--error"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ runtimeStatusLabel(item) }}</span>
              <p>{{ item.message || "失败原因未记录" }}</p>
              <div v-if="archiveSubtasks(item).length" class="archive-subtasks">
                <div
                  v-for="subtask in archiveSubtasks(item)"
                  :key="subtask.type"
                  :class="['archive-subtask', `is-${subtask.status || 'pending'}`]"
                >
                  <div class="archive-subtask__head">
                    <span>{{ subtask.label }}</span>
                    <strong>{{ archiveSubtaskStatusLabel(subtask.status) }}</strong>
                  </div>
                  <div v-if="subtask.status === 'running' || subtask.progress" class="archive-subtask__bar">
                    <span :style="{ width: `${subtask.progress || 0}%` }"></span>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="(archiveQueueForDisplay.recent_failures || []).length > failureVisibleLimit" class="task-list-footer">
              <button class="button button-secondary button-small" type="button" @click="failureVisibleLimit += TASKS_PAGE_SIZE">
                加载更多
              </button>
            </div>
          </div>
          <p v-else class="empty-copy">暂无失败任务。</p>
        </div>
      </div>
    </article>

    <article class="surface section-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">缺失 3MF</span>
          <h2>待重新下载</h2>
        </div>
        <div class="filter-actions">
          <button
            v-if="payload.missing_3mf.items.length"
            class="button button-secondary button-small"
            type="button"
            :disabled="retryingAllMissing"
            @click="retryAllMissing"
          >
            {{ retryingAllMissing ? "提交中..." : "全部重试" }}
          </button>
          <span class="count-pill">{{ payload.missing_3mf.count }} 项</span>
        </div>
      </div>
      <span class="form-status">{{ missingStatus }}</span>
      <div v-if="visibleMissingItems.length" class="table-like">
        <div class="table-like__row table-like__row--missing table-like__row--head">
          <span>模型 ID</span>
          <span>标题</span>
          <span>状态</span>
          <span>操作</span>
        </div>
        <div
          v-for="item in visibleMissingItems"
          :key="`${item.model_id}-${item.instance_id}-${item.title}`"
          class="table-like__row table-like__row--missing"
        >
          <span>
            <RouterLink
              v-if="missingDetailPath(item)"
              class="task-model-link"
              :to="missingDetailPath(item)"
            >
              {{ item.model_id || "-" }}
            </RouterLink>
            <template v-else>{{ item.model_id || "-" }}</template>
          </span>
          <span>{{ item.title || "未命名模型" }}</span>
          <span>
            <span class="missing-status">
              <strong>{{ formatMissingStatus(item.status) }}</strong>
              <small v-if="item.message">{{ item.message }}</small>
            </span>
          </span>
          <span>
            <span class="missing-actions">
            <a
              v-if="needsManualVerification(item)"
              class="button button-primary button-small"
              :href="missingVerificationHref(item)"
              target="_blank"
              rel="noreferrer noopener"
              @click="missingStatus = missingActionHint(item)"
            >
              访问源页面
            </a>
            <button
              class="button button-secondary button-small"
              type="button"
              :disabled="isMissingActionBusy(item) || isRetryLocked(item)"
              @click="retryMissing(item)"
            >
              {{ retryLabel(item) }}
            </button>
            <button
              class="button button-danger button-small"
              type="button"
              :disabled="isMissingActionBusy(item)"
              @click="cancelMissing(item)"
            >
              取消
            </button>
            </span>
          </span>
        </div>
      </div>
      <div v-if="payload.missing_3mf.items.length > missingVisibleLimit" class="task-list-footer">
        <button class="button button-secondary button-small" type="button" @click="missingVisibleLimit += TASKS_PAGE_SIZE">
          加载更多
        </button>
      </div>
      <p v-else class="empty-copy">当前没有缺失 3MF 任务。</p>
    </article>

  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import { apiRequest } from "../lib/api";
import { normalizeRuntimeStatusLabel, runtimeTaskAction } from "../lib/dashboardStatus";
import { encodeModelPath } from "../lib/helpers";
import { createPagePerformanceTracker } from "../lib/performance";
import { runtimeFailureLabel, runtimeRunLabel, runtimeTaskShape } from "../lib/runtimeStatus";
import { createHydratedResource } from "../lib/useHydratedResource";
import { createPageRefreshController } from "../lib/usePageRefresh";


const payload = ref({
  archive_queue: {
    active: [],
    queued: [],
    recent_failures: [],
    running_count: 0,
    queued_count: 0,
  },
  archive_queue_display: null,
  missing_3mf: {
    items: [],
    count: 0,
  },
  organize_tasks: {
    items: [],
    count: 0,
    queued_count: 0,
    running_count: 0,
    detected_total: 0,
  },
  summary: {
    running_or_queued: 0,
    missing_3mf_count: 0,
    organize_count: 0,
  },
});

const archiveUrl = ref("");
const archiveStatus = ref("");
const archiveSubmitDialog = ref({
  visible: false,
  variant: "success",
  title: "",
  message: "",
  summary: "",
  previewToken: "",
  url: "",
  discoveredCount: 0,
  subscriptionSupported: false,
  subscriptionName: "",
});
const missingStatus = ref("");
const submittingArchive = ref(false);
const confirmingArchiveMode = ref("");
const pendingMissingActionKey = ref("");
const retryingAllMissing = ref(false);
const clearingRecentFailures = ref(false);
const repairingArchiveQueue = ref(false);
const recentFailureStatus = ref("");
const archiveRepairStatus = ref("");
let tasksRefreshController = null;
let loadingTasks = false;
let loadingFullTasks = false;
let perf = null;

const tasksResource = createHydratedResource({
  load: ({ signal }) => apiRequest("/api/tasks/light", { signal }),
  enrich: (_current, { signal }) => apiRequest("/api/tasks", { signal }),
  onData: (response) => {
    payload.value = response;
    if (payload.value.archive_queue.recent_failures.length) {
      recentFailureStatus.value = "";
    }
  },
});
const TASKS_PAGE_SIZE = 5;
const activeVisibleLimit = ref(TASKS_PAGE_SIZE);
const queuedVisibleLimit = ref(TASKS_PAGE_SIZE);
const failureVisibleLimit = ref(TASKS_PAGE_SIZE);
const missingVisibleLimit = ref(TASKS_PAGE_SIZE);
const archiveQueueForDisplay = computed(() => payload.value.archive_queue_display || payload.value.archive_queue || {});
const visibleActiveTasks = computed(() => (archiveQueueForDisplay.value.active || []).slice(0, activeVisibleLimit.value));
const visibleQueuedTasks = computed(() => (archiveQueueForDisplay.value.queued || []).slice(0, queuedVisibleLimit.value));
const visibleFailureTasks = computed(() => (archiveQueueForDisplay.value.recent_failures || []).slice(0, failureVisibleLimit.value));
const visibleMissingItems = computed(() => payload.value.missing_3mf.items.slice(0, missingVisibleLimit.value));
const taskShape = computed(() => runtimeTaskShape(payload.value));
const runtimeMode = computed(() => taskShape.value.mode === "runtime");
const runtimeRuns = computed(() => (runtimeMode.value ? taskShape.value.runs : []));
const runtimeBatches = computed(() => (runtimeMode.value ? taskShape.value.batches : []));
const runtimeFailures = computed(() => (runtimeMode.value ? taskShape.value.failures : []));
const ARCHIVE_SUBTASK_ORDER = ["metadata", "media", "attachments", "comments", "three_mf", "finalize"];

function getMissingKey(item) {
  return [
    item?.model_id || "",
    item?.instance_id || "",
    item?.title || "",
    item?.model_url || "",
  ].join("::");
}

function isMissingActionBusy(item) {
  return pendingMissingActionKey.value === getMissingKey(item);
}

function isRetryLocked(item) {
  return ["queued", "running"].includes(String(item?.status || "").toLowerCase());
}

function retryLabel(item) {
  const status = String(item?.status || "").toLowerCase();
  if (status === "queued") return "已入队";
  if (status === "running") return "处理中";
  return "重新下载";
}

function needsManualVerification(item) {
  const status = String(item?.status || "").toLowerCase();
  return ["verification_required", "cloudflare", "auth_required", "cookie_invalid"].includes(status);
}

function sourceHomepageForMissingItem(item) {
  const source = String(item?.source || "").toLowerCase();
  const url = String(item?.model_url || "");
  if (source === "global" || url.includes("makerworld.com/")) {
    return "https://makerworld.com";
  }
  return "https://makerworld.com.cn";
}

function missingVerificationHref(item) {
  const url = String(item?.model_url || "").trim();
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  return sourceHomepageForMissingItem(item);
}

function missingActionHint(item) {
  const status = String(item?.status || "").toLowerCase();
  if (status === "cloudflare") {
    return "请完成 Cloudflare 校验或补充 cf_clearance 后回到 MakerHub 重试。";
  }
  if (status === "auth_required" || status === "cookie_invalid") {
    return "请更新对应站点 Cookie / token 后回到 MakerHub 重试。";
  }
  return "请在 MakerWorld 完成验证后回到 MakerHub 重试。";
}

function missingDetailPath(item) {
  return encodeModelPath(item || "");
}

function formatMissingStatus(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "queued") return "已入队";
  if (normalized === "running") return "处理中";
  if (normalized === "failed") return "失败";
  if (normalized === "verification_required") return "需要验证";
  if (normalized === "cloudflare") return "Cloudflare 校验";
  if (normalized === "auth_required" || normalized === "cookie_invalid") return "Cookie 失效";
  if (normalized === "download_limited") return "到达自动下载上限";
  if (normalized === "not_found") return "源端无文件";
  return status || "missing";
}

function runtimeStatusLabel(item) {
  return normalizeRuntimeStatusLabel(item?.status, item?.blocked_reason || item?.blockedReason || "");
}

function runtimeAction(item) {
  return runtimeTaskAction(item || {});
}

function runtimeExternalAction(item) {
  const action = runtimeAction(item);
  return action?.kind === "external" ? action : null;
}

function archiveSubtasks(item) {
  const subtasks = Array.isArray(item?.subtasks) ? item.subtasks : [];
  return [...subtasks]
    .filter((subtask) => subtask && subtask.type)
    .sort((left, right) => {
      const leftIndex = ARCHIVE_SUBTASK_ORDER.indexOf(left.type);
      const rightIndex = ARCHIVE_SUBTASK_ORDER.indexOf(right.type);
      return (leftIndex < 0 ? 99 : leftIndex) - (rightIndex < 0 ? 99 : rightIndex);
    });
}

function archiveSubtaskStatusLabel(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "done" || normalized === "completed") return "完成";
  if (normalized === "running") return "进行中";
  if (normalized === "failed" || normalized === "error") return "失败";
  if (normalized === "blocked") return "阻塞";
  if (normalized === "skipped") return "跳过";
  return "等待";
}

function closeArchiveSubmitDialog() {
  archiveSubmitDialog.value.visible = false;
}

function openArchiveSuccessDialog(message) {
  archiveSubmitDialog.value = {
    visible: true,
    variant: "success",
    title: "任务已提交",
    message,
    summary: "",
    previewToken: "",
    url: "",
    discoveredCount: 0,
    subscriptionSupported: false,
    subscriptionName: "",
  };
}

function openArchiveConfirmDialog(preview) {
  archiveSubmitDialog.value = {
    visible: true,
    variant: "confirm",
    title: "确认批量归档",
    message: `该链接扫描到 ${preview.discovered_count || 0} 个模型，确认后会把这批模型加入归档队列。`,
    summary: preview.message || "",
    previewToken: preview.preview_token || "",
    url: preview.url || archiveUrl.value,
    discoveredCount: preview.discovered_count || 0,
    subscriptionSupported: Boolean(preview.subscription_supported),
    subscriptionName: preview.subscription_name || "",
  };
}

async function load() {
  if (loadingTasks) {
    return;
  }
  loadingTasks = true;
  try {
    await tasksResource.load();
  } finally {
    loadingTasks = false;
  }
}

async function refreshFullTasks() {
  if (loadingFullTasks) {
    return;
  }
  loadingFullTasks = true;
  const enrichmentPerf = createPagePerformanceTracker({
    page: "tasks",
    eventKind: "enrichment",
  });
  try {
    const enriched = await tasksResource.enrich();
    if (enriched !== undefined) {
      enrichmentPerf.markEnrichmentReady();
      void enrichmentPerf.finish();
    }
  } finally {
    loadingFullTasks = false;
  }
}

function stopTasksRefreshController() {
  if (tasksRefreshController) {
    tasksRefreshController.dispose();
    tasksRefreshController = null;
  }
}

async function submitArchive() {
  if (!archiveUrl.value) {
    archiveStatus.value = "请先输入归档链接。";
    return;
  }

  submittingArchive.value = true;
  archiveStatus.value = "正在识别链接类型...";
  try {
    const preview = await apiRequest("/api/archive/preview", {
      method: "POST",
      body: { url: archiveUrl.value },
    });
    if (preview.accepted === false) {
      archiveStatus.value = preview.message || "预扫描失败。";
      return;
    }
    if (preview.requires_confirmation) {
      archiveStatus.value = preview.message || "批量链接扫描完成。";
      openArchiveConfirmDialog(preview);
      return;
    }
    await submitArchiveConfirmed({
      url: preview.url || archiveUrl.value,
      previewToken: "",
      clearInput: true,
    });
  } catch (error) {
    archiveStatus.value = error instanceof Error ? error.message : "提交失败。";
  } finally {
    submittingArchive.value = false;
  }
}

async function submitArchiveConfirmed({ url, previewToken = "", clearInput = false, createSubscription = false, subscriptionName = "" } = {}) {
  const response = await apiRequest("/api/archive", {
    method: "POST",
    body: {
      url: url || archiveUrl.value,
      preview_token: previewToken,
      create_subscription: createSubscription,
      subscription_name: subscriptionName,
    },
  });
  if (response.accepted === false) {
    throw new Error(response.message || "归档任务提交失败。");
  }
  const message = response.message || "归档任务已加入队列。";
  archiveStatus.value = message;
  openArchiveSuccessDialog(message);
  if (clearInput) {
    archiveUrl.value = "";
  }
  await refreshFullTasks();
}

async function handleArchiveDialogPrimaryAction() {
  if (archiveSubmitDialog.value.variant !== "confirm") {
    closeArchiveSubmitDialog();
    return;
  }
  await submitArchiveFromDialog(Boolean(archiveSubmitDialog.value.subscriptionSupported));
}

async function submitArchiveFromDialog(createSubscription) {
  confirmingArchiveMode.value = createSubscription ? "archive_and_subscribe" : "archive";
  try {
    await submitArchiveConfirmed({
      url: archiveSubmitDialog.value.url,
      previewToken: archiveSubmitDialog.value.previewToken,
      clearInput: true,
      createSubscription,
      subscriptionName: archiveSubmitDialog.value.subscriptionName,
    });
  } catch (error) {
    archiveStatus.value = error instanceof Error ? error.message : "提交失败。";
    closeArchiveSubmitDialog();
  } finally {
    confirmingArchiveMode.value = "";
  }
}

async function retryMissing(item) {
  pendingMissingActionKey.value = getMissingKey(item);
  try {
    const response = await apiRequest("/api/tasks/missing-3mf/retry", {
      method: "POST",
      body: {
        model_id: item.model_id,
        model_url: item.model_url,
        source: item.source,
        title: item.title,
        instance_id: item.instance_id,
      },
    });
    missingStatus.value = response.message || "已加入重试队列。";
    await refreshFullTasks();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "重试失败。";
  } finally {
    pendingMissingActionKey.value = "";
  }
}

async function clearRecentFailures() {
  if (!payload.value.archive_queue.recent_failures.length) {
    return;
  }
  if (!window.confirm("确认清除最近失败记录吗？这不会取消正在运行或排队的任务。")) {
    return;
  }

  clearingRecentFailures.value = true;
  recentFailureStatus.value = "";
  try {
    const response = await apiRequest("/api/tasks/recent-failures/clear", {
      method: "POST",
    });
    payload.value = response;
    failureVisibleLimit.value = TASKS_PAGE_SIZE;
    recentFailureStatus.value = response.message || "已清除最近失败记录。";
  } catch (error) {
    recentFailureStatus.value = error instanceof Error ? error.message : "清除失败。";
  } finally {
    clearingRecentFailures.value = false;
  }
}

async function repairArchiveQueue() {
  repairingArchiveQueue.value = true;
  archiveRepairStatus.value = "";
  try {
    const response = await apiRequest("/api/tasks/archive-queue/repair", {
      method: "POST",
    });
    const summary = response.summary || {};
    if (response.archive_queue) {
      payload.value.archive_queue = response.archive_queue;
    } else {
      await refreshFullTasks();
    }
    archiveRepairStatus.value = [
      response.message || "队列状态修复完成。",
      `检查 ${Number(summary.examined || 0)} 个`,
      `重排 ${Number(summary.requeued || 0)} 个`,
      `失败 ${Number(summary.failed || 0)} 个`,
      `跳过 ${Number(summary.skipped || 0)} 个`,
    ].join(" · ");
  } catch (error) {
    archiveRepairStatus.value = error instanceof Error ? error.message : "队列修复失败。";
  } finally {
    repairingArchiveQueue.value = false;
  }
}

async function retryAllMissing() {
  retryingAllMissing.value = true;
  try {
    const response = await apiRequest("/api/tasks/missing-3mf/retry-all", {
      method: "POST",
    });
    missingStatus.value = response.message || "已加入重试队列。";
    await refreshFullTasks();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "重试失败。";
  } finally {
    retryingAllMissing.value = false;
  }
}

async function cancelMissing(item) {
  pendingMissingActionKey.value = getMissingKey(item);
  try {
    const response = await apiRequest("/api/tasks/missing-3mf/cancel", {
      method: "POST",
      body: {
        model_id: item.model_id,
        model_url: item.model_url,
        title: item.title,
        instance_id: item.instance_id,
      },
    });
    missingStatus.value = response.message || "已取消该缺失 3MF 任务。";
    await refreshFullTasks();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "取消失败。";
  } finally {
    pendingMissingActionKey.value = "";
  }
}

onMounted(async () => {
  perf = createPagePerformanceTracker({ page: "tasks" });
  tasksRefreshController = createPageRefreshController({
    scopes: ["archive_queue", "missing_3mf", "organize_tasks"],
    refresh: () => load(),
    delayMs: 1000,
    debounceMs: 0,
    resetExistingTimer: false,
  });
  await load();
  perf.markDataReady();
  void perf.finish();
});

onBeforeUnmount(() => {
  tasksResource.cancel();
  stopTasksRefreshController();
  perf = null;
});
</script>
