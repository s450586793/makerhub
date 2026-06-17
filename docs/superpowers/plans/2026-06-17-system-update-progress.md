# System Update Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a compact staged progress bar on the Settings system update card after the user starts a web update.

**Architecture:** Keep progress derivation in a pure frontend helper so it can be tested without mounting `SettingsPage.vue`. `SettingsPage.vue` consumes the helper through a computed value and renders a status block below the update summary. CSS stays in `frontend/src/style.css` near the existing `.system-update-*` rules.

**Tech Stack:** Vue 3 composition API, Vite, Node built-in `node:test`, existing MakerHub CSS tokens.

---

## File Structure

- Create `frontend/src/lib/systemUpdateProgress.js`
  - Owns phase metadata, status normalization, display progress, labels, and failure/success/active flags.
  - No Vue imports.
- Create `frontend/src/lib/systemUpdateProgress.test.mjs`
  - Node tests for active, success, failed, restart-wait, idle, and unknown phase behavior.
- Modify `frontend/src/pages/SettingsPage.vue`
  - Import `systemUpdateProgressState`.
  - Add `systemUpdateProgress` computed.
  - Render the progress block under the three update stat cards.
- Modify `frontend/src/style.css`
  - Add `.system-update-progress*` styles near existing `.system-update-card` rules.
  - Use existing tokens and restrained dark-workstation styling.

## Task 1: Add Tested Progress State Helper

**Files:**
- Create: `frontend/src/lib/systemUpdateProgress.js`
- Create: `frontend/src/lib/systemUpdateProgress.test.mjs`

- [ ] **Step 1: Write the failing test file**

Create `frontend/src/lib/systemUpdateProgress.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

import { systemUpdateProgressState } from "./systemUpdateProgress.js";

test("system update progress maps active pulling phase", () => {
  const state = systemUpdateProgressState({
    status: "running",
    phase: "pulling",
    message: "正在拉取最新镜像，服务稍后会短暂重启。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.failed, false);
  assert.equal(state.progress, 25);
  assert.equal(state.label, "正在拉取镜像");
  assert.equal(state.message, "正在拉取最新镜像，服务稍后会短暂重启。");
  assert.equal(state.percentText, "25%");
  assert.equal(state.variant, "running");
});

test("system update progress forces completed status to 100 percent", () => {
  const state = systemUpdateProgressState({
    status: "succeeded",
    phase: "completed",
    message: "系统已重新启动，当前版本 v0.9.34。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, false);
  assert.equal(state.completed, true);
  assert.equal(state.progress, 100);
  assert.equal(state.label, "更新完成");
  assert.equal(state.percentText, "100%");
  assert.equal(state.variant, "success");
});

test("system update progress keeps failed phase location", () => {
  const state = systemUpdateProgressState({
    status: "failed",
    phase: "starting",
    message: "等待新容器恢复超时。",
    last_error: "等待新容器恢复超时。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.failed, true);
  assert.equal(state.progress, 96);
  assert.equal(state.label, "等待服务恢复");
  assert.equal(state.message, "等待新容器恢复超时。");
  assert.equal(state.variant, "failed");
});

test("system update progress handles pending startup restart wait", () => {
  const state = systemUpdateProgressState({
    status: "pending_startup",
    phase: "starting",
    message: "",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.progress, 96);
  assert.equal(state.label, "等待服务恢复");
  assert.equal(state.message, "等待服务恢复");
});

test("system update progress hides idle without message", () => {
  const state = systemUpdateProgressState({
    status: "idle",
    phase: "idle",
    message: "",
  });

  assert.equal(state.visible, false);
  assert.equal(state.progress, 0);
  assert.equal(state.variant, "idle");
});

test("system update progress shows unknown active phase at midpoint", () => {
  const state = systemUpdateProgressState({
    status: "running",
    phase: "checking_network",
    message: "正在检查网络。",
  });

  assert.equal(state.visible, true);
  assert.equal(state.active, true);
  assert.equal(state.progress, 50);
  assert.equal(state.label, "正在更新");
  assert.equal(state.message, "正在检查网络。");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
node --test frontend/src/lib/systemUpdateProgress.test.mjs
```

Expected: FAIL with module not found for `./systemUpdateProgress.js`.

- [ ] **Step 3: Create the progress helper**

Create `frontend/src/lib/systemUpdateProgress.js`:

