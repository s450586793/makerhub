<template>
  <section class="surface surface--filters">
    <form class="filter-bar" @submit.prevent="applyFilters">
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
    <span v-if="status" class="form-status model-toolbar-inline__status">{{ status }}</span>
  </section>

  <section v-if="payload.items.length" class="model-grid">
    <ModelCard
      v-for="model in payload.items"
      :key="model.model_dir"
      :data-model-dir="model.model_dir"
      :model="model"
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
    <h2>当前没有匹配的模型</h2>
    <p>你可以调整搜索、来源、标签或排序条件后再试一次。</p>
  </section>

  <section v-else class="surface empty-state">
    <h2>正在加载模型库</h2>
    <p>稍等，正在读取当前模型列表。</p>
  </section>
</template>

<script setup>
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ModelCard from "../components/ModelCard.vue";
import { subscribeArchiveCompletion } from "../lib/archiveEvents";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();
const PAGE_SIZE = 12;

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
const status = ref("");
const loaded = ref(false);
const deleting = ref(false);
const loadingMore = ref(false);
const loadMoreTrigger = ref(null);

let intersectionObserver = null;
let requestToken = 0;
let loadMoreToken = 0;
let deleteSettleToken = 0;
let observerToken = 0;
let unsubscribeArchiveEvents = null;
let refreshWhenVisible = false;
let locallyHiddenDeletedModelDirs = new Set();

function syncFiltersFromRoute() {
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
  filters.source = typeof route.query.source === "string" ? route.query.source : "all";
  filters.tag = typeof route.query.tag === "string" ? route.query.tag : "";
  filters.sort = typeof route.query.sort === "string" ? route.query.sort : "collectDate";
}

function buildQuery(page = 1, options = {}) {
  const query = new URLSearchParams();
  query.set("page", String(page));
  query.set("page_size", String(PAGE_SIZE));
  if (options.cacheKey) query.set("_", String(options.cacheKey));
  if (filters.q) query.set("q", filters.q);
  if (filters.source && filters.source !== "all") query.set("source", filters.source);
  if (filters.tag) query.set("tag", filters.tag);
  if (filters.sort && filters.sort !== "collectDate") query.set("sort", filters.sort);
  return query;
}

function buildRouteQuery() {
  return {
    q: filters.q || undefined,
    source: filters.source !== "all" ? filters.source : undefined,
    tag: filters.tag || undefined,
    sort: filters.sort !== "collectDate" ? filters.sort : undefined,
  };
}

async function fetchPage(page, options = {}) {
  return apiRequest(`/api/models?${buildQuery(page, options).toString()}`);
}

function decrementCount(value, amount) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return value;
  }
  return Math.max(0, number - amount);
}

function incrementCount(value, amount) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return value;
  }
  return number + amount;
}

function decrementSourceCounts(sourceCounts = {}, removedItems = []) {
  const nextCounts = {
    all: Number(sourceCounts.all || 0),
    cn: Number(sourceCounts.cn || 0),
    global: Number(sourceCounts.global || 0),
    local: Number(sourceCounts.local || 0),
  };
  for (const item of removedItems) {
    nextCounts.all = Math.max(0, nextCounts.all - 1);
    const source = String(item?.source || "").trim().toLowerCase();
    if (Object.prototype.hasOwnProperty.call(nextCounts, source)) {
      nextCounts[source] = Math.max(0, nextCounts[source] - 1);
    }
  }
  return nextCounts;
}

function incrementSourceCounts(sourceCounts = {}, restoredItems = []) {
  const nextCounts = {
    all: Number(sourceCounts.all || 0),
    cn: Number(sourceCounts.cn || 0),
    global: Number(sourceCounts.global || 0),
    local: Number(sourceCounts.local || 0),
  };
  for (const item of restoredItems) {
    nextCounts.all += 1;
    const source = String(item?.source || "").trim().toLowerCase();
    if (Object.prototype.hasOwnProperty.call(nextCounts, source)) {
      nextCounts[source] += 1;
    }
  }
  return nextCounts;
}

