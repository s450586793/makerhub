<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">整理</span>
      <h1>本地整理配置</h1>
      <p>控制 `/app/local` 的扫描来源、归档目标和移动模式。</p>
    </div>
  </section>

  <section class="surface">
    <form class="settings-form" @submit.prevent="saveOrganizer">
      <div class="settings-grid settings-grid--two">
        <label class="field-card">
          <span>本地整理扫描目录</span>
          <input v-model="organizerForm.source_dir" type="text" placeholder="/app/local">
        </label>
        <label class="field-card">
          <span>整理目标目录</span>
          <input v-model="organizerForm.target_dir" type="text" placeholder="/app/archive">
        </label>
      </div>
      <label class="field-card">
        <span>整理模式</span>
        <label class="switch">
          <input v-model="organizerForm.move_files" type="checkbox">
          <span>启用后移动文件，而不是复制文件</span>
        </label>
      </label>
      <div class="form-footer">
        <button class="button button-primary" type="submit">保存整理配置</button>
        <span class="form-status">{{ status }}</span>
      </div>
    </form>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from "vue";

import { appState, refreshConfig } from "../lib/appState";
import { apiRequest } from "../lib/api";


const config = computed(() => appState.config);
const status = ref("");
const organizerForm = reactive({
  source_dir: "",
  target_dir: "",
  move_files: true,
});

function applyConfig(payload) {
  organizerForm.source_dir = payload?.organizer?.source_dir || "";
  organizerForm.target_dir = payload?.organizer?.target_dir || "";
  organizerForm.move_files = payload?.organizer?.move_files !== false;
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfig(payload);
}

async function saveOrganizer() {
  try {
    await apiRequest("/api/config/organizer", {
      method: "POST",
      body: { ...organizerForm },
    });
    await refreshConfig();
    status.value = "整理配置已保存。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  }
}

onMounted(load);
</script>
