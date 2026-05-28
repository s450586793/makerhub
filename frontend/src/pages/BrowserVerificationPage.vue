<template>
  <section class="surface surface--filters page-intro app-page-toolbar browser-verification-toolbar">
    <div class="app-page-toolbar__copy">
      <span class="eyebrow">3MF 验证</span>
      <div class="app-page-toolbar__title-row">
        <h1>{{ titleText }}</h1>
      </div>
    </div>
    <div class="intro-stats app-page-toolbar__stats">
      <div class="intro-stat">
        <span>平台</span>
        <strong>{{ platformLabel }}</strong>
      </div>
      <div class="intro-stat">
        <span>状态</span>
        <strong>{{ statusText }}</strong>
      </div>
      <div class="intro-stat">
        <span>截图</span>
        <strong>{{ session?.screenshot_version || 0 }}</strong>
      </div>
    </div>
  </section>

  <section class="surface section-card browser-verification-panel">
    <div class="section-card__header section-card__header--compact">
      <div>
        <span class="eyebrow">{{ targetSubtitle }}</span>
        <h2>{{ panelHeading }}</h2>
      </div>
      <div class="browser-verification-actions">
        <RouterLink class="button button-secondary button-small" to="/tasks">返回任务</RouterLink>
        <button class="button button-secondary button-small" type="button" :disabled="loading" @click="refreshNow">
          刷新
        </button>
        <button
          class="button button-danger button-small"
          type="button"
          :disabled="cancelling || isFinished"
          @click="cancelSession"
        >
          {{ cancelling ? "取消中..." : "取消" }}
        </button>
      </div>
    </div>

    <span v-if="messageText" :class="['form-status', isError && 'is-error', isCompleted && 'is-success']">{{ messageText }}</span>

    <div class="browser-verification-viewer">
      <div
        ref="viewerRef"
        class="browser-verification-frame"
        tabindex="0"
        role="application"
        @click="sendPointerCommand('click', $event)"
        @mousemove="handleMouseMove"
        @mousedown.prevent="sendPointerCommand('mousedown', $event)"
        @mouseup.prevent="sendPointerCommand('mouseup', $event)"
        @wheel.prevent="sendWheelCommand"
        @keydown.prevent="sendKeyCommand"
        @paste.prevent="sendPasteCommand"
      >
        <img
          v-if="screenshotUrl"
          class="browser-verification-screenshot"
          :src="screenshotUrl"
          alt=""
          draggable="false"
          @load="screenshotLoaded = true"
          @error="screenshotLoaded = false"
        >
        <div v-if="!screenshotUrl || !screenshotLoaded" class="browser-verification-empty">
          <strong>{{ emptyTitle }}</strong>
          <span>{{ emptyMessage }}</span>
        </div>
      </div>
      <aside class="browser-verification-side">
        <div class="browser-verification-side__item">
          <span>模型 ID</span>
          <strong>{{ session?.target?.model_id || "-" }}</strong>
        </div>
        <div class="browser-verification-side__item">
          <span>配置</span>
          <strong>{{ session?.target?.instance_id || "-" }}</strong>
        </div>
        <div class="browser-verification-side__item">
          <span>Captcha</span>
          <strong>{{ session?.captcha_id || "-" }}</strong>
        </div>
        <div class="browser-verification-side__item">
          <span>重试结果</span>
          <strong>{{ retryResultText }}</strong>
        </div>
      </aside>
    </div>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";

import { apiRequest } from "../lib/api";
import { subscribeStateRefresh } from "../lib/stateEvents";


const route = useRoute();
const session = ref(null);
const loading = ref(false);
const cancelling = ref(false);
const screenshotUrl = ref("");
const screenshotLoaded = ref(false);
const viewerRef = ref(null);
let refreshTimer = 0;
let screenshotRefreshTimer = 0;
let screenshotObjectUrl = "";
let unsubscribeStateRefresh = null;
let mouseMoveSentAt = 0;

const sessionId = computed(() => String(route.params.sessionId || ""));
const statusText = computed(() => statusLabel(session.value?.status));
const platformLabel = computed(() => session.value?.platform === "global" ? "国际" : "国区");
const isFinished = computed(() => ["completed", "failed", "cancelled", "expired"].includes(String(session.value?.status || "")));
const isError = computed(() => ["failed", "expired"].includes(String(session.value?.status || "")));
const isCompleted = computed(() => String(session.value?.status || "") === "completed");
const titleText = computed(() => {
  const target = session.value?.target || {};
  return target.title || target.model_id || "浏览器验证";
});
const panelHeading = computed(() => {
  if (!session.value) {
    return "正在读取会话";
  }
  return isFinished.value ? "验证会话结果" : "远程验证画面";
});
const targetSubtitle = computed(() => {
  const target = session.value?.target || {};
  return target.model_url || "MakerWorld";
});
const messageText = computed(() => session.value?.error || session.value?.message || "");
const emptyTitle = computed(() => loading.value ? "正在连接 worker" : "等待浏览器画面");
const emptyMessage = computed(() => isFinished.value ? "会话已结束，返回任务页查看重试进度。" : "worker 启动浏览器后会在这里显示验证页面。");
const retryResultText = computed(() => {
  const result = session.value?.retry_result || {};
  if (!Object.keys(result).length) {
    return "-";
  }
  if (result.accepted_count !== undefined) {
    return `${result.accepted_count || 0} 项已提交`;
  }
  return result.message || "已提交";
});

