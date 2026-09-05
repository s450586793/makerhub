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
