<template>
  <section class="surface surface--filters">
    <form class="filter-bar" @submit.prevent="applyFilters">
      <label class="filter-field filter-field--wide">
        <span>搜索</span>
        <input v-model.trim="filters.q" type="text" placeholder="标题、作者、标签">
      </label>
      <label class="filter-field">
        <span>来源</span>
        <select v-model="filters.source">
          <option value="all">全部 ({{ payload.source_counts.all || 0 }})</option>
          <option value="cn">国内 ({{ payload.source_counts.cn || 0 }})</option>
          <option value="global">国际 ({{ payload.source_counts.global || 0 }})</option>
          <option value="local">本地 ({{ payload.source_counts.local || 0 }})</option>
        </select>
      </label>
      <label class="filter-field">
        <span>标签</span>
        <select v-model="filters.tag">
          <option value="">全部标签</option>
          <option v-for="tag in payload.tags" :key="tag" :value="tag">{{ tag }}</option>
        </select>
      </label>
      <label class="filter-field">
        <span>排序</span>
        <select v-model="filters.sort">
          <option value="collectDate">采集时间倒序</option>
          <option value="downloads">下载量</option>
          <option value="likes">点赞量</option>
          <option value="prints">打印量</option>
        </select>
      </label>
      <div class="filter-actions">
        <button class="button button-primary" type="submit">应用筛选</button>
        <button class="button button-secondary" type="button" @click="resetFilters">重置</button>
      </div>
    </form>
  </section>

  <section class="surface section-card section-card--compact">
    <div class="section-card__header section-card__header--compact">
      <div>
        <span class="eyebrow">选择操作</span>
        <h2>当前结果 {{ payload.items.length }} / 筛选命中 {{ payload.filtered_total }} / 总模型 {{ payload.total }}</h2>
      </div>
      <div class="bulk-actions">
        <button class="button button-secondary button-small" type="button" @click="toggleSelectionMode">
          {{ selectionMode ? "退出选择" : "选择" }}
        </button>
        <button class="button button-secondary button-small" type="button" :disabled="!payload.items.length" @click="selectAll">
          全选
        </button>
        <button class="button button-secondary button-small" type="button" :disabled="!selectedCount" @click="clearSelection">
          清空
        </button>
        <button class="button button-danger button-small" type="button" :disabled="!selectedCount || deleting" @click="deleteSelected">
          {{ deleting ? "删除中..." : `删除 (${selectedCount})` }}
        </button>
      </div>
    </div>
    <span class="form-status">{{ status }}</span>
  </section>

  <section v-if="payload.items.length" class="model-grid">
    <ModelCard
      v-for="model in payload.items"
      :key="model.model_dir"
      :model="model"
      :selection-mode="selectionMode"
      :selected="selectedSet.has(model.model_dir)"
      @toggle="toggleItem"
      @favorite="toggleFavorite"
      @printed="togglePrinted"
      @delete="deleteOne"
    />
  </section>

  <div v-if="payload.items.length" ref="loadMoreTrigger" class="list-loader-anchor">
    <span v-if="loadingMore">正在加载更多模型...</span>
    <span v-else-if="payload.has_more">下拉到底自动加载下一页</span>
    <span v-else>已经到底了</span>
  </div>

  <section v-else class="surface empty-state">
    <h2>还没有匹配的模型</h2>
    <p>当前筛选条件下没有命中结果。你可以重置条件，或者先去任务页发起新的归档任务。</p>
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
const status = ref("");
const selectionMode = ref(false);
const selectedSet = ref(new Set());
const deleting = ref(false);
const loadingMore = ref(false);
const loadMoreTrigger = ref(null);

let intersectionObserver = null;
let requestToken = 0;
let unsubscribeArchiveEvents = null;
let refreshWhenVisible = false;

const selectedCount = computed(() => selectedSet.value.size);

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

async function fetchPage(page) {
  return apiRequest(`/api/models?${buildQuery(page).toString()}`);
}

async function load({ append = false } = {}) {
  const currentToken = ++requestToken;
  syncFiltersFromRoute();

  const nextPage = append ? payload.value.page + 1 : 1;
  const response = await fetchPage(nextPage);
  if (currentToken !== requestToken) {
    return;
  }

  if (append) {
    payload.value = {
      ...response,
      items: [...payload.value.items, ...response.items],
    };
  } else {
    payload.value = response;
  }

  const available = new Set(payload.value.items.map((item) => item.model_dir));
  selectedSet.value = new Set([...selectedSet.value].filter((item) => available.has(item)));
  await nextTick();
  ensureObserver();
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
  payload.value = {
    ...lastResponse,
    items: mergedItems,
    count: mergedItems.length,
    page: pagesToLoad,
  };

  const available = new Set(payload.value.items.map((item) => item.model_dir));
  selectedSet.value = new Set([...selectedSet.value].filter((item) => available.has(item)));
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
  router.replace({
    path: "/models",
    query: {
      q: filters.q || undefined,
      source: filters.source !== "all" ? filters.source : undefined,
      tag: filters.tag || undefined,
      sort: filters.sort !== "collectDate" ? filters.sort : undefined,
    },
  });
}

function resetFilters() {
  router.replace("/models");
}

function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value;
  if (!selectionMode.value) {
    selectedSet.value = new Set();
  }
}

function toggleItem(modelDir) {
  if (!selectionMode.value) {
    selectionMode.value = true;
  }
  const next = new Set(selectedSet.value);
  if (next.has(modelDir)) {
    next.delete(modelDir);
  } else {
    next.add(modelDir);
  }
  selectedSet.value = next;
}

function selectAll() {
  selectionMode.value = true;
  selectedSet.value = new Set(payload.value.items.map((item) => item.model_dir));
}

function clearSelection() {
  selectedSet.value = new Set();
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

async function deleteSelected() {
  if (!selectedCount.value) return;
  if (!window.confirm(`确认删除选中的 ${selectedCount.value} 个模型吗？`)) return;

  deleting.value = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/models/delete", {
      method: "POST",
      body: { model_dirs: [...selectedSet.value] },
    });
    status.value = response.message || "删除完成。";
    selectedSet.value = new Set();
    selectionMode.value = false;
    await load({ append: false });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "删除失败。";
  } finally {
    deleting.value = false;
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
  if (!window.confirm(`确认删除「${model.title || modelDir}」吗？`)) return;

  deleting.value = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/models/delete", {
      method: "POST",
      body: { model_dirs: [modelDir] },
    });
    status.value = response.message || "删除完成。";
    selectedSet.value = new Set([...selectedSet.value].filter((item) => item !== modelDir));
    await reloadVisiblePages();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "删除失败。";
  } finally {
    deleting.value = false;
  }
}

watch(() => route.fullPath, () => {
  void load({ append: false });
});
onMounted(async () => {
  await load({ append: false });
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
