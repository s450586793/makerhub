<template>
  <section class="page-intro page-intro--compact">
    <div>
      <span class="eyebrow">订阅</span>
      <h1>作者与收藏夹订阅</h1>
    </div>
    <div class="intro-stats">
      <div class="intro-stat">
        <span>订阅总数</span>
        <strong>{{ payload.count }}</strong>
      </div>
      <div class="intro-stat">
        <span>启用中</span>
        <strong>{{ payload.summary.enabled }}</strong>
      </div>
      <div class="intro-stat">
        <span>同步中</span>
        <strong>{{ payload.summary.running }}</strong>
      </div>
    </div>
  </section>

  <section class="surface section-card section-card--compact subscriptions-create-card">
    <div class="section-card__header section-card__header--compact">
      <div>
        <span class="eyebrow">新增订阅</span>
        <h2>创建新的定时订阅</h2>
      </div>
    </div>
    <form class="subscription-create-form subscription-create-form--compact" @submit.prevent="createSubscription">
      <label class="filter-field filter-field--wide">
        <input v-model.trim="createForm.url" type="text" placeholder="作者上传页或收藏夹模型页链接">
      </label>
      <label class="filter-field">
        <input v-model.trim="createForm.name" type="text" placeholder="订阅名称（可选）">
      </label>
      <label class="filter-field">
        <input v-model.trim="createForm.cron" type="text" placeholder="Cron，例如 0 */6 * * *">
      </label>
      <label class="subscription-toggle subscription-toggle--compact">
        <input v-model="createForm.enabled" type="checkbox">
        <span>启用</span>
      </label>
      <div class="subscription-create-form__actions">
        <label class="subscription-toggle subscription-toggle--compact subscription-toggle--wide">
          <input v-model="createForm.initialize_from_source" type="checkbox">
          <span>创建时初始化当前源页面</span>
        </label>
        <button class="button button-primary" type="submit" :disabled="creating">
          {{ creating ? "提交中..." : "添加订阅" }}
        </button>
      </div>
    </form>
    <p class="archive-form__hint">创建后会按 Cron 定时扫描目标链接；如果链接里的模型被删除，模型库只做标记，不删除本地归档。</p>
    <span class="form-status">{{ status }}</span>
  </section>

  <section v-if="payload.items.length" class="subscription-grid">
    <article v-for="item in payload.items" :key="item.id" class="surface section-card subscription-card">
      <div class="section-card__header subscription-card__header">
        <div>
          <span class="eyebrow">{{ item.mode === "collection_models" ? "收藏夹" : "作者页" }}</span>
          <h2>{{ item.draft_name || item.name }}</h2>
        </div>
        <div class="subscription-card__status">
          <span :class="['count-pill', item.enabled && 'count-pill--ok']">{{ item.enabled ? "已启用" : "已停用" }}</span>
          <span :class="['count-pill', item.running && 'count-pill--warn']">{{ item.running ? "同步中" : item.status || "idle" }}</span>
        </div>
      </div>

      <div class="subscription-card__meta">
        <strong>链接</strong>
        <a :href="item.url" target="_blank" rel="noreferrer">{{ item.url }}</a>
      </div>

      <div class="subscription-card__form">
        <label class="filter-field">
          <input v-model.trim="item.draft_name" type="text" placeholder="订阅名称">
        </label>
        <label class="filter-field">
          <input v-model.trim="item.draft_cron" type="text" placeholder="Cron">
        </label>
        <label class="subscription-toggle">
          <input v-model="item.draft_enabled" type="checkbox">
          <span>启用</span>
        </label>
      </div>

      <div class="subscription-stats">
        <div class="summary-box">
          <strong>当前源模型</strong>
          <span>{{ item.current_count }}</span>
        </div>
        <div class="summary-box">
          <strong>累计跟踪</strong>
          <span>{{ item.tracked_count }}</span>
        </div>
        <div class="summary-box">
          <strong>上次新增</strong>
          <span>{{ item.last_new_count }}</span>
        </div>
        <div class="summary-box">
          <strong>源端删除</strong>
          <span>{{ item.deleted_count }}</span>
        </div>
      </div>

      <div class="subscription-times">
        <span><strong>下次运行</strong><em>{{ formatDateTime(item.next_run_at) }}</em></span>
        <span><strong>上次运行</strong><em>{{ formatDateTime(item.last_run_at) }}</em></span>
      </div>

      <p class="subscription-card__message">{{ item.last_message || "等待首次同步。" }}</p>

      <div class="subscription-actions">
        <button class="button button-secondary button-small" type="button" :disabled="busyId === item.id" @click="saveSubscription(item)">
          保存
        </button>
        <button class="button button-secondary button-small" type="button" :disabled="busyId === item.id || item.running" @click="syncSubscription(item)">
          立即同步
        </button>
        <button class="button button-danger button-small" type="button" :disabled="busyId === item.id" @click="deleteSubscription(item)">
          删除
        </button>
      </div>
    </article>
  </section>

  <section v-else class="surface empty-state">
    <h2>还没有订阅</h2>
    <p>你可以在这里添加作者页或收藏夹订阅，后续会按 Cron 自动同步新模型。</p>
  </section>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from "vue";

