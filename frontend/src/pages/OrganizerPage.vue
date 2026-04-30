<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">本地库</span>
      <h1>本地库管理</h1>
      <p>集中查看本地收藏、已打印、源端删除和本地删除状态。本地整理配置已移动到设置页。</p>
    </div>
    <div class="intro-stats organizer-intro-stats">
      <div class="intro-stat">
        <span>状态卡片</span>
        <strong>{{ localStateCards.length }}</strong>
      </div>
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
    </div>
  </section>

  <section class="library-section">
    <div class="library-section__head">
      <div>
        <h2>本地状态</h2>
        <p>{{ localStateCards.length }} 张卡片</p>
      </div>
      <div class="filter-actions">
        <button class="button button-secondary button-small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中..." : "刷新" }}
        </button>
      </div>
    </div>

    <div v-if="localStateCards.length" class="source-library-grid">
      <SourceLibraryCard
        v-for="card in localStateCards"
        :key="card.key"
        :card="card"
        @open="openCard"
      />
    </div>
    <section v-else class="surface empty-state subscription-inline-empty">
      <h2>本地状态为空</h2>
      <p>当前还没有本地收藏、已打印、源端删除或本地删除模型。</p>
    </section>
  </section>

  <section class="surface section-card organizer-layout">
    <div class="section-card__header">
      <div>
        <span class="eyebrow">本地整理</span>
        <h2>运行状态</h2>
      </div>
      <div class="filter-actions">
        <RouterLink class="button button-secondary button-small" :to="{ path: '/settings', query: { tab: 'organizer' } }">
          本地整理设置
        </RouterLink>
        <RouterLink class="button button-secondary button-small" :to="{ path: '/logs', query: { file: 'business.log', q: 'organizer' } }">
          任务历史
        </RouterLink>
      </div>
    </div>

    <div class="settings-grid settings-grid--three">
      <article class="field-card system-update-detail">
        <span>扫描目录</span>
        <strong>{{ organizerConfig.source_dir || "/app/local" }}</strong>
      </article>
      <article class="field-card system-update-detail">
        <span>目标目录</span>
        <strong>{{ organizerConfig.target_dir || "/app/archive" }}</strong>
      </article>
      <article class="field-card system-update-detail">
        <span>文件处理</span>
        <strong>{{ organizerConfig.move_files === false ? "复制" : "移动" }}</strong>
      </article>
    </div>

    <div class="settings-grid settings-grid--three">
      <article class="field-card system-update-stat">
        <span>候选 3MF</span>
        <strong>{{ organizerTasks.detected_total || 0 }}</strong>
        <small>扫描目录内等待整理的文件</small>
      </article>
      <article class="field-card system-update-stat">
        <span>活跃任务</span>
        <strong>{{ activeOrganizeCount }}</strong>
        <small>{{ organizerTasks.running_count || 0 }} 运行中 / {{ organizerTasks.queued_count || 0 }} 排队中</small>
      </article>
      <article class="field-card system-update-stat">
        <span>最近状态</span>
        <strong>{{ latestOrganizeTask?.status || "空闲" }}</strong>
        <small>{{ latestOrganizeTask?.message || "当前没有本地整理任务。" }}</small>
      </article>
    </div>

    <p class="archive-form__hint">
      完整本地整理任务历史已移动到日志中心；这里仅保留当前状态摘要和入口。
    </p>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RouterLink, useRouter } from "vue-router";

import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { appState, refreshConfig } from "../lib/appState";
import { createEmptySubscriptionsPayload, normalizeSubscriptionsPayload } from "../lib/subscriptions";


const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const IDLE_REFRESH_INTERVAL_MS = 30000;

const router = useRouter();
const subscriptionPayload = ref(createEmptySubscriptionsPayload());
const organizerTasks = ref({
  items: [],
  count: 0,
  queued_count: 0,
  running_count: 0,
  detected_total: 0,
});
const loading = ref(false);
let refreshTimer = null;
let disposed = false;

const organizerConfig = computed(() => appState.config?.organizer || {});
const localStateSection = computed(() => (
  subscriptionPayload.value.sections.find((section) => section?.key === "subscription_states") || { items: [] }
));
const localStateCards = computed(() => (
  Array.isArray(localStateSection.value.items) ? localStateSection.value.items : []
));
const activeOrganizeCount = computed(() => (
  Number(organizerTasks.value.running_count || 0) + Number(organizerTasks.value.queued_count || 0)
));
const latestOrganizeTask = computed(() => organizerTasks.value.items?.[0] || null);

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
    const [tasksPayload, subscriptionsPayload] = await Promise.all([
      apiRequest("/api/tasks"),
      apiRequest("/api/subscriptions"),
      refreshConfig(),
    ]);
    organizerTasks.value = tasksPayload?.organize_tasks || organizerTasks.value;
    subscriptionPayload.value = normalizeSubscriptionsPayload(subscriptionsPayload);
  } catch (error) {
    if (!silent) {
      console.error("本地库数据加载失败", error);
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
