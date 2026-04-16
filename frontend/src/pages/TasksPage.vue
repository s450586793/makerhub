<template>
  <section class="page-intro" data-tasks-page>
    <div>
      <span class="eyebrow">任务</span>
      <h1>任务总览</h1>
    </div>
    <div class="intro-stats">
      <div class="intro-stat">
        <span>运行中/排队</span>
        <strong>{{ payload.summary.running_or_queued }}</strong>
      </div>
      <div class="intro-stat">
        <span>缺失 3MF</span>
        <strong>{{ payload.summary.missing_3mf_count }}</strong>
      </div>
      <div class="intro-stat">
        <span>整理任务</span>
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
    <p class="archive-form__hint">示例：`/zh/models/...`、`/zh/@xxx/upload`、`/zh/@xxx/collections/models`</p>
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
      <div class="submit-dialog__actions">
        <button
          v-if="archiveSubmitDialog.variant === 'confirm'"
          class="button button-secondary"
          type="button"
          :disabled="confirmingArchive"
          @click="closeArchiveSubmitDialog"
        >
          取消
        </button>
        <button
          class="button button-primary"
          type="button"
          :disabled="confirmingArchive"
          @click="handleArchiveDialogPrimaryAction"
        >
          {{ archiveSubmitDialog.variant === "confirm" ? (confirmingArchive ? "提交中..." : "确认提交") : "知道了" }}
        </button>
      </div>
    </div>
  </div>

  <section class="task-layout">
    <article class="surface section-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">归档队列</span>
          <h2>当前归档任务</h2>
        </div>
        <span class="count-pill">{{ payload.archive_queue.running_count }} 运行中 / {{ payload.archive_queue.queued_count }} 排队中</span>
      </div>
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
              <span>{{ item.status }}</span>
              <div v-if="item.progress" class="progress-bar"><span :style="{ width: `${item.progress}%` }"></span></div>
              <p>{{ item.message || "正在执行中" }}</p>
            </div>
            <div v-if="payload.archive_queue.active.length > activeVisibleLimit" class="task-list-footer">
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
              <span>{{ item.status }}</span>
              <p>{{ item.message || "等待归档" }}</p>
            </div>
            <div v-if="payload.archive_queue.queued.length > queuedVisibleLimit" class="task-list-footer">
              <button class="button button-secondary button-small" type="button" @click="queuedVisibleLimit += TASKS_PAGE_SIZE">
                加载更多
              </button>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有排队中的任务。</p>
        </div>
        <div class="task-column">
          <h3>最近失败</h3>
          <div v-if="visibleFailureTasks.length">
            <div
              v-for="item in visibleFailureTasks"
              :key="item.id || item.title"
              class="task-item task-item--error"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ item.status }}</span>
              <p>{{ item.message || "失败原因未记录" }}</p>
            </div>
            <div v-if="payload.archive_queue.recent_failures.length > failureVisibleLimit" class="task-list-footer">
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
            @click="retryAllMissing"
          >
            全部重试
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
          <span>{{ item.model_id || "-" }}</span>
          <span>{{ item.title || "未命名模型" }}</span>
          <span>
            <span class="missing-status">
              <strong>{{ formatMissingStatus(item.status) }}</strong>
              <small v-if="item.message">{{ item.message }}</small>
            </span>
          </span>
          <span>
            <span class="missing-actions">
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

    <article class="surface section-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">本地整理</span>
          <h2>本地整理任务</h2>
        </div>
        <div class="filter-actions">
          <button
            v-if="payload.organize_tasks.items.length"
            class="button button-secondary button-small"
            type="button"
            :disabled="clearingOrganizeTasks"
            @click="clearOrganizeTasks"
          >
            {{ clearingOrganizeTasks ? "清空中..." : "清空记录" }}
          </button>
          <span class="count-pill">{{ payload.organize_tasks.count }} 项</span>
        </div>
      </div>
      <span class="form-status">{{ organizeStatus }}</span>
      <div v-if="payload.organize_tasks.items.length" class="table-like">
        <div class="table-like__row table-like__row--head">
          <span>文件</span>
          <span>模型目录</span>
          <span>状态</span>
        </div>
        <div
          v-for="(item, index) in payload.organize_tasks.items"
          :key="item.id || item.fingerprint || item.source_path || `${item.source_dir}-${item.target_dir}-${index}`"
          class="table-like__row"
        >
          <span>
            <span class="missing-status">
              <strong>{{ item.title || item.file_name || "未命名文件" }}</strong>
              <small>{{ item.source_path || item.source_dir || "-" }}</small>
            </span>
          </span>
          <span>
            <span class="missing-status">
              <strong>{{ item.model_dir || "-" }}</strong>
              <small>{{ item.target_path || item.target_dir || "-" }}</small>
            </span>
          </span>
          <span>
            <span class="missing-status">
              <strong>{{ item.status }}</strong>
              <small>{{ item.message || "-" }}</small>
            </span>
          </span>
        </div>
      </div>
      <p v-else class="empty-copy">当前没有本地整理任务。</p>
    </article>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import { apiRequest } from "../lib/api";