```js
const ACTIVE_STATUSES = new Set(["queued", "launching_helper", "running", "pending_startup"]);

const PHASE_PROGRESS = {
  queued: { label: "已提交更新", progress: 5 },
  launching_helper: { label: "启动更新 helper", progress: 10 },
  pulling: { label: "正在拉取镜像", progress: 25 },
  pulling_web: { label: "正在拉取 Web 镜像", progress: 30 },
  creating_web: { label: "正在创建 Web 容器", progress: 40 },
  switching_web: { label: "正在切换 Web 容器", progress: 48 },
  starting_web: { label: "正在启动 Web 容器", progress: 55 },
  web_updated: { label: "Web 容器已更新", progress: 58 },
  updating_worker: { label: "正在更新 Worker", progress: 60 },
  pulling_worker: { label: "正在拉取 Worker 镜像", progress: 62 },
  creating_worker: { label: "正在创建 Worker 容器", progress: 68 },
  switching_worker: { label: "正在切换 Worker 容器", progress: 74 },
  starting_worker: { label: "正在启动 Worker 容器", progress: 80 },
  worker_updated: { label: "Worker 容器已更新", progress: 84 },
  recreating: { label: "正在替换 App 容器", progress: 88 },
  switching: { label: "正在切换 App 容器", progress: 92 },
  starting: { label: "等待服务恢复", progress: 96 },
  completed: { label: "更新完成", progress: 100 },
  version_mismatch: { label: "版本校验失败", progress: 96 },
  failed: { label: "更新失败", progress: 0 },
};

function cleanText(value) {
  return String(value || "").trim();
}

function clampProgress(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

export function systemUpdateProgressState(update = {}) {
  const status = cleanText(update.status || "idle");
  const phase = cleanText(update.phase || status || "idle");
  const phaseState = PHASE_PROGRESS[phase] || null;
  const active = ACTIVE_STATUSES.has(status);
  const completed = status === "succeeded";
  const failed = status === "failed";
  const hasMessage = Boolean(cleanText(update.message) || cleanText(update.last_error));
  const visible = active || completed || failed || hasMessage;

  if (!visible) {
    return {
      visible: false,
      active: false,
      completed: false,
      failed: false,
      label: "",
      message: "",
      progress: 0,
      percentText: "0%",
      variant: "idle",
    };
  }

  const fallbackProgress = active ? 50 : 0;
  const progress = completed ? 100 : clampProgress(phaseState?.progress ?? fallbackProgress);
  const label = completed ? "更新完成" : phaseState?.label || (failed ? "更新失败" : "正在更新");
  const message = cleanText(failed ? update.last_error || update.message : update.message) || label;
  const variant = failed ? "failed" : completed ? "success" : active ? "running" : "idle";

  return {
    visible: true,
    active,
    completed,
    failed,
    label,
    message,
    progress,
    percentText: `${progress}%`,
    variant,
  };
}
```

- [ ] **Step 4: Run helper test**

Run:

```bash
node --test frontend/src/lib/systemUpdateProgress.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit helper**

Run:

```bash
git add frontend/src/lib/systemUpdateProgress.js frontend/src/lib/systemUpdateProgress.test.mjs
git commit -m "feat: 添加系统更新进度状态映射"
```

Expected: commit only includes the two helper files.

## Task 2: Render Progress In Settings Page

**Files:**
- Modify: `frontend/src/pages/SettingsPage.vue`
- Test: `frontend/src/lib/systemUpdateProgress.test.mjs`

- [ ] **Step 1: Verify helper still passes before page changes**

Run:

```bash
node --test frontend/src/lib/systemUpdateProgress.test.mjs
```

Expected: PASS.

- [ ] **Step 2: Import the helper in SettingsPage**

In `frontend/src/pages/SettingsPage.vue`, add this import near other `../lib/*` imports:

```js
import { systemUpdateProgressState } from "../lib/systemUpdateProgress";
```

Keep existing imports unchanged.

- [ ] **Step 3: Add the computed state**

In `frontend/src/pages/SettingsPage.vue`, place this computed near `systemUpdateStatusLabel`:

```js
const systemUpdateProgress = computed(() => systemUpdateProgressState(systemUpdate.value));
```

- [ ] **Step 4: Render the progress block**

In `frontend/src/pages/SettingsPage.vue`, after the three-card block containing 当前版本 / 最新版本 / 更新状态 and before the App 容器 / Worker 容器 block, insert:

```vue
        <div
          v-if="systemUpdateProgress.visible"
          :class="['field-card', 'system-update-progress', `is-${systemUpdateProgress.variant}`]"
          role="status"
          aria-live="polite"
        >
          <div class="system-update-progress__head">
            <div>
              <span>升级进度</span>
              <strong>{{ systemUpdateProgress.label }}</strong>
            </div>
            <em>{{ systemUpdateProgress.percentText }}</em>
          </div>
          <div
            class="system-update-progress__bar"
            role="progressbar"
            :aria-valuemin="0"
            :aria-valuemax="100"
            :aria-valuenow="systemUpdateProgress.progress"
          >
            <span :style="{ width: `${systemUpdateProgress.progress}%` }"></span>
          </div>
          <p>{{ systemUpdateProgress.message }}</p>
        </div>
```

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: PASS. If it fails because the import path requires `.js`, change the import to:

```js
import { systemUpdateProgressState } from "../lib/systemUpdateProgress.js";
```

Then rerun the build.

- [ ] **Step 6: Commit page integration**

Run:

```bash
git add frontend/src/pages/SettingsPage.vue
git commit -m "feat: 展示系统更新阶段进度"
```

Expected: commit only includes `frontend/src/pages/SettingsPage.vue`.

## Task 3: Add System Update Progress Styling And Final Verification

**Files:**
- Modify: `frontend/src/style.css`
- Test: `frontend/src/lib/systemUpdateProgress.test.mjs`

- [ ] **Step 1: Add progress styles**

In `frontend/src/style.css`, after `.system-update-grid` or before `.system-update-stat`, add:

```css
.system-update-progress {
  display: grid;
  gap: 10px;
  padding: 14px 16px;
}

.system-update-progress__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.system-update-progress__head div {
  display: grid;
  gap: 4px;
  min-width: 0;
}

.system-update-progress__head span {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
}

.system-update-progress__head strong {
  color: var(--text);
  font-size: 17px;
  line-height: 1.25;
}

.system-update-progress__head em {
  color: var(--brand);
  font-size: 18px;
  font-style: normal;
  font-weight: 800;
  line-height: 1.2;
}

.system-update-progress__bar {
  width: 100%;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--surface-soft);
  border: 1px solid var(--border);
}

.system-update-progress__bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--brand) 0%, #69d39d 100%);
  transition: width 180ms ease;
}

.system-update-progress p {
  margin: 0;
  color: var(--muted);
  line-height: 1.55;
}

.system-update-progress.is-success .system-update-progress__head em {
  color: var(--brand);
}

.system-update-progress.is-failed {
  border-color: rgba(223, 86, 86, 0.36);
}

.system-update-progress.is-failed .system-update-progress__head em,
.system-update-progress.is-failed .system-update-progress__head strong {
  color: #b93131;
}

.system-update-progress.is-failed .system-update-progress__bar span {
  background: linear-gradient(90deg, #df5656 0%, #f08a8a 100%);
}

html[data-theme="dark"] .system-update-progress.is-failed .system-update-progress__head em,
html[data-theme="dark"] .system-update-progress.is-failed .system-update-progress__head strong {
  color: #ff8a8a;
}
```

- [ ] **Step 2: Run focused helper test**

Run:

```bash
node --test frontend/src/lib/systemUpdateProgress.test.mjs
```

Expected: PASS.

- [ ] **Step 3: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected: PASS.

- [ ] **Step 4: Inspect changed files**

Run:

```bash
git status --short
git diff -- frontend/src/style.css frontend/src/pages/SettingsPage.vue frontend/src/lib/systemUpdateProgress.js frontend/src/lib/systemUpdateProgress.test.mjs
```

Expected:

- Only planned frontend files are modified or already committed.
- `videos/makerhub-intro/output/` may remain untracked and must not be staged.
- Diff shows no backend API changes.

- [ ] **Step 5: Commit styling**

Run:

```bash
git add frontend/src/style.css
git commit -m "style: 优化系统更新进度展示"
```

Expected: commit only includes `frontend/src/style.css`.

- [ ] **Step 6: Final verification**

Run:

```bash
node --test frontend/src/lib/systemUpdateProgress.test.mjs
npm --prefix frontend run build
git status --short
```

Expected:

- Helper tests PASS.
- Frontend build PASS.
- Working tree has no tracked-file changes.
- Untracked `videos/makerhub-intro/output/` may still be present and must remain untouched.

## Self-Review

Spec coverage:

- Progress after click: Task 2 renders the block whenever backend status becomes active.
- Stage mapping: Task 1 implements all phase mappings from the spec.
- Restart wait: Task 1 covers `pending_startup`; Task 2 keeps existing API failure message behavior.
- Failure state: Task 1 covers failure progress and message; Task 3 adds failed styling.
- Theme readability: Task 3 uses existing tokens and dark-mode failure color.
- No backend progress changes: File structure and tasks only touch frontend files.

Placeholder scan:

- The plan contains no placeholder instructions.
- Every code-changing step includes concrete code.

Type consistency:

- `systemUpdateProgressState()` returns `visible`, `active`, `completed`, `failed`, `label`, `message`, `progress`, `percentText`, `variant`.
- `SettingsPage.vue` uses exactly those property names.
