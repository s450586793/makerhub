<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">设置</span>
      <h1>连接、通知与用户配置</h1>
      <p>国内 Cookie、国际 Cookie 与 HTTP 代理统一收在“连接设置”，整理与源端刷新已拆成独立页面。</p>
    </div>
  </section>

  <section class="surface">
    <div class="settings-tabs">
      <button
        v-for="item in tabs"
        :key="item.key"
        :class="['settings-tab', activeTab === item.key && 'is-active']"
        type="button"
        @click="setActiveTab(item.key)"
      >
        {{ item.label }}
      </button>
    </div>

    <div v-show="activeTab === 'connections'" class="settings-panel is-active">
      <form class="settings-form" @submit.prevent="saveConnections">
        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>国内 Cookie</span>
            <SecretTextarea v-model="connectionForm.cookie_cn" placeholder="makerworld.com.cn Cookie" />
            <div class="settings-inline-actions">
              <button class="button button-secondary button-small" type="button" :disabled="testing.cookie_cn" @click="testCookie('cn')">
                {{ testing.cookie_cn ? "测试中..." : "测试国内 Cookie" }}
              </button>
              <span class="form-status">{{ statuses.cookie_cn }}</span>
            </div>
          </label>
          <label class="field-card">
            <span>国际 Cookie</span>
            <SecretTextarea v-model="connectionForm.cookie_global" placeholder="makerworld.com Cookie" />
            <div class="settings-inline-actions">
              <button class="button button-secondary button-small" type="button" :disabled="testing.cookie_global" @click="testCookie('global')">
                {{ testing.cookie_global ? "测试中..." : "测试国际 Cookie" }}
              </button>
              <span class="form-status">{{ statuses.cookie_global }}</span>
            </div>
          </label>
        </div>
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>启用 HTTP 代理</span>
            <button
              :class="['subscription-switch', connectionForm.proxy_enabled && 'is-on']"
              type="button"
              :disabled="testing.proxy"
              @click="connectionForm.proxy_enabled = !connectionForm.proxy_enabled"
            >
              <span class="subscription-switch__track" aria-hidden="true">
                <span class="subscription-switch__thumb"></span>
              </span>
              <span class="subscription-switch__label">{{ connectionForm.proxy_enabled ? "启用中" : "已停用" }}</span>
            </button>
            <small class="archive-form__hint">开启后归档、订阅与源端刷新请求会带上当前代理设置。</small>
          </label>
          <label class="field-card">
            <span>HTTP Proxy</span>
            <input v-model="connectionForm.http_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
          <label class="field-card">
            <span>HTTPS Proxy</span>
            <input v-model="connectionForm.https_proxy" type="text" placeholder="http://127.0.0.1:7890">
          </label>
        </div>
        <div class="settings-inline-actions">
          <button class="button button-secondary" type="button" :disabled="testing.proxy" @click="testProxy">
            {{ testing.proxy ? "测试中..." : "测试 HTTP 代理" }}
          </button>
          <span class="form-status">{{ statuses.proxy }}</span>
        </div>
        <div class="settings-grid settings-grid--two">
          <label class="field-card">
            <span>国区每日 3MF 下载上限</span>
            <input v-model.number="threeMfLimitsForm.cn_daily_limit" type="number" min="1" step="1">
            <small class="archive-form__hint">达到上限后，国区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
          <label class="field-card">
            <span>国际区每日 3MF 下载上限</span>
            <input v-model.number="threeMfLimitsForm.global_daily_limit" type="number" min="1" step="1">
            <small class="archive-form__hint">达到上限后，国际区缺失 3MF 会暂停到次日 00:00。</small>
          </label>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存连接设置</button>
          <span class="form-status">{{ statuses.connections }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'notifications'" class="settings-panel is-active">
      <form class="settings-form" @submit.prevent="saveNotifications">
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>启用通知</span>
            <label class="switch">
              <input v-model="notificationsForm.enabled" type="checkbox">
              <span>任务状态更新时触发通知</span>
            </label>
          </label>
          <label class="field-card">
            <span>Telegram Bot Token</span>
            <input v-model="notificationsForm.telegram_bot_token" type="text" placeholder="bot token">
          </label>
          <label class="field-card">
            <span>Telegram Chat ID</span>
            <input v-model="notificationsForm.telegram_chat_id" type="text" placeholder="chat id">
          </label>
        </div>
        <label class="field-card">
          <span>Webhook URL</span>
          <input v-model="notificationsForm.webhook_url" type="text" placeholder="https://example.com/webhook">
        </label>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存通知设置</button>
          <span class="form-status">{{ statuses.notifications }}</span>
        </div>
      </form>
    </div>

    <div v-show="activeTab === 'system'" class="settings-panel is-active">
      <section class="token-card system-update-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">系统</span>
            <h2>容器更新</h2>
          </div>
          <div class="settings-inline-actions">
            <button class="button button-secondary" type="button" :disabled="systemUpdateLoading || systemUpdateSubmitting" @click="loadSystemUpdateStatus({ force: true })">
              {{ systemUpdateLoading ? "读取中..." : "刷新状态" }}
            </button>
            <button class="button button-primary" type="button" :disabled="!canTriggerSystemUpdate" @click="triggerSystemUpdate">
              {{ systemUpdateButtonText }}
            </button>
          </div>
        </div>

        <p class="archive-form__hint">
          网页更新会拉取当前镜像标签的最新内容并重建 MakerHub 容器。执行期间页面会短暂不可用，但挂载的配置、状态和归档目录会保留。
        </p>

        <div class="settings-grid settings-grid--three system-update-grid">
          <article class="field-card system-update-stat">
            <span>当前版本</span>
            <strong>{{ appState.appVersion ? `v${appState.appVersion}` : "读取中" }}</strong>
            <small>{{ systemUpdate.current_version ? `运行中：v${systemUpdate.current_version}` : "等待应用状态" }}</small>
          </article>
          <article class="field-card system-update-stat">
            <span>最新版本</span>
            <strong>{{ githubVersionText }}</strong>
            <small>{{ appState.githubUpdateAvailable ? "检测到可更新版本" : "已是最新或尚未确认" }}</small>
          </article>
          <article class="field-card system-update-stat">
            <span>更新状态</span>
            <strong>{{ systemUpdateStatusLabel }}</strong>
            <small>{{ systemUpdate.message || "当前还没有进行中的网页更新任务。" }}</small>
          </article>
        </div>

        <div class="settings-grid settings-grid--two system-update-grid">
          <article class="field-card system-update-detail">
            <span>运行容器</span>
            <strong>{{ systemUpdate.container_name || "-" }}</strong>
            <small>{{ systemUpdate.image_ref || "未检测到镜像引用" }}</small>
          </article>
          <article class="field-card system-update-detail">
            <span>一键更新支持</span>
            <strong>{{ systemUpdate.supported ? "已启用" : "未启用" }}</strong>
            <small>{{ systemUpdateSupportText }}</small>
          </article>
        </div>

        <div class="field-card system-update-manual">
          <span>{{ systemUpdate.supported ? "执行说明" : "如何启用一键更新" }}</span>
          <p v-if="systemUpdate.supported">
            更新会复用当前容器名称、挂载、端口和重启策略。如果页面短暂报错，通常只是容器正在重启；恢复后可在日志页查看 <code>system</code> 分类结果。
          </p>
          <p v-else>
            首次仍需要手动在部署里挂载 <code>/var/run/docker.sock:/var/run/docker.sock</code>。启用后，这个页面才能直接拉取新镜像并重建容器。
          </p>
          <code class="system-update-code">{{ manualUpdateCommand }}</code>
        </div>

        <section class="system-update-changelog system-maintenance-card">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">系统维护</span>
              <h2>现有库信息补全</h2>
            </div>
            <div class="settings-inline-actions">
              <button class="button button-secondary" type="button" :disabled="profileBackfillLoading || profileBackfillSubmitting" @click="loadProfileBackfillStatus">
                {{ profileBackfillLoading ? "读取中..." : "刷新状态" }}
              </button>
              <button class="button button-primary" type="button" :disabled="profileBackfillSubmitting" @click="triggerProfileBackfill">
                {{ profileBackfillSubmitting ? "提交中..." : "补全现有库信息" }}
              </button>
            </div>
          </div>
          <p class="archive-form__hint">
            这里只负责扫描本地已归档模型，并把缺少打印配置详情、实例展示媒体或评论回复字段的模型加入归档补整理队列。实际补全会在后台归档队列继续执行，不主动消耗 3MF 下载次数。
          </p>
          <div class="settings-grid settings-grid--three system-update-grid">
            <article class="field-card system-update-stat">
              <span>发现缺失</span>
              <strong>{{ profileBackfillStats.scanned }}</strong>
              <small>{{ profileBackfill.started_at ? `最近扫描：${profileBackfill.started_at}` : "尚未执行" }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>新增入队</span>
              <strong>{{ profileBackfillStats.queued }}</strong>
              <small>已在队列：{{ profileBackfillStats.alreadyQueued }}</small>
            </article>
            <article class="field-card system-update-stat">
              <span>失败</span>
              <strong>{{ profileBackfillStats.failed }}</strong>
              <small>{{ profileBackfill.finished_at ? `最近扫描结束：${profileBackfill.finished_at}` : "等待执行" }}</small>
            </article>
          </div>
          <div class="form-footer">
            <span class="form-status">{{ statuses.profile_backfill || profileBackfill.last_error || profileBackfill.message || "这里只负责扫描并加入归档队列；实际补全会在后台继续执行，可到任务页查看进度。" }}</span>
          </div>
        </section>

        <section class="system-update-changelog">
          <div class="section-card__header">
            <div>
              <span class="eyebrow">更新日志</span>
              <h2>最近版本记录</h2>
            </div>
            <span class="count-pill">{{ changelogEntries.length }} 条</span>
          </div>
          <p class="archive-form__hint">{{ changelogSummaryText }}</p>
          <div v-if="changelogEntries.length" class="system-update-changelog__list">
            <article v-for="entry in changelogEntries" :key="`${entry.date}-${entry.version}`" class="field-card system-update-changelog__entry">
              <div class="system-update-changelog__head">
                <strong>{{ entry.version ? `v${entry.version}` : "更新记录" }}</strong>
                <span>{{ entry.date || "-" }}</span>
              </div>
              <ul class="system-update-changelog__items">
                <li v-for="item in entry.items || []" :key="item">{{ item }}</li>
              </ul>
            </article>
          </div>
          <p v-else class="empty-copy">暂时还没有读取到线上更新日志。</p>
        </section>

        <div class="form-footer">
          <span class="form-status">{{ statuses.system_update || systemUpdate.message || "当前还没有进行中的网页更新任务。" }}</span>
        </div>
      </section>
    </div>

    <div v-show="activeTab === 'user'" class="settings-panel is-active">
      <form class="settings-form token-card" @submit.prevent="saveTheme">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">主题</span>
            <h2>整体显示模式</h2>
          </div>
        </div>
        <ThemeSegment :value="themePreference" @change="themePreference = $event" />
        <p class="archive-form__hint">“自动”会跟随你设备当前的浅色 / 深色模式。</p>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存主题设置</button>
          <span class="form-status">{{ statuses.theme }}</span>
        </div>
      </form>

      <form class="settings-form" @submit.prevent="saveUser">
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>用户名</span>
            <input v-model="userForm.username" type="text" placeholder="admin">
          </label>
          <label class="field-card">
            <span>显示名称</span>
            <input v-model="userForm.display_name" type="text" placeholder="Admin">
          </label>
          <label class="field-card">
            <span>密码提示</span>
            <input v-model="userForm.password_hint" type="text" placeholder="请改成强密码">
          </label>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">保存用户信息</button>
          <span class="form-status">{{ statuses.user }}</span>
        </div>
      </form>

      <form class="settings-form token-card" @submit.prevent="savePassword">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">密码</span>
            <h2>修改登录密码</h2>
          </div>
        </div>
        <div class="settings-grid settings-grid--three">
          <label class="field-card">
            <span>当前密码</span>
            <input v-model="passwordForm.current_password" type="password" autocomplete="current-password" placeholder="当前密码">
          </label>
          <label class="field-card">
            <span>新密码</span>
            <input v-model="passwordForm.new_password" type="password" autocomplete="new-password" placeholder="至少 4 位">
          </label>
          <label class="field-card">
            <span>确认新密码</span>
            <input v-model="passwordForm.confirm_password" type="password" autocomplete="new-password" placeholder="再次输入新密码">
          </label>
        </div>
        <div class="form-footer">
          <button class="button button-primary" type="submit">更新密码</button>
          <span class="form-status">{{ statuses.password }}</span>
        </div>
      </form>

      <section class="token-card">
        <div class="section-card__header">
          <div>
            <span class="eyebrow">Token</span>
            <h2>API Token 管理</h2>
          </div>
        </div>
        <form class="token-create-form" @submit.prevent="createToken">
          <input v-model.trim="tokenName" type="text" placeholder="例如：家里 NAS / 自动化脚本">
          <button class="button button-primary" type="submit">生成 Token</button>
        </form>
        <p class="archive-form__hint">后续通过 API 调用归档时，可在 <code>Authorization: Bearer &lt;token&gt;</code> 里传入。</p>
        <div v-if="newToken" class="token-output">
          <strong>新 Token</strong>
          <code>{{ newToken }}</code>
        </div>
        <span class="form-status">{{ statuses.tokens }}</span>
        <div class="token-list">
          <article v-for="item in tokenItems" :key="item.id" class="token-item">
            <div>
              <strong>{{ item.name }}</strong>
              <span>{{ item.token_prefix }}...</span>
            </div>
            <div class="token-item__meta">
              <span>创建于 {{ item.created_at || "-" }}</span>
              <span>最近使用 {{ item.last_used_at || "未使用" }}</span>
            </div>
            <button class="button button-secondary button-small" type="button" @click="revokeToken(item.id)">撤销</button>
          </article>
          <p v-if="!tokenItems.length" class="empty-copy">当前还没有 API Token。</p>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import SecretTextarea from "../components/SecretTextarea.vue";
import ThemeSegment from "../components/ThemeSegment.vue";
import { appState, applyConfigPayload, refreshConfig, saveThemePreference } from "../lib/appState";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();

const tabs = [
  { key: "system", label: "系统" },
  { key: "connections", label: "连接设置" },
  { key: "user", label: "用户" },
  { key: "notifications", label: "通知" },
];

const activeTab = ref("system");
const themePreference = ref("auto");
const tokenName = ref("");
const newToken = ref("");
const tokenItems = ref([]);
const systemUpdate = ref(defaultSystemUpdateState());
const systemUpdateLoading = ref(false);
const systemUpdateSubmitting = ref(false);
const profileBackfill = ref(defaultProfileBackfillState());
const profileBackfillLoading = ref(false);
const profileBackfillSubmitting = ref(false);

const connectionForm = reactive({
  cookie_cn: "",
  cookie_global: "",
  proxy_enabled: false,
  http_proxy: "",
  https_proxy: "",
});
const notificationsForm = reactive({
  enabled: false,
  telegram_bot_token: "",
  telegram_chat_id: "",
  webhook_url: "",
});
const threeMfLimitsForm = reactive({
  cn_daily_limit: 100,
  global_daily_limit: 100,
});
const userForm = reactive({
  username: "admin",
  display_name: "Admin",
  password_hint: "",
});
const passwordForm = reactive({
  current_password: "",
  new_password: "",
  confirm_password: "",
});
const statuses = reactive({
  connections: "",
  cookie_cn: "",
  cookie_global: "",
  proxy: "",
  notifications: "",
  user: "",
  password: "",
  tokens: "",
  theme: "",
  system_update: "",
  profile_backfill: "",
});
const testing = reactive({
  cookie_cn: false,
  cookie_global: false,
  proxy: false,
});
let systemUpdateTimer = null;

const config = computed(() => appState.config);
const systemUpdateActive = computed(() => ["queued", "launching_helper", "running", "pending_startup"].includes(systemUpdate.value.status));
const canTriggerSystemUpdate = computed(() => (
  systemUpdate.value.supported
  && !systemUpdateActive.value
  && !systemUpdateLoading.value
  && !systemUpdateSubmitting.value
));
const systemUpdateButtonText = computed(() => (
  appState.githubUpdateAvailable ? "更新到最新版本" : "重新拉取 latest"
));
const githubVersionText = computed(() => {
  if (appState.githubLatestVersion) {
    return `v${appState.githubLatestVersion}`;
  }
  if (appState.githubVersionError) {
    return "读取失败";
  }
  return "读取中";
});
const systemUpdateStatusLabel = computed(() => {
  const labelMap = {
    idle: "空闲",
    queued: "已提交",
    launching_helper: "启动中",
    running: "执行中",
    pending_startup: "重启中",
    succeeded: "已完成",
    failed: "失败",
  };
  return labelMap[systemUpdate.value.status] || "未知";
});
const systemUpdateSupportText = computed(() => {
  if (systemUpdate.value.supported) {
    return "已检测到 docker.sock，可从网页直接触发容器重建。";
  }
  return systemUpdate.value.support_reason || "当前部署未开启网页更新能力。";
});
const manualUpdateCommand = computed(() => "docker compose pull makerhub && docker compose up -d makerhub");
const changelogEntries = computed(() => (
  Array.isArray(systemUpdate.value.github_changelog) ? systemUpdate.value.github_changelog : []
));
const changelogSummaryText = computed(() => {
  if (systemUpdate.value.github_changelog_error) {
    return `更新日志读取失败：${systemUpdate.value.github_changelog_error}`;
  }
  if (systemUpdate.value.github_changelog_checked_at) {
    return `线上 README 更新记录，最近检查时间 ${systemUpdate.value.github_changelog_checked_at}`;
  }
  return "会优先读取 GitHub 仓库 README 中的最新更新记录。";
});
const profileBackfillStats = computed(() => {
  const result = profileBackfill.value.last_result || {};
  return {
    scanned: Number(result.scanned_candidates || 0),
    queued: Number(result.queued_count || 0),
    alreadyQueued: Number(result.already_queued_count || 0),
    failed: Number(result.failed_count || 0),
  };
});

function defaultSystemUpdateState() {
  return {
    status: "idle",
    phase: "idle",
    message: "",
    request_id: "",
    requested_at: "",
    started_at: "",
    finished_at: "",
    requested_by: "",
    helper_container_id: "",
    replacement_container_id: "",
    container_name: "",
    image_ref: "",
    target_version: "",
    current_version: "",
    supported: false,
    support_reason: "",
    docker_socket_mounted: false,
    github_changelog: [],
    github_changelog_checked_at: "",
    github_changelog_error: "",
    github_changelog_source: "",
  };
}

function defaultProfileBackfillState() {
  return {
    running: false,
    started_at: "",
    finished_at: "",
    last_error: "",
    last_result: {},
    message: "",
  };
}

function applySystemUpdateStatus(payload) {
  systemUpdate.value = {
    ...defaultSystemUpdateState(),
    ...(payload || {}),
  };
}

function applyProfileBackfillStatus(payload) {
  profileBackfill.value = {
    ...defaultProfileBackfillState(),
    ...(payload || {}),
  };
}

function applyConfigToForms(payload) {
  const cookies = {};
  for (const item of payload.cookies || []) {
    cookies[item.platform] = item.cookie || "";
  }
  connectionForm.cookie_cn = cookies.cn || "";
  connectionForm.cookie_global = cookies.global || "";
  connectionForm.proxy_enabled = Boolean(payload.proxy?.enabled);
  connectionForm.http_proxy = payload.proxy?.http_proxy || "";
  connectionForm.https_proxy = payload.proxy?.https_proxy || "";
  threeMfLimitsForm.cn_daily_limit = Number(payload.three_mf_limits?.cn_daily_limit || 100);
  threeMfLimitsForm.global_daily_limit = Number(payload.three_mf_limits?.global_daily_limit || 100);

  notificationsForm.enabled = Boolean(payload.notifications?.enabled);
  notificationsForm.telegram_bot_token = payload.notifications?.telegram_bot_token || "";
  notificationsForm.telegram_chat_id = payload.notifications?.telegram_chat_id || "";
  notificationsForm.webhook_url = payload.notifications?.webhook_url || "";

  userForm.username = payload.user?.username || "admin";
  userForm.display_name = payload.user?.display_name || "Admin";
  userForm.password_hint = payload.user?.password_hint || "";
  themePreference.value = payload.user?.theme_preference || "auto";
  tokenItems.value = payload.api_tokens || [];
}

function setActiveTab(tab) {
  activeTab.value = tabs.some((item) => item.key === tab) ? tab : "system";
  if (route.query.tab !== activeTab.value) {
    router.replace({ path: "/settings", query: { tab: activeTab.value } });
  }
  if (activeTab.value === "system" && !systemUpdateLoading.value && !systemUpdateSubmitting.value) {
    loadSystemUpdateStatus({ force: true });
    loadProfileBackfillStatus({ silent: true });
  }
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "system");
}

function clearSystemUpdateTimer() {
  if (systemUpdateTimer) {
    window.clearTimeout(systemUpdateTimer);
    systemUpdateTimer = null;
  }
}

function scheduleSystemUpdatePolling() {
  clearSystemUpdateTimer();
  if (!systemUpdateActive.value) {
    return;
  }
  systemUpdateTimer = window.setTimeout(() => {
    loadSystemUpdateStatus({ silent: true });
  }, 3000);
}

async function loadSystemUpdateStatus(options = {}) {
  const { silent = false, force = false } = options;
  const wasActive = systemUpdateActive.value;
  if (!silent) {
    systemUpdateLoading.value = true;
    statuses.system_update = "";
  }
  try {
    const query = force ? "?force=true" : "";
    const payload = await apiRequest(`/api/system/update${query}`);
    applySystemUpdateStatus(payload);
    if (typeof payload.github_latest_version === "string") {
      appState.githubLatestVersion = payload.github_latest_version;
    }
    if (typeof payload.github_version_checked_at === "string") {
      appState.githubVersionCheckedAt = payload.github_version_checked_at;
    }
    if (typeof payload.github_version_error === "string") {
      appState.githubVersionError = payload.github_version_error;
    }
    if (typeof payload.github_update_available === "boolean") {
      appState.githubUpdateAvailable = payload.github_update_available;
    }
    statuses.system_update = "";
    if (systemUpdate.value.status === "succeeded" && appState.appVersion !== systemUpdate.value.current_version) {
      await refreshConfig();
    }
  } catch (error) {
    if (wasActive) {
      statuses.system_update = "正在等待服务重启完成，页面恢复后会自动继续读取状态。";
    } else if (!silent) {
      statuses.system_update = error instanceof Error ? error.message : "读取更新状态失败。";
    }
  } finally {
    if (!silent) {
      systemUpdateLoading.value = false;
    }
    scheduleSystemUpdatePolling();
  }
}

async function triggerSystemUpdate() {
  const shouldProceed = window.confirm("这会拉取最新镜像并重建当前 MakerHub 容器，页面会短暂不可用。确定继续吗？");
  if (!shouldProceed) {
    return;
  }
  systemUpdateSubmitting.value = true;
  statuses.system_update = "";
  try {
    const payload = await apiRequest("/api/system/update", {
      method: "POST",
      body: {
        target_version: appState.githubLatestVersion || "",
        force: !appState.githubUpdateAvailable,
      },
    });
    applySystemUpdateStatus(payload);
    statuses.system_update = payload.message || "更新任务已提交，服务即将短暂重启。";
  } catch (error) {
    statuses.system_update = error instanceof Error ? error.message : "提交更新任务失败。";
  } finally {
    systemUpdateSubmitting.value = false;
    scheduleSystemUpdatePolling();
  }
}

async function loadProfileBackfillStatus(options = {}) {
  const { silent = false } = options;
  if (!silent) {
    profileBackfillLoading.value = true;
    statuses.profile_backfill = "";
  }
  try {
    const payload = await apiRequest("/api/admin/archive/profile-backfill");
    applyProfileBackfillStatus(payload);
  } catch (error) {
    if (!silent) {
      statuses.profile_backfill = error instanceof Error ? error.message : "读取信息补全状态失败。";
    }
  } finally {
    if (!silent) {
      profileBackfillLoading.value = false;
    }
  }
}

async function triggerProfileBackfill() {
  const shouldProceed = window.confirm("会扫描本地归档库，并把缺少打印配置详情、实例展示媒体或评论回复字段的模型加入归档补整理队列。这里只负责扫描和入队，不会主动下载 3MF。确定继续吗？");
  if (!shouldProceed) {
    return;
  }
  profileBackfillSubmitting.value = true;
  statuses.profile_backfill = "";
  try {
    const payload = await apiRequest("/api/admin/archive/profile-backfill", {
      method: "POST",
    });
    applyProfileBackfillStatus(payload);
    statuses.profile_backfill = payload.message || "现有库信息补全扫描已提交，缺失模型会继续在归档队列后台处理。";
  } catch (error) {
    statuses.profile_backfill = error instanceof Error ? error.message : "提交信息补全失败。";
  } finally {
    profileBackfillSubmitting.value = false;
  }
}

async function saveConnections() {
  try {
    await apiRequest("/api/config/cookies", {
      method: "POST",
      body: [
        { platform: "cn", cookie: connectionForm.cookie_cn },
        { platform: "global", cookie: connectionForm.cookie_global },
      ],
    });
    await apiRequest("/api/config/proxy", {
      method: "POST",
      body: {
        enabled: connectionForm.proxy_enabled,
        http_proxy: connectionForm.http_proxy,
        https_proxy: connectionForm.https_proxy,
      },
    });
    const payload = await apiRequest("/api/config/three-mf-limits", {
      method: "POST",
      body: {
        cn_daily_limit: Number(threeMfLimitsForm.cn_daily_limit || 100),
        global_daily_limit: Number(threeMfLimitsForm.global_daily_limit || 100),
      },
    });
    applyConfigPayload(payload);
    statuses.connections = "连接设置已保存。";
  } catch (error) {
    statuses.connections = error instanceof Error ? error.message : "保存失败。";
  }
}

function buildProxyPayload() {
  return {
    enabled: connectionForm.proxy_enabled,
    http_proxy: connectionForm.http_proxy,
    https_proxy: connectionForm.https_proxy,
  };
}

async function testCookie(platform) {
  const key = platform === "global" ? "cookie_global" : "cookie_cn";
  const cookie = platform === "global" ? connectionForm.cookie_global : connectionForm.cookie_cn;
  testing[key] = true;
  statuses[key] = "";
  try {
    const response = await apiRequest("/api/config/cookies/test", {
      method: "POST",
      body: {
        platform,
        cookie,
        proxy: buildProxyPayload(),
      },
    });
    statuses[key] = response.message || "测试完成。";
  } catch (error) {
    statuses[key] = error instanceof Error ? error.message : "测试失败。";
  } finally {
    testing[key] = false;
  }
}

async function testProxy() {
  testing.proxy = true;
  statuses.proxy = "";
  try {
    const response = await apiRequest("/api/config/proxy/test", {
      method: "POST",
      body: buildProxyPayload(),
    });
    statuses.proxy = response.message || "测试完成。";
  } catch (error) {
    statuses.proxy = error instanceof Error ? error.message : "测试失败。";
  } finally {
    testing.proxy = false;
  }
}

async function saveNotifications() {
  try {
    const payload = await apiRequest("/api/config/notifications", {
      method: "POST",
      body: { ...notificationsForm },
    });
    applyConfigPayload(payload);
    statuses.notifications = "通知设置已保存。";
  } catch (error) {
    statuses.notifications = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveUser() {
  try {
    const payload = await apiRequest("/api/config/user", {
      method: "POST",
      body: { ...userForm },
    });
    applyConfigPayload(payload);
    statuses.user = "用户信息已保存。";
  } catch (error) {
    statuses.user = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveTheme() {
  try {
    await saveThemePreference(themePreference.value);
    statuses.theme = "主题设置已保存。";
  } catch (error) {
    statuses.theme = error instanceof Error ? error.message : "保存失败。";
  }
}

async function savePassword() {
  if (passwordForm.new_password !== passwordForm.confirm_password) {
    statuses.password = "两次输入的新密码不一致。";
    return;
  }
  try {
    await apiRequest("/api/auth/password", {
      method: "POST",
      body: {
        current_password: passwordForm.current_password,
        new_password: passwordForm.new_password,
      },
    });
    statuses.password = "密码已更新，请重新登录。";
    setTimeout(() => {
      window.location.assign("/login");
    }, 500);
  } catch (error) {
    statuses.password = error instanceof Error ? error.message : "更新失败。";
  }
}

async function createToken() {
  try {
    const response = await apiRequest("/api/auth/tokens", {
      method: "POST",
      body: { name: tokenName.value },
    });
    newToken.value = response.token || "";
    tokenItems.value = response.items || [];
    tokenName.value = "";
    statuses.tokens = "Token 已生成。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "生成失败。";
  }
}

async function revokeToken(tokenId) {
  try {
    const response = await apiRequest(`/api/auth/tokens/${encodeURIComponent(tokenId)}`, {
      method: "DELETE",
    });
    tokenItems.value = response.items || [];
    statuses.tokens = "Token 已撤销。";
  } catch (error) {
    statuses.tokens = error instanceof Error ? error.message : "撤销失败。";
  }
}

watch(() => route.query.tab, (value) => {
  setActiveTab(typeof value === "string" ? value : "connections");
});

onMounted(load);
onBeforeUnmount(clearSystemUpdateTimer);
</script>
