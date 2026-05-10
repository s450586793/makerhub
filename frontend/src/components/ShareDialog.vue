<template>
  <div
    v-if="visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="share-dialog-title"
    @click="$emit('close')"
  >
    <div class="submit-dialog__panel share-dialog__panel" @click.stop>
      <h2 id="share-dialog-title">分享模型</h2>
      <p>本次会生成一个静态分享码，接收方导入前会先检查是否重复。</p>

      <div class="share-dialog__summary">
        <span>已选模型</span>
        <strong>{{ modelDirs.length }} 个</strong>
      </div>

      <form class="share-dialog__form" @submit.prevent="createShare">
        <p v-if="status" class="form-status share-dialog__status">{{ status }}</p>
        <p v-if="error" class="form-status share-dialog__status is-error">{{ error }}</p>

        <div v-if="shareCode" class="share-dialog__code">
          <span>分享码</span>
          <textarea :value="shareCode" readonly rows="4"></textarea>
          <button class="button button-secondary button-small" type="button" @click="copyShareCode">复制分享码</button>
        </div>

        <div class="submit-dialog__actions">
          <button class="button button-secondary" type="button" :disabled="submitting" @click="$emit('close')">关闭</button>
          <button class="button button-primary" type="submit" :disabled="submitting || !modelDirs.length">
            {{ submitting ? "生成中..." : "生成分享码" }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup>
import { ref, watch } from "vue";

import { apiRequest } from "../lib/api";

const props = defineProps({
  visible: {
    type: Boolean,
    default: false,
  },
  modelDirs: {
    type: Array,
    default: () => [],
  },
});

defineEmits(["close"]);

const submitting = ref(false);
const status = ref("");
const error = ref("");
const shareCode = ref("");

function resetDialog() {
  status.value = "";
  error.value = "";
  shareCode.value = "";
}

async function createShare() {
  submitting.value = true;
  status.value = "";
  error.value = "";
  try {
    const response = await apiRequest("/api/sharing/create", {
      method: "POST",
      body: {
        model_dirs: props.modelDirs,
      },
    });
    shareCode.value = response.share_code || "";
    status.value = response.message || "分享码已生成。";
  } catch (err) {
    error.value = err instanceof Error ? err.message : "生成分享码失败。";
  } finally {
    submitting.value = false;
  }
}

async function copyShareCode() {
  if (!shareCode.value) {
    return;
  }
  try {
    await navigator.clipboard.writeText(shareCode.value);
    status.value = "分享码已复制。";
  } catch (err) {
    error.value = "复制失败，请手动选择分享码。";
  }
}

watch(() => props.visible, (visible) => {
  if (visible) {
    resetDialog();
  }
});
</script>
