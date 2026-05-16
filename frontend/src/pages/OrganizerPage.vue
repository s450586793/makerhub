<template>
  <section class="surface surface--filters library-toolbar">
    <div class="library-toolbar__copy">
      <span class="eyebrow">本地库</span>
      <div class="library-toolbar__title-row">
        <h1>本地库管理</h1>
      </div>
    </div>
    <div class="library-toolbar__side">
      <div class="organizer-progress-wrap">
        <button
          :class="['organizer-progress-card', `is-${organizerProgressState.variant}`]"
          type="button"
          aria-controls="organizer-progress-popover"
          :aria-expanded="organizerProgressOpen ? 'true' : 'false'"
          @click.stop="toggleOrganizerProgress"
        >
          <span class="organizer-progress-card__head">
            <span>本地整理</span>
            <strong :title="organizerProgressState.message">{{ organizerProgressState.message }}</strong>
          </span>
          <span class="organizer-progress-card__body">
            <span :title="organizerProgressState.title">{{ organizerProgressState.title }}</span>
            <em>{{ organizerProgressState.progress }}%</em>
          </span>
          <span class="organizer-progress-bar">
            <span :style="{ width: `${organizerProgressState.progress}%` }"></span>
          </span>
          <span class="organizer-progress-card__foot">
            <span>{{ organizerProgressFooterText }}</span>
          </span>
        </button>
        <section
          v-if="organizerProgressOpen"
          id="organizer-progress-popover"
          class="organizer-progress-popover"
          @click.stop
        >
          <div class="organizer-progress-popover__head">
            <div>
              <strong>本地整理进度</strong>
              <span>{{ organizerProgressState.subtitle }}</span>
            </div>
            <em :class="['organizer-progress-status', `is-${organizerProgressState.variant}`]">
              {{ organizerProgressState.statusLabel }}
            </em>
          </div>
          <div class="organizer-progress-chips">
            <span v-for="chip in organizerProgressChips" :key="chip.label">
              <em>{{ chip.label }}</em>
              <strong>{{ chip.value }}</strong>
            </span>
          </div>
          <ol v-if="recentOrganizerRows.length" class="organizer-progress-list">
            <li v-for="row in recentOrganizerRows" :key="row.key">
              <div>
                <strong :title="row.title">{{ row.title }}</strong>
                <span :title="row.message">{{ row.message }}</span>
              </div>
              <em :class="['organizer-progress-status', `is-${row.variant}`]">
                {{ row.statusLabel }}
                <small v-if="row.progress > 0 && row.progress < 100">{{ row.progress }}%</small>
              </em>
            </li>
          </ol>
          <p v-else class="organizer-progress-empty">暂无本地整理记录</p>
        </section>
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
      <p>3MF 会沿用本地整理流程；STL、zip、rar、文件夹会按文件类型生成本地模型。</p>
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
        <span>3MF / STL / zip / rar / 图片 / PDF</span>
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
      <div v-if="importSkippedFiles.length" class="local-import-dialog__files local-import-dialog__files--skipped">
        <div class="local-import-dialog__files-head">
          <span>已跳过</span>
          <strong>{{ importSkippedFiles.length }} 个</strong>
        </div>
        <ol>
          <li v-for="(item, index) in importSkippedFiles" :key="skippedFileKey(item, index)">
            <span :title="item.path">{{ item.path }}</span>
            <em>{{ item.reason }}</em>
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
import { apiRequest, apiUploadRequest } from "../lib/api";
import { refreshConfig } from "../lib/appState";
import { deletePageCache, deletePageCacheByPrefix, getPageCache, setPageCache } from "../lib/pageCache";


