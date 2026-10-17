import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { babelParse, parse } from "@vue/compiler-sfc";

import {
  LOCAL_IMPORT_PENDING_GRACE_MS,
  localImportBatchIsFresh,
  unmatchedLocalImportFailureCount,
} from "./localImportStatus.js";
import * as localImportStatus from "./localImportStatus.js";

const uploadedAt = "2026-09-03T14:36:50+08:00";
const uploadedAtMs = Date.parse(uploadedAt);

test("local import remains pending inside the organizer grace period", () => {
  assert.equal(
    localImportBatchIsFresh(
      { uploaded_at: uploadedAt },
      uploadedAtMs + LOCAL_IMPORT_PENDING_GRACE_MS - 1,
    ),
    true,
  );
  assert.equal(
    unmatchedLocalImportFailureCount({
      lastImport: { uploaded_at: uploadedAt },
      uploadedCount: 3,
      matchedCount: 0,
      now: uploadedAtMs + LOCAL_IMPORT_PENDING_GRACE_MS - 1,
    }),
    0,
  );
});

test("unmatched local imports become explicit failures after the grace period", () => {
  assert.equal(
    unmatchedLocalImportFailureCount({
      lastImport: { uploaded_at: uploadedAt },
      uploadedCount: 3,
      matchedCount: 1,
      now: uploadedAtMs + LOCAL_IMPORT_PENDING_GRACE_MS,
    }),
    2,
  );
});

test("invalid timestamps do not create false local import failures", () => {
  assert.equal(localImportBatchIsFresh({ uploaded_at: "invalid" }, uploadedAtMs), false);
  assert.equal(
    unmatchedLocalImportFailureCount({
      lastImport: { uploaded_at: "invalid" },
      uploadedCount: 2,
      matchedCount: 0,
      now: uploadedAtMs,
    }),
    0,
  );
});

function importBatch(files, extra = {}) {
  return {
    uploaded_at: uploadedAt,
    uploaded_count: files.length,
    source_dir: "/app/input",
    upload_dir: "/app/input/uploads",
    files: files.map((sourcePath) => ({ file_name: "part.3mf", source_path: sourcePath })),
    ...extra,
  };
}

function summarize(lastImport, items, elapsed = 0) {
  return localImportStatus.recentLocalImportStatus(
    { last_import: lastImport, items },
    uploadedAtMs + elapsed,
  );
}

test("a previous same-name import cannot complete a newly uploaded batch", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"]);
  const result = summarize(lastImport, [{
    id: "old",
    file_name: "part.3mf",
    source_path: "/app/input/uploads/part.3mf",
    status: "success",
    updated_at: new Date(uploadedAtMs - 86400000).toISOString(),
    snapshot_ready: true,
  }]);
  assert.deepEqual(result.counts, { success: 0, skipped: 0, failed: 0, pending: 1 });
  assert.equal(result.matchedCount, 0);
  assert.equal(result.outcome, "pending");
});

test("recent same-name files from another directory do not match this upload", () => {
  const result = summarize(importBatch(["/app/input/uploads/part.3mf"]), [{
    file_name: "part.3mf",
    source_path: "/app/input/other/part.3mf",
    status: "success",
    updated_at: uploadedAt,
    snapshot_ready: true,
  }]);
  assert.equal(result.counts.pending, 1);
  assert.equal(result.counts.success, 0);
});

test("relative source paths resolve within the configured source directory", () => {
  const result = summarize(importBatch(["/app/input/uploads/part.3mf"]), [{
    source_path: "uploads/part.3mf",
    status: "success",
    updated_at: uploadedAt,
    snapshot_ready: true,
  }]);
  assert.equal(result.matchedCount, 1);
  assert.equal(result.outcome, "success");
});

test("newest task wins once per file and partial batches remain pending", () => {
  const result = summarize(importBatch([
    "/app/input/uploads/part.3mf",
    "/app/input/uploads/part_2.3mf",
  ]), [
    { id: "same", source_path: "/app/input/uploads/part.3mf", status: "running", updated_at: uploadedAt },
    { id: "same", source_path: "/app/input/uploads/part.3mf", status: "success", updated_at: new Date(uploadedAtMs + 1000).toISOString(), snapshot_ready: true },
  ]);
  assert.deepEqual(result.counts, { success: 1, skipped: 0, failed: 0, pending: 1 });
  assert.equal(result.matchedCount, 1);
  assert.equal(result.outcome, "pending");
});

