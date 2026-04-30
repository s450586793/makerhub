<template>
  <section class="logs-workbench">
    <section class="surface logs-hero">
      <div class="logs-hero__top">
        <div class="logs-hero__title">
          <span class="logs-hero__icon">LOG</span>
          <div>
            <span class="eyebrow">日志中心</span>
            <h1>服务日志</h1>
            <p>统一查看归档、订阅、本地整理、缺失 3MF 和代理测试等业务输出。</p>
          </div>
        </div>

        <div class="logs-hero__actions">
          <button class="button button-secondary" type="button" :disabled="loading" @click="load">
            {{ loading ? "刷新中..." : "立即刷新" }}
          </button>
          <button
            :class="['button', autoRefresh ? 'button-primary' : 'button-secondary']"
            type="button"
            @click="toggleAutoRefresh"
          >
            {{ autoRefresh ? "关闭自动追踪" : "开启自动追踪" }}
          </button>
        </div>
      </div>

      <div class="logs-hero__chips">
        <span class="count-pill logs-hero__chip">总计 {{ entries.length }} 行</span>
        <span class="count-pill logs-hero__chip">当前已加载 {{ filteredEntries.length }} 行</span>
        <span :class="['count-pill', 'logs-hero__chip', autoRefresh ? 'count-pill--ok' : 'count-pill--warn']">
          {{ autoRefresh ? "自动追踪已开启" : "自动追踪已关闭" }}
        </span>
        <span class="count-pill logs-hero__chip">最近时间 {{ latestTimeText }}</span>
      </div>

      <p class="logs-hero__status">{{ status || "选择日志文件后即可查看最新业务输出。" }}</p>
    </section>

    <section class="logs-layout">
      <aside class="surface logs-sidebar">
        <section class="logs-sidebar__section">
          <div class="logs-sidebar__head">
            <span class="eyebrow">来源</span>
            <strong>日志文件</strong>
          </div>
          <label class="filter-field">
            <select v-model="selectedFile" aria-label="日志文件">
              <option v-for="file in fileOptions" :key="file.name" :value="file.name">
                {{ file.name }}{{ file.primary ? "（业务）" : "" }}
              </option>
            </select>
          </label>
          <div v-if="selectedFileMeta" class="logs-sidebar__meta">
            <span>
              <strong>大小</strong>
              <em>{{ formatBytes(selectedFileMeta.size) }}</em>
            </span>
            <span>
              <strong>更新</strong>
              <em>{{ selectedFileMeta.modified_at || "暂无" }}</em>
            </span>
          </div>
        </section>

        <section class="logs-sidebar__section">
          <div class="logs-sidebar__head">
            <span class="eyebrow">过滤</span>
            <strong>搜索与范围</strong>
          </div>
          <label class="filter-field">
            <input
              v-model.trim="query"
              type="text"
              placeholder="搜索事件、消息、JSON 字段"
            >
          </label>
          <label class="filter-field">
            <select v-model.number="limit" aria-label="日志行数">
              <option :value="120">最近 120 条</option>
              <option :value="300">最近 300 条</option>
              <option :value="800">最近 800 条</option>
              <option :value="1500">最近 1500 条</option>
            </select>
          </label>
          <div class="logs-sidebar__controls">
            <button
              :class="['button', activeLevel === 'all' ? 'button-primary' : 'button-secondary']"
              type="button"
              @click="activeLevel = 'all'"
            >
              全部显示
            </button>
            <button class="button button-secondary" type="button" @click="clearFilters">
              重置筛选
            </button>
          </div>
          <div class="logs-preset-list">
            <button
              v-for="preset in logPresets"
              :key="preset.key"
              class="button button-secondary button-small"
              type="button"
              @click="applyLogPreset(preset)"
            >
              {{ preset.label }}
            </button>
          </div>
        </section>

        <section class="logs-sidebar__section">
          <div class="logs-sidebar__head">
            <span class="eyebrow">等级</span>
            <strong>日志级别</strong>
          </div>

          <div class="logs-levels">
            <button
              v-for="level in levelCards"
              :key="level.key"
              :class="['logs-level-card', `logs-level-card--${level.key}`, activeLevel === level.key && 'is-active']"
              type="button"
              @click="activeLevel = level.key"
            >
              <div class="logs-level-card__head">
                <span class="logs-level-card__label">{{ level.label }}</span>
                <strong>{{ level.count }}</strong>
              </div>
              <p>{{ level.description }}</p>
            </button>
          </div>
        </section>
      </aside>

      <section class="surface logs-stream">
        <div class="logs-stream__head">
          <div>
            <span class="eyebrow">日志流</span>
            <h2>{{ streamTitle }}</h2>
          </div>
          <span class="count-pill">{{ filteredEntries.length }} 条</span>
        </div>

        <div class="logs-stream__summary">
          <span>{{ selectedLevelText }}</span>
          <span>{{ query ? `关键词：${query}` : "未设置关键词" }}</span>
          <span>{{ selectedFileMeta?.exists === false ? "文件不存在" : "文件正常" }}</span>
        </div>

        <div v-if="filteredEntries.length" class="logs-stream__list">
          <article
            v-for="entry in filteredEntries"
            :key="entry._id"
            :class="['logs-stream-entry', `logs-stream-entry--${entry._level}`]"
          >
            <div class="logs-stream-entry__head">
              <span :class="['logs-stream-entry__badge', `is-${entry._level}`]">{{ entry._badge }}</span>
              <span class="logs-stream-entry__time">{{ entry.time || "-" }}</span>
              <span class="logs-stream-entry__category">{{ entry.category || "-" }}</span>
              <strong class="logs-stream-entry__event">{{ entry.event || "event" }}</strong>
            </div>
            <p v-if="entry.message" class="logs-stream-entry__message">{{ entry.message }}</p>
            <pre v-if="entry._payloadText" class="logs-stream-entry__payload">{{ entry._payloadText }}</pre>
            <pre v-else-if="entry.raw && entry.event === 'line'" class="logs-stream-entry__payload">{{ entry.raw }}</pre>
          </article>
        </div>

        <p v-else class="empty-copy logs-stream__empty">当前筛选下没有日志内容。</p>
      </section>
    </section>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "../lib/api";


