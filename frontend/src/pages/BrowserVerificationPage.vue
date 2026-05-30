<template>
  <main class="browser-verification-shell">
    <section class="browser-verification-panel">
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
            <span v-if="emptyMessage">{{ emptyMessage }}</span>
          </div>
        </div>
      </div>
      <span
        v-if="visibleMessageText"
        :class="['form-status', isError && 'is-error', isCompleted && 'is-success']"
      >{{ visibleMessageText }}</span>
    </section>
  </main>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { apiRequest } from "../lib/api";
import { subscribeStateRefresh } from "../lib/stateEvents";


const route = useRoute();
const session = ref(null);
const loading = ref(false);
const screenshotUrl = ref("");
const screenshotLoaded = ref(false);
const viewerRef = ref(null);
let refreshTimer = 0;
let screenshotRefreshTimer = 0;
let screenshotObjectUrl = "";
let unsubscribeStateRefresh = null;
let mouseMoveSentAt = 0;

const sessionId = computed(() => String(route.params.sessionId || ""));
const isFinished = computed(() => ["completed", "failed", "cancelled", "expired"].includes(String(session.value?.status || "")));
const isError = computed(() => ["failed", "expired"].includes(String(session.value?.status || "")));
const isCompleted = computed(() => String(session.value?.status || "") === "completed");
const visibleMessageText = computed(() => {
  if (!isFinished.value) {
    return "";
  }
  return session.value?.error || session.value?.message || "";
});
const emptyTitle = computed(() => {
  if (isError.value) {
    return "验证页面加载失败";
  }
  if (isCompleted.value) {
    return "验证已完成";
  }
  return "正在加载验证页面";
});
const emptyMessage = computed(() => {
  if (!isFinished.value) {
    return "";
  }
  return session.value?.error || session.value?.message || "";
});

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
