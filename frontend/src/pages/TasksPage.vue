<template>
  <section class="page-intro" data-tasks-page>
    <div>
      <span class="eyebrow">任务</span>
      <h1>归档队列、缺失 3MF 与本地整理任务</h1>
      <p>首页只显示摘要，完整任务状态统一收纳在这里。</p>
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
          <div v-if="payload.archive_queue.active.length">
            <div
              v-for="item in payload.archive_queue.active"
              :key="item.id || item.title"
              class="task-item"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ item.status }}</span>
              <div v-if="item.progress" class="progress-bar"><span :style="{ width: `${item.progress}%` }"></span></div>
              <p>{{ item.message || "正在执行中" }}</p>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有运行中的归档任务。</p>
        </div>
        <div class="task-column">
          <h3>排队中</h3>
          <div v-if="payload.archive_queue.queued.length">
            <div
              v-for="item in payload.archive_queue.queued"
              :key="item.id || item.title"
              class="task-item"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ item.status }}</span>
              <p>{{ item.message || "等待归档" }}</p>
            </div>
          </div>
          <p v-else class="empty-copy">当前没有排队中的任务。</p>
        </div>
        <div class="task-column">
          <h3>最近失败</h3>
          <div v-if="payload.archive_queue.recent_failures.length">
            <div
              v-for="item in payload.archive_queue.recent_failures"
              :key="item.id || item.title"
              class="task-item task-item--error"
            >
              <strong>{{ item.title || item.url || "未命名任务" }}</strong>
              <span>{{ item.status }}</span>
              <p>{{ item.message || "失败原因未记录" }}</p>
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
      <div v-if="payload.missing_3mf.items.length" class="table-like">
        <div class="table-like__row table-like__row--missing table-like__row--head">
          <span>模型 ID</span>
          <span>标题</span>
          <span>状态</span>
          <span>操作</span>
        </div>
        <div
          v-for="item in payload.missing_3mf.items"
          :key="`${item.model_id}-${item.instance_id}-${item.title}`"
          class="table-like__row table-like__row--missing"
        >
          <span>{{ item.model_id || "-" }}</span>
          <span>{{ item.title || "未命名模型" }}</span>
          <span>{{ item.status }}</span>
          <span>
            <button
              class="button button-secondary button-small"
              type="button"
              @click="retryMissing(item)"
            >
              重新下载
            </button>
          </span>
        </div>
      </div>
      <p v-else class="empty-copy">当前没有缺失 3MF 任务。</p>
    </article>

    <article class="surface section-card">
      <div class="section-card__header">
        <div>
          <span class="eyebrow">本地整理</span>
          <h2>本地整理任务</h2>
        </div>
        <span class="count-pill">{{ payload.organize_tasks.count }} 项</span>
      </div>
      <div v-if="payload.organize_tasks.items.length" class="table-like">
        <div class="table-like__row table-like__row--head">
          <span>源目录</span>
          <span>目标目录</span>
          <span>状态</span>
        </div>
        <div
          v-for="item in payload.organize_tasks.items"
          :key="`${item.source_dir}-${item.target_dir}`"
          class="table-like__row"
        >
          <span>{{ item.source_dir }}</span>
          <span>{{ item.target_dir }}</span>
          <span>{{ item.status }}</span>
        </div>
      </div>
      <p v-else class="empty-copy">当前没有本地整理任务。</p>
    </article>
  </section>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";

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
const missingStatus = ref("");
const submittingArchive = ref(false);
let refreshTimer = null;

function syncAutoRefresh() {
  const hasRunning = payload.value.summary.running_or_queued > 0;
  if (hasRunning && !refreshTimer) {
    refreshTimer = window.setInterval(load, 5000);
    return;
  }
  if (!hasRunning && refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

async function load() {
  payload.value = await apiRequest("/api/tasks");
  syncAutoRefresh();
}

async function submitArchive() {
  if (!archiveUrl.value) {
    archiveStatus.value = "请先输入归档链接。";
    return;
  }

  submittingArchive.value = true;
  archiveStatus.value = "";
  try {
    const response = await apiRequest("/api/archive", {
      method: "POST",
      body: { url: archiveUrl.value },
    });
    archiveStatus.value = response.message || "任务已提交。";
    archiveUrl.value = "";
    await load();
  } catch (error) {
    archiveStatus.value = error instanceof Error ? error.message : "提交失败。";
  } finally {
    submittingArchive.value = false;
  }
}

async function retryMissing(item) {
  try {
    const response = await apiRequest("/api/tasks/missing-3mf/retry", {
      method: "POST",
      body: {
        model_id: item.model_id,
        model_url: item.model_url,
        title: item.title,
      },
    });
    missingStatus.value = response.message || "已加入重试队列。";
    await load();
  } catch (error) {
    missingStatus.value = error instanceof Error ? error.message : "重试失败。";
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

onMounted(load);

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>
