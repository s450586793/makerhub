<template>
  <section class="page-intro page-intro--compact">
    <div>
      <span class="eyebrow">订阅</span>
      <h1>订阅来源与同步</h1>
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
      <div class="subscription-toolbar">
        <button class="button button-secondary" type="button" @click="openSettingsDialog">订阅设置</button>
      </div>
    </div>
    <form class="subscription-create-form subscription-create-form--compact" @submit.prevent="createSubscription">
      <label class="filter-field filter-field--wide">
        <input v-model.trim="createForm.url" type="text" placeholder="作者上传页、收藏夹模型页或合集详情页链接">
      </label>
      <label class="filter-field">
        <input v-model.trim="createForm.name" type="text" placeholder="订阅名称（可选）">
      </label>
      <div class="subscription-create-form__actions">
        <div class="subscription-create-form__submit">
          <button class="button button-primary" type="submit" :disabled="creating">
            {{ creating ? "提交中..." : "添加订阅" }}
          </button>
        </div>
      </div>
      <details class="subscription-advanced">
        <summary class="subscription-advanced__summary">
          <span>高级选项</span>
          <small>默认值来自订阅设置</small>
        </summary>
        <div class="subscription-advanced__content">
          <CronField
            v-model="createForm.cron"
            class="filter-field"
            placeholder="Cron，例如 0 */6 * * *"
            dialog-title="设置新增订阅 Cron"
          />
          <label class="subscription-toggle subscription-toggle--compact">
            <input v-model="createForm.enabled" type="checkbox">
            <span>启用定时订阅</span>
          </label>
          <label class="subscription-toggle subscription-toggle--compact subscription-toggle--wide">
            <input v-model="createForm.initialize_from_source" type="checkbox">
            <span class="subscription-toggle__copy subscription-toggle__copy--wide">创建时初始化当前源页面</span>
          </label>
        </div>
      </details>
    </form>
    <p class="archive-form__hint">模型库只负责展示全部模型；这里集中看订阅来源、本地状态和同步管理。</p>
    <span class="form-status">{{ status }}</span>
  </section>

  <section v-for="section in payload.sections" :key="section.key" class="library-section">
    <div class="library-section__head">
      <div>
        <h2>{{ section.label }}</h2>
        <p>{{ section.count }} 张卡片</p>
      </div>
    </div>

    <div v-if="section.items?.length" class="source-library-grid">
      <SourceLibraryCard
        v-for="card in section.items"
        :key="card.key"
        :card="card"
        @open="openCard"
      />
    </div>
    <section v-else class="surface empty-state subscription-inline-empty">
      <h2>{{ section.label }}为空</h2>
      <p v-if="section.key === 'subscription_sources'">当前没有可展示的订阅来源卡片。你可以先新增作者、合集或收藏夹订阅。</p>
      <p v-else>当前还没有命中的本地状态卡片。</p>
    </section>
  </section>

  <section class="library-section">
    <div class="library-section__head">
      <div>
        <h2>订阅管理</h2>
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
      <p>你可以在这里添加作者页、收藏夹或合集订阅，后续会按 Cron 自动同步新模型。</p>
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
          <input v-model="settingsDialog.form.default_initialize_from_source" type="checkbox">
          <span class="subscription-toggle__copy subscription-toggle__copy--wide">新增订阅默认初始化当前源页面</span>
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
import { useRouter } from "vue-router";

import CronField from "../components/CronField.vue";
import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { formatServerDateTime } from "../lib/helpers";


const router = useRouter();
const DEFAULT_SETTINGS = {
  default_cron: "0 */6 * * *",
  default_enabled: true,
  default_initialize_from_source: true,
  card_sort: "recent",
  hide_disabled_from_cards: false,
};

const payload = ref({
  items: [],
  count: 0,
  summary: {
    enabled: 0,
    running: 0,
    deleted_marked: 0,
  },
  sections: [],
  settings: { ...DEFAULT_SETTINGS },
});
const createForm = reactive({
  url: "",
  name: "",
  cron: DEFAULT_SETTINGS.default_cron,
  enabled: DEFAULT_SETTINGS.default_enabled,
  initialize_from_source: DEFAULT_SETTINGS.default_initialize_from_source,
});
const status = ref("");
const creating = ref(false);
const busyId = ref("");
const savingEdit = ref(false);
const editDialog = reactive({
  visible: false,
  id: "",
  url: "",
  name: "",
  cron: DEFAULT_SETTINGS.default_cron,
  enabled: true,
});
const settingsDialog = reactive({
  visible: false,
  saving: false,
  form: { ...DEFAULT_SETTINGS },
});
let refreshTimer = null;

function subscriptionModeLabel(mode) {
  if (mode === "author_upload") return "作者页";
  return "合集 / 收藏夹";
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

function resetCreateForm() {
  createForm.url = "";
  createForm.name = "";
  createForm.cron = String(payload.value.settings?.default_cron || DEFAULT_SETTINGS.default_cron);
  createForm.enabled = Boolean(payload.value.settings?.default_enabled ?? DEFAULT_SETTINGS.default_enabled);
  createForm.initialize_from_source = Boolean(
    payload.value.settings?.default_initialize_from_source ?? DEFAULT_SETTINGS.default_initialize_from_source,
  );
}

function normalizePayload(response) {
  payload.value = {
    items: response.items || [],
    count: Number(response.count || 0),
    summary: {
      enabled: Number(response.summary?.enabled || 0),
      running: Number(response.summary?.running || 0),
      deleted_marked: Number(response.summary?.deleted_marked || 0),
    },
    sections: Array.isArray(response.sections) ? response.sections : [],
    settings: {
      ...DEFAULT_SETTINGS,
      ...(response.settings || {}),
    },
  };
  syncAutoRefresh();
}

async function load() {
  const response = await apiRequest("/api/subscriptions");
  normalizePayload(response);
  if (!createForm.url && !createForm.name) {
    resetCreateForm();
  }
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

function openSettingsDialog() {
  settingsDialog.visible = true;
  settingsDialog.form = {
    ...DEFAULT_SETTINGS,
    ...(payload.value.settings || {}),
  };
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
      settings: {
        ...DEFAULT_SETTINGS,
        ...(response.subscription_settings || settingsDialog.form),
      },
    };
    status.value = "订阅设置已保存。";
    closeSettingsDialog(true);
    await load();
    resetCreateForm();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "保存订阅设置失败。";
  } finally {
    settingsDialog.saving = false;
  }
}

async function createSubscription() {
  if (!createForm.url) {
    status.value = "请先输入作者页、合集或收藏夹链接。";
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
    normalizePayload(response.subscriptions || { items: [], count: 0, summary: { enabled: 0, running: 0 }, sections: [], settings: payload.value.settings });
    resetCreateForm();
  } catch (error) {
    status.value = error instanceof Error ? error.message : "创建订阅失败。";
  } finally {
    creating.value = false;
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
    await load();
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
  editDialog.cron = String(payload.value.settings?.default_cron || DEFAULT_SETTINGS.default_cron);
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
    await load();
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
    normalizePayload(response.subscriptions || { items: [], count: 0, summary: { enabled: 0, running: 0 }, sections: [], settings: payload.value.settings });
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
