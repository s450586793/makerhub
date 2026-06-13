<template>
  <section class="surface surface--filters library-toolbar">
    <div class="library-toolbar__copy">
      <span class="eyebrow">订阅库</span>
      <div class="library-toolbar__title-row">
        <h1>订阅来源</h1>
      </div>
    </div>
    <div class="library-toolbar__side">
      <div class="toolbar-stats">
        <span class="toolbar-stat">
          <em>订阅</em>
          <strong>{{ payload.count }}</strong>
        </span>
        <span class="toolbar-stat">
          <em>启用</em>
          <strong>{{ payload.summary.enabled }}</strong>
        </span>
        <span class="toolbar-stat">
          <em>同步</em>
          <strong>{{ subscriptionSyncActiveRuns.length || payload.summary.running }}</strong>
        </span>
      </div>
      <div class="filter-actions">
        <button class="button button-primary" type="button" @click="openCreateDialog">添加订阅</button>
        <button class="button button-secondary" type="button" @click="toggleSelectMode">
          {{ selectMode ? "取消选择" : "选择" }}
        </button>
        <button
          v-if="selectMode"
          class="button button-secondary"
          type="button"
          :disabled="!shareableCards.length"
          @click="selectAllShareableCards"
        >
          全选当前已加载
        </button>
        <button
          v-if="selectMode"
          class="button button-primary"
          type="button"
          :disabled="selectedModelDirs.length < 1"
          @click="openShareDialog"
        >
          分享 {{ selectedModelDirs.length }}
        </button>
        <RouterLink class="button button-secondary" to="/subscriptions/manage">订阅库管理</RouterLink>
      </div>
    </div>
  </section>

  <p v-if="status && !createDialog.visible" class="subscription-page-status">{{ status }}</p>

  <section v-if="!initialLoaded" class="surface empty-state subscription-inline-empty">
    <h2>正在加载订阅库</h2>
    <p>正在读取订阅来源和卡片数据。</p>
  </section>

  <template v-else>
    <section v-for="section in sourceSections" :key="section.key" class="library-section">
      <template v-if="section.items?.length">
        <div class="source-library-grid">
          <SourceLibraryCard
            v-for="card in section.items"
            :key="card.key"
            :card="card"
            :select-mode="selectMode"
            :selected="isCardSelected(card)"
            @open="openCard"
            @select="toggleCardSelected"
          />
        </div>
        <div
          v-if="section.key === 'subscription_sources' && section.items?.length"
          ref="loadMoreTrigger"
          class="list-loader-anchor"
        >
          <button
            v-if="section.has_more && !subscriptionsAutoLoadSupported"
            class="button button-secondary"
            type="button"
            :disabled="loadingMore"
            @click="loadMoreSubscriptionSources"
          >
            <span v-if="loadingMore">正在加载更多订阅来源...</span>
            <span v-else>加载更多</span>
          </button>
          <span v-else-if="loadingMore">正在加载更多订阅来源...</span>
          <span v-else-if="section.has_more">下拉到底自动加载下一页</span>
          <span v-else>已经到底了</span>
        </div>
      </template>
      <section v-else class="surface empty-state subscription-inline-empty">
        <h2>{{ section.label }}为空</h2>
        <p>当前没有可展示的订阅来源卡片。你可以先添加作者、合集或收藏夹订阅。</p>
      </section>
    </section>
  </template>

  <div
    v-if="createDialog.visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="subscription-create-dialog-title"
    @click="closeCreateDialog"
  >
    <div class="submit-dialog__panel subscription-create-dialog__panel" @click.stop>
      <h2 id="subscription-create-dialog-title">添加订阅</h2>
      <p>填写链接、名称和 Cron。保存后会立即扫描来源，并直接触发首轮模型同步。</p>
      <form class="subscription-create-dialog__form" @submit.prevent="createSubscription">
        <label class="filter-field filter-field--wide">
          <span>订阅链接</span>
          <input
            v-model.trim="createDialog.url"
            type="text"
            placeholder="作者上传页、收藏夹模型页或合集详情页链接"
          >
        </label>
        <label class="filter-field">
          <span>订阅名称</span>
          <input v-model.trim="createDialog.name" type="text" placeholder="订阅名称（可选）">
        </label>
        <CronField
          v-model="createDialog.cron"
          class="filter-field"
          placeholder="Cron，例如 0 */6 * * *"
          dialog-title="设置新增订阅 Cron"
        />
        <p v-if="status" class="subscription-page-status subscription-page-status--dialog">{{ status }}</p>
        <div class="submit-dialog__actions">
          <button class="button button-secondary" type="button" :disabled="creating" @click="closeCreateDialog">
            取消
          </button>
          <button class="button button-primary" type="submit" :disabled="creating">
            {{ creating ? "添加中..." : "添加订阅" }}
          </button>
        </div>
      </form>
    </div>
  </div>

  <ShareDialog
    :visible="shareDialogVisible"
    :model-dirs="selectedModelDirs"
    @close="closeShareDialog"
  />
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { RouterLink, useRoute, useRouter } from "vue-router";

