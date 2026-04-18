<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">本地整理</span>
      <h1>本地整理配置</h1>
      <p>控制 `/app/local` 的扫描来源、归档目标和移动模式。</p>
    </div>
    <div class="intro-stats organizer-intro-stats">
      <div class="intro-stat">
        <span>候选 3MF</span>
        <strong>{{ organizerTasks.detected_total || 0 }}</strong>
      </div>
      <div class="intro-stat">
        <span>运行中</span>
        <strong>{{ organizerTasks.running_count || 0 }}</strong>
      </div>
      <div class="intro-stat">
        <span>排队中</span>
        <strong>{{ organizerTasks.queued_count || 0 }}</strong>
      </div>
      <div class="intro-stat">
        <span>记录数</span>
        <strong>{{ organizerTasks.items.length || 0 }}</strong>
      </div>
    </div>
  </section>

  <section class="surface organizer-layout">
    <form class="settings-form" @submit.prevent="saveOrganizer">
      <div class="settings-grid settings-grid--two">
        <label class="field-card">
          <span>本地整理扫描目录</span>
          <input v-model="organizerForm.source_dir" type="text" placeholder="/app/local">
        </label>
        <label class="field-card">
          <span>整理目标目录</span>
          <input v-model="organizerForm.target_dir" type="text" placeholder="/app/archive">
        </label>
      </div>
      <label class="field-card">
        <span>整理模式</span>
        <label class="switch">
          <input v-model="organizerForm.move_files" type="checkbox">
          <span>启用后移动文件，而不是复制文件</span>
        </label>
      </label>
      <div class="form-footer">
        <button class="button button-primary" type="submit">保存整理配置</button>
        <span class="form-status">{{ status }}</span>
      </div>
    </form>
  </section>

  <section class="surface section-card organizer-layout">
    <div class="section-card__header">
      <div>
        <span class="eyebrow">本地整理</span>
        <h2>本地整理任务</h2>
      </div>
      <div class="filter-actions">
        <button
          v-if="organizerTasks.items.length"
          class="button button-secondary button-small"
          type="button"
          :disabled="clearingOrganizeTasks"
          @click="clearOrganizeTasks"
        >
          {{ clearingOrganizeTasks ? "清空中..." : "清空记录" }}
        </button>
        <button
          class="button button-secondary button-small"
          type="button"
          :disabled="loadingTasks"
          @click="load"
        >
          刷新
        </button>
        <span class="count-pill">{{ organizerTasks.running_count || 0 }} 运行中 / {{ organizerTasks.queued_count || 0 }} 排队中</span>
      </div>
    </div>
    <span class="form-status">{{ organizeStatus }}</span>
    <p v-if="organizerTasks.detected_total" class="archive-form__hint">
      当前检测到 {{ organizerTasks.detected_total }} 个候选 3MF
      <template v-if="organizerTasks.detected_total > organizerTasks.items.length">
        ，当前仅展示前 {{ organizerTasks.items.length }} 条
      </template>
    </p>
    <div v-if="visibleOrganizeTasks.length" class="table-like">
      <div class="table-like__row table-like__row--head">
        <span>文件</span>
        <span>模型目录</span>
        <span>状态</span>
      </div>
      <div
        v-for="(item, index) in visibleOrganizeTasks"
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
    <div v-if="organizerTasks.items.length > organizeVisibleLimit" class="task-list-footer">
      <button class="button button-secondary button-small" type="button" @click="organizeVisibleLimit += TASKS_PAGE_SIZE">
        加载更多
      </button>
    </div>
    <p v-else class="empty-copy">当前没有本地整理任务。</p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";

import { appState, refreshConfig } from "../lib/appState";
import { apiRequest } from "../lib/api";


const config = computed(() => appState.config);
const TASKS_PAGE_SIZE = 5;
const status = ref("");
const organizeStatus = ref("");
const loadingTasks = ref(false);
const clearingOrganizeTasks = ref(false);
const organizeVisibleLimit = ref(TASKS_PAGE_SIZE);
const organizerTasks = ref({
  items: [],
  count: 0,
  queued_count: 0,
  running_count: 0,
  detected_total: 0,
});
const organizerForm = reactive({
  source_dir: "",
  target_dir: "",
  move_files: true,
});
let refreshTimer = null;

const visibleOrganizeTasks = computed(() => organizerTasks.value.items.slice(0, organizeVisibleLimit.value));

function applyConfig(payload) {
  organizerForm.source_dir = payload?.organizer?.source_dir || "";
  organizerForm.target_dir = payload?.organizer?.target_dir || "";
  organizerForm.move_files = payload?.organizer?.move_files !== false;
}

function syncTaskTimer() {
  if (refreshTimer) {
    return;
  }
  refreshTimer = window.setInterval(loadTasks, 5000);
}

async function loadTasks() {
  if (loadingTasks.value) {
    return;
  }
  loadingTasks.value = true;
  try {
    const payload = await apiRequest("/api/tasks");
    organizerTasks.value = payload?.organize_tasks || organizerTasks.value;
    syncTaskTimer();
  } finally {
    loadingTasks.value = false;
  }
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfig(payload);
  await loadTasks();
}

async function saveOrganizer() {
  try {
    await apiRequest("/api/config/organizer", {
      method: "POST",
      body: { ...organizerForm },
    });
    await refreshConfig();
    status.value = "整理配置已保存。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  }
}

async function clearOrganizeTasks() {
  clearingOrganizeTasks.value = true;
  try {
    const response = await apiRequest("/api/tasks/organize/clear", {
      method: "POST",
    });
    organizeStatus.value = response.message || "已清空本地整理任务记录。";
    organizeVisibleLimit.value = TASKS_PAGE_SIZE;
    await loadTasks();
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
