<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">远端刷新</span>
      <h1>远端刷新配置</h1>
      <p>控制远端评论、附件、打印配置与源端删除标记的分批同步节奏。</p>
    </div>
  </section>

  <section class="surface">
    <form class="settings-form token-card" @submit.prevent="saveRemoteRefresh">
      <div class="settings-grid settings-grid--three">
        <label class="field-card">
          <span>启用远端刷新</span>
          <label class="switch">
            <input v-model="remoteRefreshForm.enabled" type="checkbox">
            <span>默认开启。仅对模型库内已有远端来源链接的模型做增量刷新。</span>
          </label>
        </label>
        <label class="field-card">
          <span>Cron</span>
          <input v-model.trim="remoteRefreshForm.cron" type="text" placeholder="0 */2 * * *">
        </label>
        <label class="field-card">
          <span>单轮数量</span>
          <input v-model.number="remoteRefreshForm.batch_size" type="number" min="1" max="200" placeholder="12">
        </label>
      </div>
      <p class="archive-form__hint">
        启用后会按计划分批刷新远端评论、附件与打印配置。已成功下载过的 3MF 不会重复下载。
        如果模型总数较多，可以缩短 Cron 或增大单轮数量。
      </p>
      <div class="settings-grid settings-grid--three">
        <label class="field-card">
          <span>当前状态</span>
          <strong>{{ formatRemoteRefreshStatus(remoteRefreshState.status) }}</strong>
        </label>
        <label class="field-card">
          <span>下次运行</span>
          <strong>{{ remoteRefreshState.next_run_at || "-" }}</strong>
        </label>
        <label class="field-card">
          <span>上次结果</span>
          <strong>{{ remoteRefreshState.last_message || "-" }}</strong>
        </label>
      </div>
      <div class="form-footer">
        <button class="button button-primary" type="submit">保存远端刷新设置</button>
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
const remoteRefreshState = computed(() => config.value?.remote_refresh_state || {});
const status = ref("");
const remoteRefreshForm = reactive({
  enabled: true,
  cron: "0 */2 * * *",
  batch_size: 12,
});

function applyConfig(payload) {
  remoteRefreshForm.enabled = payload?.remote_refresh?.enabled !== false;
  remoteRefreshForm.cron = payload?.remote_refresh?.cron || "0 */2 * * *";
  remoteRefreshForm.batch_size = Number(payload?.remote_refresh?.batch_size || 12);
}

function formatRemoteRefreshStatus(value) {
  const mapping = {
    idle: "空闲",
    running: "运行中",
    error: "异常",
    disabled: "已停用",
  };
  return mapping[String(value || "").trim()] || "空闲";
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfig(payload);
}

async function saveRemoteRefresh() {
  try {
    await apiRequest("/api/config/remote-refresh", {
      method: "POST",
      body: {
        enabled: remoteRefreshForm.enabled,
        cron: remoteRefreshForm.cron,
        batch_size: Number(remoteRefreshForm.batch_size || 12),
      },
    });
    await refreshConfig();
    status.value = "远端刷新设置已保存。";
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  }
}

onMounted(load);
</script>