import CronField from "../components/CronField.vue";
import ShareDialog from "../components/ShareDialog.vue";
import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { createAutoLoadObserver } from "../lib/autoLoadObserver";
import { getPageCache, setPageCache } from "../lib/pageCache";
import { createPagePerformanceTracker } from "../lib/performance";
import { subscribeStateRefresh } from "../lib/stateEvents";
import {
  DEFAULT_SUBSCRIPTION_SETTINGS,
  createEmptySubscriptionsPayload,
  normalizeSubscriptionsPayload,
} from "../lib/subscriptions";


const route = useRoute();
const router = useRouter();
const PAGE_SIZE = 8;
const payload = ref(createEmptySubscriptionsPayload());
const status = ref("");
const creating = ref(false);
const initialLoaded = ref(false);
const loadingMore = ref(false);
const loadMoreTrigger = ref(null);
const subscriptionsAutoLoadSupported = ref(false);
const selectMode = ref(false);
const selectedCardKeySet = ref(new Set());
const shareDialogVisible = ref(false);
const createDialog = reactive({
  visible: false,
  url: "",
  name: "",
  cron: DEFAULT_SUBSCRIPTION_SETTINGS.default_cron,
});
let unsubscribeStateRefresh = null;
let requestToken = 0;

const sourceSections = computed(() => (
  payload.value.sections.filter((section) => section?.key === "subscription_sources")
));
const subscriptionSources = computed(() => subscriptionSourcesSection());
const runtimeSubscriptions = computed(() => payload.value.runtime?.subscriptions || {});
const subscriptionSyncActiveRuns = computed(() => {
  const runs = runtimeSubscriptions.value?.active_runs;
  return Array.isArray(runs) ? runs : [];
});
const hasMoreSubscriptionSources = computed(() => Boolean(subscriptionSources.value?.has_more));
const loadMoreObserver = createAutoLoadObserver({
  triggerRef: loadMoreTrigger,
  canLoad: () => Boolean(hasMoreSubscriptionSources.value),
  isLoading: () => Boolean(loadingMore.value),
  load: loadMoreSubscriptionSources,
  nextTick,
});
const shareableCards = computed(() => (
  sourceSections.value
    .flatMap((section) => section.items || [])
    .filter((card) => Array.isArray(card?.model_dirs) && card.model_dirs.length)
));
const selectedModelDirs = computed(() => {
  const selectedKeys = selectedCardKeySet.value;
  const modelDirs = [];
  const seen = new Set();
  for (const card of shareableCards.value) {
    if (!selectedKeys.has(String(card.key || ""))) {
      continue;
    }
    for (const modelDir of card.model_dirs || []) {
      const cleanModelDir = String(modelDir || "").trim();
      if (!cleanModelDir || seen.has(cleanModelDir)) {
        continue;
      }
      seen.add(cleanModelDir);
      modelDirs.push(cleanModelDir);
    }
  }
  return modelDirs;
});

function rememberSubscriptionsPage() {
  setPageCache("subscriptions", {
    payload: payload.value,
    page: Number(subscriptionSourcesSection()?.page || 1),
  });
}

function hydrateSubscriptionsPageFromCache() {
  const cached = getPageCache("subscriptions");
  if (!cached?.payload) {
    return false;
  }
  payload.value = normalizeSubscriptionsPayload(cached.payload);
  initialLoaded.value = true;
  return true;
}

function routePage() {
  const rawPage = Array.isArray(route.query.page) ? route.query.page[0] : route.query.page;
  const page = Number.parseInt(String(rawPage || ""), 10);
  if (!Number.isFinite(page) || page <= 1) {
    return 1;
  }
  return Math.min(page, 200);
}

