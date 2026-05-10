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
        <label class="field-card">
          <span>有效期</span>
          <input v-model.number="form.expires_days" type="number" min="1" max="90" step="1">
        </label>

        <div class="share-dialog__checks">
          <label class="switch">
            <input v-model="form.include_images" type="checkbox">
            <span>包含图片</span>
          </label>
          <label class="switch">
            <input v-model="form.include_model_files" type="checkbox">
            <span>包含模型文件</span>
          </label>
          <label class="switch">
            <input v-model="form.include_attachments" type="checkbox">
            <span>包含附件</span>
          </label>
          <label class="switch">
            <input v-model="form.include_comments" type="checkbox">
            <span>包含评论</span>
          </label>
        </div>

        <div class="share-dialog__chips">
          <span>模型文件</span>
          <label v-for="item in modelFileOptions" :key="item.value">
            <input v-model="form.model_file_types" type="checkbox" :value="item.value" :disabled="!form.include_model_files">
            <em>{{ item.label }}</em>
          </label>
        </div>

        <div class="share-dialog__chips">
          <span>附件</span>
          <label v-for="item in attachmentOptions" :key="item.value">
            <input v-model="form.attachment_file_types" type="checkbox" :value="item.value" :disabled="!form.include_attachments">
            <em>{{ item.label }}</em>
          </label>
        </div>

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
import { reactive, ref, watch } from "vue";

import { apiRequest } from "../lib/api";
import { appState, refreshConfig } from "../lib/appState";


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

const modelFileOptions = [
  { value: "3mf", label: "3MF" },
  { value: "stl", label: "STL" },
  { value: "step", label: "STEP" },
  { value: "obj", label: "OBJ" },
];
const attachmentOptions = [
  { value: "pdf", label: "PDF" },
  { value: "excel", label: "Excel" },
];

const form = reactive({
  expires_days: 7,
  include_images: true,
  include_model_files: true,
  model_file_types: ["3mf", "stl", "step", "obj"],
  include_attachments: true,
  attachment_file_types: ["pdf", "excel"],
  include_comments: true,
});
const submitting = ref(false);
const status = ref("");
const error = ref("");
const shareCode = ref("");

async function applyDefaults() {
  let config = appState.config;
  if (!config) {
    try {
      config = await refreshConfig();
    } catch (err) {
      config = null;
    }
  }
  const sharing = config?.sharing || {};
  form.expires_days = Number(sharing.default_expires_days || 7);
  form.include_images = sharing.include_images !== false;
  form.include_model_files = sharing.include_model_files !== false;
  form.model_file_types = Array.isArray(sharing.model_file_types) && sharing.model_file_types.length
    ? [...sharing.model_file_types]
    : ["3mf", "stl", "step", "obj"];
  form.include_attachments = sharing.include_attachments !== false;
  form.attachment_file_types = Array.isArray(sharing.attachment_file_types) && sharing.attachment_file_types.length
    ? [...sharing.attachment_file_types]
    : ["pdf", "excel"];
  form.include_comments = sharing.include_comments !== false;
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
        options: { ...form },
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
    void applyDefaults();
  }
});
</script>