import { apiRequest } from "../lib/api";


const payload = ref({
  items: [],
  count: 0,
  summary: {
    enabled: 0,
    running: 0,
  },
});
const createForm = reactive({
  url: "",
  name: "",
  cron: "0 */6 * * *",
  enabled: true,
  initialize_from_source: true,
});
const status = ref("");
const creating = ref(false);
const busyId = ref("");
let refreshTimer = null;

function normalizePayload(response) {
  payload.value = {
    ...response,
    items: (response.items || []).map((item) => ({
      ...item,
      draft_name: item.name,
      draft_cron: item.cron,
      draft_enabled: Boolean(item.enabled),
    })),
  };
  syncAutoRefresh();
}

function syncAutoRefresh() {
  const hasRunning = payload.value.items.some((item) => item.running);
  if (hasRunning && !refreshTimer) {
    refreshTimer = window.setInterval(load, 5000);
    return;
  }
  if (!hasRunning && refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

async function load() {
  const response = await apiRequest("/api/subscriptions");
  normalizePayload(response);
}

async function createSubscription() {
  if (!createForm.url) {
    status.value = "请先输入作者页或收藏夹链接。";
    return;
  }
  creating.value = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/subscriptions", {
      method: "POST",
      body: createForm,
    });
    status.value = response.message || "订阅已创建。";
    createForm.url = "";
    createForm.name = "";
    createForm.cron = "0 */6 * * *";
    createForm.enabled = true;
    createForm.initialize_from_source = true;
    normalizePayload(response.subscriptions || { items: [], count: 0, summary: { enabled: 0, running: 0 } });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "创建订阅失败。";
  } finally {
    creating.value = false;
  }
}

async function saveSubscription(item) {
  busyId.value = item.id;
  status.value = "";
  try {
    const response = await apiRequest(`/api/subscriptions/${item.id}`, {
      method: "PUT",
      body: {
        name: item.draft_name,
        cron: item.draft_cron,
        enabled: item.draft_enabled,
      },
    });
    status.value = response.message || "订阅已更新。";
    await load();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  } finally {
    busyId.value = "";
  }
}

async function syncSubscription(item) {
  busyId.value = item.id;
  status.value = "";
  try {
    const response = await apiRequest(`/api/subscriptions/${item.id}/sync`, {
      method: "POST",
    });
    status.value = response.message || "同步已触发。";
    await load();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "触发同步失败。";
  } finally {
    busyId.value = "";
  }
}

async function deleteSubscription(item) {
  if (!window.confirm(`确认删除订阅「${item.name}」吗？`)) {
    return;
  }
  busyId.value = item.id;
  status.value = "";
  try {
    const response = await apiRequest(`/api/subscriptions/${item.id}`, {
      method: "DELETE",
    });
    status.value = response.message || "订阅已删除。";
    normalizePayload(response.subscriptions || { items: [], count: 0, summary: { enabled: 0, running: 0 } });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "删除失败。";
  } finally {
    busyId.value = "";
  }
}

function formatDateTime(value) {
  if (!value) {
    return "未安排";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

onMounted(load);

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>