function shouldHideLocallyDeletedItem(item) {
  if (filters.tag === "__local_deleted__") {
    return false;
  }
  const modelDir = String(item?.model_dir || "").trim();
  return Boolean(modelDir && locallyHiddenDeletedModelDirs.has(modelDir));
}

function suppressLocallyDeletedItems(response) {
  const items = Array.isArray(response?.items) ? response.items : [];
  const removedItems = items.filter((item) => shouldHideLocallyDeletedItem(item));
  if (!removedItems.length) {
    return response;
  }
  const visibleItems = items.filter((item) => !shouldHideLocallyDeletedItem(item));
  return {
    ...response,
    items: visibleItems,
    count: decrementCount(response.count, removedItems.length),
    filtered_total: decrementCount(response.filtered_total, removedItems.length),
    total: decrementCount(response.total, removedItems.length),
    source_counts: decrementSourceCounts(response.source_counts, removedItems),
  };
}

function removeModelFromCurrentPayload(modelDir) {
  const target = String(modelDir || "").trim();
  if (!target) {
    return false;
  }
  const items = payload.value.items || [];
  const removedItems = items.filter((item) => item.model_dir === target);
  if (!removedItems.length) {
    return false;
  }
  const nextItems = items.filter((item) => item.model_dir !== target);
  payload.value = {
    ...payload.value,
    items: nextItems,
    count: nextItems.length,
    filtered_total: decrementCount(payload.value.filtered_total, removedItems.length),
    total: decrementCount(payload.value.total, removedItems.length),
    source_counts: decrementSourceCounts(payload.value.source_counts, removedItems),
  };
  return true;
}

function restoreModelToCurrentPayload(model, index) {
  const target = String(model?.model_dir || "").trim();
  if (!target || payload.value.items.some((item) => item.model_dir === target)) {
    return false;
  }
  const nextItems = [...payload.value.items];
  const safeIndex = Math.max(0, Math.min(Number(index) || 0, nextItems.length));
  nextItems.splice(safeIndex, 0, model);
  payload.value = {
    ...payload.value,
    items: nextItems,
    count: nextItems.length,
    filtered_total: incrementCount(payload.value.filtered_total, 1),
    total: incrementCount(payload.value.total, 1),
    source_counts: incrementSourceCounts(payload.value.source_counts, [model]),
  };
  return true;
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

async function load({ append = false, refresh = false } = {}) {
  const currentToken = ++requestToken;
  if (!append) {
    loadMoreToken += 1;
    loadingMore.value = false;
  }
  syncFiltersFromRoute();

  const nextPage = append ? payload.value.page + 1 : 1;
  const cacheKey = refresh ? `${Date.now()}-${nextPage}` : "";
  const response = suppressLocallyDeletedItems(await fetchPage(nextPage, { cacheKey }));
  if (currentToken !== requestToken) {
    return;
  }

  if (append) {
    const mergedItems = mergeUniqueModelItems(payload.value.items, response.items || []);
    payload.value = {
      ...response,
      items: mergedItems,
      count: mergedItems.length,
    };
  } else {
    payload.value = response;
  }
  loaded.value = true;
  await nextTick();
  ensureObserver();
}

async function reloadVisiblePages({ refresh = false } = {}) {
  const pagesToLoad = Math.max(Number(payload.value.page) || 1, 1);
  const currentToken = ++requestToken;
  loadMoreToken += 1;
  loadingMore.value = false;
  syncFiltersFromRoute();
  const cacheKeyBase = refresh ? Date.now() : "";

  const responses = [];
  for (let page = 1; page <= pagesToLoad; page += 1) {
    const cacheKey = cacheKeyBase ? `${cacheKeyBase}-${page}` : "";
    responses.push(suppressLocallyDeletedItems(await fetchPage(page, { cacheKey })));
  }

  if (!responses.length || currentToken !== requestToken) {
    return;
  }

  const lastResponse = responses[responses.length - 1];
  const mergedItems = responses.flatMap((response) => response.items || []);
  payload.value = {
    ...lastResponse,
    items: mergedItems,
    count: mergedItems.length,
    page: pagesToLoad,
  };
  await nextTick();
  ensureObserver();
}

function findModelCardElement(modelDir) {
  const target = String(modelDir || "");
  if (!target || typeof document === "undefined") {
    return null;
  }
  return Array.from(document.querySelectorAll("[data-model-dir]"))
    .find((element) => element?.dataset?.modelDir === target) || null;
}

function captureModelListAnchor(modelDir) {
  if (typeof window === "undefined") {
    return null;
  }
  const items = payload.value.items || [];
  const index = items.findIndex((item) => item.model_dir === modelDir);
  const candidates = [
    items[index + 1]?.model_dir,
    items[index - 1]?.model_dir,
    modelDir,
  ].filter(Boolean);

  for (const candidate of candidates) {
    const element = findModelCardElement(candidate);
    if (element) {
      return {
        modelDir: candidate,
        top: element.getBoundingClientRect().top,
        scrollY: window.scrollY,
      };
    }
  }

  return {
    modelDir: "",
    top: 0,
    scrollY: window.scrollY,
  };
}

async function restoreModelListAnchor(anchor) {
  if (!anchor || typeof window === "undefined") {
    return;
  }
  await nextTick();
  await new Promise((resolve) => {
    window.requestAnimationFrame(() => {
      const element = findModelCardElement(anchor.modelDir);
      if (element) {
        const delta = element.getBoundingClientRect().top - Number(anchor.top || 0);
        if (Math.abs(delta) > 1) {
          window.scrollBy({ top: delta, behavior: "auto" });
        }
        resolve();
        return;
      }
      if (Number.isFinite(anchor.scrollY)) {
        window.scrollTo({ top: anchor.scrollY, behavior: "auto" });
      }
      resolve();
    });
  });
}

async function waitForNextFrame() {
  if (typeof window === "undefined") {
    return;
  }
  await new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve());
  });
}