function buildRouteQuery(page = 1) {
  const query = {};
  const safePage = Math.max(Number(page) || 1, 1);
  if (safePage > 1) {
    query.page = String(Math.floor(safePage));
  }
  return query;
}

function subscriptionSourcesSection(sourcePayload = payload.value) {
  return (sourcePayload.sections || []).find((section) => section?.key === "subscription_sources") || null;
}

function mergeSubscriptionSourceItems(existing = [], incoming = []) {
  const merged = [];
  const seen = new Set();
  for (const item of [...existing, ...incoming]) {
    const key = String(item?.key || "").trim();
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(item);
  }
  return merged;
}

function replaceSubscriptionSourcesSection(basePayload, items, sectionMeta = {}) {
  const sections = (basePayload.sections || []).map((section) => (
    section?.key === "subscription_sources"
      ? {
          ...section,
          ...sectionMeta,
          items,
          count: items.length,
        }
      : section
  ));
  return {
    ...basePayload,
    sections,
  };
}

function buildSubscriptionsQuery(page = 1, options = {}) {
  const query = new URLSearchParams();
  const safePage = Math.max(Number(page) || 1, 1);
  query.set("page", String(safePage));
  query.set("page_size", String(PAGE_SIZE));
  if (options.includeUntilPage) {
    query.set("limit", String(Math.max(1, Math.floor(safePage)) * PAGE_SIZE));
  }
  return query;
}

async function fetchSubscriptionsPage(page = 1, options = {}) {
  return normalizeSubscriptionsPayload(
    await apiRequest(`/api/subscriptions?${buildSubscriptionsQuery(page, options).toString()}`),
  );
}

function resetCreateForm() {
  createDialog.url = "";
  createDialog.name = "";
  createDialog.cron = String(payload.value.settings?.default_cron || DEFAULT_SUBSCRIPTION_SETTINGS.default_cron);
}

async function load({ silent = false, pages = routePage() } = {}) {
  const currentToken = ++requestToken;
  disconnectObserver();
  loadingMore.value = false;
  const pagesToLoad = Math.max(Number(pages) || 1, 1);
  let failed = false;
  try {
    const response = await fetchSubscriptionsPage(pagesToLoad, { includeUntilPage: pagesToLoad > 1 });
    if (currentToken !== requestToken) {
      return;
    }
    const section = subscriptionSourcesSection(response);
    payload.value = replaceSubscriptionSourcesSection(
      response,
      section?.items || [],
      {
        ...(section || {}),
        page: pagesToLoad,
        page_size: PAGE_SIZE,
        has_more: Boolean(section?.has_more),
        total: Number(section?.total || section?.items?.length || 0),
      },
    );
    initialLoaded.value = true;
    pruneSelectionsToLoadedCards();
    rememberSubscriptionsPage();
  } catch (error) {
    failed = true;
    if (!silent) {
      status.value = error instanceof Error ? error.message : "订阅数据加载失败。";
    }
  } finally {
    if (currentToken === requestToken && !failed) {
      await nextTick();
      ensureObserver();
    }
  }
}

async function updateRoutePage(page) {
  await router.replace({
    path: route.path,
    query: buildRouteQuery(page),
  });
}

async function loadMoreSubscriptionSources() {
  if (loadingMore.value || !hasMoreSubscriptionSources.value) {
    return false;
  }
  const currentToken = ++requestToken;
  const nextPage = Math.max(Number(subscriptionSources.value?.page || 1), 1) + 1;
  let failed = false;
  disconnectObserver();
  loadingMore.value = true;
  try {
    const response = await fetchSubscriptionsPage(nextPage);
    if (currentToken !== requestToken) {
      return false;
    }
    const incomingSection = subscriptionSourcesSection(response);
    const mergedItems = mergeSubscriptionSourceItems(subscriptionSources.value?.items || [], incomingSection?.items || []);
    payload.value = replaceSubscriptionSourcesSection(
      response,
      mergedItems,
      {
        ...(incomingSection || {}),
        page: nextPage,
        page_size: PAGE_SIZE,
        has_more: Boolean(incomingSection?.has_more),
        total: Number(incomingSection?.total || mergedItems.length),
      },
    );
    initialLoaded.value = true;
    pruneSelectionsToLoadedCards();
    rememberSubscriptionsPage();
    await updateRoutePage(nextPage);
    return true;
  } catch (error) {
    failed = true;
    status.value = error instanceof Error ? error.message : "加载更多订阅来源失败。";
    return false;
  } finally {
    if (currentToken === requestToken) {
      loadingMore.value = false;
      await nextTick();
      if (!failed) {
        ensureObserver();
      }
    }
  }
}

