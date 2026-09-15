<template>
  <section class="mw-reference-aside">
    <div v-if="!isLocal" class="mw-license">
      <h2>许可证</h2>
      <div class="mw-license-body">
        <ShieldCheck :size="28" aria-hidden="true" />
        <div>
          <p>{{ licenseText || "归档中未记录许可证" }}</p>
          <a v-if="sourceUrl" :href="sourceUrl" target="_blank" rel="noreferrer">查看源站许可 <ExternalLink :size="12" /></a>
        </div>
      </div>
    </div>
    <div ref="relatedRoot" class="mw-related">
      <h2>相关模型</h2>
      <p v-if="loading" class="empty-copy" role="status">加载中…</p>
      <button v-else-if="error" class="mw-related-retry" type="button" @click="loadRelated">{{ error }} <RefreshCw :size="14" /></button>
      <div v-else-if="items.length" class="mw-related-list">
        <RouterLink v-for="model in items" :key="model.model_dir" :to="model.detail_path" class="mw-related-item">
          <img :src="model.cover_url" :alt="model.title" loading="lazy">
          <div>
            <h3>{{ model.title }}</h3>
            <span class="mw-related-author">{{ model.author?.name || "未知作者" }}</span>
            <span class="mw-related-stats"><Download :size="13" /> {{ compact(model.stats?.downloads) }} <ThumbsUp :size="13" /> {{ compact(model.stats?.likes) }}</span>
          </div>
        </RouterLink>
      </div>
      <p v-else class="empty-copy">暂无相关归档</p>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { Download, ExternalLink, RefreshCw, ShieldCheck, ThumbsUp } from "@lucide/vue";
import { apiRequest } from "../lib/api";
import { safeExternalUrl } from "../lib/modelDetail";

const props = defineProps({ detail: { type: Object, required: true } });
const relatedRoot = ref(null);
const items = shallowRef([]);
const loading = ref(false);
const error = ref("");
const isLocal = computed(() => props.detail.source === "local");
const sourceUrl = computed(() => safeExternalUrl(props.detail.origin_url));
const licenseText = computed(() => props.detail.license || "");
let observer;
let controller;
let visible = false;
let generation = 0;
const compact = value => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value) || 0);

async function loadRelated() {
  const current = ++generation;
  controller?.abort();
  controller = new AbortController();
  items.value = [];
  error.value = "";
  loading.value = true;
  const params = new URLSearchParams({ page: "1", page_size: "7", sort: "likes" });
  const tag = props.detail.tags?.find(tag => tag && tag !== "本地导入");
  if (tag) params.set("tag", tag);
  else if (props.detail.author?.name && props.detail.author.name !== "未知作者") params.set("q", props.detail.author.name);
  else {
    loading.value = false;
    return;
  }
  try {
    const payload = await apiRequest(`/api/models/light?${params}`, { signal: controller.signal });
    if (current !== generation) return;
    items.value = (payload.items || []).filter(item => item.model_dir !== props.detail.model_dir && item.detail_path).slice(0, 6);
  } catch (cause) {
    if (current === generation && cause.name !== "AbortError") error.value = "加载失败，点击重试";
  } finally {
    if (current === generation) loading.value = false;
  }
}

watch(() => props.detail.model_dir, () => { if (visible) loadRelated(); });
onMounted(() => {
  if (!("IntersectionObserver" in window)) { visible = true; loadRelated(); return; }
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      visible = true;
      observer.disconnect();
      loadRelated();
    }
  }, { rootMargin: "120px" });
  observer.observe(relatedRoot.value);
});
onBeforeUnmount(() => { generation += 1; observer?.disconnect(); controller?.abort(); });
</script>