function isLoadMoreTriggerNearViewport(margin = 420) {
  if (typeof window === "undefined" || !loadMoreTrigger.value) {
    return false;
  }
  const rect = loadMoreTrigger.value.getBoundingClientRect();
  return rect.top <= window.innerHeight + margin && rect.bottom >= -margin;
}

async function settleLoadMoreAfterDelete(routeAtDelete, currentDeleteSettleToken) {
  await nextTick();
  await waitForNextFrame();
  if (route.fullPath !== routeAtDelete || currentDeleteSettleToken !== deleteSettleToken) {
    return;
  }
  if (payload.value.has_more && isLoadMoreTriggerNearViewport()) {
    await loadMore();
    return;
  }
  ensureObserver();
}

async function refreshCurrentModelLibrary(anchor = null, options = {}) {
  await reloadVisiblePages(options);
  await restoreModelListAnchor(anchor);
}

async function loadMore() {
  if (loadingMore.value || !payload.value.has_more) {
    return false;
  }
  const currentToken = ++loadMoreToken;
  const routeAtLoad = route.fullPath;
  const nextPage = Math.max(Number(payload.value.page) || 1, 1) + 1;
  let failed = false;
  disconnectObserver();
  loadingMore.value = true;
  try {
    syncFiltersFromRoute();
    const response = suppressLocallyDeletedItems(await fetchPage(nextPage, { cacheKey: `${Date.now()}-${nextPage}` }));
    if (currentToken !== loadMoreToken || route.fullPath !== routeAtLoad) {
      return false;
    }
    const mergedItems = mergeUniqueModelItems(payload.value.items, response.items || []);
    payload.value = {
      ...response,
      items: mergedItems,
      count: mergedItems.length,
      page: nextPage,
    };
    loaded.value = true;
    return true;
  } catch (error) {
    failed = true;
    status.value = error instanceof Error ? error.message : "加载更多模型失败。";
    return false;
  } finally {
    if (currentToken === loadMoreToken) {
      loadingMore.value = false;
      await nextTick();
      if (!failed) {
        ensureObserver();
      }
    }
  }
}

