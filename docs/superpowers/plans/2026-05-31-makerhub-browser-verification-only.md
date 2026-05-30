# Pure Browser Verification View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MakerHub browser verification popup show only the remote verification surface during normal verification, with no task metadata, no model metadata, and no visible action controls.

**Architecture:** Keep the existing real-browser verification pipeline. The backend continues to launch Chromium, crop screenshots, forward input, capture the verification proof, and retry 3MF downloads; the frontend becomes a minimal viewport over that remote screenshot with concise fallback text only when the screenshot is unavailable or the session has ended.

**Tech Stack:** Vue 3 single-file component, Vite frontend build, Node test runner, Python `unittest`, existing MakerHub browser verification services.

---

## File Structure

- Modify `frontend/src/pages/BrowserVerificationPage.vue`: remove the normal header/action surface, remove unused navigation/cancel state, and keep only the screenshot viewport plus minimal fallback messaging.
- Modify `frontend/src/style.css`: remove verification action styles, remove card-like panel chrome, and size the popup content around the verification image.
- Modify `frontend/src/lib/routerShape.test.mjs`: expand source-level assertions so the verification page cannot reintroduce action controls, task navigation, or normal status headings.
- Modify `frontend/src/lib/browserVerificationWindow.js`: tune the popup window dimensions for the pure verification surface.
- Modify `frontend/src/lib/browserVerificationWindow.test.mjs`: update popup feature expectations.
- Modify `VERSION`: bump from `0.8.10` to `0.8.11`.
- Modify `README.md`: add `v0.8.11` notes and keep only the latest three releases expanded.

No backend code is needed for this implementation because the required real-browser, cropped screenshot, coordinate offset, Bambu API cookie, and token handling already exist in `app/services/browser_verification.py`.

## Task 1: Lock the Pure Verification Surface With Failing Frontend Tests

**Files:**
- Modify: `frontend/src/lib/routerShape.test.mjs`
- Modify: `frontend/src/lib/browserVerificationWindow.test.mjs`

- [ ] **Step 1: Expand the page-shape test**

In `frontend/src/lib/routerShape.test.mjs`, replace the second test with this stricter version:

```js
test("browser verification page only renders the validation surface", () => {
  const source = readFileSync(new URL("../pages/BrowserVerificationPage.vue", import.meta.url), "utf8");

  assert.doesNotMatch(source, /browser-verification-topbar/);
  assert.doesNotMatch(source, /browser-verification-stats/);
  assert.doesNotMatch(source, /section-card__header/);
  assert.doesNotMatch(source, /browser-verification-actions/);
  assert.doesNotMatch(source, /RouterLink/);
  assert.doesNotMatch(source, /panelHeading/);
  assert.doesNotMatch(source, /cancelSession/);
  assert.doesNotMatch(source, /cancelling/);
  assert.doesNotMatch(source, />返回任务</);
  assert.doesNotMatch(source, />刷新</);
  assert.doesNotMatch(source, />取消</);
  assert.doesNotMatch(source, />平台</);
  assert.doesNotMatch(source, />状态</);
  assert.doesNotMatch(source, />截图</);
  assert.doesNotMatch(source, />模型 ID</);
  assert.doesNotMatch(source, />配置</);
  assert.doesNotMatch(source, />Captcha</);
  assert.match(source, /visibleMessageText/);
});
```

- [ ] **Step 2: Update the popup-size expectations**

In `frontend/src/lib/browserVerificationWindow.test.mjs`, change both expected feature strings from:

```js
features: "popup=yes,width=640,height=720,left=120,top=60",
```

to:

```js
features: "popup=yes,width=560,height=620,left=120,top=60",
```

- [ ] **Step 3: Run the frontend red tests**

Run:

```bash
node --test frontend/src/lib/browserVerificationWindow.test.mjs frontend/src/lib/routerShape.test.mjs
```

Expected result:

```text
not ok
```

The failures should mention at least one of `section-card__header`, `browser-verification-actions`, `RouterLink`, `panelHeading`, or the old popup feature string. If the tests pass before implementation, tighten the assertions until the current UI fails.

- [ ] **Step 4: Commit the red tests**

Run:

```bash
git add frontend/src/lib/routerShape.test.mjs frontend/src/lib/browserVerificationWindow.test.mjs
git commit -m "test: lock pure browser verification surface"
```

## Task 2: Convert the Vue Page Into a Pure Screenshot Viewport

**Files:**
- Modify: `frontend/src/pages/BrowserVerificationPage.vue`
- Test: `frontend/src/lib/routerShape.test.mjs`

- [ ] **Step 1: Replace the template**

In `frontend/src/pages/BrowserVerificationPage.vue`, replace the full `<template>` block with:

```vue
<template>
  <main class="browser-verification-shell">
    <section class="browser-verification-panel">
      <div class="browser-verification-viewer">
        <div
          ref="viewerRef"
          class="browser-verification-frame"
          tabindex="0"
          role="application"
          @click="sendPointerCommand('click', $event)"
          @mousemove="handleMouseMove"
          @mousedown.prevent="sendPointerCommand('mousedown', $event)"
          @mouseup.prevent="sendPointerCommand('mouseup', $event)"
          @wheel.prevent="sendWheelCommand"
          @keydown.prevent="sendKeyCommand"
          @paste.prevent="sendPasteCommand"
        >
          <img
            v-if="screenshotUrl"
            class="browser-verification-screenshot"
            :src="screenshotUrl"
            alt=""
            draggable="false"
            @load="screenshotLoaded = true"
            @error="screenshotLoaded = false"
          >
          <div v-if="!screenshotUrl || !screenshotLoaded" class="browser-verification-empty">
            <strong>{{ emptyTitle }}</strong>
            <span v-if="emptyMessage">{{ emptyMessage }}</span>
          </div>
        </div>
      </div>
      <span
        v-if="visibleMessageText"
        :class="['form-status', isError && 'is-error', isCompleted && 'is-success']"
      >{{ visibleMessageText }}</span>
    </section>
  </main>
</template>
```

- [ ] **Step 2: Simplify imports and reactive state**

In the `<script setup>` block, change:

```js
import { RouterLink, useRoute } from "vue-router";
```

to:

```js
import { useRoute } from "vue-router";
```

Remove this state:

```js
const cancelling = ref(false);
```

Remove this computed block:

```js
const panelHeading = computed(() => {
  if (!session.value) {
    return "正在读取会话";
  }
  return isFinished.value ? "验证会话结果" : "远程验证画面";
});
const messageText = computed(() => session.value?.error || session.value?.message || "");
```

Add these computed values after `isCompleted`:

```js
const visibleMessageText = computed(() => {
  if (!isFinished.value) {
    return "";
  }
  return session.value?.error || session.value?.message || "";
});
const emptyTitle = computed(() => {
  if (isError.value) {
    return "验证页面加载失败";
  }
  if (isCompleted.value) {
    return "验证已完成";
  }
  return "正在加载验证页面";
});
const emptyMessage = computed(() => {
  if (!isFinished.value) {
    return "";
  }
  return session.value?.error || session.value?.message || "";
});
```

Remove the old `emptyTitle` and `emptyMessage` computed values:

```js
const emptyTitle = computed(() => loading.value ? "正在连接 worker" : "等待浏览器画面");
const emptyMessage = computed(() => isFinished.value ? "会话已结束，返回任务页查看重试进度。" : "worker 启动浏览器后会在这里显示验证页面。");
```

- [ ] **Step 3: Remove the cancel function**

Delete this function from `frontend/src/pages/BrowserVerificationPage.vue`:

```js
async function cancelSession() {
  cancelling.value = true;
  try {
    session.value = await apiRequest(`/api/browser-verification/sessions/${encodeURIComponent(sessionId.value)}/cancel`, {
      method: "POST",
    });
  } finally {
    cancelling.value = false;
  }
}
```

- [ ] **Step 4: Run the page-shape tests**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs
```

Expected result:

```text
pass 2
fail 0
```

- [ ] **Step 5: Run the frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected result:

```text
✓ built
```

- [ ] **Step 6: Commit the Vue change**

Run:

```bash
git add frontend/src/pages/BrowserVerificationPage.vue
git commit -m "refactor: show only browser verification viewport"
```

## Task 3: Remove Extra Visual Chrome and Resize the Popup

**Files:**
- Modify: `frontend/src/style.css`
- Modify: `frontend/src/lib/browserVerificationWindow.js`
- Test: `frontend/src/lib/browserVerificationWindow.test.mjs`
- Test: `frontend/src/lib/routerShape.test.mjs`

- [ ] **Step 1: Update popup dimensions**

In `frontend/src/lib/browserVerificationWindow.js`, change:

```js
const WINDOW_FEATURES = "popup=yes,width=640,height=720,left=120,top=60";
```

to:

```js
const WINDOW_FEATURES = "popup=yes,width=560,height=620,left=120,top=60";
```

- [ ] **Step 2: Replace browser verification CSS**

In `frontend/src/style.css`, replace the browser verification block from `.browser-verification-page` through `.browser-verification-empty strong` with:

```css
.browser-verification-page {
  background: var(--bg);
}

.browser-verification-shell {
  width: min(560px, 100vw);
  min-height: 100vh;
  min-height: 100dvh;
  margin: 0 auto;
  padding: 0;
  display: grid;
  align-content: start;
}

.browser-verification-panel {
  min-width: 0;
  padding: 0;
  border: 0;
  border-radius: 0;
  display: grid;
  gap: 0;
  background: transparent;
  box-shadow: none;
}

.browser-verification-viewer {
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 0;
  align-items: start;
}

.browser-verification-frame {
  position: relative;
  min-height: 420px;
  aspect-ratio: 520 / 640;
  overflow: hidden;
  border: 0;
  border-radius: 0;
  background: var(--surface-strong);
  outline: none;
}

.browser-verification-frame:focus {
  box-shadow: inset 0 0 0 2px color-mix(in srgb, var(--brand) 25%, transparent);
}

.browser-verification-screenshot {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: fill;
  user-select: none;
}

.browser-verification-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  gap: 6px;
  padding: 20px;
  text-align: center;
  color: var(--muted);
}

