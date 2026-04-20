<template>
  <section class="surface surface--filters">
    <form class="filter-bar" @submit.prevent="applyFilters">
      <label class="filter-field filter-field--wide">
        <input
          v-model.trim="filters.q"
          type="text"
          aria-label="搜索来源卡片"
          placeholder="搜索作者、合集、收藏夹、本地状态"
          @blur="applyFiltersIfChanged"
          @keydown.enter.prevent="applyFilters"
        >
      </label>
      <div class="filter-actions">
        <button class="button button-secondary" type="button" @click="resetFilters">重置</button>
      </div>
    </form>
    <span v-if="status" class="form-status model-toolbar-inline__status">{{ status }}</span>
  </section>

  <template v-if="loaded && visibleSections.length">
    <section
      v-for="section in visibleSections"
      :key="section.key"
      class="library-section"
    >
      <div class="library-section__head">
        <div>
          <h2>{{ section.label }}</h2>
          <p>{{ section.count }} 张卡片</p>
        </div>
      </div>

      <div class="source-library-grid">
        <SourceLibraryCard
          v-for="card in section.items"
          :key="card.key"
          :card="card"
          @open="openCard"
        />
      </div>
    </section>
  </template>

  <section v-else-if="loaded" class="surface empty-state">
    <h2>没有匹配的来源卡片</h2>
    <p>当前搜索条件没有命中任何作者、合集、收藏夹或状态卡，你可以清空搜索后再看一次。</p>
  </section>

  <section v-else class="surface empty-state">
    <h2>正在加载模型库</h2>
    <p>稍等，正在整理作者、合集、收藏夹和状态卡片。</p>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();

const payload = ref({
  sections: [],
  count: 0,
  filters: { q: "" },
  summary: { card_count: 0, model_count: 0 },
});
const filters = reactive({
  q: "",
});
const status = ref("");
const loaded = ref(false);

const visibleSections = computed(() => (payload.value.sections || []).filter((section) => (section.items || []).length));

function syncFiltersFromRoute() {
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
}

function buildRouteQuery() {
  return {
    q: filters.q || undefined,
  };
}

async function load() {
  syncFiltersFromRoute();
  const query = new URLSearchParams();
  if (filters.q) {
    query.set("q", filters.q);
  }
  payload.value = await apiRequest(`/api/source-library${query.toString() ? `?${query.toString()}` : ""}`);
  loaded.value = true;
}

function applyFilters() {
  router.replace({ path: "/models", query: buildRouteQuery() });
}

function applyFiltersIfChanged() {
  const nextQuery = buildRouteQuery();
  const currentQuery = {
    q: typeof route.query.q === "string" ? route.query.q : undefined,
  };
  if (currentQuery.q === nextQuery.q) {
    return;
  }
  applyFilters();
}

function resetFilters() {
  router.replace("/models");
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

watch(() => route.fullPath, () => {
  status.value = "";
  void load().catch((error) => {
    status.value = error instanceof Error ? error.message : "来源卡片加载失败。";
    loaded.value = true;
  });
});

onMounted(async () => {
  try {
    await load();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "来源卡片加载失败。";
    loaded.value = true;
  }
});
</script>
