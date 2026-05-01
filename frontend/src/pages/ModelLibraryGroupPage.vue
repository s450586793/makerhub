<template>
  <section v-if="view" class="surface library-group-hero">
    <div class="library-group-hero__head">
      <button class="button button-secondary" type="button" @click="goBack">{{ groupBackLabel }}</button>
      <form class="filter-bar library-group-filter" @submit.prevent="applyFilters">
        <label class="filter-field filter-field--wide">
          <input
            v-model.trim="filters.q"
            type="text"
            aria-label="搜索模型"
            placeholder="标题、作者、标签"
            @blur="applyFiltersIfChanged"
            @keydown.enter.prevent="applyFilters"
          >
        </label>
        <label class="filter-field">
          <select v-model="filters.source" aria-label="来源筛选" @change="applyFiltersIfChanged">
            <option value="all">全部 ({{ payload.source_counts.all || 0 }})</option>
            <option value="cn">国内 ({{ payload.source_counts.cn || 0 }})</option>
            <option value="global">国际 ({{ payload.source_counts.global || 0 }})</option>
            <option value="local">本地 ({{ payload.source_counts.local || 0 }})</option>
          </select>
        </label>
        <label class="filter-field">
          <select v-model="filters.tag" aria-label="标签筛选" @change="applyFiltersIfChanged">
            <option value="">全部</option>
            <option value="__favorite__">收藏</option>
            <option value="__printed__">已打印</option>
            <option value="__source_deleted__">源端删除</option>
            <option value="__local_deleted__">本地删除</option>
            <option v-for="tag in payload.tags" :key="tag" :value="tag">{{ tag }}</option>
          </select>
        </label>
        <label class="filter-field">
          <select v-model="filters.sort" aria-label="排序方式" @change="applyFiltersIfChanged">
            <option value="collectDate">采集时间倒序</option>
            <option value="publishDate">发布时间倒序</option>
            <option value="downloads">下载量</option>
            <option value="likes">点赞量</option>
            <option value="prints">打印量</option>
          </select>
        </label>
        <div class="filter-actions">
          <button class="button button-secondary" type="button" @click="resetFilters">重置</button>
        </div>
      </form>
    </div>
    <span v-if="status" class="form-status model-toolbar-inline__status library-group-hero__status">{{ status }}</span>
    <div class="library-group-hero__body">
      <div class="library-group-hero__copy">
        <h1>{{ view.title }}</h1>
        <p v-if="view.subtitle">{{ view.subtitle }}</p>
      </div>
      <div v-if="view.stats?.length" class="library-group-hero__stats">
        <span v-for="stat in view.stats" :key="stat.label">
          <strong>{{ formatCompact(stat.value) }}</strong>
          <em>{{ stat.label }}</em>
        </span>
      </div>
    </div>
  </section>

  <section v-if="payload.items.length" class="model-grid">
    <ModelCard
      v-for="model in payload.items"
      :key="model.model_dir"
      :data-model-dir="model.model_dir"
      :model="model"
      :return-to="buildModelReturnTo(model.model_dir)"
      :return-context="activeNavContext"
      return-label="返回"
      @favorite="toggleFavorite"
      @printed="togglePrinted"
      @delete="deleteOne"
      @restore="restoreOne"
    />
  </section>

  <div v-if="payload.items.length" ref="loadMoreTrigger" class="list-loader-anchor">
    <span v-if="loadingMore">正在加载更多模型...</span>
    <span v-else-if="payload.has_more">下拉到底自动加载下一页</span>
    <span v-else>已经到底了</span>
  </div>

  <section v-else-if="loaded" class="surface empty-state">
    <h2>这个分组里还没有模型</h2>
    <p>当前筛选条件下没有命中结果。你可以重置条件，或者返回模型库首页查看其它来源卡片。</p>
  </section>

  <section v-else class="surface empty-state">
    <h2>正在加载模型列表</h2>
    <p>稍等，正在读取当前来源下的模型。</p>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ModelCard from "../components/ModelCard.vue";
import { apiRequest } from "../lib/api";
import { subscribeArchiveCompletion } from "../lib/archiveEvents";


const route = useRoute();
const router = useRouter();
const PAGE_SIZE = 8;

