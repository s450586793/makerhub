<template>
  <section class="surface surface--filters library-toolbar">
    <div class="library-toolbar__copy">
      <span class="eyebrow">本地库</span>
      <div class="library-toolbar__title-row">
        <h1>本地库管理</h1>
      </div>
    </div>
    <div class="library-toolbar__side">
      <div class="toolbar-stats">
        <span class="toolbar-stat">
          <em>空闲</em>
          <strong>{{ organizerStatusText }}</strong>
        </span>
        <span class="toolbar-stat">
          <em>候选</em>
          <strong>{{ detectedTotalText }}</strong>
        </span>
        <span class="toolbar-stat">
          <em>运行</em>
          <strong>{{ runningCountText }}</strong>
        </span>
        <span class="toolbar-stat">
          <em>排队</em>
          <strong>{{ queuedCountText }}</strong>
        </span>
      </div>
      <div class="filter-actions">
        <button class="button button-primary" type="button" @click="openImportDialog">
          导入
        </button>
      </div>
    </div>
  </section>

  <section v-if="!initialLoaded && !loadError" class="surface empty-state subscription-inline-empty">
    <h2>正在加载本地库</h2>
    <p>正在读取本地整理入口和本地状态卡片。</p>
  </section>

  <section v-else-if="loadError" class="surface empty-state subscription-inline-empty">
    <h2>本地库加载失败</h2>
    <p>{{ loadError }}</p>
    <button class="button button-secondary" type="button" :disabled="loading" @click="load()">
      {{ loading ? "刷新中..." : "重新加载" }}
    </button>
  </section>

  <section v-else class="library-section">
    <div v-if="localLibraryCards.length" class="source-library-grid">
      <SourceLibraryCard
        v-for="card in localLibraryCards"
        :key="card.key"
        :card="card"
        @open="openCard"
      />
    </div>
    <section v-else class="surface empty-state subscription-inline-empty">
      <h2>本地整理为空</h2>
      <p>当前还没有本地整理导入模型。</p>
    </section>
  </section>

  <div
    v-if="importDialog.visible"
    class="submit-dialog"
    role="dialog"
    aria-modal="true"
    aria-labelledby="local-import-dialog-title"
    @click="closeImportDialog"
  >
    <div class="submit-dialog__panel local-import-dialog__panel" @click.stop>
      <h2 id="local-import-dialog-title">导入本地 3MF</h2>
      <p>确认后先写入暂存区，再移动到本地整理扫描目录，并按现有本地整理流程入库。</p>
      <div
        :class="['local-import-dialog__dropzone', importDialog.dragging && 'is-dragging']"
        role="button"
        tabindex="0"
        @click="openImportFilePicker"
        @keydown.enter.prevent="openImportFilePicker"
        @keydown.space.prevent="openImportFilePicker"
        @dragenter.prevent="setImportDragging(true)"
        @dragover.prevent="setImportDragging(true)"
        @dragleave.prevent="setImportDragging(false)"
        @drop.prevent="handleImportDrop"
      >
        <strong>拖入 3MF</strong>
        <span>或点击选择文件</span>
      </div>
      <input
        ref="importFileInput"
        class="local-import-dialog__input"
        type="file"
        accept=".3mf"
        multiple
        @change="handleImportFileChange"
      >
      <div v-if="importFiles.length" class="local-import-dialog__files">
        <div class="local-import-dialog__files-head">
          <span>待导入</span>
          <strong>{{ importFiles.length }} 个</strong>
        </div>
        <ol>
          <li v-for="(file, index) in importFiles" :key="fileKey(file, index)">
            <span>{{ file.name }}</span>
            <em>{{ formatFileSize(file.size) }}</em>
            <button type="button" :disabled="importDialog.uploading" @click="removeImportFile(index)">移除</button>
          </li>
        </ol>
      </div>
      <p v-if="importDialog.status" class="form-status local-import-dialog__status">{{ importDialog.status }}</p>
      <p v-if="importDialog.error" class="form-status local-import-dialog__status is-error">{{ importDialog.error }}</p>
      <div class="submit-dialog__actions">
        <button class="button button-secondary" type="button" :disabled="importDialog.uploading" @click="closeImportDialog">
          取消
        </button>
        <button class="button button-primary" type="button" :disabled="!importFiles.length || importDialog.uploading" @click="submitImportFiles">
          {{ importDialog.uploading ? "导入中..." : "确认导入" }}
        </button>
      </div>
    </div>
  </div>

