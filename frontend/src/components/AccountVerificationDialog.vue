<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="submit-dialog account-verification-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="account-verification-title"
      @click.self="requestClose"
      @keydown="handleDialogKeydown"
    >
      <div ref="panel" class="submit-dialog__panel account-verification-dialog__panel">
        <header class="account-verification-dialog__header">
          <h2 id="account-verification-title">{{ platformLabel }}验证</h2>
          <button ref="closeButton" class="button button-secondary button-small" type="button" @click="requestClose">关闭</button>
        </header>

        <p class="account-verification-dialog__message" role="status" aria-live="polite">
          {{ state.message || "请在画面中完成验证，然后点击“验证完成”。" }}
        </p>
        <div v-if="imageUrl && !state.completed" class="account-verification-dialog__view-tools">
          <div class="account-verification-dialog__zoom" role="group" aria-label="画面缩放">
            <button
              v-for="level in [1, 1.5, 2]"
              :key="level"
              :class="['button', zoom === level ? 'button-primary' : 'button-secondary', 'button-small']"
              type="button"
              :aria-pressed="zoom === level"
              @click="setZoom(level)"
            >{{ level * 100 }}%</button>
          </div>
          <button
            v-if="zoom > 1"
            :class="['button', panMode ? 'button-primary' : 'button-secondary', 'button-small']"
            type="button"
            :aria-pressed="panMode"
            title="开启后可拖动画面，关闭后操作验证码"
            @click="panMode = !panMode"
          >移动画面</button>
        </div>
        <div
          ref="viewport"
          class="account-verification-dialog__viewport"
          :class="{ 'has-image': imageUrl, 'is-interactive': canInteract, 'is-zoomed': zoom > 1, 'is-panning': panMode }"
          tabindex="0"
          :aria-label="panMode ? '拖动以移动验证画面' : '远程验证页面，可点击、拖动、滚动和输入文字'"
          :aria-busy="state.loading || state.finishing"
          @pointerdown="handlePointerDown"
          @pointermove="handlePointerMove"
          @pointerup="handlePointerUp"
          @pointercancel="handlePointerUp"
          @lostpointercapture="handlePointerUp"
          @wheel.prevent="handleWheel"
          @keydown="handleRemoteKeydown"
          @paste="handlePaste"
        >
          <img
            v-if="imageUrl"
            ref="remoteImage"
            :src="imageUrl"
            :width="state.width"
            :height="state.height"
            :style="imageStyle"
            alt="当前验证页面"
            draggable="false"
            @dragstart.prevent
          >
          <span v-else>{{ state.error || ['error', 'failed', 'cancelled', 'expired'].includes(state.status) ? "验证页面未能加载。" : "正在加载验证页面…" }}</span>
        </div>

        <p v-if="state.error || inputError" class="form-status is-error account-verification-dialog__error" role="alert">{{ inputError || state.error }}</p>
        <form v-if="canInteract" class="account-verification-dialog__text" @submit.prevent="sendText">
          <input v-model="text" type="text" maxlength="2000" autocomplete="off" aria-label="向验证页面输入文字" placeholder="输入验证码或文字">
          <button class="button button-secondary button-small" type="submit" :disabled="!text">发送文字</button>
        </form>
        <footer class="submit-dialog__actions account-verification-dialog__actions">
          <button
            v-if="['error', 'failed', 'cancelled', 'expired'].includes(state.status)"
            class="button button-primary button-small"
            type="button"
            @click="restart"
          >重新打开</button>
          <template v-else-if="!state.completed">
            <button class="button button-secondary button-small" type="button" :disabled="state.loading || state.finishing" @click="controller.refresh()">刷新画面</button>
            <button class="button button-primary button-small" type="button" :disabled="state.loading || state.finishing || !state.session_id" @click="finish">
              {{ state.finishing ? "正在检查…" : "验证完成" }}
            </button>
          </template>
          <button v-else class="button button-primary button-small" type="button" @click="requestClose">完成</button>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import { apiRequest } from "../lib/api";
import { createManualVerificationController, verificationImageUrl, verificationKeyboardInput, verificationPointerPoint } from "../lib/manualVerification";

const props = defineProps({
  visible: { type: Boolean, default: false },
  action: { type: Object, default: () => ({}) },
  platform: { type: String, default: "" },
});
const emit = defineEmits(["close", "completed"]);
const panel = ref(null);
const closeButton = ref(null);
const viewport = ref(null);
const remoteImage = ref(null);
const state = ref({ loading: true, completed: false });
const text = ref("");
const inputError = ref("");
const zoom = ref(1);
const baseImageWidth = ref(0);
const panMode = ref(false);
const imageUrl = computed(() => verificationImageUrl(state.value.image));
const imageStyle = computed(() => zoom.value > 1 ? { width: `${baseImageWidth.value * zoom.value}px`, maxWidth: "none", maxHeight: "none", flexShrink: 0 } : {});
const platformLabel = computed(() => (state.value.platform || props.platform) === "global" ? "国际站" : "国内站");
const canInteract = computed(() => Boolean(imageUrl.value && state.value.session_id && !state.value.loading && !state.value.finishing && !state.value.completed && !["error", "failed", "cancelled", "expired"].includes(state.value.status)));
let previousFocus = null;
let pointer = null;
const controller = createManualVerificationController({
  request: apiRequest,
  onChange: (value) => { state.value = value; },
  onComplete: (value) => emit("completed", value),
});