const payload = ref({
  items: [],
  count: 0,
  filtered_total: 0,
  total: 0,
  page: 1,
  page_size: PAGE_SIZE,
  has_more: false,
  tags: [],
  source_counts: { all: 0, cn: 0, global: 0, local: 0 },
});
const filters = reactive({
  q: "",
  source: "all",
  tag: "",
  sort: "collectDate",
});
const view = ref(null);
const status = ref("");
const deleting = ref(false);
const loaded = ref(false);
const loadingMore = ref(false);
const loadMoreTrigger = ref(null);

let intersectionObserver = null;
let requestToken = 0;
let unsubscribeArchiveEvents = null;
let refreshWhenVisible = false;

const activeNavContext = computed(() => {
  const explicit = normalizeNavContext(route.query.nav_context);
  if (explicit) {
    return explicit;
  }
  if (route.name === "model-library-state") {
    return "organizer";
  }
  return String(route.params.sourceType || "") === "local" ? "organizer" : "subscriptions";
});
const groupBackTarget = computed(() => (
  activeNavContext.value === "organizer" ? "/organizer" : "/subscriptions"
));
const groupBackLabel = computed(() => (
  "返回"
));

function normalizeNavContext(value) {
  const raw = Array.isArray(value) ? value[0] : value;
  const normalized = String(raw || "").trim();
  return ["subscriptions", "organizer"].includes(normalized) ? normalized : "";
}

function syncFiltersFromRoute() {
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
  filters.source = typeof route.query.source === "string" ? route.query.source : "all";
  filters.tag = typeof route.query.tag === "string" ? route.query.tag : "";
  filters.sort = typeof route.query.sort === "string" ? route.query.sort : "collectDate";
}

function buildQuery(page = 1) {
  const query = new URLSearchParams();
  query.set("page", String(page));
  query.set("page_size", String(PAGE_SIZE));
  if (filters.q) query.set("q", filters.q);
  if (filters.source && filters.source !== "all") query.set("source", filters.source);
  if (filters.tag) query.set("tag", filters.tag);
  if (filters.sort && filters.sort !== "collectDate") query.set("sort", filters.sort);
  return query;
}

function buildRouteQuery(options = {}) {
  const query = {
    nav_context: activeNavContext.value || undefined,
    q: filters.q || undefined,
    source: filters.source !== "all" ? filters.source : undefined,
    tag: filters.tag || undefined,
    sort: filters.sort !== "collectDate" ? filters.sort : undefined,
  };
  const page = Number(options.page || 0);
  if (Number.isFinite(page) && page > 1) {
    query.page = String(Math.floor(page));
  }
  if (options.anchor) {
    query.anchor = String(options.anchor);
  }
  return query;
}

function endpointBase() {
  if (route.name === "model-library-state") {
    return `/api/source-library/states/${encodeURIComponent(String(route.params.stateKey || ""))}`;
  }
  return `/api/source-library/sources/${encodeURIComponent(String(route.params.sourceType || ""))}/${encodeURIComponent(String(route.params.sourceKey || ""))}`;
}

async function fetchPage(page) {
  return apiRequest(`${endpointBase()}?${buildQuery(page).toString()}`);
}

function mergeUniqueModelItems(currentItems = [], incomingItems = []) {
  const mergedItems = [...currentItems];
  const seenModelDirs = new Set(
    mergedItems.map((item) => String(item?.model_dir || "").trim()).filter(Boolean),
  );
  for (const item of incomingItems || []) {
    const modelDir = String(item?.model_dir || "").trim();
    if (!modelDir || seenModelDirs.has(modelDir)) {
      continue;
    }
    seenModelDirs.add(modelDir);
    mergedItems.push(item);
  }
  return mergedItems;
}

function routePage() {
  const rawPage = Array.isArray(route.query.page) ? route.query.page[0] : route.query.page;
  const page = Number.parseInt(String(rawPage || ""), 10);
  if (!Number.isFinite(page) || page <= 1) {
    return 1;
  }
  return Math.min(page, 200);
}

function routeAnchor() {
  const rawAnchor = Array.isArray(route.query.anchor) ? route.query.anchor[0] : route.query.anchor;
  return String(rawAnchor || "").trim();
}

function findModelCardElement(modelDir) {
  const target = String(modelDir || "");
  if (!target || typeof document === "undefined") {
    return null;
  }
  return Array.from(document.querySelectorAll("[data-model-dir]"))
    .find((element) => element?.dataset?.modelDir === target) || null;
}