function statusLabel(status) {
  const mapping = {
    queued: "排队",
    starting: "启动中",
    running: "验证中",
    verified: "已验证",
    retrying: "重试中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
    expired: "已超时",
  };
  return mapping[String(status || "")] || "未知";
}

function scheduleRefresh(delay = 1200) {
  window.clearTimeout(refreshTimer);
  if (isFinished.value) {
    return;
  }
  refreshTimer = window.setTimeout(() => {
    void refreshNow();
  }, delay);
}

function scheduleScreenshotRefresh(delay = 1200) {
  window.clearTimeout(screenshotRefreshTimer);
  if (isFinished.value) {
    return;
  }
  screenshotRefreshTimer = window.setTimeout(() => {
    void loadScreenshot();
  }, delay);
}

async function refreshNow() {
  if (!sessionId.value || loading.value) {
    return;
  }
  loading.value = true;
  try {
    session.value = await apiRequest(`/api/browser-verification/sessions/${encodeURIComponent(sessionId.value)}`);
    await loadScreenshot();
  } catch (error) {
    session.value = {
      status: "failed",
      message: error instanceof Error ? error.message : "验证会话读取失败。",
      target: {},
    };
  } finally {
    loading.value = false;
    scheduleRefresh();
  }
}

async function loadScreenshot() {
  if (!sessionId.value) {
    return;
  }
  try {
    const response = await fetch(`/api/browser-verification/sessions/${encodeURIComponent(sessionId.value)}/screenshot?ts=${Date.now()}`, {
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "image/jpeg" },
    });
    if (response.status === 204) {
      scheduleScreenshotRefresh();
      return;
    }
    if (!response.ok) {
      scheduleScreenshotRefresh();
      return;
    }
    const blob = await response.blob();
    if (!blob.size) {
      scheduleScreenshotRefresh();
      return;
    }
    if (screenshotObjectUrl) {
      URL.revokeObjectURL(screenshotObjectUrl);
    }
    screenshotObjectUrl = URL.createObjectURL(blob);
    screenshotUrl.value = screenshotObjectUrl;
    scheduleScreenshotRefresh();
  } catch {
    scheduleScreenshotRefresh();
  }
}

function commandCoordinates(event) {
  const rect = viewerRef.value?.getBoundingClientRect();
  const viewport = session.value?.viewport || {};
  if (!rect || !rect.width || !rect.height) {
    return { x: 0, y: 0 };
  }
  const scaleX = Number(viewport.width || 1365) / rect.width;
  const scaleY = Number(viewport.height || 768) / rect.height;
  return {
    x: Math.max(0, Math.round((event.clientX - rect.left) * scaleX)),
    y: Math.max(0, Math.round((event.clientY - rect.top) * scaleY)),
  };
}

async function sendInput(payload) {
  if (!sessionId.value || isFinished.value) {
    return;
  }
  try {
    await apiRequest(`/api/browser-verification/sessions/${encodeURIComponent(sessionId.value)}/input`, {
      method: "POST",
      body: payload,
    });
  } catch (error) {
    console.warn("验证输入发送失败", error);
  }
}

function sendPointerCommand(type, event) {
  viewerRef.value?.focus();
  void sendInput({ type, ...commandCoordinates(event) });
}

function handleMouseMove(event) {
  const now = Date.now();
  if (now - mouseMoveSentAt < 180) {
    return;
  }
  mouseMoveSentAt = now;
  void sendInput({ type: "mousemove", ...commandCoordinates(event) });
}

function sendWheelCommand(event) {
  void sendInput({
    type: "wheel",
    ...commandCoordinates(event),
    delta_x: Math.round(event.deltaX || 0),
    delta_y: Math.round(event.deltaY || 0),
  });
}

function sendKeyCommand(event) {
  const key = String(event.key || "");
  if (!key) {
    return;
  }
  void sendInput({ type: "key", key });
}

function sendPasteCommand(event) {
  const text = event.clipboardData?.getData("text") || "";
  if (text) {
    void sendInput({ type: "text", text });
  }
}

async function cancelSession() {
  cancelling.value = true;
  try {
    session.value = await apiRequest(`/api/browser-verification/sessions/${encodeURIComponent(sessionId.value)}/cancel`, {
      method: "POST",
    });
  } finally {
    cancelling.value = false;
  }
}

watch(() => session.value?.status, () => {
  if (isFinished.value) {
    window.clearTimeout(refreshTimer);
  }
});

onMounted(() => {
  unsubscribeStateRefresh = subscribeStateRefresh(["browser_verification", "archive_queue", "missing_3mf"], () => {
    void refreshNow();
  });
  void refreshNow();
});

onBeforeUnmount(() => {
  window.clearTimeout(refreshTimer);
  window.clearTimeout(screenshotRefreshTimer);
  if (screenshotObjectUrl) {
    URL.revokeObjectURL(screenshotObjectUrl);
    screenshotObjectUrl = "";
  }
  if (typeof unsubscribeStateRefresh === "function") {
    unsubscribeStateRefresh();
    unsubscribeStateRefresh = null;
  }
});
</script>
