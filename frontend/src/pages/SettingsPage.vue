<template>
  <section class="page-intro">
    <div>
      <span class="eyebrow">设置</span>
      <h1>连接、通知与用户配置</h1>
      <p>国内 Cookie、国际 Cookie 与 HTTP 代理统一收在“连接设置”，整理与远端刷新已拆成独立页面。</p>
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
            <small class="archive-form__hint">开启后归档、订阅与远端刷新请求会带上当前代理设置。</small>
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
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import SecretTextarea from "../components/SecretTextarea.vue";
import ThemeSegment from "../components/ThemeSegment.vue";
import { appState, refreshConfig, saveThemePreference } from "../lib/appState";
import { apiRequest } from "../lib/api";


const route = useRoute();
const router = useRouter();

const tabs = [
  { key: "connections", label: "连接设置" },
  { key: "notifications", label: "通知" },
  { key: "user", label: "用户" },
];

const activeTab = ref("connections");
const themePreference = ref("auto");
const tokenName = ref("");
const newToken = ref("");
const tokenItems = ref([]);

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
});
const testing = reactive({
  cookie_cn: false,
  cookie_global: false,
  proxy: false,
});

const config = computed(() => appState.config);

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
  activeTab.value = tabs.some((item) => item.key === tab) ? tab : "connections";
  if (route.query.tab !== activeTab.value) {
    router.replace({ path: "/settings", query: { tab: activeTab.value } });
  }
}

async function load() {
  const payload = config.value || await refreshConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "connections");
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
    await refreshConfig();
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
    await apiRequest("/api/config/notifications", {
      method: "POST",
      body: { ...notificationsForm },
    });
    await refreshConfig();
    statuses.notifications = "通知设置已保存。";
  } catch (error) {
    statuses.notifications = error instanceof Error ? error.message : "保存失败。";
  }
}

async function saveUser() {
  try {
    await apiRequest("/api/config/user", {
      method: "POST",
      body: { ...userForm },
    });
    await refreshConfig();
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
</script>
