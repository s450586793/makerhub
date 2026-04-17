<template>
  <section class="surface empty-state">
    <h2>{{ loading ? "正在准备预览" : "暂无可预览模型" }}</h2>
    <p>{{ status }}</p>
  </section>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { apiRequest } from "../lib/api";


const router = useRouter();
const loading = ref(true);
const status = ref("请稍候。");

onMounted(async () => {
  try {
    const payload = await apiRequest("/api/models");
    const first = payload.items?.[0];
    if (first?.model_dir) {
      router.replace({
        name: "model-detail",
        params: {
          modelDir: String(first.model_dir || ""),
        },
      });
      return;
    }
    status.value = "当前还没有归档模型，无法生成详情预览。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "读取预览失败。";
  } finally {
    loading.value = false;
  }
});
</script>