const ACTIVE_REFRESH_INTERVAL_MS = 5000;
const IDLE_REFRESH_INTERVAL_MS = 30000;
const RECENT_IMPORT_PENDING_GRACE_MS = 10 * 60 * 1000;
const IMPORT_PROGRESS_STORAGE_KEY = "makerhub:local-import-progress";
const IMPORT_PROGRESS_STALE_MS = 30 * 60 * 1000;
const IMPORT_UPLOAD_PROGRESS_CAP = 35;
const IMPORT_PROCESS_PROGRESS_START = IMPORT_UPLOAD_PROGRESS_CAP;
const LOCAL_IMPORT_ACCEPT = [
  ".3mf",
  ".stl",
  ".step",
  ".stp",
  ".obj",
  ".zip",
  ".rar",
  ".jpg",
  ".jpeg",
  ".png",
  ".webp",
  ".gif",
  ".bmp",
  ".avif",
  ".pdf",
  ".doc",
  ".docx",
  ".xls",
  ".xlsx",
  ".xlsm",
  ".xlsb",
  ".xlt",
  ".xltx",
  ".xltm",
  ".csv",
  ".tsv",
  ".ods",
  ".txt",
  ".md",
  ".markdown",
  ".html",
  ".htm",
  ".mp4",
  ".mov",
  ".webm",
  ".avi",
  ".mkv",
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
const importSkippedFiles = ref([]);
const importDialog = reactive({
  visible: false,
  dragging: false,
  uploading: false,
  status: "",
  error: "",
});
const importUploadProgress = reactive({
  visible: false,
  fromStorage: false,
  id: "",
  phase: "idle",
  status: "",
  title: "",
  fileCount: 0,
  fileNames: [],
  progress: 0,
  uploadPercent: 0,
  loaded: 0,
  total: 0,
  message: "",
  startedAt: 0,
  updatedAt: 0,
});
const organizerProgressOpen = ref(false);
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
      recent_summary: "",
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
    recent_summary: "",
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
const organizeItems = computed(() => (
  Array.isArray(organizerTasks.value?.items) ? organizerTasks.value.items : []
));
const sortedOrganizerItems = computed(() => (
  [...organizeItems.value].sort((left, right) => organizerItemTimestamp(right) - organizerItemTimestamp(left))
));
const activeOrganizerTask = computed(() => {
  const items = sortedOrganizerItems.value;
  return items.find((item) => isMobileImportTask(item) && organizerStatusVariant(item?.status) === "running")
    || items.find((item) => organizerStatusVariant(item?.status) === "running")
    || items.find((item) => organizerStatusVariant(item?.status) === "queued")
    || null;
});
const currentImportOrganizerTask = computed(() => (
  importUploadProgress.visible ? findCurrentImportOrganizerTask() : null
));
const currentOrganizerTask = computed(() => (
  currentImportOrganizerTask.value
    || (importUploadProgress.visible ? currentImportSyntheticTask() : null)
    || activeOrganizerTask.value
    || (hasRecentImportWork() ? recentImportSyntheticTask() : null)
    || null
));
const organizerProgressState = computed(() => {
  if (loadError.value) {
    return {
      variant: "failed",
      statusLabel: "加载失败",
      title: "任务状态不可用",
      message: loadError.value,
      subtitle: "本地整理状态读取失败",
      progress: 0,
    };
  }
  if (!initialLoaded.value) {
    return {
      variant: "queued",
      statusLabel: "读取中",
      title: "正在读取本地整理状态",
      message: "正在读取任务状态。",
      subtitle: "等待任务状态返回",
      progress: 0,
    };
  }

  const task = currentOrganizerTask.value;
  if (task) {
    const variant = organizerStatusVariant(task.status);
    return {
      variant,
      statusLabel: organizerTaskStatusLabel(task),
      title: organizerTaskTitle(task),
      message: organizerTaskMessage(task),
      subtitle: localImportSummaryText.value || organizerProgressSubtitle.value,
      progress: displayOrganizerProgressPercent(task),
    };
  }

  return {
    variant: "idle",
    statusLabel: "空闲",
    title: "没有正在整理的本地导入",
    message: "最近没有正在运行的本地整理任务。",
    subtitle: organizerProgressSubtitle.value,
    progress: 0,
  };
});
const organizerProgressSubtitle = computed(() => (
  `候选 ${detectedTotalText.value} / 运行 ${runningCountText.value} / 排队 ${queuedCountText.value}`
));
const organizerProgressFooterText = computed(() => {
  if (!initialLoaded.value && !loadError.value) {
    return "正在读取任务状态";
  }
  if (loadError.value) {
    return "任务状态读取失败";
  }
  const task = currentOrganizerTask.value;
  if (mobileImportInUploadStage(task)) {
    return "上传中";
  }
  if (importUploadProgressIsActive()) {
    if (importUploadProgress.phase === "uploading") {
      return "上传中";
    }
    return "正在等待本地整理";
  }
  const running = Number(organizerTasks.value.running_count || 0);
  const queued = Number(organizerTasks.value.queued_count || 0);
  if (running > 0 || queued > 0) {
    return `运行 ${running} / 排队 ${queued}`;
  }
  if (hasRecentImportWork()) {
    return "等待本地整理处理";
  }
  return "当前无运行任务";
});
const organizerProgressChips = computed(() => {
  const { uploadedCount, counts } = recentImportStatusCounts(organizerTasks.value);
  if (importUploadProgress.visible) {
    const matchedTask = currentImportOrganizerTask.value;
    return [
      { label: "总进度", value: `${Math.round(importUploadProgress.progress || 0)}%` },
      { label: "文件", value: importUploadProgress.fileCount || 0 },
      { label: "大小", value: importUploadProgress.total ? formatFileSize(importUploadProgress.total) : "-" },
      { label: "阶段", value: matchedTask ? "整理" : importUploadPhaseLabel(importUploadProgress.phase) },
      { label: "状态", value: matchedTask ? organizerTaskStatusLabel(matchedTask) : organizerStatusLabel(importUploadProgress.status) },
    ];
  }
  const activeTask = currentOrganizerTask.value;
  if (isMobileImportTask(activeTask)) {
    return [
      { label: "总进度", value: `${displayOrganizerProgressPercent(activeTask)}%` },
      { label: "文件", value: 1 },
      { label: "阶段", value: mobileImportInUploadStage(activeTask) ? "上传" : "整理" },
      { label: "状态", value: organizerTaskStatusLabel(activeTask) },
    ];
  }
  return [
    { label: "上传", value: uploadedCount || 0 },
    { label: "新增", value: counts.success },
    { label: "跳过", value: counts.skipped },
    { label: "失败", value: counts.failed },
    { label: "处理中", value: counts.pending || activeOrganizeCount.value },
  ];
});
const recentOrganizerRows = computed(() => {
  const rows = [];
  const seen = new Set();
  for (const item of sortedOrganizerItems.value) {
    const row = organizerTaskRow(item, `task-${rows.length}`);
    if (seen.has(row.identity)) {
      continue;
    }
    seen.add(row.identity);
    rows.push(row);
    if (rows.length >= 8) {
      break;
    }
  }

  const lastImport = organizerTasks.value?.last_import;
  const importFiles = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const matchedImportTask = currentImportOrganizerTask.value;
  for (const file of importFiles) {
    if (rows.length >= 8) {
      break;
    }
    if (matchedImportTask && organizerItemMatchesImport(matchedImportTask, fileIdentitySet([file]))) {
      continue;
    }
    const row = organizerTaskRow(
      {
        ...file,
        status: file.status || (importBatchStillFresh(lastImport) ? "pending" : ""),
        message: file.message || (importBatchStillFresh(lastImport) ? "等待本地整理处理。" : "最近上传文件。"),
        progress: file.progress || 0,
      },
      `import-${rows.length}`,
    );
    if (seen.has(row.identity)) {
      continue;
    }
    seen.add(row.identity);
    rows.push(row);
  }
  return rows;
});

function fileIdentitySet(files) {
  const identities = new Set();
  for (const item of Array.isArray(files) ? files : []) {
    const sourcePath = String(item?.source_path || "").trim();
    const targetPath = String(item?.target_path || "").trim();
    const fileName = String(item?.file_name || "").trim();
    if (sourcePath) {
      identities.add(`path:${sourcePath}`);
      identities.add(`suffix:/${sourcePath.replace(/^\/+/, "")}`);
    }
    if (targetPath) {
      identities.add(`path:${targetPath}`);
      identities.add(`suffix:/${targetPath.replace(/^\/+/, "")}`);
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
  const targetPath = String(item?.target_path || "").trim();
  const packageSource = String(item?.package_source || "").trim();
  const fileName = String(item?.file_name || "").trim();
  return (sourcePath && (identities.has(`path:${sourcePath}`) || identities.has(`suffix:/${sourcePath.replace(/^\/+/, "")}`)))
    || (targetPath && (identities.has(`path:${targetPath}`) || identities.has(`suffix:/${targetPath.replace(/^\/+/, "")}`)))
    || (packageSource && (identities.has(`path:${packageSource}`) || identities.has(`suffix:/${packageSource.replace(/^\/+/, "")}`)))
    || (fileName && identities.has(`name:${fileName}`));
}

function normalizeImportStatus(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "success" || normalized === "organized") return "success";
  if (normalized === "skipped" || normalized === "duplicate_skipped" || normalized === "deleted_model_skipped") return "skipped";
  if (normalized === "failed" || normalized === "error" || normalized === "organize_failed" || normalized === "worker_timeout") return "failed";
  if (normalized === "running" || normalized === "pending" || normalized === "queued") return "pending";
  return "";
}

function organizerStatusVariant(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "running") return "running";
  if (normalized === "pending" || normalized === "queued") return "queued";
  if (normalized === "success" || normalized === "organized") return "success";
  if (normalized === "skipped" || normalized === "duplicate_skipped" || normalized === "deleted_model_skipped") return "skipped";
  if (normalized === "failed" || normalized === "error" || normalized === "organize_failed" || normalized === "worker_timeout" || normalized === "duplicate_skip_failed") return "failed";
  return "idle";
}

function organizerStatusIsTerminal(status) {
  return ["success", "skipped", "failed"].includes(organizerStatusVariant(status));
}

function organizerStatusLabel(status) {
  const variant = organizerStatusVariant(status);
  if (variant === "running") return "整理中";
  if (variant === "queued") return "排队中";
  if (variant === "success") return "已完成";
  if (variant === "skipped") return "已跳过";
  if (variant === "failed") return "失败";
  return "空闲";
}

function isMobileImportTask(item) {
  return String(item?.kind || "") === "mobile_import_upload";
}

function mobileImportInUploadStage(item) {
  if (!isMobileImportTask(item)) {
    return false;
  }
  const variant = organizerStatusVariant(item?.status);
  return (variant === "running" || variant === "queued") && organizerProgressPercent(item) < IMPORT_PROCESS_PROGRESS_START;
}

function organizerTaskStatusLabel(item) {
  if (mobileImportInUploadStage(item)) {
    return "上传中";
  }
  return organizerStatusLabel(item?.status);
}

function organizerProgressPercent(item) {
  const progress = Number(item?.progress || 0);
  if (!Number.isFinite(progress)) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(progress)));
}

