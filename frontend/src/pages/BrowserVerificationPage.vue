<template>
  <main class="browser-verification-shell">
    <section class="browser-verification-panel">
      <div class="browser-verification-viewer">
        <div
          ref="viewerRef"
          class="browser-verification-frame"
          tabindex="0"
          role="application"
          :style="viewerFrameStyle"
          @click="handleClick"
          @pointerdown.prevent="handlePointerDown"
          @pointermove.prevent="handlePointerMove"
          @pointerup.prevent="handlePointerUp"
          @pointercancel.prevent="handlePointerCancel"
          @lostpointercapture="handlePointerCancel"
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
import {
  createDragState,
  frameAspectRatio,
  isDragClickSuppressed,
  mapFramePointToViewport,
  markPointerMoveSent,
  notePointerMove,
  shouldSendPointerMove,
} from "../lib/browserVerificationInput";
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
let activePointerState = null;
let suppressNextClick = false;
let inputQueue = Promise.resolve();

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
const viewerFrameStyle = computed(() => ({
  "--browser-verification-aspect-ratio": frameAspectRatio(session.value?.viewport),
}));
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
  if (activePointerState && delay < 2400) {
    delay = 2400;
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
  return mapFramePointToViewport(event, rect, session.value?.viewport || {});
}

function pointerStateCoordinates(state) {
  return commandCoordinates({
    clientX: state.lastX,
    clientY: state.lastY,
  });
}

async function postInput(payload) {
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

function sendInput(payload) {
  inputQueue = inputQueue.then(
    () => postInput(payload),
    () => postInput(payload),
  );
  return inputQueue;
}

function sendPointerCommand(type, event) {
  viewerRef.value?.focus();
  void sendInput({ type, ...commandCoordinates(event) });
}

function handleClick(event) {
  if (suppressNextClick) {
    suppressNextClick = false;
    return;
  }
  sendPointerCommand("click", event);
}

function handlePointerDown(event) {
  viewerRef.value?.focus();
  const coordinates = commandCoordinates(event);
  activePointerState = createDragState({
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    now: Date.now(),
  });
  suppressNextClick = false;
  try {
    event.currentTarget?.setPointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture is best effort.
  }
  void sendInput({ type: "mousedown", ...coordinates });
}

function handlePointerMove(event) {
  if (!activePointerState) {
    return;
  }
  const now = Date.now();
  notePointerMove(activePointerState, {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    now,
  });
  if (!shouldSendPointerMove({ state: activePointerState, pointerId: event.pointerId, now, dragging: true })) {
    return;
  }
  markPointerMoveSent(activePointerState, now);
  void sendInput({ type: "mousemove", ...commandCoordinates(event) });
}

function finishPointerDrag(event, cancelled = false) {
  if (!activePointerState || activePointerState.pointerId !== event.pointerId) {
    return;
  }
  const state = activePointerState;
  const releaseCoordinates = cancelled ? pointerStateCoordinates(state) : commandCoordinates(event);
  if (!cancelled) {
    void sendInput({ type: "mousemove", ...releaseCoordinates });
  }
  const releaseCommand = sendInput({ type: "mouseup", ...releaseCoordinates });
  suppressNextClick = isDragClickSuppressed(state);
  activePointerState = null;
  try {
    event.currentTarget?.releasePointerCapture?.(event.pointerId);
  } catch {
    // Pointer capture release is best effort.
  }
  void releaseCommand.finally(() => {
    scheduleScreenshotRefresh(80);
  });
}

function handlePointerUp(event) {
  finishPointerDrag(event, false);
}

function handlePointerCancel(event) {
  finishPointerDrag(event, true);
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