function restoreFocus() {
  previousFocus?.focus?.({ preventScroll: true });
  previousFocus = null;
}

function requestClose() {
  pointer = null;
  void controller.close();
  emit("close");
}

function restart() {
  pointer = null;
  text.value = "";
  inputError.value = "";
  zoom.value = 1;
  baseImageWidth.value = 0;
  panMode.value = false;
  void controller.open(props.action);
}

async function setZoom(level) {
  if (pointer || ![1, 1.5, 2].includes(level)) return;
  baseImageWidth.value = (remoteImage.value?.getBoundingClientRect().width || 0) / zoom.value;
  zoom.value = level;
  if (level === 1) panMode.value = false;
  await nextTick();
  if (viewport.value) {
    viewport.value.scrollLeft = Math.max(0, (viewport.value.scrollWidth - viewport.value.clientWidth) / 2);
    viewport.value.scrollTop = Math.max(0, (viewport.value.scrollHeight - viewport.value.clientHeight) / 2);
  }
}

function point(event) {
  const bounds = remoteImage.value?.getBoundingClientRect();
  return verificationPointerPoint(event, bounds, Number(state.value.width), Number(state.value.height));
}

function pointerTime(event = {}) {
  return Number.isFinite(event.timeStamp) && event.timeStamp > 0 ? event.timeStamp : performance.now();
}

function pointerDelay(time) {
  return Math.round(Math.max(0, Math.min(50, time - pointer.lastAt)));
}

function handlePointerDown(event) {
  if (!canInteract.value || pointer || (event.pointerType === "mouse" && event.button !== 0)) return;
  if (panMode.value && zoom.value > 1) {
    event.preventDefault();
    pointer = { id: event.pointerId, mode: "pan", clientX: event.clientX, clientY: event.clientY, scrollLeft: viewport.value.scrollLeft, scrollTop: viewport.value.scrollTop };
    viewport.value?.focus({ preventScroll: true });
    try { viewport.value?.setPointerCapture(event.pointerId); } catch { /* 保留普通拖动画面。 */ }
    return;
  }
  const position = point(event);
  if (!position) return;
  event.preventDefault();
  viewport.value?.focus({ preventScroll: true });
  pointer = { id: event.pointerId, mode: "remote", ...position, lastAt: pointerTime(event) };
  try { viewport.value?.setPointerCapture(event.pointerId); } catch { /* 不支持捕获时仍允许普通点击。 */ }
  void controller.sendEvents([{ type: "pointerdown", ...position, delay_ms: 0 }]);
}

function handlePointerMove(event) {
  if (!pointer || pointer.id !== event.pointerId || !canInteract.value) return;
  if (pointer.mode === "pan") {
    event.preventDefault();
    viewport.value.scrollLeft = pointer.scrollLeft - (event.clientX - pointer.clientX);
    viewport.value.scrollTop = pointer.scrollTop - (event.clientY - pointer.clientY);
    return;
  }
  const position = point(event);
  if (!position) return;
  event.preventDefault();
  const time = pointerTime(event);
  pointer = { ...pointer, ...position };
  if (time - pointer.lastAt < 16) return;
  const delay = pointerDelay(time);
  pointer.lastAt = time;
  void controller.sendEvents([{ type: "pointermove", ...position, delay_ms: delay }]);
}

function handlePointerUp(event) {
  if (!pointer || pointer.id !== event.pointerId) return;
  if (pointer.mode === "pan") {
    pointer = null;
    try { viewport.value?.releasePointerCapture(event.pointerId); } catch { /* 取消事件可能已经释放捕获。 */ }
    return;
  }
  const position = point(event) || { x: pointer.x, y: pointer.y };
  const delay = pointerDelay(pointerTime(event));
  pointer = null;
  void controller.sendEvents([{ type: "pointerup", ...position, delay_ms: delay }]);
  try { viewport.value?.releasePointerCapture(event.pointerId); } catch { /* 取消事件可能已经释放捕获。 */ }
}

function handleWheel(event) {
  if (!canInteract.value) return;
  if (panMode.value && zoom.value > 1) {
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.value.clientHeight : 1;
    viewport.value.scrollTop += event.deltaY * unit;
    viewport.value.scrollLeft += (event.deltaX || 0) * unit;
    return;
  }
  const position = point(event);
  if (!position || !Number.isFinite(event.deltaY)) return;
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? Number(state.value.height) : 1;
  void controller.sendEvents([{ type: "wheel", ...position, delta_y: Math.max(-1200, Math.min(1200, event.deltaY * unit)) }]);
}