const payload = ref({
  archive_queue: {
    active: [],
    queued: [],
    recent_failures: [],
    running_count: 0,
    queued_count: 0,
  },
  missing_3mf: {
    items: [],
    count: 0,
  },
  organize_tasks: {
    items: [],
    count: 0,
  },
  summary: {
    running_or_queued: 0,
    missing_3mf_count: 0,
    organize_count: 0,
  },
});

const archiveUrl = ref("");
const archiveStatus = ref("");
const organizeStatus = ref("");
const archiveSubmitDialog = ref({
  visible: false,
  variant: "success",
  title: "",
  message: "",
  summary: "",
  previewToken: "",
  url: "",
  discoveredCount: 0,
});
const missingStatus = ref("");
const submittingArchive = ref(false);
const confirmingArchive = ref(false);
const clearingOrganizeTasks = ref(false);
const pendingMissingActionKey = ref("");
let refreshTimer = null;
let loadingTasks = false;
const TASKS_PAGE_SIZE = 5;
const activeVisibleLimit = ref(TASKS_PAGE_SIZE);
const queuedVisibleLimit = ref(TASKS_PAGE_SIZE);
const failureVisibleLimit = ref(TASKS_PAGE_SIZE);
const missingVisibleLimit = ref(TASKS_PAGE_SIZE);

const visibleActiveTasks = computed(() => payload.value.archive_queue.active.slice(0, activeVisibleLimit.value));
const visibleQueuedTasks = computed(() => payload.value.archive_queue.queued.slice(0, queuedVisibleLimit.value));
const visibleFailureTasks = computed(() => payload.value.archive_queue.recent_failures.slice(0, failureVisibleLimit.value));
const visibleMissingItems = computed(() => payload.value.missing_3mf.items.slice(0, missingVisibleLimit.value));

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

function formatMissingStatus(status) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "queued") return "已入队";
  if (normalized === "running") return "处理中";
  if (normalized === "failed") return "失败";
  return status || "missing";
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
  };
}

function syncAutoRefresh() {
  if (!refreshTimer) {
    refreshTimer = window.setInterval(load, 5000);
  }
}

async function load() {
  if (loadingTasks) {
    return;
  }
  loadingTasks = true;
  try {
    payload.value = await apiRequest("/api/tasks");
    syncAutoRefresh();
  } finally {
    loadingTasks = false;
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

async function submitArchiveConfirmed({ url, previewToken = "", clearInput = false } = {}) {
  const response = await apiRequest("/api/archive", {
    method: "POST",
    body: {
      url: url || archiveUrl.value,
      preview_token: previewToken,
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
  await load();
}

async function handleArchiveDialogPrimaryAction() {
  if (archiveSubmitDialog.value.variant !== "confirm") {
    closeArchiveSubmitDialog();
    return;
  }
  confirmingArchive.value = true;
  try {
    await submitArchiveConfirmed({
      url: archiveSubmitDialog.value.url,
      previewToken: archiveSubmitDialog.value.previewToken,
      clearInput: true,
    });
  } catch (error) {
    archiveStatus.value = error instanceof Error ? error.message : "提交失败。";
    closeArchiveSubmitDialog();
  } finally {
    confirmingArchive.value = false;
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
        title: item.title,
        instance_id: item.instance_id,
      },
    });
    missingStatus.value = response.message || "已加入重试队列。";
    await load();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "重试失败。";
  } finally {
    pendingMissingActionKey.value = "";
  }
}

async function retryAllMissing() {
  try {
    const response = await apiRequest("/api/tasks/missing-3mf/retry-all", {
      method: "POST",
    });
    missingStatus.value = response.message || "已加入重试队列。";
    await load();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "重试失败。";
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
    await load();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "取消失败。";
  } finally {
    pendingMissingActionKey.value = "";
  }
}

async function clearOrganizeTasks() {
  clearingOrganizeTasks.value = true;
  try {
    const response = await apiRequest("/api/tasks/organize/clear", {
      method: "POST",
    });
    organizeStatus.value = response.message || "已清空本地整理任务记录。";
    await load();
  } catch (error) {
    organizeStatus.value = error instanceof Error ? error.message : "清空本地整理任务记录失败。";
  } finally {
    clearingOrganizeTasks.value = false;
  }
}

onMounted(load);

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>