async function scrollToRouteAnchor() {
  const anchor = routeAnchor();
  if (!anchor || typeof window === "undefined") {
    return;
  }
  await nextTick();
  await new Promise((resolve) => {
    window.requestAnimationFrame(() => {
      const element = findModelCardElement(anchor);
      if (element) {
        element.scrollIntoView({ block: "center", behavior: "auto" });
      }
      resolve();
    });
  });
}

function buildModelReturnTo(modelDir) {
  const page = Math.max(Number(payload.value.page) || 1, 1);
  return router.resolve({
    path: route.path,
    query: buildRouteQuery({ page, anchor: modelDir }),
  }).fullPath;
}

async function load({ append = false } = {}) {
  const currentToken = ++requestToken;
  syncFiltersFromRoute();

  const nextPage = append ? payload.value.page + 1 : routePage();
  const responses = [];
  for (let page = append ? nextPage : 1; page <= nextPage; page += 1) {
    responses.push(await fetchPage(page));
  }
  if (currentToken !== requestToken) {
    return;
  }

  const response = responses[responses.length - 1];
  view.value = response.view || view.value;
  if (append) {
    const mergedItems = mergeUniqueModelItems(payload.value.items, response.items || []);
    payload.value = {
      ...response,
      items: mergedItems,
      count: mergedItems.length,
    };
  } else {
    const mergedItems = responses.reduce(
      (items, item) => mergeUniqueModelItems(items, item.items || []),
      [],
    );
    payload.value = {
      ...response,
      items: mergedItems,
      count: mergedItems.length,
      page: nextPage,
    };
  }
  loaded.value = true;
  await nextTick();
  ensureObserver();
  if (!append) {
    await scrollToRouteAnchor();
  }
}

async function reloadVisiblePages() {
  const pagesToLoad = Math.max(Number(payload.value.page) || 1, 1);
  const currentToken = ++requestToken;
  syncFiltersFromRoute();

  const responses = [];
  for (let page = 1; page <= pagesToLoad; page += 1) {
    responses.push(await fetchPage(page));
  }

  if (!responses.length || currentToken !== requestToken) {
    return;
  }

  const lastResponse = responses[responses.length - 1];
  const mergedItems = responses.flatMap((response) => response.items || []);
  view.value = lastResponse.view || view.value;
  payload.value = {
    ...lastResponse,
    items: mergedItems,
    count: mergedItems.length,
    page: pagesToLoad,
  };
  await nextTick();
  ensureObserver();
}

async function loadMore() {
  if (loadingMore.value || !payload.value.has_more) {
    return;
  }
  loadingMore.value = true;
  try {
    await load({ append: true });
  } finally {
    loadingMore.value = false;
  }
}

function disconnectObserver() {
  if (intersectionObserver) {
    intersectionObserver.disconnect();
    intersectionObserver = null;
  }
}

function ensureObserver() {
  disconnectObserver();
  if (!loadMoreTrigger.value) {
    return;
  }
  intersectionObserver = new IntersectionObserver((entries) => {
    const [entry] = entries;
    if (entry?.isIntersecting) {
      void loadMore();
    }
  }, {
    rootMargin: "320px 0px",
  });
  intersectionObserver.observe(loadMoreTrigger.value);
}

function applyFilters() {
  router.replace({ path: route.path, query: buildRouteQuery() });
}

function applyFiltersIfChanged() {
  const nextQuery = buildRouteQuery();
  const currentQuery = {
    nav_context: normalizeNavContext(route.query.nav_context) || undefined,
    q: typeof route.query.q === "string" ? route.query.q : undefined,
    source: typeof route.query.source === "string" ? route.query.source : undefined,
    tag: typeof route.query.tag === "string" ? route.query.tag : undefined,
    sort: typeof route.query.sort === "string" ? route.query.sort : undefined,
  };

  if (
    currentQuery.q === nextQuery.q
    && currentQuery.nav_context === nextQuery.nav_context
    && currentQuery.source === nextQuery.source
    && currentQuery.tag === nextQuery.tag
    && currentQuery.sort === nextQuery.sort
  ) {
    return;
  }

  applyFilters();
}

function resetFilters() {
  router.replace({ path: route.path, query: buildRouteQuery() });
}

function goBack() {
  router.push(groupBackTarget.value);
}

function findModel(modelDir) {
  return payload.value.items.find((item) => item.model_dir === modelDir) || null;
}