function handleRemoteKeydown(event) {
  if (!canInteract.value || panMode.value || event.key === "Escape") return;
  const input = verificationKeyboardInput(event);
  if (!input) return;
  event.preventDefault();
  event.stopPropagation();
  void controller.sendEvents([input]);
}

function handlePaste(event) {
  if (!canInteract.value || panMode.value) return;
  event.preventDefault();
  const value = event.clipboardData?.getData("text/plain") || "";
  if (value.length > 2000) {
    inputError.value = "每次最多输入 2000 个字符。";
    return;
  }
  if (value) void controller.sendEvents([{ type: "text", text: value }]);
}

function sendText() {
  if (!canInteract.value || !text.value) return;
  void controller.sendEvents([{ type: "text", text: text.value }]);
  text.value = "";
  inputError.value = "";
  viewport.value?.focus({ preventScroll: true });
}

function finish() {
  if (pointer?.mode === "remote") {
    void controller.sendEvents([{ type: "pointerup", x: pointer.x, y: pointer.y, delay_ms: pointerDelay(performance.now()) }]);
    pointer = null;
  }
  pointer = null;
  void controller.finish();
}

function handleDialogKeydown(event) {
  if (!props.visible || event.defaultPrevented) return;
  if (event.key === "Escape" && !event.isComposing) {
    event.preventDefault();
    event.stopPropagation();
    requestClose();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = Array.from(panel.value?.querySelectorAll('button:not([disabled]), input:not([disabled]), [tabindex="0"]') || []);
  const index = focusable.indexOf(document.activeElement);
  if (index === -1 || (event.shiftKey ? index === 0 : index === focusable.length - 1)) {
    event.preventDefault();
    (event.shiftKey ? focusable.at(-1) : focusable[0])?.focus();
  }
}

watch(() => [props.visible, props.action], async ([visible], previous) => {
  if (!visible) {
    document.removeEventListener("keydown", handleDialogKeydown);
    pointer = null;
    void controller.close();
    restoreFocus();
    return;
  }
  document.addEventListener("keydown", handleDialogKeydown);
  if (!previous?.[0]) previousFocus = document.activeElement;
  restart();
  await nextTick();
  if (props.visible) closeButton.value?.focus();
}, { immediate: true });

onBeforeUnmount(() => {
  document.removeEventListener("keydown", handleDialogKeydown);
  pointer = null;
  void controller.close();
  restoreFocus();
});
</script>

<style scoped>
.account-verification-dialog {
  padding: 16px;
  background: color-mix(in srgb, var(--bg) 72%, transparent);
  backdrop-filter: none;
}

.account-verification-dialog__panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(680px, 100%);
  min-width: 0;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  padding: 16px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  text-align: left;
  background: var(--surface);
  box-shadow: none;
}

.account-verification-dialog__header,
.account-verification-dialog__view-tools,
.account-verification-dialog__text {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.account-verification-dialog__zoom {
  display: flex;
  gap: 6px;
}

.account-verification-dialog__view-tools {
  flex-wrap: wrap;
  gap: 8px;
}

.account-verification-dialog__header h2 {
  margin: 0;
  font-size: 18px;
}

.account-verification-dialog__panel .account-verification-dialog__message,
.account-verification-dialog__panel .account-verification-dialog__error {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.account-verification-dialog__viewport {
  display: flex;
  flex: 0 1 auto;
  align-items: center;
  justify-content: center;
  min-width: 0;
  min-height: 88px;
  max-height: min(480px, 56dvh);
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-soft);
  color: var(--muted);
  touch-action: none;
}

.account-verification-dialog__viewport.has-image {
  align-items: flex-start;
  min-height: 0;
}

.account-verification-dialog__viewport:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}

.account-verification-dialog__viewport.is-interactive {
  cursor: crosshair;
}

.account-verification-dialog__viewport.is-zoomed {
  align-items: flex-start;
  justify-content: flex-start;
}

.account-verification-dialog__viewport.is-panning {
  cursor: grab;
}

.account-verification-dialog__viewport img {
  display: block;
  width: auto;
  height: auto;
  max-width: 100%;
  flex-shrink: 0;
  object-fit: contain;
  user-select: none;
  -webkit-user-drag: none;
}

.account-verification-dialog__text input {
  flex: 1;
  min-width: 0;
}

.account-verification-dialog__text button {
  flex-shrink: 0;
}

.account-verification-dialog__actions {
  justify-content: flex-end;
  gap: 8px;
}

@media (max-width: 600px) {
  .account-verification-dialog {
    padding: 8px;
  }

  .account-verification-dialog__panel {
    max-height: calc(100dvh - 16px);
    padding: 12px;
  }

  .account-verification-dialog__viewport {
    max-height: 48dvh;
  }
}
</style>