.browser-verification-empty strong {
  color: var(--text);
  font-size: 15px;
}
```

- [ ] **Step 3: Remove mobile action styles**

In the `@media (max-width: 720px)` block, delete these rules:

```css
.browser-verification-actions {
  width: 100%;
  justify-content: stretch;
}

.browser-verification-actions .button {
  flex: 1 1 auto;
}
```

Keep the mobile frame height rule:

```css
.browser-verification-frame {
  min-height: 360px;
}
```

- [ ] **Step 4: Run frontend tests**

Run:

```bash
node --test frontend/src/lib/browserVerificationWindow.test.mjs frontend/src/lib/routerShape.test.mjs
```

Expected result:

```text
pass 5
fail 0
```

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected result:

```text
✓ built
```

- [ ] **Step 6: Commit style and popup sizing**

Run:

```bash
git add frontend/src/style.css frontend/src/lib/browserVerificationWindow.js frontend/src/lib/browserVerificationWindow.test.mjs
git commit -m "style: trim browser verification popup chrome"
```

## Task 4: Update Version and Release Notes

**Files:**
- Modify: `VERSION`
- Modify: `README.md`

- [ ] **Step 1: Bump the version file**

In `VERSION`, replace:

```text
0.8.10
```

with:

```text
0.8.11
```

- [ ] **Step 2: Update README current version**

In `README.md`, replace:

```markdown
> 当前版本：`v0.8.10`
```

with:

```markdown
> 当前版本：`v0.8.11`
```

- [ ] **Step 3: Add the new release entry**

Under `## 更新记录`, insert this entry above `v0.8.10`:

```markdown
### 2026-05-31 · v0.8.11

- 浏览器验证弹窗改为纯验证画面，正常状态下只显示远程验证码区域，不再显示返回、刷新、取消等操作控件。
- 验证页只在加载失败、超时或完成时显示必要提示，避免模型、任务和状态信息干扰手动验证。
- 缩小验证弹窗尺寸，并增加纯验证页面形态的前端回归测试。
```

- [ ] **Step 4: Keep only the latest three releases expanded**

Move the current `v0.8.8` section into the existing `<details>` history block so the expanded release list is:

```markdown
### 2026-05-31 · v0.8.11
### 2026-05-30 · v0.8.10
### 2026-05-29 · v0.8.9
```

The `<details>` block should start with `v0.8.8`, followed by older entries.

- [ ] **Step 5: Run README/version checks**

Run:

```bash
rg -n "当前版本|v0\\.8\\.11|v0\\.8\\.10|v0\\.8\\.9|v0\\.8\\.8|历史更新记录" README.md VERSION
```

Expected result:

```text
README.md:7:> 当前版本：`v0.8.11`
README.md:209:### 2026-05-31 · v0.8.11
```

The exact line numbers may differ, but `v0.8.11`, `v0.8.10`, and `v0.8.9` must appear before `<summary>历史更新记录</summary>`, and `v0.8.8` must appear after it.

- [ ] **Step 6: Commit release notes**

Run:

```bash
git add VERSION README.md
git commit -m "docs: document pure verification popup"
```

## Task 5: Full Verification and Final Integration

**Files:**
- Verify committed changes from Tasks 1 through 4.

- [ ] **Step 1: Run backend verification tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_browser_verification tests.test_browser_verification_api tests.test_web_routes tests.test_github_changelog
```

Expected result:

```text
OK
```

- [ ] **Step 2: Run frontend verification tests**

Run:

```bash
node --test frontend/src/lib/browserVerificationWindow.test.mjs frontend/src/lib/routerShape.test.mjs
```

Expected result:

```text
pass 5
fail 0
```

- [ ] **Step 3: Run Python syntax check**

Run:

```bash
.venv/bin/python -m py_compile app/services/browser_verification.py
```

Expected result: command exits with code `0` and prints no errors.

- [ ] **Step 4: Run frontend production build**

Run:

```bash
npm --prefix frontend run build
```

Expected result:

```text
✓ built
```

- [ ] **Step 5: Check diff hygiene**

Run:

```bash
git diff --check
```

Expected result: command exits with code `0` and prints no whitespace errors.

- [ ] **Step 6: Inspect final history and status**

Run:

```bash
git status --short --branch
git log --oneline -6
```

Expected result: the branch contains the task commits and has no unstaged implementation changes.

## Self-Review

Spec coverage:

- Pure popup surface is covered by Tasks 1, 2, and 3.
- Hidden metadata and hidden controls are covered by Task 1 assertions and Task 2 template removal.
- Minimal fallback messages are covered by Task 2 computed values.
- Existing backend verification flow remains unchanged and is verified in Task 5.
- Version and release notes are covered by Task 4.

Placeholder scan:

- The plan contains no `TBD`, no incomplete code blocks, and no undefined file paths.

Type consistency:

- Frontend state names are consistent: `visibleMessageText`, `emptyTitle`, `emptyMessage`, `isError`, and `isCompleted`.
- Popup feature string is consistently `popup=yes,width=560,height=620,left=120,top=60` in implementation and tests.
