export const LOCAL_IMPORT_PENDING_GRACE_MS = 10 * 60 * 1000;

function localImportUploadedAt(lastImport) {
  return Date.parse(String(lastImport?.uploaded_at || ""));
}

export function localImportBatchIsFresh(lastImport, now = Date.now()) {
  const uploadedAt = localImportUploadedAt(lastImport);
  if (!Number.isFinite(uploadedAt)) {
    return false;
  }
  return Number(now) - uploadedAt < LOCAL_IMPORT_PENDING_GRACE_MS;
}

export function unmatchedLocalImportFailureCount({
  lastImport,
  uploadedCount,
  matchedCount,
  now = Date.now(),
}) {
  const uploadedAt = localImportUploadedAt(lastImport);
  if (!Number.isFinite(uploadedAt) || Number(now) - uploadedAt < LOCAL_IMPORT_PENDING_GRACE_MS) {
    return 0;
  }
  return Math.max(0, Number(uploadedCount || 0) - Number(matchedCount || 0));
}

export function normalizeLocalImportStatus(status) {
  const normalized = String(status || "").trim().toLowerCase();
  if (normalized === "success" || normalized === "organized") return "success";
  if (["skipped", "duplicate_skipped", "deleted_model_skipped"].includes(normalized)) return "skipped";
  if (["failed", "error", "organize_failed", "worker_timeout", "duplicate_skip_failed"].includes(normalized)) return "failed";
  if (["running", "pending", "queued"].includes(normalized)) return "pending";
  return "";
}

export function localImportUpdatedAfter(lastImport, timestamp) {
  const uploadedAt = localImportUploadedAt(lastImport);
  return Number.isFinite(uploadedAt) && uploadedAt >= Number(timestamp || 0) - 1000;
}

function itemTimestamp(item) {
  return Date.parse(String(item?.updated_at || item?.uploaded_at || "")) || 0;
}

function itemBelongsToBatch(item, lastImport) {
  const startedAt = Date.parse(String(lastImport?.started_at || lastImport?.uploaded_at || ""));
  return Number.isFinite(startedAt) && itemTimestamp(item) >= startedAt - 1000;
}

function normalizePath(value) {
  return String(value || "").trim().replace(/\\/g, "/").replace(/\/+/g, "/").replace(/\/$/, "");
}

function pathsMatch(left, right, sourceDir) {
  let leftPath = normalizePath(left);
  let rightPath = normalizePath(right);
  if (!leftPath || !rightPath) return false;
  const root = normalizePath(sourceDir);
  if (root) {
    if (!leftPath.startsWith("/")) leftPath = `${root}/${leftPath.replace(/^\.\//, "")}`;
    if (!rightPath.startsWith("/")) rightPath = `${root}/${rightPath.replace(/^\.\//, "")}`;
  }
  return leftPath === rightPath;
}

export function localImportItemMatchesFile(item, file, lastImport) {
  if (!itemBelongsToBatch(item, lastImport)) return false;
  const fileId = String(file?.task_id || file?.id || "").trim();
  if (fileId && fileId === String(item?.id || "").trim()) return true;
  const filePaths = [file?.source_path, file?.original_source_path, file?.target_path];
  const itemPaths = [item?.source_path, item?.original_source_path, item?.target_path, item?.package_source];
  return filePaths.some((filePath) => itemPaths.some((itemPath) => pathsMatch(filePath, itemPath, lastImport?.source_dir)));
}

export function findLocalImportPackageTask(tasks) {
  const lastImport = tasks?.last_import;
  if (!lastImport?.upload_dir) return null;
  return (Array.isArray(tasks?.items) ? [...tasks.items] : [])
    .sort((left, right) => itemTimestamp(right) - itemTimestamp(left))
    .find((item) => (
      ["local_package_import", "local_upload"].includes(String(item?.kind || ""))
      && itemBelongsToBatch(item, lastImport)
      && [item?.source_path, item?.original_source_path, item?.staging_dir, item?.package_source]
        .some((path) => pathsMatch(path, lastImport.upload_dir, lastImport.source_dir))
    )) || null;
}