function combinedImportProgress(processProgress = 0) {
  const safeProcessProgress = Math.max(0, Math.min(100, Number(processProgress || 0)));
  const mapped = IMPORT_PROCESS_PROGRESS_START + ((100 - IMPORT_PROCESS_PROGRESS_START) * safeProcessProgress / 100);
  return Math.max(IMPORT_PROCESS_PROGRESS_START, Math.min(100, Math.round(mapped)));
}

function displayOrganizerProgressPercent(item) {
  const progress = organizerProgressPercent(item);
  if (isMobileImportTask(item)) {
    return progress;
  }
  if (importUploadProgress.visible && currentImportOrganizerTask.value === item) {
    return combinedImportProgress(progress);
  }
  return progress;
}

function organizerItemTimestamp(item) {
  const parsed = Date.parse(String(item?.updated_at || item?.uploaded_at || ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function organizerItemUpdatedAfter(item, timestamp) {
  const itemTimestamp = organizerItemTimestamp(item);
  return Number.isFinite(itemTimestamp) && itemTimestamp >= Number(timestamp || 0) - 1000;
}

function findCurrentImportOrganizerTask() {
  if (!importUploadProgress.visible) {
    return null;
  }
  const currentId = String(importUploadProgress.id || "").trim();
  const currentTitle = String(importUploadProgress.title || "").trim();
  return sortedOrganizerItems.value.find((item) => {
    const itemId = String(item?.id || "").trim();
    const fingerprint = String(item?.fingerprint || "").trim();
    if (
      currentId
      && (
        itemId === currentId
        || fingerprint === currentId
        || fingerprint === `local-package:${currentId}`
        || item?.package_source === `local-package:${currentId}`
      )
    ) {
      return true;
    }
    if (!organizerItemUpdatedAfter(item, importUploadProgress.startedAt)) {
      return false;
    }
    const itemTitle = String(item?.title || item?.file_name || "").trim();
    if (currentTitle && itemTitle && (itemTitle === currentTitle || currentTitle.startsWith(itemTitle) || itemTitle.startsWith(currentTitle))) {
      return true;
    }
    const itemName = String(item?.source_path || "").split("/").filter(Boolean).pop();
    return currentTitle && itemName && currentTitle.includes(itemName);
  }) || null;
}

function organizerTaskTitle(item) {
  const title = String(item?.title || item?.file_name || item?.source_path || "本地整理任务").trim();
  return title || "本地整理任务";
}

function organizerTaskMessage(item) {
  const message = String(item?.message || "").trim();
  if (message) {
    return message;
  }
  if (isMobileImportTask(item)) {
    return mobileImportInUploadStage(item)
      ? "移动端上传中。"
      : "移动端上传已进入本地整理流程。";
  }
  if (organizerStatusVariant(item?.status) === "running") {
    return "正在处理本地导入文件。";
  }
  if (organizerStatusVariant(item?.status) === "queued") {
    return "等待本地整理处理。";
  }
  if (organizerStatusVariant(item?.status) === "success") {
    return "本地整理已完成。";
  }
  if (organizerStatusVariant(item?.status) === "skipped") {
    return "该文件已跳过。";
  }
  if (organizerStatusVariant(item?.status) === "failed") {
    return "本地整理失败。";
  }
  return "暂无进度消息。";
}

function organizerTaskRow(item, fallbackKey) {
  const identity = String(item?.id || item?.fingerprint || item?.source_path || item?.file_name || fallbackKey);
  const progress = displayOrganizerProgressPercent(item);
  return {
    key: `${identity}-${fallbackKey}`,
    identity,
    title: organizerTaskTitle(item),
    message: organizerTaskMessage(item),
    progress,
    variant: organizerStatusVariant(item?.status),
    statusLabel: organizerTaskStatusLabel(item),
  };
}

function applyImportUploadProgress(next = {}) {
  const now = Date.now();
  Object.assign(importUploadProgress, {
    ...next,
    updatedAt: now,
  });
  persistImportUploadProgress();
}

function resetImportUploadProgress({ clearStorage = true } = {}) {
  Object.assign(importUploadProgress, {
    visible: false,
    fromStorage: false,
    id: "",
    phase: "idle",
    status: "",
    title: "",
    fileCount: 0,
    fileNames: [],
    progress: 0,
    uploadPercent: 0,
    loaded: 0,
    total: 0,
    message: "",
    startedAt: 0,
    updatedAt: 0,
  });
  if (clearStorage) {
    clearImportUploadProgressStorage();
  }
}

function importUploadProgressIsActive() {
  return importUploadProgress.visible && !["success", "skipped", "failed", "idle"].includes(String(importUploadProgress.phase || ""));
}

function importUploadPhaseLabel(phase) {
  const normalized = String(phase || "").trim();
  if (normalized === "uploading") return "上传";
  if (normalized === "processing") return "整理";
  if (normalized === "syncing") return "同步";
  if (normalized === "success") return "完成";
  if (normalized === "skipped") return "跳过";
  if (normalized === "failed") return "失败";
  return "等待";
}

function importUploadStatusFromPhase(phase) {
  const normalized = String(phase || "").trim();
  if (normalized === "success") return "success";
  if (normalized === "skipped") return "skipped";
  if (normalized === "failed") return "failed";
  if (normalized === "syncing") return "queued";
  return "running";
}

function importUploadTitleFromItems(items) {
  const selected = Array.isArray(items) ? items : [];
  const first = selected[0];
  const firstName = first ? importItemPath(first).split("/").filter(Boolean).pop() : "";
  return selected.length > 1 ? `${firstName || "本地导入"} 等 ${selected.length} 项` : firstName || "本地导入";
}

function importUploadPayloadFromItems(items) {
  const selected = Array.isArray(items) ? items : [];
  const fileNames = selected.map((item) => importItemPath(item)).filter(Boolean).slice(0, 20);
  const total = selected.reduce((sum, item) => sum + Number(item?.file?.size || 0), 0);
  const now = Date.now();
  return {
    visible: true,
    fromStorage: false,
    id: `local-import-${now}`,
    phase: "uploading",
    status: "running",
    title: importUploadTitleFromItems(selected),
    fileCount: selected.length,
    fileNames,
    progress: 0,
    uploadPercent: 0,
    loaded: 0,
    total,
    message: "上传中。",
    startedAt: now,
    updatedAt: now,
  };
}

function currentImportSyntheticTask() {
  const status = importUploadProgress.status || importUploadStatusFromPhase(importUploadProgress.phase);
  return {
    id: importUploadProgress.id || "local-import-uploading",
    title: importUploadProgress.title || importUploadTitleFromItems(importFiles.value),
    status,
    progress: importUploadProgress.progress || 0,
    message: importUploadProgress.message || "正在上传并准备本地整理。",
    updated_at: importUploadProgress.updatedAt ? new Date(importUploadProgress.updatedAt).toISOString() : "",
  };
}

function safeReadImportUploadStorage() {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.sessionStorage.getItem(IMPORT_PROGRESS_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function persistImportUploadProgress() {
  if (typeof window === "undefined") {
    return;
  }
  if (!importUploadProgress.visible) {
    clearImportUploadProgressStorage();
    return;
  }
  try {
    window.sessionStorage.setItem(
      IMPORT_PROGRESS_STORAGE_KEY,
      JSON.stringify({
        id: importUploadProgress.id,
        phase: importUploadProgress.phase,
        status: importUploadProgress.status,
        title: importUploadProgress.title,
        fileCount: importUploadProgress.fileCount,
        fileNames: importUploadProgress.fileNames,
        progress: importUploadProgress.progress,
        uploadPercent: importUploadProgress.uploadPercent,
        loaded: importUploadProgress.loaded,
        total: importUploadProgress.total,
        message: importUploadProgress.message,
        startedAt: importUploadProgress.startedAt,
        updatedAt: importUploadProgress.updatedAt,
      }),
    );
  } catch {
    // sessionStorage can be unavailable in private contexts; the live UI still works.
  }
}

function clearImportUploadProgressStorage() {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.sessionStorage.removeItem(IMPORT_PROGRESS_STORAGE_KEY);
  } catch {
    // Ignore storage failures.
  }
}

function restoreImportUploadProgress() {
  const stored = safeReadImportUploadStorage();
  if (!stored || !stored.updatedAt) {
    return false;
  }
  if (Date.now() - Number(stored.updatedAt || 0) > IMPORT_PROGRESS_STALE_MS) {
    clearImportUploadProgressStorage();
    return false;
  }
  const phase = String(stored.phase || "syncing");
  if (["success", "failed", "idle"].includes(phase)) {
    clearImportUploadProgressStorage();
    return false;
  }
  const storedProgress = Math.max(0, Math.min(99, Number(stored.progress || 0)));
  Object.assign(importUploadProgress, {
    visible: true,
    fromStorage: true,
    id: String(stored.id || "local-import-restored"),
    phase: "syncing",
    status: "queued",
    title: String(stored.title || "本地导入"),
    fileCount: Number(stored.fileCount || 0),
    fileNames: Array.isArray(stored.fileNames) ? stored.fileNames : [],
    progress: storedProgress,
    uploadPercent: Number(stored.uploadPercent || 100),
    loaded: Number(stored.loaded || 0),
    total: Number(stored.total || 0),
    message: "页面已恢复，正在同步本地整理结果。",
    startedAt: Number(stored.startedAt || stored.updatedAt || Date.now()),
    updatedAt: Number(stored.updatedAt || Date.now()),
  });
  organizerProgressOpen.value = true;
  return true;
}

function recentImportSyntheticTask() {
  const lastImport = organizerTasks.value?.last_import || {};
  const files = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const uploadedCount = Number(lastImport?.uploaded_count || files.length || 0);
  const firstName = String(files[0]?.file_name || files[0]?.source_path || "").split("/").filter(Boolean).pop();
  return {
    id: "local-import-recent",
    title: uploadedCount > 1 ? `${firstName || "本地上传"} 等 ${uploadedCount} 项` : firstName || "本地上传",
    status: "pending",
    progress: 0,
    message: "上传文件已进入本地整理流程。",
  };
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

function findLastImportPackageTask(tasks) {
  const lastImport = tasks?.last_import && typeof tasks.last_import === "object" ? tasks.last_import : null;
  const uploadDir = String(lastImport?.upload_dir || "").trim();
  if (!uploadDir) {
    return null;
  }
  const matches = (Array.isArray(tasks?.items) ? tasks.items : []).filter((item) => (
    (
      String(item?.kind || "") === "local_package_import"
      || String(item?.kind || "") === "local_upload"
    )
    && (
      String(item?.staging_dir || "").trim() === uploadDir
      || String(item?.source_path || "").trim() === uploadDir
      || uploadDir.endsWith(`/${String(item?.package_source || "").trim().replace(/^\/+/, "")}`)
    )
  ));
  return matches.sort((left, right) => organizerItemTimestamp(right) - organizerItemTimestamp(left))[0] || null;
}

function recentImportStatusCounts(tasks) {
  const lastImport = tasks?.last_import && typeof tasks.last_import === "object" ? tasks.last_import : null;
  if (!lastImport) {
    return { uploadedCount: 0, counts: { success: 0, skipped: 0, failed: 0, pending: 0 }, matchedCount: 0 };
  }
  const allItems = Array.isArray(tasks?.items) ? tasks.items : [];
  const importFiles = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const uploadedCount = Number(lastImport?.uploaded_count || importFiles.length || 0);
  const packageTask = findLastImportPackageTask(tasks);
  const packageVariant = organizerStatusVariant(packageTask?.status);
  if (packageTask && organizerStatusIsTerminal(packageTask.status) && uploadedCount > 0) {
    return {
      uploadedCount,
      counts: {
        success: packageVariant === "success" ? uploadedCount : 0,
        skipped: packageVariant === "skipped" ? uploadedCount : 0,
        failed: packageVariant === "failed" ? uploadedCount : 0,
        pending: 0,
      },
      matchedCount: uploadedCount,
    };
  }
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
    uploadedCount,
    counts,
    matchedCount: batchItems.length,
  };
}

function lastImportUpdatedAfter(lastImport, timestamp) {
  const uploadedAt = Date.parse(String(lastImport?.uploaded_at || ""));
  return Number.isFinite(uploadedAt) && uploadedAt >= Number(timestamp || 0) - 1000;
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
  const recentCounts = lastImport ? recentImportStatusCounts(tasks) : null;
  const uploadedCount = recentCounts?.uploadedCount ?? Number(lastImport?.uploaded_count || 0);
  const matchedCount = recentCounts?.matchedCount ?? batchItems.length;

  const counts = recentCounts?.counts || summaryItems.reduce(
    (result, item) => {
      countImportStatus(result, item?.status);
      return result;
    },
    { success: 0, skipped: 0, failed: 0, pending: 0 }
  );

  if (lastImport && !recentCounts && !batchItems.length) {
    for (const file of importFiles) {
      countImportStatus(counts, file?.status);
    }
  }

  if (uploadedCount > 0) {
    const finishedCount = counts.success + counts.skipped + counts.failed;
    const parts = [`上传 ${uploadedCount}`, `新增 ${counts.success}`, `跳过 ${counts.skipped}`];
    if (counts.failed) {
      parts.push(`失败 ${counts.failed}`);
    }
    if (counts.pending) {
      parts.push(`处理中 ${counts.pending}`);
    } else if (finishedCount < uploadedCount && matchedCount < uploadedCount && importBatchStillFresh(lastImport)) {
      parts.push(`处理中 ${uploadedCount - matchedCount}`);
    } else if (finishedCount < uploadedCount && matchedCount < uploadedCount) {
      parts.push(`待同步 ${uploadedCount - matchedCount}`);
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
  return importUploadProgressIsActive() || activeOrganizeCount.value > 0 || hasRecentImportWork();
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
    if (refreshLibrary) {
      sourceLibraryRefreshDeferred = true;
    }
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
    reconcileImportUploadProgress();
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

function reconcileImportUploadProgress() {
  if (!importUploadProgress.visible) {
    return;
  }
  const lastImport = organizerTasks.value?.last_import;
  const matchingTask = findCurrentImportOrganizerTask();
  if (
    lastImport
    && lastImportUpdatedAfter(lastImport, importUploadProgress.startedAt)
    && !hasRecentImportWork()
    && importUploadProgress.phase === "syncing"
    && (!matchingTask || organizerStatusVariant(matchingTask.status) !== "success" || matchingTask.snapshot_ready)
  ) {
    resetImportUploadProgress();
    return;
  }
  if (!matchingTask) {
    return;
  }
  const variant = organizerStatusVariant(matchingTask.status);
  if (variant === "success" || variant === "skipped") {
    if (!matchingTask?.snapshot_ready && variant === "success") {
      applyImportUploadProgress({
        phase: "syncing",
        status: "running",
        progress: Math.max(importUploadProgress.progress || 0, combinedImportProgress(96)),
        message: "本地整理已写入，正在刷新本地库列表。",
      });
      return;
    }
    resetImportUploadProgress();
    return;
  }
  if (variant === "failed") {
    applyImportUploadProgress({
      phase: "failed",
      status: "failed",
      progress: combinedImportProgress(organizerProgressPercent(matchingTask)),
      message: organizerTaskMessage(matchingTask),
    });
    clearImportUploadProgressStorage();
    return;
  }
  applyImportUploadProgress({
    phase: "processing",
    status: matchingTask.status || "running",
    progress: Math.max(importUploadProgress.progress || 0, combinedImportProgress(organizerProgressPercent(matchingTask))),
    message: organizerTaskMessage(matchingTask),
  });
}

function clearLibraryCachesAfterImport() {
  deletePageCache("organizer");
  deletePageCacheByPrefix("models:");
  deletePageCacheByPrefix("model-library-group:");
  deletePageCacheByPrefix("model-detail:");
}

function resetImportDialogState({ keepFiles = false } = {}) {
  importDialog.dragging = false;
  importDialog.status = "";
  importDialog.error = "";
  if (!keepFiles) {
    importFiles.value = [];
    importSkippedFiles.value = [];
  }
  if (importFileInput.value) {
    importFileInput.value.value = "";
  }
  if (importFolderInput.value) {
    importFolderInput.value.value = "";
  }
}

function openImportDialog() {
  closeOrganizerProgressPopover();
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

function skippedFileKey(item, index) {
  return `${item?.path || "skipped"}-${item?.size || 0}-${item?.lastModified || 0}-${index}`;
}

function importSuffix(path) {
  const match = String(path || "").toLowerCase().match(/\.[^.\/]+$/);
  return match ? match[0] : "";
}

function isSupportedImportFile(file, relativePath = "") {
  const suffix = importSuffix(relativePath || file?.name || "");
  return LOCAL_IMPORT_SUPPORTED_SUFFIXES.has(suffix);
}

function skippedImportReason(file, relativePath = "") {
  if (Number(file?.size || 0) <= 0) {
    return "空文件";
  }
  if (!isSupportedImportFile(file, relativePath)) {
    return "暂不支持";
  }
  return "";
}

function validateImportSelection(items) {
  const hasFolderItem = items.some((item) => importItemPath(item).includes("/"));
  const hasArchive = items.some((item) => [".zip", ".rar"].includes(importSuffix(importItemPath(item))));
  const has3mf = items.some((item) => importSuffix(importItemPath(item)) === ".3mf");
  if (!hasFolderItem && !hasArchive && has3mf && items.some((item) => importSuffix(importItemPath(item)) !== ".3mf")) {
    return "3MF 请单独导入；包含图片、说明、STL、zip 或 rar 时，请打包为 zip/rar 或选择文件夹。";
  }
  return "";
}

function addImportFileItems(items) {
  const incoming = Array.from(items || []);
  if (!incoming.length) {
    return;
  }
  const currentKeys = new Set(importFiles.value.map((item) => `${importItemPath(item)}-${item.file.size}-${item.file.lastModified}`));
  const skippedKeys = new Set(importSkippedFiles.value.map((item) => `${item.path}-${item.size}-${item.lastModified}`));
  const nextFiles = [...importFiles.value];
  const nextSkippedFiles = [...importSkippedFiles.value];
  for (const item of incoming) {
    const file = item?.file || item;
    const relativePath = normalizeImportPath(file, item?.relativePath || "");
    const skippedReason = skippedImportReason(file, relativePath);
    if (skippedReason) {
      const path = relativePath || file.name || "未命名文件";
      const key = `${path}-${file.size || 0}-${file.lastModified || 0}`;
      if (!skippedKeys.has(key)) {
        skippedKeys.add(key);
        nextSkippedFiles.push({
          path,
          size: file.size || 0,
          lastModified: file.lastModified || 0,
          reason: skippedReason,
        });
      }
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
  importSkippedFiles.value = nextSkippedFiles;
  importDialog.error = selectionError;
  importDialog.status = nextSkippedFiles.length ? `已跳过 ${nextSkippedFiles.length} 个空文件或暂不支持的文件。` : "";
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
  importDialog.visible = false;
  organizerProgressOpen.value = true;
  applyImportUploadProgress(importUploadPayloadFromItems(importFiles.value));
  syncTaskTimer();
  const formData = new FormData();
  for (const item of importFiles.value) {
    formData.append("files", item.file);
    formData.append("paths", importItemPath(item));
  }
  try {
    const result = await apiUploadRequest("/api/local-library/import", {
      method: "POST",
      body: formData,
      onProgress: ({ loaded, total, percent, lengthComputable }) => {
        const knownTotal = total || importUploadProgress.total || 0;
        const fallbackPercent = knownTotal > 0 ? (loaded / knownTotal) * 100 : 0;
        const uploadPercent = lengthComputable ? percent : fallbackPercent;
        const cappedProgress = Math.max(1, Math.min(IMPORT_UPLOAD_PROGRESS_CAP, Math.round(uploadPercent * IMPORT_UPLOAD_PROGRESS_CAP / 100)));
        applyImportUploadProgress({
          phase: "uploading",
          status: "running",
          uploadPercent: Math.max(0, Math.min(100, uploadPercent || 0)),
          loaded,
          total: knownTotal,
          progress: cappedProgress,
          message: knownTotal
            ? `上传中：${formatFileSize(loaded)} / ${formatFileSize(knownTotal)}`
            : "上传中。",
        });
      },
      onUploadComplete: () => {
        applyImportUploadProgress({
          phase: "processing",
          status: "running",
          uploadPercent: 100,
          progress: Math.max(importUploadProgress.progress || 0, IMPORT_PROCESS_PROGRESS_START),
          loaded: importUploadProgress.total || importUploadProgress.loaded,
          message: "上传完成，等待后台本地整理。",
        });
      },
    });
    importDialog.uploading = false;
    importDialog.visible = false;
    const resultSkipped = result?.duplicate || (Array.isArray(result?.uploaded) && result.uploaded.some((item) => normalizeImportStatus(item?.status) === "skipped"));
    const resultQueued = Boolean(result?.queued || (Array.isArray(result?.uploaded) && result.uploaded.some((item) => normalizeImportStatus(item?.status) === "queued")));
    const snapshotReady = Boolean(result?.snapshot_ready || resultSkipped) && !resultQueued;
    applyImportUploadProgress({
      id: result?.task_id || importUploadProgress.id,
      phase: resultSkipped ? "skipped" : (snapshotReady ? "success" : "syncing"),
      status: resultSkipped ? "skipped" : (snapshotReady ? "success" : "queued"),
      progress: resultSkipped || snapshotReady ? 100 : IMPORT_PROCESS_PROGRESS_START,
      uploadPercent: 100,
      message: resultSkipped
        ? (result?.message || "本地导入已跳过。")
        : (snapshotReady ? (result?.message || "本地导入已完成。") : (result?.message || "已上传，后台正在整理本地模型包。")),
    });
    if (resultSkipped || snapshotReady) {
      clearImportUploadProgressStorage();
    } else {
      persistImportUploadProgress();
    }
    resetImportDialogState();
    clearLibraryCachesAfterImport();
    clearTaskTimer();
    await load({ silent: true, refreshLibrary: true });
    if (resultSkipped || snapshotReady) {
      resetImportUploadProgress();
    }
  } catch (error) {
    importDialog.uploading = false;
    importDialog.visible = true;
    importDialog.error = error instanceof Error ? error.message : "导入失败。";
    applyImportUploadProgress({
      phase: "failed",
      status: "failed",
      progress: importUploadProgress.progress || 0,
      message: importDialog.error,
    });
    clearImportUploadProgressStorage();
  } finally {
    importDialog.uploading = false;
    syncTaskTimer();
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

function toggleOrganizerProgress() {
  organizerProgressOpen.value = !organizerProgressOpen.value;
}

function closeOrganizerProgressPopover() {
  organizerProgressOpen.value = false;
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
  document.addEventListener("click", closeOrganizerProgressPopover);
  document.addEventListener("visibilitychange", handleVisibilityChange);
  hydrateOrganizerPageFromCache();
  restoreImportUploadProgress();
  void load({ refreshLibrary: !hasActiveOrganizeTasks() || !hasSourceLibraryPayload() });
});

onBeforeUnmount(() => {
  disposed = true;
  clearTaskTimer();
  document.removeEventListener("click", closeOrganizerProgressPopover);
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>
