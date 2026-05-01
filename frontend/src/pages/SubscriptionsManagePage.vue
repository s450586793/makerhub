<template>
  <section class="page-intro page-intro--compact subscription-page-intro">
    <div>
      <span class="eyebrow">订阅库管理</span>
      <h1>订阅链接与同步明细</h1>
      <p>集中查看所有订阅来源的链接、Cron、同步状态和操作入口。</p>
    </div>
    <div class="subscription-page-intro__side">
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
      <div class="subscription-page-tools">
        <RouterLink class="button button-secondary" to="/subscriptions">返回</RouterLink>
        <button class="button button-secondary" type="button" @click="openSettingsDialog">订阅设置</button>
      </div>
    </div>
  </section>

  <p v-if="status" class="subscription-page-status">{{ status }}</p>

  <section class="library-section">
    <div class="library-section__head">
      <div>
        <h2>订阅库管理</h2>
        <p>{{ payload.count }} 条订阅记录</p>
      </div>
    </div>

    <section v-if="payload.items.length" class="subscription-grid">
      <article v-for="item in payload.items" :key="item.id" class="surface section-card subscription-card">
        <div class="section-card__header subscription-card__header">
          <div>
            <span class="eyebrow">{{ subscriptionModeLabel(item.mode) }}</span>
            <h2>{{ item.name }}</h2>
          </div>
          <div class="subscription-card__status">
            <button
              :class="['subscription-switch', item.enabled && 'is-on']"
              type="button"
              :disabled="busyId === item.id || item.running"
              @click="toggleSubscriptionEnabled(item)"
            >
              <span class="subscription-switch__track" aria-hidden="true">
                <span class="subscription-switch__thumb"></span>
              </span>
              <span class="subscription-switch__label">{{ item.enabled ? "启用中" : "已停用" }}</span>
            </button>
            <span :class="['count-pill', item.running && 'count-pill--warn']">{{ item.running ? "同步中" : item.status || "idle" }}</span>
          </div>
        </div>

        <div class="subscription-card__meta-grid">
          <div class="subscription-card__meta">
            <strong>链接</strong>
            <a :href="item.url" target="_blank" rel="noreferrer">{{ item.url }}</a>
          </div>
          <div class="subscription-card__meta">
            <strong>Cron</strong>
            <span>{{ item.cron }}</span>
          </div>
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
          <button class="button button-secondary button-small" type="button" :disabled="busyId === item.id" @click="openEditDialog(item)">
            编辑
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
      <p>你可以先回到订阅卡片页添加作者页、收藏夹或合集订阅。</p>
    </section>
  </section>

  <div
    v-if="editDialog.visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="subscription-edit-dialog-title"
    @click="closeEditDialog"
  >
    <div class="submit-dialog__panel subscription-edit-dialog__panel" @click.stop>
      <h2 id="subscription-edit-dialog-title">编辑订阅</h2>
      <p>在这里修改订阅链接、名称和 Cron 表达式。</p>
      <form class="subscription-edit-dialog__form" @submit.prevent="submitEditDialog">
        <label class="filter-field filter-field--wide">
          <input v-model.trim="editDialog.url" type="text" placeholder="作者上传页或收藏夹模型页链接">
        </label>
        <label class="filter-field">
          <input v-model.trim="editDialog.name" type="text" placeholder="订阅名称（可选）">
        </label>
        <CronField
          v-model="editDialog.cron"
          class="filter-field"
          placeholder="Cron，例如 0 */6 * * *"
          dialog-title="设置订阅编辑 Cron"
        />
        <div class="submit-dialog__actions">
          <button class="button button-secondary" type="button" :disabled="savingEdit" @click="closeEditDialog">
            取消
          </button>
          <button class="button button-primary" type="submit" :disabled="savingEdit">
            {{ savingEdit ? "保存中..." : "保存修改" }}
          </button>
        </div>
      </form>
    </div>
  </div>

  <div
    v-if="settingsDialog.visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="subscription-settings-dialog-title"
    @click="closeSettingsDialog"
  >
    <div class="submit-dialog__panel subscription-settings-dialog__panel" @click.stop>
      <h2 id="subscription-settings-dialog-title">订阅设置</h2>
      <p>这里控制新增订阅的默认行为，以及顶部来源卡片区的展示方式。</p>
      <form class="subscription-settings-form" @submit.prevent="saveSubscriptionSettings">
        <CronField
          v-model="settingsDialog.form.default_cron"
          class="filter-field"
          placeholder="Cron，例如 0 */6 * * *"
          dialog-title="设置默认订阅 Cron"
        />
        <label class="filter-field">
          <span>卡片排序</span>
          <select v-model="settingsDialog.form.card_sort">
            <option value="recent">最近更新优先</option>
            <option value="models">模型数优先</option>
            <option value="followers">粉丝数优先</option>
          </select>
        </label>
        <label class="subscription-toggle subscription-toggle--compact subscription-toggle--wide">
          <input v-model="settingsDialog.form.default_enabled" type="checkbox">
          <span class="subscription-toggle__copy subscription-toggle__copy--wide">新增订阅默认启用</span>
        </label>
        <label class="subscription-toggle subscription-toggle--compact subscription-toggle--wide">
          <input v-model="settingsDialog.form.hide_disabled_from_cards" type="checkbox">
          <span class="subscription-toggle__copy subscription-toggle__copy--wide">来源卡片区默认隐藏已停用订阅</span>
        </label>
        <div class="submit-dialog__actions">
          <button class="button button-secondary" type="button" :disabled="settingsDialog.saving" @click="closeSettingsDialog">
            取消
          </button>
          <button class="button button-primary" type="submit" :disabled="settingsDialog.saving">
            {{ settingsDialog.saving ? "保存中..." : "保存设置" }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { RouterLink } from "vue-router";

import CronField from "../components/CronField.vue";
import { apiRequest } from "../lib/api";
import { formatServerDateTime } from "../lib/helpers";
import {
  DEFAULT_SUBSCRIPTION_SETTINGS,
  createEmptySubscriptionsPayload,
  mergeSubscriptionSettings,
  normalizeSubscriptionsPayload,
  subscriptionModeLabel,
} from "../lib/subscriptions";


const payload = ref(createEmptySubscriptionsPayload());
const status = ref("");
const busyId = ref("");
const savingEdit = ref(false);
const editDialog = reactive({
  visible: false,
  id: "",
  url: "",
  name: "",
  cron: DEFAULT_SUBSCRIPTION_SETTINGS.default_cron,
  enabled: true,
});
const settingsDialog = reactive({
  visible: false,
  saving: false,
  form: mergeSubscriptionSettings(),
});
let refreshTimer = null;

function syncAutoRefresh() {
  const hasRunning = payload.value.items.some((item) => item.running);
  if (hasRunning && !refreshTimer) {
    refreshTimer = window.setInterval(() => {
      void load({ silent: true });
    }, 5000);
    return;
  }
  if (!hasRunning && refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

async function load({ silent = false } = {}) {
  try {
    const response = await apiRequest("/api/subscriptions");
    payload.value = normalizeSubscriptionsPayload(response);
    syncAutoRefresh();
  } catch (error) {
    if (!silent) {
      status.value = error instanceof Error ? error.message : "订阅数据加载失败。";
    }
  }
}

function openSettingsDialog() {
  settingsDialog.visible = true;
  settingsDialog.form = mergeSubscriptionSettings(payload.value.settings || {});
  status.value = "";
}

function closeSettingsDialog(force = false) {
  if (settingsDialog.saving && !force) {
    return;
  }
  settingsDialog.visible = false;
}

async function saveSubscriptionSettings() {
  settingsDialog.saving = true;
  status.value = "";
  try {
    const response = await apiRequest("/api/config/subscriptions", {
      method: "POST",
      body: settingsDialog.form,
    });
    payload.value = {
      ...payload.value,
      settings: mergeSubscriptionSettings(response.subscription_settings || settingsDialog.form),
    };
    status.value = "订阅设置已保存。";
    closeSettingsDialog(true);
    await load({ silent: true });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存订阅设置失败。";
  } finally {
    settingsDialog.saving = false;
  }
}

async function toggleSubscriptionEnabled(item) {
  busyId.value = item.id;
  status.value = "";
  try {
    const response = await apiRequest(`/api/subscriptions/${item.id}`, {
      method: "PUT",
      body: {
        url: item.url,
        name: item.name,
        cron: item.cron,
        enabled: !item.enabled,
      },
    });
    status.value = response.message || "订阅状态已更新。";
    await load({ silent: true });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "切换启用状态失败。";
  } finally {
    busyId.value = "";
  }
}

function openEditDialog(item) {
  editDialog.visible = true;
  editDialog.id = item.id;
  editDialog.url = item.url;
  editDialog.name = item.name;
  editDialog.cron = item.cron;
  editDialog.enabled = Boolean(item.enabled);
  status.value = "";
}

function closeEditDialog(force = false) {
  if (savingEdit.value && !force) {
    return;
  }
  editDialog.visible = false;
  editDialog.id = "";
  editDialog.url = "";
  editDialog.name = "";
  editDialog.cron = String(payload.value.settings?.default_cron || DEFAULT_SUBSCRIPTION_SETTINGS.default_cron);
  editDialog.enabled = true;
}

async function submitEditDialog() {
  if (!editDialog.id) {
    return;
  }
  savingEdit.value = true;
  busyId.value = editDialog.id;
  status.value = "";
  try {
    const response = await apiRequest(`/api/subscriptions/${editDialog.id}`, {
      method: "PUT",
      body: {
        url: editDialog.url,
        name: editDialog.name,
        cron: editDialog.cron,
        enabled: editDialog.enabled,
      },
    });
    status.value = response.message || "订阅已更新。";
    closeEditDialog(true);
    await load({ silent: true });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存失败。";
  } finally {
    savingEdit.value = false;
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
    await load({ silent: true });
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
    payload.value = normalizeSubscriptionsPayload(response.subscriptions || {});
    syncAutoRefresh();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "删除失败。";
  } finally {
    busyId.value = "";
  }
}

function formatDateTime(value) {
  return formatServerDateTime(value, {
    fallback: value || "未安排",
  });
}

onMounted(async () => {
  await load();
});

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
  }
});
</script>