function matchImportFiles(lastImport, items) {
  const candidates = [...items].sort((left, right) => itemTimestamp(right) - itemTimestamp(left));
  const usedTasks = new Set();
  return (Array.isArray(lastImport?.files) ? lastImport.files : []).map((file) => {
    const task = candidates.find((item) => (
      !usedTasks.has(item.id || item)
      && localImportItemMatchesFile(item, file, lastImport)
    )) || null;
    if (task) usedTasks.add(task.id || task);
    return { file, task };
  });
}

export function recentLocalImportStatus(tasks, now = Date.now()) {
  const lastImport = tasks?.last_import;
  const files = Array.isArray(lastImport?.files) ? lastImport.files : [];
  const uploadedCount = Math.max(0, Number(lastImport?.uploaded_count || files.length || 0));
  const result = {
    uploadedCount,
    counts: { success: 0, skipped: 0, failed: 0, pending: 0 },
    matchedCount: 0,
    unmatchedFailedCount: 0,
    matchedItems: [],
    failedTask: null,
    packageTask: findLocalImportPackageTask(tasks),
    outcome: "pending",
  };
  if (!lastImport || !uploadedCount) return result;
  const packageStatus = normalizeLocalImportStatus(result.packageTask?.status);
  if (result.packageTask && packageStatus) {
    result.counts[packageStatus] = uploadedCount;
    result.matchedCount = uploadedCount;
    result.matchedItems = [result.packageTask];
  } else {
    const matches = matchImportFiles(lastImport, Array.isArray(tasks?.items) ? tasks.items : []);
    for (const { file, task } of matches.slice(0, uploadedCount)) {
      const fileStatus = normalizeLocalImportStatus(file?.status);
      const status = normalizeLocalImportStatus(task?.status) || fileStatus;
      const matched = Boolean(task) || ["success", "skipped", "failed"].includes(fileStatus);
      if (matched) result.matchedCount += 1;
      if (task) result.matchedItems.push(task);
      if (status && matched) result.counts[status] += 1;
    }
    result.unmatchedFailedCount = unmatchedLocalImportFailureCount({ lastImport, uploadedCount, matchedCount: result.matchedCount, now });
    result.counts.failed += result.unmatchedFailedCount;
    const counted = Object.values(result.counts).reduce((sum, count) => sum + count, 0);
    result.counts.pending += Math.max(0, uploadedCount - counted);
  }
  result.failedTask = result.matchedItems.find((item) => normalizeLocalImportStatus(item.status) === "failed")
    || files.find((file) => normalizeLocalImportStatus(file.status) === "failed")
    || null;
  if (result.counts.failed > 0) {
    result.outcome = "failed";
  } else if (result.counts.success + result.counts.skipped === uploadedCount) {
    result.outcome = result.matchedItems.some((item) => normalizeLocalImportStatus(item.status) === "success" && item.snapshot_ready === false)
      ? "syncing" : "success";
  }
  return result;
}

export function findCurrentLocalImportTask(tasks, progress) {
  const items = (Array.isArray(tasks?.items) ? [...tasks.items] : [])
    .sort((left, right) => itemTimestamp(right) - itemTimestamp(left));
  const currentId = String(progress?.id || "").trim();
  const exactTask = currentId ? items.find((item) => (
    item.id === currentId
    || item.fingerprint === currentId
    || item.fingerprint === `local-package:${currentId}`
    || item.package_source === `local-package:${currentId}`
  )) : null;
  const status = localImportUpdatedAfter(tasks?.last_import, progress?.startedAt) ? recentLocalImportStatus(tasks) : null;
  if (exactTask && (!status || status.outcome !== "pending" || !["success", "skipped"].includes(normalizeLocalImportStatus(exactTask.status)))) {
    return exactTask;
  }
  if (!status) return null;
  return status.failedTask || status.packageTask
    || status.matchedItems.find((item) => normalizeLocalImportStatus(item.status) === "pending")
    || (status.outcome !== "pending" ? status.matchedItems[0] : null) || null;
}