</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";

import SourceLibraryCard from "../components/SourceLibraryCard.vue";
import { apiRequest } from "../lib/api";
import { refreshConfig } from "../lib/appState";
import { getPageCache, setPageCache } from "../lib/pageCache";


const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const IDLE_REFRESH_INTERVAL_MS = 30000;
const RECENT_IMPORT_PENDING_GRACE_MS = 10 * 60 * 1000;

const router = useRouter();
const sourceLibraryPayload = ref({
  sections: [],
});
const organizerTasks = ref({
  items: [],
  count: 0,
  queued_count: 0,
  running_count: 0,
  detected_total: 0,
});
const loading = ref(false);
const initialLoaded = ref(false);
const loadError = ref("");
const importFileInput = ref(null);
const importFiles = ref([]);
const importDialog = reactive({
  visible: false,
  dragging: false,
  uploading: false,
  status: "",
  error: "",
});
let refreshTimer = null;
let disposed = false;

function rememberOrganizerPage() {
  setPageCache("organizer", {
    sourceLibraryPayload: sourceLibraryPayload.value,
    organizerTasks: organizerTasks.value,
  });
}

function hydrateOrganizerPageFromCache() {
  const cached = getPageCache("organizer");
  if (!cached?.sourceLibraryPayload || !cached?.organizerTasks) {
    return false;
  }
  sourceLibraryPayload.value = cached.sourceLibraryPayload;
  organizerTasks.value = cached.organizerTasks;
  loadError.value = "";
  initialLoaded.value = true;
  return true;
}

const localSourceSection = computed(() => (
  sourceLibraryPayload.value.sections.find((section) => section?.key === "locals") || { items: [] }
));
const localStateSection = computed(() => (
  sourceLibraryPayload.value.sections.find((section) => section?.key === "states") || { items: [] }
));
const localOrganizerCard = computed(() => {
  const card = localSourceSection.value.items?.find((item) => item?.key === "local-organizer");
  if (card) {
    return {
      ...card,
      title: "本地整理",
      recent_summary: localImportSummaryText.value,
    };
  }
  return {
    key: "local-organizer",
    kind: "local",
    card_kind: "collection",
    title: "本地整理",
    subtitle: "本地 3MF 归档",
    site: "local",
    site_badge: "LOCAL",
    route_kind: "source",
    model_count: 0,
    stats: [
      { label: "候选", value: Number(organizerTasks.value.detected_total || 0) },
      { label: "活跃", value: activeOrganizeCount.value },
    ],
    recent_summary: localImportSummaryText.value,
    preview_models: [],
  };
});
const localStateCards = computed(() => (
  Array.isArray(localStateSection.value.items) ? localStateSection.value.items : []
));
const localLibraryCards = computed(() => (
  initialLoaded.value
    ? [localOrganizerCard.value, ...localStateCards.value]
    : []
));
const activeOrganizeCount = computed(() => (
  Number(organizerTasks.value.running_count || 0) + Number(organizerTasks.value.queued_count || 0)
));
const organizerStatusText = computed(() => {
  if (!initialLoaded.value && loadError.value) {
    return "失败";
  }
  if (!initialLoaded.value) {
    return "读取中";
  }
  return activeOrganizeCount.value > 0 ? "否" : "是";
});
const detectedTotalText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.detected_total || 0)
));
const runningCountText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.running_count || 0)
));
const queuedCountText = computed(() => (
  !initialLoaded.value && loadError.value ? "-" :
  !initialLoaded.value ? "..." : String(organizerTasks.value.queued_count || 0)
));
const localImportSummaryText = computed(() => buildLocalImportSummary(organizerTasks.value));

function fileIdentitySet(files) {
  const identities = new Set();
  for (const item of Array.isArray(files) ? files : []) {
    const sourcePath = String(item?.source_path || "").trim();
    const fileName = String(item?.file_name || "").trim();
    if (sourcePath) {
      identities.add(`path:${sourcePath}`);
    }
    if (fileName) {
      identities.add(`name:${fileName}`);
    }
  }
  return identities;
}