const LEVEL_DEFINITIONS = [
  { key: "debug", label: "DEBUG", description: "调试信息与上下文" },
  { key: "info", label: "INFO", description: "常规业务状态与结果" },
  { key: "warning", label: "WARN", description: "预警、重试与回退" },
  { key: "error", label: "ERROR", description: "失败、异常与中断" },
  { key: "success", label: "SUCCESS", description: "明确成功完成的步骤" },
];
const LOG_PRESETS = [
  { key: "organizer", label: "本地整理历史", file: "business.log", q: "organizer" },
  { key: "subscription", label: "订阅同步", file: "business.log", q: "subscription" },
  { key: "archive", label: "归档任务", file: "business.log", q: "archive" },
  { key: "missing_3mf", label: "缺失 3MF", file: "business.log", q: "missing_3mf" },
];

const route = useRoute();
const router = useRouter();
const files = ref([]);
const entries = ref([]);
const selectedFile = ref("business.log");
const query = ref("");
const limit = ref(300);
const activeLevel = ref("all");
const status = ref("");
const loading = ref(false);
const autoRefresh = ref(false);

let refreshTimer = null;
let searchTimer = null;
let applyingRouteQuery = false;

const logPresets = LOG_PRESETS;

const fileOptions = computed(() => (
  files.value.length
    ? files.value
    : [{ name: selectedFile.value || "business.log", size: 0, modified_at: "", exists: false, primary: true }]
));

const selectedFileMeta = computed(() => fileOptions.value.find((item) => item.name === selectedFile.value) || null);

