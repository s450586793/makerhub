<template>
  <template v-if="loading">
    <section class="surface empty-state">
      <h2>正在加载首页数据</h2>
      <p>请稍候。</p>
    </section>
  </template>

  <template v-else>
    <section class="stats-grid">
      <article v-for="card in payload.stats" :key="card.label" class="surface stat-card">
        <span>{{ card.label }}</span>
        <strong>{{ card.value }}</strong>
        <small>{{ card.hint }}</small>
      </article>
    </section>

    <section class="dashboard-layout">
      <article class="surface section-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">最近模型</span>
            <h2>最近采集的 8 个模型</h2>
          </div>
          <RouterLink class="section-link" to="/models">查看全部</RouterLink>
        </div>
        <div v-if="payload.recent_models?.length" class="mini-model-grid">
          <RouterLink
            v-for="model in payload.recent_models"
            :key="model.model_dir"
            class="mini-model-card"
            :to="model.detail_path"
          >
            <img
              v-if="model.cover_url"
              :src="model.cover_url"
              :alt="model.title"
              loading="lazy"
            >
            <div v-else class="media-placeholder">{{ model.title.slice(0, 1) }}</div>
            <div>
              <strong>{{ model.title }}</strong>
              <span>{{ model.author.name }} · {{ model.collect_date || "未知时间" }}</span>
            </div>
          </RouterLink>
        </div>
        <p v-else class="empty-copy">当前还没有归档模型，后续归档完成后这里会自动展示最新内容。</p>
      </article>

      <article class="surface section-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">系统状态</span>
            <h2>连接与代理状态</h2>
          </div>
          <RouterLink class="section-link" to="/settings?tab=connections">去设置</RouterLink>
        </div>
        <div class="status-list">
          <div v-for="item in payload.system_status" :key="item.title" class="status-item">
            <div>
              <strong>{{ item.title }}</strong>
              <span>{{ item.status }}</span>
            </div>
            <span :class="['status-dot', item.enabled && 'is-ok']"></span>
          </div>
        </div>
      </article>

      <article class="surface section-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">任务摘要</span>
            <h2>当前运行情况</h2>
          </div>
          <RouterLink class="section-link" to="/tasks">展开任务页</RouterLink>
        </div>
        <div class="summary-stack">
          <div class="summary-box">
            <strong>运行中任务</strong>
            <span>{{ payload.task_summary?.running?.length || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>排队中任务</strong>
            <span>{{ payload.task_summary?.queued_count || 0 }} 个</span>
          </div>
          <div class="summary-box">
            <strong>待补 3MF</strong>
            <span>{{ payload.task_summary?.missing_3mf?.length || 0 }} 项</span>
          </div>
        </div>

        <div v-if="payload.task_summary?.recent_failures?.length" class="summary-list">
          <h3>最近失败</h3>
          <div
            v-for="item in payload.task_summary.recent_failures"
            :key="item.id || item.title || item.url"
            class="summary-list__item"
          >
            <strong>{{ item.title || item.url || "未命名任务" }}</strong>
            <span>{{ item.message || "失败原因未记录" }}</span>
          </div>
        </div>
      </article>
    </section>
  </template>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { RouterLink } from "vue-router";

import { apiRequest } from "../lib/api";


const loading = ref(true);
const payload = ref({
  stats: [],
  recent_models: [],
  system_status: [],
  task_summary: {
    running: [],
    queued_count: 0,
    recent_failures: [],
    missing_3mf: [],
  },
});

async function load() {
  loading.value = true;
  try {
    payload.value = await apiRequest("/api/dashboard");
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>