function disconnectObserver() {
  observerToken += 1;
  if (intersectionObserver) {
    intersectionObserver.disconnect();
    intersectionObserver = null;
  }
}

async function loadMoreIfTriggerIsVisible(currentObserverToken) {
  await nextTick();
  await waitForNextFrame();
  if (
    currentObserverToken !== observerToken
    || loadingMore.value
    || !payload.value.has_more
    || !isLoadMoreTriggerNearViewport()
  ) {
    return;
  }
  void loadMore();
}

function ensureObserver() {
  disconnectObserver();
  if (!loadMoreTrigger.value || loadingMore.value) {
    return;
  }
  const currentObserverToken = ++observerToken;
  intersectionObserver = new IntersectionObserver((entries) => {
    const [entry] = entries;
    if (entry?.isIntersecting) {
      void loadMore();
    }
  }, {
    rootMargin: "0px 0px 420px 0px",
  });
  intersectionObserver.observe(loadMoreTrigger.value);
  void loadMoreIfTriggerIsVisible(currentObserverToken);
}

function applyFilters() {
  router.replace({ path: "/models", query: buildRouteQuery() });
}

function applyFiltersIfChanged() {
  const nextQuery = buildRouteQuery();
  const currentQuery = {
    q: typeof route.query.q === "string" ? route.query.q : undefined,
    source: typeof route.query.source === "string" ? route.query.source : undefined,
    tag: typeof route.query.tag === "string" ? route.query.tag : undefined,
    sort: typeof route.query.sort === "string" ? route.query.sort : undefined,
  };

  if (
    currentQuery.q === nextQuery.q
    && currentQuery.source === nextQuery.source
    && currentQuery.tag === nextQuery.tag
    && currentQuery.sort === nextQuery.sort
  ) {
    return;
  }

  applyFilters();
}

function resetFilters() {
  router.replace("/models");
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

  const cleanModelDir = String(modelDir || "").trim();
  requestToken += 1;
  loadMoreToken += 1;
  const currentDeleteSettleToken = ++deleteSettleToken;
  disconnectObserver();
  loadingMore.value = false;
  const scrollAnchor = captureModelListAnchor(modelDir);
  const originalIndex = payload.value.items.findIndex((item) => item.model_dir === modelDir);
  const removedModel = {
    ...model,
    local_flags: { ...(model.local_flags || {}) },
    subscription_flags: { ...(model.subscription_flags || {}) },
  };
  const routeAtDelete = route.fullPath;
  locallyHiddenDeletedModelDirs.add(cleanModelDir);
  removeModelFromCurrentPayload(modelDir);
  status.value = "已从当前列表隐藏，正在后台删除。";
  void settleLoadMoreAfterDelete(routeAtDelete, currentDeleteSettleToken);

  deleting.value = true;
  try {
    const response = await apiRequest("/api/models/delete", {
      method: "POST",
      body: { model_dirs: [modelDir] },
    });
    if (!response.success) {
      throw new Error(response.message || "没有标记任何模型。");
    }
    status.value = response.message || "模型已在 MakerHub 中删除并隐藏。";
  } catch (error) {
    if (currentDeleteSettleToken === deleteSettleToken) {
      deleteSettleToken += 1;
    }
    locallyHiddenDeletedModelDirs.delete(cleanModelDir);
    if (route.fullPath === routeAtDelete) {
      restoreModelToCurrentPayload(removedModel, originalIndex);
      await nextTick();
      ensureObserver();
      await restoreModelListAnchor(scrollAnchor);
    }
    status.value = error instanceof Error ? error.message : "本地删除失败。";
  } finally {
    deleting.value = false;
  }
}

async function restoreOne(modelDir) {
  const model = findModel(modelDir);
  if (!model) return;

  locallyHiddenDeletedModelDirs.delete(String(modelDir || "").trim());
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
