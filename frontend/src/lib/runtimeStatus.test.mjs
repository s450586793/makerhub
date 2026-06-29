import test from "node:test";
import assert from "node:assert/strict";

import {
  runtimeFailureLabel,
  runtimeRunLabel,
  runtimeTaskShape,
} from "./runtimeStatus.js";

test("runtime run labels are compact", () => {
  assert.equal(runtimeRunLabel("queued"), "排队中");
  assert.equal(runtimeRunLabel("running"), "运行中");
  assert.equal(runtimeRunLabel("blocked"), "需处理");
  assert.equal(runtimeRunLabel("completed"), "已完成");
});

test("runtime failure labels preserve actionable states", () => {
  assert.equal(runtimeFailureLabel("missing_3mf"), "缺失 3MF");
  assert.equal(runtimeFailureLabel("verification_required"), "需要验证");
  assert.equal(runtimeFailureLabel("cookie_invalid"), "Cookie 异常");
});

test("runtime task shape prefers runtime payload when present", () => {
  const payload = runtimeTaskShape({
    runtime: {
      runs: [{ run_id: "run-1", status: "running" }],
      batches: [{ batch_id: "batch-1", status: "queued" }],
      failures: [{ failure_id: "failure-1", status: "missing_3mf" }],
    },
    archive_queue: { active: [{ id: "legacy" }] },
  });

  assert.equal(payload.mode, "runtime");
  assert.equal(payload.runs.length, 1);
  assert.equal(payload.batches.length, 1);
  assert.equal(payload.failures.length, 1);
});

test("runtime task shape falls back when runtime payload is empty", () => {
  const payload = runtimeTaskShape({
    runtime: {
      runs: [],
      batches: [],
      failures: [],
    },
    archive_queue: {
      active: [{ id: "archive-1" }],
      queued: [{ id: "archive-2" }],
    },
  });

  assert.equal(payload.mode, "legacy");
  assert.equal(payload.legacy.archive_queue.active[0].id, "archive-1");
  assert.equal(payload.legacy.archive_queue.queued[0].id, "archive-2");
});

test("runtime task shape falls back to legacy payload", () => {
  const payload = runtimeTaskShape({ archive_queue: { active: [{ id: "legacy" }] } });

  assert.equal(payload.mode, "legacy");
  assert.equal(payload.legacy.archive_queue.active[0].id, "legacy");
});
