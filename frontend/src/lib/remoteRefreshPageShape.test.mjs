import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(
  path.resolve(currentDir, "../pages/RemoteRefreshPage.vue"),
  "utf8",
);

test("source refresh page renders compact config and state without runtime run projections", () => {
  assert.match(pageSource, /const config = payload\?\.config \|\| \{\}/);
  assert.match(pageSource, /const state = payload\?\.state \|\| \{\}/);
  assert.doesNotMatch(pageSource, /sourceRefreshActiveRuns/);
  assert.doesNotMatch(pageSource, /runtimePayload/);
  assert.doesNotMatch(pageSource, /payload\?\.runtime/);
});
