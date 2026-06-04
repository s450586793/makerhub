import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CAPTURE_VIEWPORT,
  captureTargets,
  resolveCaptureUrl,
  safeCaptureFilename,
  validateCapturePlan,
} from "./capture-plan.mjs";
import { storyboardSegments, VIDEO_DURATION_SECONDS } from "./storyboard.mjs";

test("capture viewport is 16:9 desktop and large enough for MakerHub UI", () => {
  assert.deepEqual(CAPTURE_VIEWPORT, { width: 1920, height: 1080, deviceScaleFactor: 1 });
});

test("capture plan covers every storyboard segment", () => {
  const targetIds = new Set(captureTargets.map((target) => target.id));
  for (const segment of storyboardSegments) {
    assert.equal(targetIds.has(segment.id), true, `missing capture target for ${segment.id}`);
  }
});

test("capture target timings match the 45 second storyboard", () => {
  const lastSegment = storyboardSegments.at(-1);
  assert.equal(lastSegment.start + lastSegment.duration, VIDEO_DURATION_SECONDS);
  assert.equal(validateCapturePlan().valid, true);
});

test("capture filenames are stable and do not include routes or hosts", () => {
  assert.equal(safeCaptureFilename("model-library"), "01-model-library.png");
  assert.equal(safeCaptureFilename("remote-refresh"), "05-remote-refresh.png");
  assert.throws(() => safeCaptureFilename("https://example.test/models"), /Unknown capture target/);
});

test("resolveCaptureUrl keeps query and hash from the target only", () => {
  assert.equal(
    resolveCaptureUrl("https://demo.invalid/base", "/models?tag=__source_deleted__"),
    "https://demo.invalid/models?tag=__source_deleted__",
  );
  assert.equal(
    resolveCaptureUrl("https://demo.invalid/base/", "/settings?tab=accounts"),
    "https://demo.invalid/settings?tab=accounts",
  );
});

test("capture targets do not submit share creation or expose sensitive routes", () => {
  const sharing = captureTargets.find((target) => target.id === "sharing");
  assert.equal(sharing.actions.some((action) => action.type === "click" && /生成|复制|确定/.test(action.name || "")), false);
  for (const target of captureTargets) {
    assert.equal(/token|cookie|password|share_code/i.test(JSON.stringify(target)), false, target.id);
  }
});