const normalizedEntries = computed(() => entries.value.map((entry, index) => {
  const level = normalizeLevel(entry.level);
  return {
    ...entry,
    _id: `${entry.time || "line"}-${entry.category || "system"}-${entry.event || "event"}-${index}`,
    _level: level,
    _badge: badgeText(level),
    _payloadText: payloadText(entry),
  };
}));

const levelCounts = computed(() => normalizedEntries.value.reduce((result, entry) => {
  result[entry._level] = (result[entry._level] || 0) + 1;
  return result;
}, { debug: 0, info: 0, warning: 0, error: 0, success: 0 }));

const levelCards = computed(() => LEVEL_DEFINITIONS.map((level) => ({
  ...level,
  count: levelCounts.value[level.key] || 0,
})));

const filteredEntries = computed(() => (
  activeLevel.value === "all"
    ? normalizedEntries.value
    : normalizedEntries.value.filter((entry) => entry._level === activeLevel.value)
));

const selectedLevelText = computed(() => {
  if (activeLevel.value === "all") {
    return "当前显示：全部等级";
  }
  return `当前显示：${badgeText(activeLevel.value)}`;
});

const streamTitle = computed(() => (
  activeLevel.value === "all"
    ? (selectedFile.value || "business.log")
    : `${selectedFile.value || "business.log"} · ${badgeText(activeLevel.value)}`
));

const latestTimeText = computed(() => {
  const firstEntry = normalizedEntries.value.find((entry) => entry.time);
  return firstEntry?.time || selectedFileMeta.value?.modified_at || "暂无";
});

function normalizeLevel(level) {
  const value = String(level || "info").toLowerCase();
  if (value.includes("debug")) return "debug";
  if (value.includes("warn")) return "warning";
  if (value.includes("error") || value.includes("fatal")) return "error";
  if (value.includes("success") || value.includes("ok")) return "success";
  return "info";
}

function badgeText(level) {
  if (level === "warning") return "WARN";
  return String(level || "info").toUpperCase();
}

function payloadText(entry) {
  const payload = entry?.payload || {};
  const keys = Object.keys(payload).filter((key) => payload[key] !== "" && payload[key] !== null && payload[key] !== undefined);
  if (!keys.length) {
    return "";
  }
  const sanitized = keys.reduce((result, key) => {
    result[key] = payload[key];
    return result;
  }, {});
  return JSON.stringify(sanitized, null, 2);
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
    status.value = `已加载 ${payload.count || 0} 条日志。`;
  } catch (error) {
    status.value = error instanceof Error ? error.message : "日志读取失败。";
  } finally {
    loading.value = false;
  }
}

function clearFilters() {
  activeLevel.value = "all";
  query.value = "";
  limit.value = 300;
  router.replace({ path: "/logs" });
  load();
}

function applyLogPreset(preset) {
  router.replace({
    path: "/logs",
    query: {
      file: preset.file,
      q: preset.q,
    },
  });
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
  if (applyingRouteQuery) {
    return;
  }
  load();
});

watch(query, () => {
  if (applyingRouteQuery) {
    return;
  }
  if (searchTimer) {
    window.clearTimeout(searchTimer);
  }
  searchTimer = window.setTimeout(load, 320);
});

watch(() => route.fullPath, () => {
  applyRouteQuery();
});

function applyRouteQuery() {
  applyingRouteQuery = true;
  const routeFile = typeof route.query.file === "string" ? route.query.file : "";
  const routeQuery = typeof route.query.q === "string" ? route.query.q : "";
  const routeLevel = typeof route.query.level === "string" ? route.query.level : "";
  const routeLimit = Number(route.query.limit || 0);
  selectedFile.value = routeFile || "business.log";
  query.value = routeQuery;
  activeLevel.value = routeLevel || "all";
  if (Number.isFinite(routeLimit) && routeLimit > 0) {
    limit.value = Math.min(Math.max(Math.trunc(routeLimit), 1), 2000);
  }
  applyingRouteQuery = false;
  load();
}

onMounted(() => {
  applyRouteQuery();
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
