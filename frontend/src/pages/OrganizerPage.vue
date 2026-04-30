<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">本地库</span>
      <h1>本地库管理</h1>
      <p>集中查看本地 3MF 导入入口、整理配置和运行状态。</p>
    </div>
    <div class="intro-stats organizer-intro-stats">
      <div class="intro-stat">
        <span>是否空闲</span>
        <strong>{{ organizerStatusText }}</strong>
      </div>
      <div class="intro-stat">
        <span>候选 3MF</span>
        <strong>{{ detectedTotalText }}</strong>
      </div>
      <div class="intro-stat">
        <span>运行中</span>
        <strong>{{ runningCountText }}</strong>
      </div>
      <div class="intro-stat">
        <span>排队中</span>
        <strong>{{ queuedCountText }}</strong>
      </div>
    </div>
  </section>

  <section v-if="!initialLoaded && !loadError" class="surface empty-state subscription-inline-empty">
    <h2>正在加载本地库</h2>
    <p>正在读取本地整理入口和本地状态卡片。</p>
  </section>

  <section v-else-if="loadError" class="surface empty-state subscription-inline-empty">
    <h2>本地库加载失败</h2>
    <p>{{ loadError }}</p>
    <button class="button button-secondary" type="button" :disabled="loading" @click="load()">
      {{ loading ? "刷新中..." : "重新加载" }}
    </button>
  </section>

  <section v-else class="library-section">
    <div class="library-section__head">
      <div aria-hidden="true"></div>
      <div class="filter-actions">
        <RouterLink class="button button-primary button-small" :to="{ path: '/settings', query: { tab: 'organizer' } }">
          导入
        </RouterLink>
      </div>
    </div>

    <div v-if="localLibraryCards.length" class="source-library-grid">
      <SourceLibraryCard
        v-for="card in localLibraryCards"
        :key="card.key"
        :card="card"
        @open="openCard"
      />
    </div>
    <section v-else class="surface empty-state subscription-inline-empty">
      <h2>本地整理为空</h2>
      <p>当前还没有本地整理导入模型。</p>
    </section>
  </section>

</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";

import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { refreshConfig } from "../lib/appState";


const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const IDLE_REFRESH_INTERVAL_MS = 30000;

const router = useRouter();
const sourceLibraryPayload = ref({
  sections: [],
});
const organizerTasks = ref({
  items: [],
  count: 0,
  queued_count: 0,
  running_count: 0,
  detected_total: 0,
});
const loading = ref(false);
const initialLoaded = ref(false);
const loadError = ref("");
let refreshTimer = null;
let disposed = false;

const localSourceSection = computed(() => (
  sourceLibraryPayload.value.sections.find((section) => section?.key === "locals") || { items: [] }
));
const localStateSection = computed(() => (
  sourceLibraryPayload.value.sections.find((section) => section?.key === "states") || { items: [] }
));
const localOrganizerCard = computed(() => {
  const card = localSourceSection.value.items?.find((item) => item?.key === "local-organizer");
  if (card) {
    return {
      ...card,
      title: "本地整理",
    };
  }
  return {
    key: "local-organizer",
    kind: "local",
    card_kind: "collection",
    title: "本地整理",
    subtitle: "本地 3MF 归档",
    site: "local",
    site_badge: "LOCAL",
    route_kind: "source",
    model_count: 0,
    stats: [
      { label: "候选", value: Number(organizerTasks.value.detected_total || 0) },
      { label: "活跃", value: activeOrganizeCount.value },
    ],
    preview_models: [],
  };
});
const localStateCards = computed(() => (
  Array.isArray(localStateSection.value.items) ? localStateSection.value.items : []
));
const localLibraryCards = computed(() => (
  initialLoaded.value
    ? [localOrganizerCard.value, ...localStateCards.value]
    : []
));
const activeOrganizeCount = computed(() => (
  Number(organizerTasks.value.running_count || 0) + Number(organizerTasks.value.queued_count || 0)
));
const organizerStatusText = computed(() => {
  if (!initialLoaded.value && loadError.value) {
    return "失败";
  }
  if (!initialLoaded.value) {
    return "读取中";
  }
  return activeOrganizeCount.value > 0 ? "否" : "是";
});
const detectedTotalText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.detected_total || 0)
));
const runningCountText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.running_count || 0)
));
const queuedCountText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.queued_count || 0)
));

function clearTaskTimer() {
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function hasActiveOrganizeTasks() {
  return activeOrganizeCount.value > 0;
}

function syncTaskTimer() {
  clearTaskTimer();
  if (disposed || typeof window === "undefined" || document.hidden) {
    return;
  }
  const delay = hasActiveOrganizeTasks() ? ACTIVE_REFRESH_INTERVAL_MS : IDLE_REFRESH_INTERVAL_MS;
  refreshTimer = window.setTimeout(() => {
    void load({ silent: true });
  }, delay);
}

async function load({ silent = false } = {}) {
  if (loading.value) {
    return;
  }
  loading.value = true;
  try {
    const [tasksPayload, sourceLibraryPayloadResponse] = await Promise.all([
      apiRequest("/api/tasks"),
      apiRequest("/api/source-library"),
      refreshConfig(),
    ]);
    organizerTasks.value = tasksPayload?.organize_tasks || organizerTasks.value;
    sourceLibraryPayload.value = {
      sections: Array.isArray(sourceLibraryPayloadResponse?.sections) ? sourceLibraryPayloadResponse.sections : [],
    };
    loadError.value = "";
    initialLoaded.value = true;
  } catch (error) {
    if (!silent) {
      console.error("本地库数据加载失败", error);
      loadError.value = error instanceof Error ? error.message : "本地库数据加载失败。";
    }
  } finally {
    loading.value = false;
    syncTaskTimer();
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
    });
    return;
  }
  router.push({
    name: "model-library-source",
    params: {
      sourceType: String(card.kind || ""),
      sourceKey: String(card.key),
    },
  });
}

function handleVisibilityChange() {
  if (document.hidden) {
    clearTaskTimer();
    return;
  }
  void load({ silent: true });
}

onMounted(() => {
  disposed = false;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  void load();
});

onBeforeUnmount(() => {
  disposed = true;
  clearTaskTimer();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