function disconnectObserver() {
  loadMoreObserver.disconnect();
}

function ensureObserver() {
  loadMoreObserver.ensure();
}

function openCard(card) {
  if (!card || !card.key) {
    return;
  }
  if (card.route_kind === "state") {
    router.push({
      name: "model-library-state",
      params: {
        stateKey: String(card.key),
      },
      query: {
        nav_context: "subscriptions",
      },
    });
    return;
  }
  router.push({
    name: "model-library-source",
    params: {
      sourceType: String(card.kind || ""),
      sourceKey: String(card.key),
    },
    query: {
      nav_context: "subscriptions",
    },
  });
}

function toggleSelectMode() {
  if (selectMode.value) {
    selectMode.value = false;
    selectedCardKeySet.value = new Set();
    return;
  }
  selectMode.value = true;
  status.value = "";
}

function isCardSelected(card) {
  return selectedCardKeySet.value.has(String(card?.key || ""));
}

function toggleCardSelected(card) {
  const key = String(card?.key || "").trim();
  if (!key || !Array.isArray(card?.model_dirs) || !card.model_dirs.length) {
    status.value = "这个来源下当前没有可分享的已归档模型。";
    return;
  }
  const nextSet = new Set(selectedCardKeySet.value);
  if (nextSet.has(key)) {
    nextSet.delete(key);
  } else {
    nextSet.add(key);
  }
  selectedCardKeySet.value = nextSet;
}

function selectAllShareableCards() {
  selectedCardKeySet.value = new Set(shareableCards.value.map((card) => String(card.key || "")).filter(Boolean));
}

function pruneSelectionsToLoadedCards() {
  const loadedKeys = new Set(shareableCards.value.map((card) => String(card.key || "")).filter(Boolean));
  const nextSet = new Set();
  for (const key of selectedCardKeySet.value) {
    if (loadedKeys.has(key)) {
      nextSet.add(key);
    }
  }
  selectedCardKeySet.value = nextSet;
}

function openShareDialog() {
  if (!selectedModelDirs.value.length) {
    status.value = "请先选择要分享的订阅来源。";
    return;
  }
  shareDialogVisible.value = true;
}

function closeShareDialog() {
  shareDialogVisible.value = false;
}

function openCreateDialog() {
  resetCreateForm();
  status.value = "";
  createDialog.visible = true;
}

function closeCreateDialog(force = false) {
  if (creating.value && !force) {
    return;
  }
  createDialog.visible = false;
}

async function createSubscription() {
  if (!createDialog.url) {
    status.value = "请先输入作者页、合集或收藏夹链接。";
    return;
  }
  creating.value = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/subscriptions", {
      method: "POST",
      body: {
        url: createDialog.url,
        name: createDialog.name,
        cron: createDialog.cron,
        enabled: Boolean(payload.value.settings?.default_enabled ?? true),
        initialize_from_source: true,
      },
    });
    status.value = response.message || "订阅已创建。";
    closeCreateDialog(true);
    resetCreateForm();
    await updateRoutePage(1);
    await load({ silent: true, pages: 1 });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "创建订阅失败。";
  } finally {
    creating.value = false;
  }
}

onMounted(async () => {
  const perf = createPagePerformanceTracker({ page: "subscriptions", route: () => route.fullPath });
  subscriptionsAutoLoadSupported.value = typeof window !== "undefined" && "IntersectionObserver" in window;
  unsubscribeStateRefresh = subscribeStateRefresh(
    ["subscriptions_state", "source_library", "archive_queue"],
    () => {
      void load({ silent: true, pages: Number(subscriptionSources.value?.page || routePage()) });
    },
  );
  hydrateSubscriptionsPageFromCache();
  await load();
  void perf.finish();
});

onBeforeUnmount(() => {
  disconnectObserver();
  if (typeof unsubscribeStateRefresh === "function") {
    unsubscribeStateRefresh();
    unsubscribeStateRefresh = null;
  }
});
</script>