test("unmatched files fail after ten minutes without hiding matched successes", () => {
  const result = summarize(importBatch([
    "/app/input/uploads/part.3mf",
    "/app/input/uploads/part_2.3mf",
  ]), [{
    source_path: "/app/input/uploads/part.3mf",
    status: "success",
    updated_at: uploadedAt,
    snapshot_ready: true,
  }], LOCAL_IMPORT_PENDING_GRACE_MS);
  assert.deepEqual(result.counts, { success: 1, skipped: 0, failed: 1, pending: 0 });
  assert.equal(result.unmatchedFailedCount, 1);
  assert.equal(result.outcome, "failed");
});

test("a terminal failed task keeps its error instead of completing the batch", () => {
  const failed = {
    source_path: "/app/input/uploads/part.3mf",
    status: "failed",
    updated_at: uploadedAt,
    message: "metadata write failed",
  };
  const result = summarize(importBatch([failed.source_path]), [failed]);
  assert.equal(result.outcome, "failed");
  assert.equal(result.failedTask, failed);
});

test("successful batches wait for snapshot readiness and skipped files finish", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"]);
  const task = { source_path: lastImport.files[0].source_path, updated_at: uploadedAt, status: "success", snapshot_ready: false };
  assert.equal(summarize(lastImport, [task]).outcome, "syncing");
  assert.equal(summarize(lastImport, [{ ...task, snapshot_ready: true }]).outcome, "success");
  assert.equal(summarize(lastImport, [{ ...task, status: "skipped" }]).outcome, "success");
});

test("package tasks match the original upload directory after staging moves", () => {
  const lastImport = importBatch(["/app/input/uploads/batch/part.3mf"], { upload_dir: "/app/input/uploads/batch" });
  const task = {
    id: "package",
    kind: "local_package_import",
    source_path: "/app/staging/package",
    original_source_path: lastImport.upload_dir,
    staging_dir: "/app/staging/package",
    updated_at: uploadedAt,
    status: "failed",
    message: "extraction timed out",
  };
  const result = summarize(lastImport, [task]);
  assert.equal(result.packageTask, task);
  assert.equal(result.outcome, "failed");
  assert.equal(result.failedTask.message, "extraction timed out");
});

test("one task cannot complete two distinct uploaded files with the same name", () => {
  const result = summarize(importBatch([
    "/app/input/uploads/one/part.3mf",
    "/app/input/uploads/two/part.3mf",
  ]), [{ source_path: "/app/input/uploads/one/part.3mf", file_name: "part.3mf", status: "success", updated_at: uploadedAt, snapshot_ready: true }]);
  assert.equal(result.counts.success, 1);
  assert.equal(result.counts.pending, 1);
});

test("current-task matching uses ids or this batch rather than filename prefixes", () => {
  const tasks = {
    last_import: importBatch(["/app/input/uploads/part.3mf"]),
    items: [{ id: "unrelated", title: "part", source_path: "/app/input/other/part.3mf", updated_at: uploadedAt, status: "running" }],
  };
  assert.equal(localImportStatus.findCurrentLocalImportTask(tasks, { id: "local-import-new", title: "part.3mf", startedAt: uploadedAtMs }), null);
  assert.equal(localImportStatus.findCurrentLocalImportTask(tasks, { id: "unrelated", startedAt: uploadedAtMs }), tasks.items[0]);
});

test("a file processed while its batch is still uploading matches the batch start", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], {
    started_at: new Date(uploadedAtMs - 60000).toISOString(),
  });
  const result = summarize(lastImport, [{
    source_path: lastImport.files[0].source_path,
    updated_at: new Date(uploadedAtMs - 30000).toISOString(),
    status: "success",
    snapshot_ready: true,
  }]);
  assert.equal(result.outcome, "success");
});

