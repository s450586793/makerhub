<template>
  <section class="page-intro page-intro--compact">
    <div>
      <span class="eyebrow">日志</span>
      <h1>业务日志</h1>
    </div>
    <div class="intro-stats">
      <div class="intro-stat">
        <span>日志文件</span>
        <strong>{{ files.length }}</strong>
      </div>
      <div class="intro-stat">
        <span>当前显示</span>
        <strong>{{ entries.length }}</strong>
      </div>
      <div class="intro-stat">
        <span>自动刷新</span>
        <strong>{{ autoRefresh ? "开" : "关" }}</strong>
      </div>
    </div>
  </section>

  <section class="surface section-card section-card--compact logs-toolbar-card">
    <div class="section-card__header section-card__header--compact">
      <div>
        <span class="eyebrow">查看</span>
        <h2>日志来源</h2>
      </div>
      <span class="form-status">{{ status }}</span>
    </div>

    <form class="logs-toolbar" @submit.prevent="load">
      <label class="filter-field">
        <select v-model="selectedFile" aria-label="日志文件">
          <option v-for="file in files" :key="file.name" :value="file.name">
            {{ file.name }}{{ file.primary ? "（业务）" : "" }}
          </option>
        </select>
      </label>
      <label class="filter-field filter-field--wide">
        <input v-model.trim="query" type="text" placeholder="搜索日志内容">
      </label>
      <label class="filter-field logs-limit-field">
        <select v-model.number="limit" aria-label="日志行数">
          <option :value="100">最近 100 条</option>
          <option :value="300">最近 300 条</option>
          <option :value="800">最近 800 条</option>
          <option :value="1500">最近 1500 条</option>
        </select>
      </label>
      <div class="filter-actions logs-toolbar__actions">
        <button class="button button-secondary" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中..." : "刷新" }}
        </button>
        <button
          :class="['button', autoRefresh ? 'button-primary' : 'button-secondary']"
          type="button"
          @click="toggleAutoRefresh"
        >
          {{ autoRefresh ? "停止自动刷新" : "自动刷新" }}
        </button>
      </div>
    </form>

    <div v-if="selectedFileMeta" class="logs-file-meta">
      <span>文件：{{ selectedFileMeta.name }}</span>
      <span>大小：{{ formatBytes(selectedFileMeta.size) }}</span>
      <span>更新：{{ selectedFileMeta.modified_at || "暂无" }}</span>
    </div>
  </section>

  <section class="surface section-card logs-panel">
    <div class="section-card__header">
      <div>
        <span class="eyebrow">内容</span>
        <h2>{{ selectedFile || "business.log" }}</h2>
      </div>
      <span class="count-pill">{{ entries.length }} 条</span>
    </div>

    <div v-if="entries.length" class="logs-list">
      <article
        v-for="(entry, index) in entries"
        :key="`${entry.time}-${entry.category}-${entry.event}-${index}`"
        :class="['log-entry', `log-entry--${normalizeLevel(entry.level)}`]"
      >
        <div class="log-entry__head">
          <span class="log-entry__time">{{ entry.time || "-" }}</span>
          <span class="log-entry__badge">{{ normalizeLevel(entry.level) }}</span>
          <span class="log-entry__category">{{ entry.category || "-" }}</span>
          <strong>{{ entry.event || "event" }}</strong>
        </div>
        <p v-if="entry.message" class="log-entry__message">{{ entry.message }}</p>
        <pre v-if="payloadText(entry)" class="log-entry__payload">{{ payloadText(entry) }}</pre>
        <pre v-else-if="entry.raw && entry.event === 'line'" class="log-entry__payload">{{ entry.raw }}</pre>
      </article>
    </div>
    <p v-else class="empty-copy">当前没有日志内容。</p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";

import { apiRequest } from "../lib/api";


const files = ref([]);
const entries = ref([]);
const selectedFile = ref("business.log");
const query = ref("");
const limit = ref(300);
const status = ref("");
const loading = ref(false);
const autoRefresh = ref(false);

let refreshTimer = null;
let searchTimer = null;

const selectedFileMeta = computed(() => files.value.find((item) => item.name === selectedFile.value) || null);

function normalizeLevel(level) {
  const value = String(level || "info").toLowerCase();
  if (["error", "warning", "warn", "debug"].includes(value)) {
    return value === "warn" ? "warning" : value;
  }
  return "info";
}

function payloadText(entry) {
  const payload = entry?.payload || {};
  const keys = Object.keys(payload).filter((key) => payload[key] !== "" && payload[key] !== null && payload[key] !== undefined);
  if (!keys.length) {
    return "";
  }
  return JSON.stringify(keys.reduce((result, key) => ({ ...result, [key]: payload[key] }), {}), null, 2);
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

async function load() {
  loading.value = true;
  status.value = "";
  const params = new URLSearchParams();
  params.set("file", selectedFile.value || "business.log");
  params.set("limit", String(limit.value || 300));
  if (query.value) {
    params.set("q", query.value);
  }
  try {
    const payload = await apiRequest(`/api/logs?${params.toString()}`);
    files.value = payload.files || [];
    entries.value = payload.entries || [];
    selectedFile.value = payload.file || selectedFile.value || "business.log";
    status.value = "日志已刷新。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "日志读取失败。";
  } finally {
    loading.value = false;
  }
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value;
  syncAutoRefresh();
}

function syncAutoRefresh() {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (autoRefresh.value) {
    refreshTimer = window.setInterval(load, 5000);
  }
}

watch([selectedFile, limit], () => {
  load();
});

watch(query, () => {
  if (searchTimer) {
    window.clearTimeout(searchTimer);
  }
  searchTimer = window.setTimeout(load, 350);
});

onMounted(() => {
  load();
});

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
  if (searchTimer) {
    window.clearTimeout(searchTimer);
  }
});
</script>
