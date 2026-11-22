<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="submit-dialog account-verification-dialog"
      :class="{ 'account-verification-dialog--challenge': showChallenge }"
      role="dialog"
      aria-modal="true"
      :aria-label="`${platformLabel}验证`"
      @click.self="requestClose"
      @keydown="handleDialogKeydown"
    >
      <div ref="panel" class="account-verification-dialog__panel" tabindex="-1">
        <div v-show="showChallenge" ref="captchaHost" class="account-verification-dialog__captcha" :aria-busy="sdkLoading"></div>
        <section v-if="!showChallenge || sdkLoading" class="account-verification-dialog__status">
          <header class="account-verification-dialog__header">
            <h2>{{ platformLabel }}验证</h2>
            <button ref="closeButton" class="button button-secondary button-small" type="button" @click="requestClose">关闭</button>
          </header>
          <p class="account-verification-dialog__message" role="status" aria-live="polite">
            {{ sdkLoading ? "正在加载验证码…" : (state.message || "正在准备验证码…") }}
          </p>
          <p v-if="state.error || sdkError" class="form-status is-error account-verification-dialog__error" role="alert">{{ sdkError || state.error }}</p>
          <footer v-if="canRetry || state.completed" class="submit-dialog__actions account-verification-dialog__actions">
            <button v-if="canRetry" class="button button-primary button-small" type="button" @click="restart">重试</button>
            <button v-else class="button button-primary button-small" type="button" @click="requestClose">完成</button>
          </footer>
        </section>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import { apiRequest } from "../lib/api";
import { createGeetestFrame } from "../lib/geetestFrame";
import { createManualVerificationController } from "../lib/manualVerification";

const props = defineProps({
  visible: { type: Boolean, default: false },
  action: { type: Object, default: () => ({}) },
  platform: { type: String, default: "" },
});
const emit = defineEmits(["close", "completed"]);
const panel = ref(null);
const closeButton = ref(null);
const captchaHost = ref(null);
const state = ref({ loading: true, status: "preparing", completed: false, challenge: null });
const sdkLoading = ref(false);
const sdkError = ref("");
const showChallenge = computed(() => state.value.status === "challenge" && Boolean(state.value.challenge) && !sdkError.value);
const canRetry = computed(() => Boolean(sdkError.value) || ["failed", "cancelled", "expired"].includes(state.value.status));
const platformLabel = computed(() => (state.value.platform || props.platform) === "global" ? "国际站" : "国内站");
const challengeKey = computed(() => props.visible && state.value.status === "challenge" && state.value.challenge
  ? `${state.value.session_id}:${JSON.stringify(state.value.challenge)}` : "");
let previousFocus = null;
let sdk = null;
let sdkVersion = 0;
let mountedKey = "";
const controller = createManualVerificationController({
  request: apiRequest,
  onChange: (value) => { state.value = value; },
  onComplete: (value) => emit("completed", value),
});

function clearSdk() {
  sdkVersion += 1;
  sdk?.destroy();
  sdk = null;
  mountedKey = "";
  sdkLoading.value = false;
}

function restoreFocus() {
  previousFocus?.focus?.({ preventScroll: true });
  previousFocus = null;
}

function focusDialog() {
  const target = closeButton.value || captchaHost.value?.querySelector("iframe") || panel.value;
  target?.focus({ preventScroll: true });
}

function requestClose() {
  clearSdk();
  void controller.close();
  emit("close");
}

function restart() {
  clearSdk();
  sdkError.value = "";
  void controller.open(props.action);
}

async function mountChallenge(key) {
  if (key && mountedKey === key) return;
  clearSdk();
  sdkError.value = "";
  if (!key) return;
  const version = sdkVersion;
  const challenge = state.value.challenge;
  sdkLoading.value = true;
  await nextTick();
  const current = () => props.visible && version === sdkVersion && key === challengeKey.value;
  if (!current() || !captchaHost.value) return;
  mountedKey = key;
  try {
    sdk = createGeetestFrame({
      container: captchaHost.value,
      challenge,
      onReady: () => {
        if (!current()) return;
        sdkLoading.value = false;
        void nextTick(() => { if (current()) focusDialog(); });
      },
      onSuccess: (proof) => {
        if (!current()) return;
        sdkLoading.value = false;
        void controller.complete(challenge.challenge_id, proof);
      },
      onError: (message) => {
        if (!current()) return;
        sdkLoading.value = false;
        sdkError.value = message;
      },
      onClose: () => {
        if (!current()) return;
        requestClose();
      },
    });
  } catch (error) {
    if (!current()) return;
    sdkLoading.value = false;
    sdkError.value = error instanceof Error ? error.message : "验证码加载失败，请重试。";
  }
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
  const focusable = Array.from(panel.value?.querySelectorAll('button:not([disabled]), iframe, [tabindex="0"]') || []);
  const index = focusable.indexOf(document.activeElement);
  if (index === -1 || (event.shiftKey ? index === 0 : index === focusable.length - 1)) {
    event.preventDefault();
    (event.shiftKey ? focusable.at(-1) : focusable[0])?.focus();
  }
}

function keepDialogFocus(event) {
  if (props.visible && panel.value && !panel.value.contains(event.target)) focusDialog();
}

watch(() => [props.visible, props.action], async ([visible], previous) => {
  if (!visible) {
    document.removeEventListener("keydown", handleDialogKeydown);
    document.removeEventListener("focusin", keepDialogFocus);
    clearSdk();
    void controller.close();
    restoreFocus();
    return;
  }
  document.addEventListener("keydown", handleDialogKeydown);
  document.addEventListener("focusin", keepDialogFocus);
  if (!previous?.[0]) previousFocus = document.activeElement;
  restart();
  await nextTick();
  if (props.visible) focusDialog();
}, { immediate: true });

watch(challengeKey, mountChallenge);

onBeforeUnmount(() => {
  document.removeEventListener("keydown", handleDialogKeydown);
  document.removeEventListener("focusin", keepDialogFocus);
  clearSdk();
  void controller.close();
  restoreFocus();
});
</script>

<style scoped>
.account-verification-dialog {
  padding: 0;
  background: color-mix(in srgb, var(--bg) 72%, transparent);
  backdrop-filter: none;
}

.account-verification-dialog--challenge {
  background: transparent;
}

.account-verification-dialog__panel {
  position: relative;
  display: grid;
  place-items: center;
  width: 100%;
  height: 100dvh;
  min-width: 0;
  overflow: hidden;
  outline: none;
}

.account-verification-dialog__status {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  width: min(360px, calc(100% - 32px));
  min-width: 0;
  max-height: calc(100dvh - 32px);
  overflow-y: auto;
  padding: var(--space-4);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  text-align: left;
  background: var(--surface);
  box-shadow: none;
}

.account-verification-dialog__header {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.account-verification-dialog__header h2 {
  margin: 0;
  font-size: 18px;
}

.account-verification-dialog__message,
.account-verification-dialog__error {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.account-verification-dialog__captcha {
  position: absolute;
  inset: 0;
  min-width: 0;
  overflow: hidden;
}

.account-verification-dialog__captcha :deep(iframe) {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
  color-scheme: light;
}

.account-verification-dialog__actions {
  flex-shrink: 0;
}
</style>
