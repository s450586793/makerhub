<template>
  <section class="logs-workbench">
    <section class="surface surface--filters logs-toolbar app-page-toolbar">
      <div class="app-page-toolbar__copy">
        <span class="eyebrow">日志中心</span>
        <div class="app-page-toolbar__title-row">
          <h1>排障日志</h1>
          <span :class="['count-pill', autoRefresh ? 'count-pill--ok' : 'count-pill--warn']">
            {{ autoRefresh ? "自动追踪" : "手动刷新" }}
          </span>
        </div>
        <p class="logs-toolbar__status">{{ status || "按模块、级别、事件和关键词快速定位问题。" }}</p>
      </div>

      <div class="logs-toolbar__actions">
        <button class="button button-secondary" type="button" :disabled="loading" @click="reload">
          {{ loading ? "刷新中..." : "刷新" }}
        </button>
        <button
          :class="['button', autoRefresh ? 'button-primary' : 'button-secondary']"
          type="button"
          @click="toggleAutoRefresh"
        >
          {{ autoRefresh ? "停止追踪" : "实时追踪" }}
        </button>
      </div>
    </section>

    <section class="surface logs-console">
      <div class="logs-console__filters">
        <label class="filter-field filter-field--wide">
          <input
            v-model.trim="filters.q"
            type="text"
            placeholder="搜索消息、事件、payload"
            aria-label="搜索日志"
            @keydown.enter.prevent="applyFilters"
          >
        </label>
        <label class="filter-field">
          <select v-model="filters.level" aria-label="日志级别" @change="applyFilters">
            <option value="">全部级别</option>
            <option v-for="level in levelOptions" :key="level.value" :value="level.value">
              {{ level.label }}{{ level.count ? ` (${level.count})` : "" }}
            </option>
          </select>
        </label>
        <label class="filter-field">
          <select v-model="filters.category" aria-label="日志模块" @change="applyFilters">
            <option value="">全部模块</option>
            <option v-for="category in categoryOptions" :key="category.value" :value="category.value">
              {{ category.value }}{{ category.count ? ` (${category.count})` : "" }}
            </option>
          </select>
        </label>
        <label class="filter-field">
          <select v-model="filters.since" aria-label="时间范围" @change="applyFilters">
            <option :value="ALL_TIME_VALUE">不限时间</option>
            <option v-for="option in sinceOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <div class="filter-actions logs-console__filter-actions">
          <button class="button button-primary" type="button" :disabled="loading" @click="applyFilters">查询</button>
          <button class="button button-secondary" type="button" @click="resetFilters">重置</button>
        </div>
      </div>

      <div class="logs-quickbar" aria-label="日志快捷筛选">
        <button
          v-for="preset in logPresets"
          :key="preset.key"
          :class="['button button-small', activePresetKey === preset.key ? 'button-primary' : 'button-secondary']"
          type="button"
          @click="applyPreset(preset)"
        >
          {{ preset.label }}
        </button>
      </div>

      <div class="logs-console__summary">
        <span class="count-pill">{{ entries.length }} 条</span>
        <span v-if="payload.has_more" class="count-pill count-pill--warn">还有更多</span>
        <span v-if="latestTimeText" class="count-pill">最新 {{ latestTimeText }}</span>
        <span v-if="selectedFileMeta?.count" class="count-pill">库内 {{ selectedFileMeta.count }} 条</span>
        <span v-if="payload.source === 'database_unavailable'" class="count-pill count-pill--danger">数据库不可用</span>
      </div>

      <div class="logs-table-wrap">
        <table class="logs-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>级别</th>
              <th>模块</th>
              <th>事件</th>
              <th>消息</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody v-if="entries.length">
            <tr
              v-for="entry in normalizedEntries"
              :key="entry._key"
              :class="['logs-table__row', `logs-table__row--${entry._level}`]"
              @click="selectEntry(entry)"
            >
              <td class="logs-table__time">{{ compactTime(entry.time || entry.created_at) }}</td>
              <td>
                <span :class="['logs-level-badge', `is-${entry._level}`]">{{ entry._badge }}</span>
              </td>
              <td class="logs-table__category">{{ entry.category || "-" }}</td>
              <td class="logs-table__event">{{ entry.event || "event" }}</td>
              <td class="logs-table__message">
                <strong v-if="entry.message">{{ entry.message }}</strong>
                <span v-else>{{ entry.raw || "-" }}</span>
              </td>
              <td>
                <button class="button button-secondary button-small" type="button" @click.stop="selectEntry(entry)">
                  查看
                </button>
              </td>
            </tr>
          </tbody>
          <tbody v-else>
            <tr>
              <td colspan="6" class="logs-table__empty">
                {{ loaded ? "当前条件下没有日志。" : "正在读取日志。" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="entries.length" ref="loadMoreTrigger" class="list-loader-anchor">
        <span v-if="loadingMore">正在加载更多日志...</span>
        <span v-else-if="payload.has_more">下拉到底自动加载下一页</span>
        <span v-else>已经到底了</span>
      </div>
    </section>

    <div
      v-if="selectedEntry"
      class="logs-detail-backdrop"
      role="presentation"
      @click="closeDetail"
    >
      <aside
        class="logs-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="logs-detail-title"
        @click.stop
      >
        <header class="logs-detail-drawer__head">
          <div>
            <span :class="['logs-level-badge', `is-${selectedEntry._level}`]">{{ selectedEntry._badge }}</span>
            <h2 id="logs-detail-title">{{ selectedEntry.event || "event" }}</h2>
            <p>{{ selectedEntry.category || "-" }} · {{ selectedEntry.time || selectedEntry.created_at || "-" }}</p>
          </div>
          <button class="button button-secondary button-small" type="button" @click="closeDetail">关闭</button>
        </header>

        <section class="logs-detail-drawer__section">
          <span class="eyebrow">消息</span>
          <p>{{ selectedEntry.message || selectedEntry.raw || "无消息内容。" }}</p>
        </section>

        <section v-if="selectedEntry._payloadText" class="logs-detail-drawer__section">
          <span class="eyebrow">Payload</span>
          <pre>{{ selectedEntry._payloadText }}</pre>
        </section>

        <section v-if="selectedEntry.raw" class="logs-detail-drawer__section">
          <span class="eyebrow">Raw</span>
          <pre>{{ selectedEntry.raw }}</pre>
        </section>
      </aside>
    </div>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { apiRequest } from "../lib/api";
import { createAutoLoadObserver } from "../lib/autoLoadObserver";
import { createPagePerformanceTracker } from "../lib/performance";
import { createPageRefreshController } from "../lib/usePageRefresh";


const DEFAULT_FILE = "business.log";
const DEFAULT_LIMIT = 160;
const DEFAULT_SINCE = "6h";
const ALL_TIME_VALUE = "all";
const LEVEL_LABELS = {
  debug: "DEBUG",
  info: "INFO",
  warning: "WARN",
  error: "ERROR",
  success: "SUCCESS",
};
const LOG_PRESETS = [
  { key: "errors", label: "近 6 小时错误", level: "error", since: DEFAULT_SINCE },
  { key: "archive", label: "近 6 小时归档", category: "archive", since: DEFAULT_SINCE },
  { key: "subscription", label: "近 6 小时订阅", category: "subscription", since: DEFAULT_SINCE },
  { key: "account", label: "近 6 小时账号/Cookie", q: "cookie", since: DEFAULT_SINCE },
  { key: "organizer", label: "近 6 小时本地导入", category: "organizer", since: DEFAULT_SINCE },
  { key: "update", label: "近 6 小时系统更新", category: "self_update", since: DEFAULT_SINCE },
  { key: "history_errors", label: "历史错误", level: "error", since: ALL_TIME_VALUE },
];
const SINCE_PRESETS = [
  { value: "15m", label: "最近 15 分钟", ms: 15 * 60 * 1000 },
  { value: "1h", label: "最近 1 小时", ms: 60 * 60 * 1000 },
  { value: "6h", label: "最近 6 小时", ms: 6 * 60 * 60 * 1000 },
  { value: "24h", label: "最近 24 小时", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "最近 7 天", ms: 7 * 24 * 60 * 60 * 1000 },
];

const route = useRoute();
const router = useRouter();
const payload = ref({
  entries: [],
  files: [],
  facets: { levels: [], categories: [], events: [] },
  has_more: false,
  next_cursor: "",
  source: "database",
});
const filters = reactive({
  file: DEFAULT_FILE,
  q: "",
  level: "",
  category: "",
  event: "",
  since: DEFAULT_SINCE,
  limit: DEFAULT_LIMIT,
});
const selectedEntry = ref(null);
const status = ref("");
const loaded = ref(false);
const loading = ref(false);
const loadingMore = ref(false);
const autoRefresh = ref(false);
const loadMoreTrigger = ref(null);

let logRefreshController = null;
let searchTimer = null;
let applyingRouteQuery = false;
let requestToken = 0;
let loadMoreToken = 0;

const logPresets = LOG_PRESETS;
const sinceOptions = SINCE_PRESETS;
const entries = computed(() => payload.value.entries || []);
const fileOptions = computed(() => (
  payload.value.files?.length
    ? payload.value.files
    : [{ name: filters.file || DEFAULT_FILE, count: 0, modified_at: "", exists: false, primary: true }]
));
const selectedFileMeta = computed(() => fileOptions.value.find((item) => item.name === filters.file) || null);
const facets = computed(() => payload.value.facets || { levels: [], categories: [], events: [] });
const levelOptions = computed(() => {
  const known = new Map((facets.value.levels || []).map((item) => [String(item.value || ""), item]));
  return Object.keys(LEVEL_LABELS).map((level) => ({
    value: level,
    label: LEVEL_LABELS[level] || level.toUpperCase(),
    count: Number(known.get(level)?.count || 0),
  }));
});
const categoryOptions = computed(() => facets.value.categories || []);
const activePresetKey = computed(() => {
  const normalized = normalizeFiltersForCompare(filters);
  const preset = LOG_PRESETS.find((item) => {
    const candidate = normalizeFiltersForCompare({ ...emptyPresetFilters(), ...item });
    return ["q", "level", "category", "event", "since"].every((key) => normalized[key] === candidate[key]);
  });
  return preset?.key || "";
});
const normalizedEntries = computed(() => entries.value.map((entry, index) => {
  const level = normalizeLevel(entry.level);
  return {
    ...entry,
    _key: `${entry.id || index}-${entry.time || entry.created_at || ""}-${entry.event || ""}`,
    _level: level,
    _badge: LEVEL_LABELS[level] || level.toUpperCase(),
    _payloadText: payloadText(entry),
  };
}));
const latestTimeText = computed(() => {
  const entry = normalizedEntries.value.find((item) => item.time || item.created_at);
  return entry ? compactTime(entry.time || entry.created_at) : "";
});

const loadMoreObserver = createAutoLoadObserver({
  triggerRef: loadMoreTrigger,
  canLoad: () => Boolean(payload.value.has_more),
  isLoading: () => Boolean(loadingMore.value || loading.value),
  load: loadMore,
  nextTick,
});

function emptyPresetFilters() {
  return { q: "", level: "", category: "", event: "", since: DEFAULT_SINCE };
}

function normalizeFiltersForCompare(source) {
  return {
    q: String(source.q || "").trim(),
    level: String(source.level || "").trim(),
    category: String(source.category || "").trim(),
    event: String(source.event || "").trim(),
    since: String(source.since || DEFAULT_SINCE).trim(),
  };
}

function normalizeLevel(level) {
  const value = String(level || "info").toLowerCase();
  if (value.includes("debug")) return "debug";
  if (value.includes("warn")) return "warning";
  if (value.includes("error") || value.includes("fatal")) return "error";
  if (value.includes("success") || value.includes("ok")) return "success";
  return "info";
}

function payloadText(entry) {
  const payloadValue = entry?.payload || {};
  const keys = Object.keys(payloadValue).filter((key) => payloadValue[key] !== "" && payloadValue[key] !== null && payloadValue[key] !== undefined);
  if (!keys.length) {
    return "";
  }
  return JSON.stringify(keys.reduce((result, key) => {
    result[key] = payloadValue[key];
    return result;
  }, {}), null, 2);
}

function compactTime(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  return text
    .replace("T", " ")
    .replace(/\.\d+/, "")
    .replace(/\+08:00$/, "")
    .replace(/Z$/, "");
}

function resolveSinceValue(value) {
  if (value === ALL_TIME_VALUE) {
    return "";
  }
  const preset = SINCE_PRESETS.find((item) => item.value === value);
  if (!preset) {
    return String(value || "").trim();
  }
  return new Date(Date.now() - preset.ms).toISOString();
}

function syncFiltersFromRoute() {
  applyingRouteQuery = true;
  filters.file = typeof route.query.file === "string" ? route.query.file : DEFAULT_FILE;
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
  filters.level = typeof route.query.level === "string" ? route.query.level : "";
  filters.category = typeof route.query.category === "string" ? route.query.category : "";
  filters.event = typeof route.query.event === "string" ? route.query.event : "";
  const routeSince = typeof route.query.since === "string" ? route.query.since : DEFAULT_SINCE;
  filters.since = routeSince || DEFAULT_SINCE;
  const routeLimit = Number(route.query.limit || DEFAULT_LIMIT);
  filters.limit = Number.isFinite(routeLimit) ? Math.min(Math.max(Math.trunc(routeLimit), 1), 2000) : DEFAULT_LIMIT;
  applyingRouteQuery = false;
}

function buildQuery(cursor = "", includeFacets = true) {
  const query = new URLSearchParams();
  query.set("file", filters.file || DEFAULT_FILE);
  query.set("limit", String(filters.limit || DEFAULT_LIMIT));
  if (filters.q) query.set("q", filters.q);
  if (filters.level) query.set("level", filters.level);
  if (filters.category) query.set("category", filters.category);
  if (filters.event) query.set("event", filters.event);
  if (filters.since && filters.since !== ALL_TIME_VALUE) query.set("since", resolveSinceValue(filters.since));
  if (cursor) query.set("cursor", cursor);
  if (!includeFacets) {
    query.set("include_facets", "false");
    query.set("include_files", "false");
  }
  return query;
}

function buildRouteQuery() {
  const query = {};
  if (filters.file && filters.file !== DEFAULT_FILE) query.file = filters.file;
  if (filters.q) query.q = filters.q;
  if (filters.level) query.level = filters.level;
  if (filters.category) query.category = filters.category;
  if (filters.event) query.event = filters.event;
  if (filters.since && filters.since !== DEFAULT_SINCE) query.since = filters.since;
  if (Number(filters.limit || DEFAULT_LIMIT) !== DEFAULT_LIMIT) query.limit = String(filters.limit);
  return query;
}

async function load({ append = false, cursor = "", includeFacets = !append } = {}) {
  const token = append ? ++loadMoreToken : ++requestToken;
  const routeAtLoad = route.fullPath;
  if (!append) {
    disconnectObserver();
  }
  if (append) {
    loadingMore.value = true;
  } else {
    loading.value = true;
    status.value = "";
  }

  try {
    const nextPayload = await apiRequest(`/api/logs?${buildQuery(cursor, includeFacets).toString()}`);
    if (
      route.fullPath !== routeAtLoad
      || (append && token !== loadMoreToken)
      || (!append && token !== requestToken)
    ) {
      return;
    }
    payload.value = {
      ...nextPayload,
      entries: append
        ? [...entries.value, ...(nextPayload.entries || [])]
        : (nextPayload.entries || []),
      files: nextPayload.files || payload.value.files || [],
      facets: nextPayload.facets || payload.value.facets || { levels: [], categories: [], events: [] },
    };
    filters.file = nextPayload.file || filters.file || DEFAULT_FILE;
    loaded.value = true;
    status.value = append
      ? `已追加 ${nextPayload.count || 0} 条日志。`
      : `已加载 ${nextPayload.count || 0} 条日志。`;
    await nextTick();
    ensureObserver();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "日志读取失败。";
  } finally {
    if (append) {
      loadingMore.value = false;
    } else {
      loading.value = false;
    }
  }
}

function applyFilters() {
  const nextQuery = buildRouteQuery();
  if (JSON.stringify(route.query || {}) === JSON.stringify(nextQuery)) {
    load();
    return;
  }
  router.replace({ path: "/logs", query: nextQuery });
}

function resetFilters() {
  filters.file = DEFAULT_FILE;
  filters.q = "";
  filters.level = "";
  filters.category = "";
  filters.event = "";
  filters.since = DEFAULT_SINCE;
  filters.limit = DEFAULT_LIMIT;
  selectedEntry.value = null;
  if (route.fullPath === "/logs") {
    load();
    return;
  }
  router.replace({ path: "/logs" });
}

function applyPreset(preset) {
  filters.q = preset.q || "";
  filters.level = preset.level || "";
  filters.category = preset.category || "";
  filters.event = preset.event || "";
  filters.since = preset.since || DEFAULT_SINCE;
  applyFilters();
}

function reload() {
  selectedEntry.value = null;
  load();
}

async function loadMore() {
  if (!payload.value.has_more || loadingMore.value || loading.value) {
    return;
  }
  disconnectObserver();
  await load({ append: true, cursor: payload.value.next_cursor || "" });
}

function disconnectObserver() {
  loadMoreObserver.disconnect();
}

function ensureObserver() {
  loadMoreObserver.ensure();
}

function selectEntry(entry) {
  selectedEntry.value = entry;
}

function closeDetail() {
  selectedEntry.value = null;
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value;
  syncAutoRefresh();
}

async function loadAndScheduleNextAutoRefresh() {
  await load({ includeFacets: false });
  if (autoRefresh.value && logRefreshController) {
    logRefreshController.schedule("auto-refresh-next");
  }
}

function stopLogRefreshController() {
  if (logRefreshController) {
    logRefreshController.dispose();
    logRefreshController = null;
  }
}

function syncAutoRefresh() {
  stopLogRefreshController();
  if (!autoRefresh.value) {
    return;
  }
  logRefreshController = createPageRefreshController({
    refresh: loadAndScheduleNextAutoRefresh,
    delayMs: 5000,
    scopes: ["business_logs"],
  });
  logRefreshController.schedule("auto-refresh-started");
}

watch(() => route.fullPath, () => {
  syncFiltersFromRoute();
  load();
});

watch(() => filters.q, () => {
  if (applyingRouteQuery) {
    return;
  }
  if (searchTimer) {
    window.clearTimeout(searchTimer);
  }
  searchTimer = window.setTimeout(applyFilters, 360);
});

onMounted(async () => {
  const perf = createPagePerformanceTracker({ page: "logs", route: () => route.fullPath });
  syncFiltersFromRoute();
  await load();
  void perf.finish();
  ensureObserver();
});

onBeforeUnmount(() => {
  stopLogRefreshController();
  disconnectObserver();
  if (searchTimer) {
    window.clearTimeout(searchTimer);
  }
});
</script>
