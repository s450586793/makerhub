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
      <h2 id="local-import-dialog-title">导入本地模型</h2>
      <p>3MF 会沿用本地整理流程；STL、zip、文件夹会按文件类型生成本地模型。</p>
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
        <strong>拖入文件或文件夹</strong>
        <span>3MF / STL / zip / 图片 / PDF</span>
      </div>
      <div class="local-import-dialog__pickers">
        <button class="button button-secondary button-small" type="button" :disabled="importDialog.uploading" @click="openImportFilePicker">
          选择文件
        </button>
        <button class="button button-secondary button-small" type="button" :disabled="importDialog.uploading" @click="openImportFolderPicker">
          选择文件夹
        </button>
      </div>
      <input
        ref="importFileInput"
        class="local-import-dialog__input"
        type="file"
        :accept="LOCAL_IMPORT_ACCEPT"
        multiple
        @change="handleImportFileChange"
      >
      <input
        ref="importFolderInput"
        class="local-import-dialog__input"
        type="file"
        :accept="LOCAL_IMPORT_ACCEPT"
        webkitdirectory
        multiple
        @change="handleImportFolderChange"
      >
      <div v-if="importFiles.length" class="local-import-dialog__files">
        <div class="local-import-dialog__files-head">
          <span>待导入</span>
          <strong>{{ importFiles.length }} 个</strong>
        </div>
        <ol>
          <li v-for="(item, index) in importFiles" :key="fileKey(item, index)">
            <span :title="importItemPath(item)">{{ importItemPath(item) }}</span>
            <em>{{ formatFileSize(item.file?.size) }}</em>
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
const LOCAL_IMPORT_ACCEPT = [
  ".3mf",
  ".stl",
  ".step",
  ".stp",
  ".obj",
  ".zip",
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
  ".gif",
  ".bmp",
  ".avif",
  ".pdf",
  ".txt",
  ".md",
  ".markdown",
  ".html",
  ".htm",
  ".mp4",
  ".mov",
  ".webm",
].join(",");
const LOCAL_IMPORT_SUPPORTED_SUFFIXES = new Set(LOCAL_IMPORT_ACCEPT.split(","));

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
const importFolderInput = ref(null);
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
let sourceLibraryRefreshDeferred = false;

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

