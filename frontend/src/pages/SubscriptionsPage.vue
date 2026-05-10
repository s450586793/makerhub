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
          <strong>{{ payload.summary.running }}</strong>
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
          全选当前
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
      <div v-if="section.items?.length" class="source-library-grid">
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
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";

import CronField from "../components/CronField.vue";
import ShareDialog from "../components/ShareDialog.vue";
import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { getPageCache, setPageCache } from "../lib/pageCache";
import {
  DEFAULT_SUBSCRIPTION_SETTINGS,
  createEmptySubscriptionsPayload,
  normalizeSubscriptionsPayload,
} from "../lib/subscriptions";


const router = useRouter();
const payload = ref(createEmptySubscriptionsPayload());
const status = ref("");
const creating = ref(false);
const initialLoaded = ref(false);
const selectMode = ref(false);
const selectedCardKeySet = ref(new Set());
const shareDialogVisible = ref(false);
const createDialog = reactive({
  visible: false,
  url: "",
  name: "",
  cron: DEFAULT_SUBSCRIPTION_SETTINGS.default_cron,
});
let refreshTimer = null;

const sourceSections = computed(() => (
  payload.value.sections.filter((section) => section?.key === "subscription_sources")
));
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
  });
}

function hydrateSubscriptionsPageFromCache() {
  const cached = getPageCache("subscriptions");
  if (!cached?.payload) {
    return false;
  }
  payload.value = normalizeSubscriptionsPayload(cached.payload);
  initialLoaded.value = true;
  syncAutoRefresh();
  return true;
}

function syncAutoRefresh() {
  const hasRunning = payload.value.items.some((item) => item.running);
  if (hasRunning && !refreshTimer) {
    refreshTimer = window.setInterval(() => {
      void load({ silent: true });
    }, 5000);
    return;
  }
  if (!hasRunning && refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function resetCreateForm() {
  createDialog.url = "";
  createDialog.name = "";
  createDialog.cron = String(payload.value.settings?.default_cron || DEFAULT_SUBSCRIPTION_SETTINGS.default_cron);
}

async function load({ silent = false } = {}) {
  try {
    const response = await apiRequest("/api/subscriptions");
    payload.value = normalizeSubscriptionsPayload(response);
    initialLoaded.value = true;
    rememberSubscriptionsPage();
    syncAutoRefresh();
  } catch (error) {
    if (!silent) {
      status.value = error instanceof Error ? error.message : "订阅数据加载失败。";
    }
  }
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
    payload.value = normalizeSubscriptionsPayload(response.subscriptions || {});
    rememberSubscriptionsPage();
    syncAutoRefresh();
    status.value = response.message || "订阅已创建。";
    closeCreateDialog(true);
    resetCreateForm();
    await load({ silent: true });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "创建订阅失败。";
  } finally {
    creating.value = false;
  }
}

onMounted(async () => {
  hydrateSubscriptionsPageFromCache();
  await load();
});

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>