function organizerItemMatchesImport(item, identities) {
  if (!identities.size) {
    return false;
  }
  const sourcePath = String(item?.source_path || "").trim();
  const fileName = String(item?.file_name || "").trim();
  return (sourcePath && identities.has(`path:${sourcePath}`)) || (fileName && identities.has(`name:${fileName}`));
}

function normalizeImportStatus(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "success" || normalized === "organized") return "success";
  if (normalized === "skipped" || normalized === "duplicate_skipped" || normalized === "deleted_model_skipped") return "skipped";
  if (normalized === "failed" || normalized === "error" || normalized === "organize_failed" || normalized === "worker_timeout") return "failed";
  if (normalized === "running" || normalized === "pending" || normalized === "queued") return "pending";
  return "";
}

function countImportStatus(result, status) {
  const normalized = normalizeImportStatus(status);
  if (normalized && Object.prototype.hasOwnProperty.call(result, normalized)) {
    result[normalized] += 1;
    return true;
  }
  return false;
}

function importBatchStillFresh(lastImport) {
  const uploadedAt = Date.parse(String(lastImport?.uploaded_at || ""));
  if (!Number.isFinite(uploadedAt)) {
    return false;
  }
  return Date.now() - uploadedAt < RECENT_IMPORT_PENDING_GRACE_MS;
}

function buildLocalImportSummary(tasks) {
  const lastImport = tasks?.last_import && typeof tasks.last_import === "object" ? tasks.last_import : null;
  const allItems = Array.isArray(tasks?.items) ? tasks.items : [];
  const importFiles = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const identities = fileIdentitySet(importFiles);
  const batchItems = identities.size ? allItems.filter((item) => organizerItemMatchesImport(item, identities)) : [];
  const summaryItems = lastImport ? batchItems : allItems;
  const uploadedCount = Number(lastImport?.uploaded_count || 0);

  const counts = summaryItems.reduce(
    (result, item) => {
      countImportStatus(result, item?.status);
      return result;
    },
    { success: 0, skipped: 0, failed: 0, pending: 0 }
  );

  if (lastImport && !batchItems.length) {
    for (const file of importFiles) {
      countImportStatus(counts, file?.status);
    }
  }

  if (uploadedCount > 0) {
    const parts = [`上传 ${uploadedCount}`, `新增 ${counts.success}`, `跳过 ${counts.skipped}`];
    if (counts.failed) {
      parts.push(`失败 ${counts.failed}`);
    }
    if (counts.pending) {
      parts.push(`处理中 ${counts.pending}`);
    } else if (batchItems.length < uploadedCount && importBatchStillFresh(lastImport)) {
      parts.push(`处理中 ${uploadedCount - batchItems.length}`);
    } else if (batchItems.length < uploadedCount && counts.success + counts.skipped + counts.failed < uploadedCount) {
      parts.push(`待同步 ${uploadedCount - batchItems.length}`);
    }
    return `最近导入：${parts.join(" / ")}`;
  }

  if (!summaryItems.length) {
    return "";
  }
  const total = Number(tasks?.count || summaryItems.length || 0);
  const parts = [`记录 ${total}`, `新增 ${counts.success}`, `跳过 ${counts.skipped}`];
  if (counts.failed) {
    parts.push(`失败 ${counts.failed}`);
  }
  return `最近整理：${parts.join(" / ")}`;
}