function patchLocalFlag(modelDir, key, value) {
  payload.value = {
    ...payload.value,
    items: payload.value.items.map((item) => {
      if (item.model_dir !== modelDir) {
        return item;
      }
      return {
        ...item,
        local_flags: {
          favorite: Boolean(item.local_flags?.favorite),
          printed: Boolean(item.local_flags?.printed),
          deleted: Boolean(item.local_flags?.deleted),
          [key]: value,
        },
      };
    }),
  };
}

function handleArchiveCompleted() {
  if (document.hidden) {
    refreshWhenVisible = true;
    return;
  }
  void reloadVisiblePages();
}

function handleVisibilityChange() {
  if (document.hidden) {
    return;
  }
  const shouldRefresh = refreshWhenVisible;
  refreshWhenVisible = false;
  if (shouldRefresh) {
    void reloadVisiblePages();
  }
}

async function toggleFavorite(modelDir) {
  const model = findModel(modelDir);
  if (!model) return;

  const nextValue = !Boolean(model.local_flags?.favorite);
  patchLocalFlag(modelDir, "favorite", nextValue);
  try {
    await apiRequest("/api/models/flags/favorite", {
      method: "POST",
      body: {
        model_dir: modelDir,
        value: nextValue,
      },
    });
    status.value = nextValue ? "已加入本地收藏。" : "已取消本地收藏。";
  } catch (error) {
    patchLocalFlag(modelDir, "favorite", !nextValue);
    status.value = error instanceof Error ? error.message : "更新本地收藏失败。";
  }
}

async function togglePrinted(modelDir) {
  const model = findModel(modelDir);
  if (!model) return;

  const nextValue = !Boolean(model.local_flags?.printed);
  patchLocalFlag(modelDir, "printed", nextValue);
  try {
    await apiRequest("/api/models/flags/printed", {
      method: "POST",
      body: {
        model_dir: modelDir,
        value: nextValue,
      },
    });
    status.value = nextValue ? "已标记为已打印。" : "已取消已打印标记。";
  } catch (error) {
    patchLocalFlag(modelDir, "printed", !nextValue);
    status.value = error instanceof Error ? error.message : "更新已打印状态失败。";
  }
}

async function deleteOne(modelDir) {
  const model = findModel(modelDir);
  if (!model) return;
  if (!window.confirm(`确认在 MakerHub 中删除并隐藏「${model.title || modelDir}」吗？`)) return;

  deleting.value = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/models/delete", {
      method: "POST",
      body: { model_dirs: [modelDir] },
    });
    status.value = response.message || "模型已在 MakerHub 中删除并隐藏。";
    await reloadVisiblePages();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "本地删除失败。";
  } finally {
    deleting.value = false;
  }
}

async function restoreOne(modelDir) {
  const model = findModel(modelDir);
  if (!model) return;

  patchLocalFlag(modelDir, "deleted", false);
  status.value = "";
  try {
    const response = await apiRequest("/api/models/flags/deleted", {
      method: "POST",
      body: {
        model_dir: modelDir,
        value: false,
      },
    });
    status.value = response.message || "模型已恢复到模型库。";
    await reloadVisiblePages();
  } catch (error) {
    patchLocalFlag(modelDir, "deleted", true);
    status.value = error instanceof Error ? error.message : "恢复模型失败。";
  }
}

function formatCompact(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric)) {
    return String(value || "0");
  }
  if (numeric >= 1_000_000) {
    return `${(numeric / 1_000_000).toFixed(numeric >= 10_000_000 ? 0 : 1)} M`;
  }
  if (numeric >= 1_000) {
    return `${(numeric / 1_000).toFixed(numeric >= 10_000 ? 0 : 1)} k`;
  }
  return new Intl.NumberFormat("zh-CN").format(numeric);
}

watch(() => route.fullPath, () => {
  status.value = "";
  void load({ append: false }).catch((error) => {
    status.value = error instanceof Error ? error.message : "模型列表加载失败。";
    loaded.value = true;
  });
});

onMounted(async () => {
  try {
    await load({ append: false });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "模型列表加载失败。";
    loaded.value = true;
  }
  unsubscribeArchiveEvents = subscribeArchiveCompletion(handleArchiveCompleted);
  document.addEventListener("visibilitychange", handleVisibilityChange);
});

onBeforeUnmount(() => {
  disconnectObserver();
  if (typeof unsubscribeArchiveEvents === "function") {
    unsubscribeArchiveEvents();
    unsubscribeArchiveEvents = null;
  }
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
