<template>
  <details ref="menu" class="model-tag-filter" @toggle="handleToggle" @focusout="handleFocusOut" @keydown.esc.prevent="close">
    <summary aria-label="标签筛选" :title="selectedLabel">{{ selectedLabel }}</summary>
    <div class="model-tag-filter__menu" :style="menuStyle">
      <input ref="searchInput" v-model="query" type="search" aria-label="搜索标签" placeholder="搜索标签" maxlength="200" @input="scheduleSearch" @keydown.enter.prevent="search">
      <select :value="modelValue" size="8" aria-label="标签候选" @change="choose($event.target.value)">
        <option v-for="option in stateOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
        <option v-if="modelValue && !stateOptions.some(option => option.value === modelValue) && !items.includes(modelValue)" :value="modelValue">{{ modelValue }}</option>
        <option v-for="tag in items" :key="tag" :value="tag">{{ tag }}</option>
      </select>
      <span v-if="loading" role="status">正在加载...</span>
      <span v-else-if="error" role="alert">{{ error }}</span>
      <span v-else-if="hasMore">还有更多标签，请搜索</span>
      <span v-else-if="!items.length">没有匹配的标签</span>
    </div>
  </details>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onDeactivated, ref } from "vue";
import { apiRequest } from "../lib/api";
import { createHydratedResource } from "../lib/useHydratedResource";

const props = defineProps({ modelValue: { type: String, default: "" } });
const emit = defineEmits(["update:modelValue", "change"]);
const stateOptions = [
  { value: "", label: "全部标签" },
  { value: "__favorite__", label: "收藏" },
  { value: "__printed__", label: "已打印" },
  { value: "__source_deleted__", label: "源端删除" },
  { value: "__local_deleted__", label: "本地删除" },
];
const selectedLabel = computed(() => props.modelValue ? stateOptions.find(option => option.value === props.modelValue)?.label || props.modelValue : "标签");
const menu = ref(null);
const searchInput = ref(null);
const query = ref("");
const items = ref([]);
const hasMore = ref(false);
const loading = ref(false);
const error = ref("");
const menuStyle = ref({});
let searchTimer = null;
const resource = createHydratedResource({
  load: ({ signal }) => apiRequest(`/api/models/tags?${new URLSearchParams({ q: query.value, limit: "50" })}`, { signal }),
  onData: (response) => {
    items.value = response?.items || [];
    hasMore.value = Boolean(response?.has_more);
  },
  onLoading: value => { loading.value = value; },
});

async function search() {
  error.value = "";
  try {
    await resource.load();
  } catch (failure) {
    error.value = failure instanceof Error ? failure.message : "标签加载失败";
  }
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  resource.cancel();
  items.value = [];
  hasMore.value = false;
  searchTimer = setTimeout(search, 250);
}

async function handleToggle() {
  if (menu.value?.open) {
    const bounds = menu.value.getBoundingClientRect();
    const width = Math.min(260, window.innerWidth - 24);
    const left = Math.max(12 - bounds.left, Math.min(0, window.innerWidth - bounds.left - width - 12));
    menuStyle.value = { left: `${left}px` };
    window.addEventListener("resize", close);
    query.value = "";
    void search();
    await nextTick();
    searchInput.value?.focus();
  } else {
    window.removeEventListener("resize", close);
    clearTimeout(searchTimer);
    resource.cancel();
  }
}

function close() {
  window.removeEventListener("resize", close);
  clearTimeout(searchTimer);
  resource.cancel();
  if (menu.value) menu.value.open = false;
}

function choose(value) {
  emit("update:modelValue", value);
  emit("change");
  close();
  menu.value?.querySelector("summary")?.focus();
}

function handleFocusOut(event) {
  if (!event.currentTarget.contains(event.relatedTarget)) close();
}

onDeactivated(close);
onBeforeUnmount(close);
</script>

<style scoped>
.model-tag-filter { position: relative; min-width: 0; width: 100%; }
.model-tag-filter summary { height: 46px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--input-bg); color: var(--text); cursor: pointer; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.model-tag-filter__menu { position: absolute; top: calc(100% + 4px); left: 0; z-index: 30; width: 260px; max-width: calc(100vw - 24px); padding: 10px; display: grid; gap: 8px; background: var(--surface-floating); border: 1px solid var(--border-strong); border-radius: 6px; }
.model-tag-filter__menu input, .model-tag-filter__menu select { width: 100%; min-width: 0; }
.model-tag-filter__menu select { height: 224px; }
.model-tag-filter__menu option { padding: 6px; overflow: hidden; text-overflow: ellipsis; }
.model-tag-filter__menu span { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 760px) {
  .model-tag-filter summary { height: 40px; font-size: 13px; }
}
</style>