function clearTaskTimer() {
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function hasActiveOrganizeTasks() {
  return activeOrganizeCount.value > 0;
}

function syncTaskTimer() {
  clearTaskTimer();
  if (disposed || typeof window === "undefined" || document.hidden) {
    return;
  }
  const delay = hasActiveOrganizeTasks() ? ACTIVE_REFRESH_INTERVAL_MS : IDLE_REFRESH_INTERVAL_MS;
  refreshTimer = window.setTimeout(() => {
    void load({ silent: true });
  }, delay);
}

async function load({ silent = false } = {}) {
  if (loading.value) {
    return;
  }
  loading.value = true;
  try {
    const [tasksPayload, sourceLibraryPayloadResponse] = await Promise.all([
      apiRequest("/api/tasks"),
      apiRequest("/api/source-library"),
      refreshConfig(),
    ]);
    organizerTasks.value = tasksPayload?.organize_tasks || organizerTasks.value;
    sourceLibraryPayload.value = {
      sections: Array.isArray(sourceLibraryPayloadResponse?.sections) ? sourceLibraryPayloadResponse.sections : [],
    };
    loadError.value = "";
    initialLoaded.value = true;
    rememberOrganizerPage();
  } catch (error) {
    if (!silent) {
      console.error("本地库数据加载失败", error);
      loadError.value = error instanceof Error ? error.message : "本地库数据加载失败。";
    }
  } finally {
    loading.value = false;
    syncTaskTimer();
  }
}

function resetImportDialogState({ keepFiles = false } = {}) {
  importDialog.dragging = false;
  importDialog.status = "";
  importDialog.error = "";
  if (!keepFiles) {
    importFiles.value = [];
  }
  if (importFileInput.value) {
    importFileInput.value.value = "";
  }
}

function openImportDialog() {
  resetImportDialogState();
  importDialog.visible = true;
}

function closeImportDialog() {
  if (importDialog.uploading) {
    return;
  }
  importDialog.visible = false;
  resetImportDialogState();
}

function openImportFilePicker() {
  if (importDialog.uploading) {
    return;
  }
  importFileInput.value?.click();
}

function setImportDragging(value) {
  if (importDialog.uploading) {
    return;
  }
  importDialog.dragging = Boolean(value);
}

function fileKey(file, index) {
  return `${file.name}-${file.size}-${file.lastModified}-${index}`;
}

function addImportFiles(fileList) {
  const incoming = Array.from(fileList || []);
  if (!incoming.length) {
    return;
  }
  const currentKeys = new Set(importFiles.value.map((file) => `${file.name}-${file.size}-${file.lastModified}`));
  const nextFiles = [...importFiles.value];
  let skipped = 0;
  for (const file of incoming) {
    if (!String(file.name || "").toLowerCase().endsWith(".3mf")) {
      skipped += 1;
      continue;
    }
    const key = `${file.name}-${file.size}-${file.lastModified}`;
    if (currentKeys.has(key)) {
      continue;
    }
    currentKeys.add(key);
    nextFiles.push(file);
  }
  importFiles.value = nextFiles;
  importDialog.error = "";
  importDialog.status = skipped ? `已跳过 ${skipped} 个非 3MF 文件。` : "";
}

function handleImportFileChange(event) {
  addImportFiles(event.target.files);
  event.target.value = "";
}

function handleImportDrop(event) {
  setImportDragging(false);
  addImportFiles(event.dataTransfer?.files);
}

function removeImportFile(index) {
  if (importDialog.uploading) {
    return;
  }
  importFiles.value = importFiles.value.filter((_, itemIndex) => itemIndex !== index);
}

function formatFileSize(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) {
    return "0 B";
  }
  if (size >= 1024 * 1024 * 1024) {
    return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`;
  }
  if (size >= 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }
  if (size >= 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${size} B`;
}

async function submitImportFiles() {
  if (!importFiles.value.length) {
    importDialog.error = "请选择要导入的 3MF 文件。";
    return;
  }
  importDialog.uploading = true;
  importDialog.status = "";
  importDialog.error = "";
  const formData = new FormData();
  for (const file of importFiles.value) {
    formData.append("files", file);
  }
  try {
    await apiRequest("/api/local-library/import", {
      method: "POST",
      body: formData,
    });
    importDialog.visible = false;
    resetImportDialogState();
    await load({ silent: true });
  } catch (error) {
    importDialog.error = error instanceof Error ? error.message : "导入失败。";
  } finally {
    importDialog.uploading = false;
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
      query: {
        nav_context: "organizer",
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
    query: {
      nav_context: "organizer",
    },
  });
}

function handleVisibilityChange() {
  if (document.hidden) {
    clearTaskTimer();
    return;
  }
  void load({ silent: true });
}

onMounted(() => {
  disposed = false;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  hydrateOrganizerPageFromCache();
  void load();
});

onBeforeUnmount(() => {
  disposed = true;
  clearTaskTimer();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
