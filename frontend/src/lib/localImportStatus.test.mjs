import assert from "node:assert/strict";
import test from "node:test";

import {
  LOCAL_IMPORT_PENDING_GRACE_MS,
  localImportBatchIsFresh,
  unmatchedLocalImportFailureCount,
} from "./localImportStatus.js";

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
