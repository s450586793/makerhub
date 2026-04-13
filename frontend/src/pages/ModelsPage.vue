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
        <h2>当前结果 {{ payload.count }} / 总模型 {{ payload.total }}</h2>
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
    />
  </section>

  <section v-else class="surface empty-state">
    <h2>还没有匹配的模型</h2>
    <p>当前筛选条件下没有命中结果。你可以重置条件，或者先去任务页发起新的归档任务。</p>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import ModelCard from "../components/ModelCard.vue";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();

const payload = ref({
  items: [],
  count: 0,
  total: 0,
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

const selectedCount = computed(() => selectedSet.value.size);

async function load() {
  filters.q = typeof route.query.q === "string" ? route.query.q : "";
  filters.source = typeof route.query.source === "string" ? route.query.source : "all";
  filters.tag = typeof route.query.tag === "string" ? route.query.tag : "";
  filters.sort = typeof route.query.sort === "string" ? route.query.sort : "collectDate";

  const query = new URLSearchParams();
  if (filters.q) query.set("q", filters.q);
  if (filters.source && filters.source !== "all") query.set("source", filters.source);
  if (filters.tag) query.set("tag", filters.tag);
  if (filters.sort && filters.sort !== "collectDate") query.set("sort", filters.sort);

  payload.value = await apiRequest(`/api/models${query.toString() ? `?${query.toString()}` : ""}`);

  const available = new Set(payload.value.items.map((item) => item.model_dir));
  selectedSet.value = new Set([...selectedSet.value].filter((item) => available.has(item)));
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
    await load();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "删除失败。";
  } finally {
    deleting.value = false;
  }
}

watch(() => route.fullPath, load);
onMounted(load);
</script>