test("pending upload records without a backend task fail once without double counting", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"]);
  lastImport.files[0].status = "queued";
  const result = summarize(lastImport, [], LOCAL_IMPORT_PENDING_GRACE_MS);
  assert.deepEqual(result.counts, { success: 0, skipped: 0, failed: 1, pending: 0 });
});

const pageSource = readFileSync(new URL("../pages/OrganizerPage.vue", import.meta.url), "utf8");
const pageScript = parse(pageSource).descriptor.scriptSetup.content;
const pageAst = babelParse(pageScript, { sourceType: "module" });
const progressFunctionNames = new Set([
  "reconcileImportUploadProgress", "findCurrentImportOrganizerTask", "organizerStatusVariant",
  "organizerTaskMessage", "isMobileImportTask", "mobileImportInUploadStage",
  "organizerProgressPercent", "combinedImportProgress",
]);
const progressFunctions = pageAst.program.body
  .filter((node) => node.type === "FunctionDeclaration" && progressFunctionNames.has(node.id.name))
  .map((node) => pageScript.slice(node.start, node.end)).join("\n");

function reconcilePage(tasks, progress = {}) {
  const currentProgress = {
    visible: true, phase: "syncing", status: "queued", progress: 35,
    id: "local-import-current", startedAt: Date.parse(tasks.last_import.uploaded_at) - 1000,
    ...progress,
  };
  const context = vm.createContext({
    organizerTasks: { value: tasks },
    importUploadProgress: currentProgress,
    importDialog: { uploading: false },
    IMPORT_PROCESS_PROGRESS_START: 35,
    findCurrentLocalImportTask: localImportStatus.findCurrentLocalImportTask,
    recentImportStatusCounts: localImportStatus.recentLocalImportStatus,
    lastImportUpdatedAfter: localImportStatus.localImportUpdatedAfter,
    scheduleImportPendingCheck() {},
    applyImportUploadProgress(next) { Object.assign(currentProgress, next); },
    clearImportUploadProgressStorage() {},
    resetImportUploadProgress() { currentProgress.visible = false; },
  });
  vm.runInContext(`${progressFunctions}\nreconcileImportUploadProgress();`, context);
  return currentProgress;
}

test("page reconciliation preserves backend failure details and the visible failed progress", () => {
  const now = new Date().toISOString();
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], { uploaded_at: now });
  const result = reconcilePage({ last_import: lastImport, items: [{ source_path: lastImport.files[0].source_path, updated_at: now, status: "failed", message: "metadata write failed" }] });
  assert.equal(result.visible, true);
  assert.equal(result.phase, "failed");
  assert.equal(result.message, "metadata write failed");
});

test("page reconciliation keeps 35 percent while backend has not taken this upload", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], { uploaded_at: new Date().toISOString() });
  const result = reconcilePage({ last_import: lastImport, items: [{ source_path: lastImport.files[0].source_path, updated_at: uploadedAt, status: "success", snapshot_ready: true }] });
  assert.equal(result.visible, true);
  assert.equal(result.progress, 35);
  assert.equal(result.status, "queued");
});

test("page reconciliation clears successful complete batches", () => {
  const now = new Date().toISOString();
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], { uploaded_at: now });
  const result = reconcilePage({ last_import: lastImport, items: [{ source_path: lastImport.files[0].source_path, updated_at: now, status: "success", snapshot_ready: true }] });
  assert.equal(result.visible, false);
});

test("page reconciliation does not complete a parent package before its child 3MF tasks", () => {
  const now = new Date().toISOString();
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], { uploaded_at: now });
  const result = reconcilePage({
    last_import: lastImport,
    items: [{ id: "parent", kind: "local_package_import", source_path: "/app/input/old-package", updated_at: now, status: "success", snapshot_ready: true }],
  }, { id: "parent" });
  assert.equal(result.visible, true);
  assert.equal(result.status, "queued");
});

test("page reconciliation reports timed-out unclaimed uploads without clearing the failure", () => {
  const lastImport = importBatch(["/app/input/uploads/part.3mf"], { uploaded_at: new Date(Date.now() - LOCAL_IMPORT_PENDING_GRACE_MS - 1000).toISOString() });
  const result = reconcilePage({ last_import: lastImport, items: [] });
  assert.equal(result.visible, true);
  assert.equal(result.phase, "failed");
});