function hasSourceLibraryPayload() {
  return Array.isArray(sourceLibraryPayload.value?.sections) && sourceLibraryPayload.value.sections.length > 0;
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
  return hasActiveOrganizeTasks() ? "否" : "是";
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

function recentImportStatusCounts(tasks) {
  const lastImport = tasks?.last_import && typeof tasks.last_import === "object" ? tasks.last_import : null;
  if (!lastImport) {
    return { uploadedCount: 0, counts: { success: 0, skipped: 0, failed: 0, pending: 0 }, matchedCount: 0 };
  }
  const allItems = Array.isArray(tasks?.items) ? tasks.items : [];
  const importFiles = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const identities = fileIdentitySet(importFiles);
  const batchItems = identities.size ? allItems.filter((item) => organizerItemMatchesImport(item, identities)) : [];
  const summaryItems = batchItems.length ? batchItems : importFiles;
  const counts = summaryItems.reduce(
    (result, item) => {
      countImportStatus(result, item?.status);
      return result;
    },
    { success: 0, skipped: 0, failed: 0, pending: 0 }
  );
  return {
    uploadedCount: Number(lastImport?.uploaded_count || 0),
    counts,
    matchedCount: batchItems.length,
  };
}

function hasRecentImportWork() {
  const lastImport = organizerTasks.value?.last_import;
  if (!lastImport || !importBatchStillFresh(lastImport)) {
    return false;
  }
  const { uploadedCount, counts } = recentImportStatusCounts(organizerTasks.value);
  if (uploadedCount <= 0) {
    return false;
  }
  return counts.pending > 0 || counts.success + counts.skipped + counts.failed < uploadedCount;
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
  return activeOrganizeCount.value > 0 || hasRecentImportWork();
}

function syncTaskTimer() {
  clearTaskTimer();
  if (disposed || typeof window === "undefined" || document.hidden) {
    return;
  }
  const active = hasActiveOrganizeTasks();
  const delay = active ? ACTIVE_REFRESH_INTERVAL_MS : IDLE_REFRESH_INTERVAL_MS;
  refreshTimer = window.setTimeout(() => {
    void load({ silent: true, refreshLibrary: !active || !hasSourceLibraryPayload() });
  }, delay);
}

async function load({ silent = false, refreshLibrary = true } = {}) {
  if (loading.value) {
    return;
  }
  const includeSourceLibrary = Boolean(refreshLibrary || !hasSourceLibraryPayload());
  if (!includeSourceLibrary) {
    sourceLibraryRefreshDeferred = true;
  }
  loading.value = true;
  let shouldRefreshDeferredLibrary = false;
  try {
    const requests = includeSourceLibrary
      ? [
          apiRequest("/api/tasks"),
          apiRequest("/api/source-library"),
          refreshConfig(),
        ]
      : [apiRequest("/api/tasks")];
    const [tasksPayload, sourceLibraryPayloadResponse] = await Promise.all(requests);
    organizerTasks.value = tasksPayload?.organize_tasks || organizerTasks.value;
    if (includeSourceLibrary) {
      sourceLibraryPayload.value = {
        sections: Array.isArray(sourceLibraryPayloadResponse?.sections) ? sourceLibraryPayloadResponse.sections : [],
      };
      sourceLibraryRefreshDeferred = false;
    }
    loadError.value = "";
    initialLoaded.value = true;
    rememberOrganizerPage();
    shouldRefreshDeferredLibrary = !includeSourceLibrary && sourceLibraryRefreshDeferred && !hasActiveOrganizeTasks();
  } catch (error) {
    if (!silent) {
      console.error("本地库数据加载失败", error);
      loadError.value = error instanceof Error ? error.message : "本地库数据加载失败。";
    }
  } finally {
    loading.value = false;
    if (shouldRefreshDeferredLibrary && !disposed && typeof window !== "undefined" && !document.hidden) {
      void load({ silent: true, refreshLibrary: true });
    } else {
      syncTaskTimer();
    }
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
  if (importFolderInput.value) {
    importFolderInput.value.value = "";
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

function openImportFolderPicker() {
  if (importDialog.uploading) {
    return;
  }
  importFolderInput.value?.click();
}

function setImportDragging(value) {
  if (importDialog.uploading) {
    return;
  }
  importDialog.dragging = Boolean(value);
}

function normalizeImportPath(file, explicitPath = "") {
  return String(explicitPath || file?.webkitRelativePath || file?.name || "").replace(/\\/g, "/").replace(/^\/+/, "");
}

function importItemPath(item) {
  return item?.relativePath || item?.file?.name || "";
}

function fileKey(item, index) {
  const file = item?.file || item;
  return `${importItemPath(item)}-${file?.size || 0}-${file?.lastModified || 0}-${index}`;
}

function importSuffix(path) {
  const match = String(path || "").toLowerCase().match(/\.[^.\/]+$/);
  return match ? match[0] : "";
}

function isSupportedImportFile(file, relativePath = "") {
  const suffix = importSuffix(relativePath || file?.name || "");
  return LOCAL_IMPORT_SUPPORTED_SUFFIXES.has(suffix);
}

function validateImportSelection(items) {
  const hasFolderItem = items.some((item) => importItemPath(item).includes("/"));
  const hasZip = items.some((item) => importSuffix(importItemPath(item)) === ".zip");
  const has3mf = items.some((item) => importSuffix(importItemPath(item)) === ".3mf");
  if (!hasFolderItem && !hasZip && has3mf && items.some((item) => importSuffix(importItemPath(item)) !== ".3mf")) {
    return "3MF 请单独导入；包含图片、说明、STL 或 zip 时，请打包为 zip 或选择文件夹。";
  }
  return "";
}

function addImportFileItems(items) {
  const incoming = Array.from(items || []);
  if (!incoming.length) {
    return;
  }
  const currentKeys = new Set(importFiles.value.map((item) => `${importItemPath(item)}-${item.file.size}-${item.file.lastModified}`));
  const nextFiles = [...importFiles.value];
  let skipped = 0;
  for (const item of incoming) {
    const file = item?.file || item;
    const relativePath = normalizeImportPath(file, item?.relativePath || "");
    if (!isSupportedImportFile(file, relativePath)) {
      skipped += 1;
      continue;
    }
    const key = `${relativePath || file.name}-${file.size}-${file.lastModified}`;
    if (currentKeys.has(key)) {
      continue;
    }
    currentKeys.add(key);
    nextFiles.push({ file, relativePath: relativePath || file.name });
  }
  const selectionError = validateImportSelection(nextFiles);
  importFiles.value = nextFiles;
  importDialog.error = selectionError;
  importDialog.status = skipped ? `已跳过 ${skipped} 个暂不支持的文件。` : "";
}

function addImportFiles(fileList, { fromFolder = false } = {}) {
  const incoming = Array.from(fileList || []).map((file) => ({
    file,
    relativePath: normalizeImportPath(file, fromFolder ? file.webkitRelativePath : ""),
  }));
  addImportFileItems(incoming);
}

function handleImportFileChange(event) {
  addImportFiles(event.target.files);
  event.target.value = "";
}

function handleImportFolderChange(event) {
  addImportFiles(event.target.files, { fromFolder: true });
  event.target.value = "";
}

function readEntryFile(entry) {
  return new Promise((resolve) => {
    entry.file(
      (file) => resolve(file),
      () => resolve(null),
    );
  });
}

function readDirectoryBatch(reader) {
  return new Promise((resolve) => {
    reader.readEntries(
      (entries) => resolve(Array.from(entries || [])),
      () => resolve([]),
    );
  });
}

async function collectEntryFiles(entry, basePath = "") {
  if (!entry) {
    return [];
  }
  const entryPath = `${basePath}${entry.name || ""}`;
  if (entry.isFile) {
    const file = await readEntryFile(entry);
    return file ? [{ file, relativePath: entryPath || file.name }] : [];
  }
  if (!entry.isDirectory || typeof entry.createReader !== "function") {
    return [];
  }
  const reader = entry.createReader();
  const collected = [];
  while (true) {
    const batch = await readDirectoryBatch(reader);
    if (!batch.length) {
      break;
    }
    for (const child of batch) {
      collected.push(...await collectEntryFiles(child, `${entryPath}/`));
    }
  }
  return collected;
}

async function collectDroppedFiles(dataTransfer) {
  const items = Array.from(dataTransfer?.items || []);
  const entries = items
    .map((item) => (typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!entries.length) {
    return Array.from(dataTransfer?.files || []).map((file) => ({
      file,
      relativePath: normalizeImportPath(file),
    }));
  }
  const collected = [];
  for (const entry of entries) {
    collected.push(...await collectEntryFiles(entry));
  }
  return collected;
}

async function handleImportDrop(event) {
  setImportDragging(false);
  const droppedFiles = await collectDroppedFiles(event.dataTransfer);
  addImportFileItems(droppedFiles);
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
    importDialog.error = "请选择要导入的文件。";
    return;
  }
  const selectionError = validateImportSelection(importFiles.value);
  if (selectionError) {
    importDialog.error = selectionError;
    return;
  }
  importDialog.uploading = true;
  importDialog.status = "";
  importDialog.error = "";
  const formData = new FormData();
  for (const item of importFiles.value) {
    formData.append("files", item.file);
    formData.append("paths", importItemPath(item));
  }
  try {
    await apiRequest("/api/local-library/import", {
      method: "POST",
      body: formData,
    });
    importDialog.visible = false;
    resetImportDialogState();
    await load({ silent: true, refreshLibrary: false });
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
  void load({ silent: true, refreshLibrary: !hasActiveOrganizeTasks() || !hasSourceLibraryPayload() });
}

onMounted(() => {
  disposed = false;
  document.addEventListener("visibilitychange", handleVisibilityChange);
  hydrateOrganizerPageFromCache();
  void load({ refreshLibrary: !hasActiveOrganizeTasks() || !hasSourceLibraryPayload() });
});

onBeforeUnmount(() => {
  disposed = true;
  clearTaskTimer();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
