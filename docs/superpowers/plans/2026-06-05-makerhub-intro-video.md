# MakerHub Intro Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable MakerHub promotional-video workflow that captures safe visuals from a real online instance and renders 45-second 16:9 and 9:16 intro videos with HyperFrames.

**Architecture:** Keep all video-specific code isolated under `videos/makerhub-intro/`. Use Playwright for authenticated page capture, a pure Node leak scanner for privacy checks, and HTML/CSS/JS HyperFrames compositions for the final renders. Runtime secrets enter only through environment variables and are never committed.

**Tech Stack:** Node.js 24, npm scripts, Playwright 1.60, HyperFrames 0.6, HTML/CSS/JS compositions, FFmpeg, existing MakerHub Vue route paths.

---

## File Structure

- Modify `.gitignore`
  - Ignore captured material, generated audio, rendered output, local env files, and Playwright browser state under `videos/makerhub-intro/`.
- Create `videos/makerhub-intro/package.json`
  - Owns only the video workflow dependencies and scripts.
  - Keeps HyperFrames and Playwright out of the MakerHub frontend package.
- Create `videos/makerhub-intro/.env.example`
  - Documents required runtime variables without real values.
- Create `videos/makerhub-intro/storyboard.md`
  - Mirrors the approved Chinese storyboard and timing.
- Create `videos/makerhub-intro/scripts/storyboard.mjs`
  - Exports the storyboard as data used by composition and tests.
- Create `videos/makerhub-intro/scripts/capture-plan.mjs`
  - Defines capture targets, route paths, safe filenames, waits, and crop behavior.
- Create `videos/makerhub-intro/scripts/capture-plan.test.mjs`
  - Tests route coverage, safe filenames, timings, and no sensitive values.
- Create `videos/makerhub-intro/scripts/capture.mjs`
  - Logs into the online instance using environment variables and captures only the app content area.
- Create `videos/makerhub-intro/scripts/redaction-check.mjs`
  - Scans generated project files and manifests for configured secrets and token-like leaks.
- Create `videos/makerhub-intro/scripts/redaction-check.test.mjs`
  - Tests scanner behavior with clean and unsafe fixture content.
- Create `videos/makerhub-intro/scripts/ensure-placeholders.mjs`
  - Creates non-sensitive SVG placeholder assets for local linting and composition development when real capture assets are absent.
- Create `videos/makerhub-intro/compositions/intro-16x9.html`
  - Primary 1920x1080 HyperFrames composition.
- Create `videos/makerhub-intro/compositions/intro-9x16.html`
  - Derived 1080x1920 HyperFrames composition using focused crops.
- Create `videos/makerhub-intro/compositions/styles.css`
  - Shared visual treatment, caption styles, overlays, and MakerHub dark workstation palette.
- Create `videos/makerhub-intro/scripts/render.mjs`
  - Runs placeholder preparation, leak check, HyperFrames lint, and render commands.
- Create `videos/makerhub-intro/README.md`
  - Explains setup, safe capture, preview, render, verification, and privacy rules.

---

### Task 1: Add Video Workflow Skeleton And Storyboard

**Files:**
- Modify: `.gitignore`
- Create: `videos/makerhub-intro/package.json`
- Create: `videos/makerhub-intro/.env.example`
- Create: `videos/makerhub-intro/storyboard.md`
- Create: `videos/makerhub-intro/scripts/storyboard.mjs`

- [ ] **Step 1: Add generated video paths to `.gitignore`**

Append these entries to `.gitignore`:

```gitignore

# MakerHub intro video local artifacts
videos/makerhub-intro/.env
videos/makerhub-intro/assets/captured/
videos/makerhub-intro/assets/placeholders/
videos/makerhub-intro/assets/audio/
videos/makerhub-intro/output/
videos/makerhub-intro/.auth/
videos/makerhub-intro/node_modules/
```

- [ ] **Step 2: Create the video workflow package**

Create `videos/makerhub-intro/package.json`:

```json
{
  "name": "makerhub-intro-video",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "test": "node --test scripts/*.test.mjs",
    "capture": "node scripts/capture.mjs",
    "redaction:check": "node scripts/redaction-check.mjs",
    "placeholders": "node scripts/ensure-placeholders.mjs",
    "lint:hyperframes": "npm run placeholders && hyperframes lint compositions",
    "preview": "npm run placeholders && hyperframes preview --port 3002",
    "render": "node scripts/render.mjs",
    "render:16x9": "node scripts/render.mjs --only=16x9",
    "render:9x16": "node scripts/render.mjs --only=9x16"
  },
  "dependencies": {
    "hyperframes": "^0.6.72",
    "playwright": "^1.60.0"
  }
}
```

- [ ] **Step 3: Create the environment example**

Create `videos/makerhub-intro/.env.example`:

```bash
# Copy to .env locally or export these variables in your shell.
# Do not commit .env or real values.

MAKERHUB_VIDEO_BASE_URL=
MAKERHUB_VIDEO_USERNAME=
MAKERHUB_VIDEO_PASSWORD=

# Optional. Defaults to chromium from Playwright.
MAKERHUB_VIDEO_BROWSER=chromium
MAKERHUB_VIDEO_HEADLESS=true

# Optional. Set to 1 to keep browser storage state for repeated local captures.
MAKERHUB_VIDEO_SAVE_AUTH_STATE=0

# Optional. Comma-separated extra leak strings to reject in generated files.
MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS=
```

- [ ] **Step 4: Create the Chinese storyboard document**

Create `videos/makerhub-intro/storyboard.md`:

```markdown
# MakerHub 45 秒介绍视频分镜

## 成片目标

- 45 秒中文宣传片，不做逐步教程。
- 16:9 横屏主版，9:16 竖屏派生版。
- 真实线上 MakerHub 页面素材。
- 只截 MakerHub 应用内容区域，不显示浏览器地址栏。
- 中文旁白和同步字幕，初版旁白可以是可替换 TTS 占位。

## 分镜

| 时间 | 画面 | 屏幕标题 | 旁白 |
| --- | --- | --- | --- |
| 0-4s | 模型库封面网格快速推进，突出模型卡片细节。 | 私有模型库 | 把 MakerWorld 模型，整理成你自己的私有模型库。 |
| 4-8s | 首页状态卡片扫过归档、订阅、本地导入、任务和源站状态。 | 一个工作台看全局 | 归档、订阅、导入、任务和源站状态，都集中在一个工作台。 |
| 8-14s | 线上账号区域，随后切到同步后的订阅来源卡片。 | 登录后自动同步 | 登录线上账号，关注作者、收藏夹和合集会自动进入订阅库。 |
| 14-19s | 订阅库/来源库卡片，以及来源详情中的模型聚合。 | 持续发现新模型 | MakerHub 会按订阅来源持续检查，把新模型加入归档流程。 |
| 19-25s | 源端刷新页面，展示运行进度和结果摘要。 | 保持源端信息新鲜 | 源端刷新会更新评论、附件、打印配置和模型状态。 |
| 25-30s | 模型卡片或详情页上的源端删除/远端状态标记。 | 识别源端删除 | 如果源站模型已经消失，本地资料库也能清楚标记。 |
| 30-35s | 本地上传入口和本地整理进度。 | 本地模型也能整理 | 3MF、STL、STEP、OBJ 和压缩包，可以从网页、手机或本地文件夹导入。 |
| 35-40s | 分享弹窗和接收分享入口，敏感码隐藏或避开。 | 模型安全分享 | 生成分享码，把模型发送给另一台 MakerHub，并自动检查重复。 |
| 40-45s | 首页验证异常提示和重试入口。 | 验证后继续归档 | 遇到 MakerWorld 验证，去源站完成验证，回到 MakerHub 重试即可。 |

## 隐私要求

- 线上实例地址、账号凭据、Cookie、Token、分享码、公网地址、代理值和服务器路径不能进入成片或提交文件。
- 模型名、作者名、收藏夹名和公开模型封面默认可以保留。
- 分享素材优先拍分享入口和弹窗，不提交生成分享码的操作。
```

- [ ] **Step 5: Create the storyboard data module**

Create `videos/makerhub-intro/scripts/storyboard.mjs`:

```js
export const VIDEO_DURATION_SECONDS = 45;

export const storyboardSegments = [
  {
    id: "model-library",
    start: 0,
    duration: 4,
    title: "私有模型库",
    visual: "模型库封面网格快速推进，突出模型卡片细节。",
    voiceover: "把 MakerWorld 模型，整理成你自己的私有模型库。",
  },
  {
    id: "dashboard",
    start: 4,
    duration: 4,
    title: "一个工作台看全局",
    visual: "首页状态卡片扫过归档、订阅、本地导入、任务和源站状态。",
    voiceover: "归档、订阅、导入、任务和源站状态，都集中在一个工作台。",
  },
  {
    id: "online-sync",
    start: 8,
    duration: 6,
    title: "登录后自动同步",
    visual: "线上账号区域，随后切到同步后的订阅来源卡片。",
    voiceover: "登录线上账号，关注作者、收藏夹和合集会自动进入订阅库。",
  },
  {
    id: "subscriptions",
    start: 14,
    duration: 5,
    title: "持续发现新模型",
    visual: "订阅库/来源库卡片，以及来源详情中的模型聚合。",
    voiceover: "MakerHub 会按订阅来源持续检查，把新模型加入归档流程。",
  },
  {
    id: "remote-refresh",
    start: 19,
    duration: 6,
    title: "保持源端信息新鲜",
    visual: "源端刷新页面，展示运行进度和结果摘要。",
    voiceover: "源端刷新会更新评论、附件、打印配置和模型状态。",
  },
  {
    id: "source-deleted",
    start: 25,
    duration: 5,
    title: "识别源端删除",
    visual: "模型卡片或详情页上的源端删除/远端状态标记。",
    voiceover: "如果源站模型已经消失，本地资料库也能清楚标记。",
  },
  {
    id: "local-upload",
    start: 30,
    duration: 5,
    title: "本地模型也能整理",
    visual: "本地上传入口和本地整理进度。",
    voiceover: "3MF、STL、STEP、OBJ 和压缩包，可以从网页、手机或本地文件夹导入。",
  },
  {
    id: "sharing",
    start: 35,
    duration: 5,
    title: "模型安全分享",
    visual: "分享弹窗和接收分享入口，敏感码隐藏或避开。",
    voiceover: "生成分享码，把模型发送给另一台 MakerHub，并自动检查重复。",
  },
  {
    id: "verification",
    start: 40,
    duration: 5,
    title: "验证后继续归档",
    visual: "首页验证异常提示和重试入口。",
    voiceover: "遇到 MakerWorld 验证，去源站完成验证，回到 MakerHub 重试即可。",
  },
];

export function storyboardById(id) {
  return storyboardSegments.find((segment) => segment.id === id) || null;
}
```

- [ ] **Step 6: Install video workflow dependencies**

Run:

```bash
npm --prefix videos/makerhub-intro install
```

Expected: `videos/makerhub-intro/package-lock.json` is created, and npm installs `hyperframes` and `playwright`.

- [ ] **Step 7: Verify dependency installation**

Run:

```bash
npm --prefix videos/makerhub-intro exec -- hyperframes info
```

Expected: prints HyperFrames version and environment information. This confirms the local video package can run the HyperFrames CLI.

- [ ] **Step 8: Commit the skeleton**

Run:

```bash
git add .gitignore videos/makerhub-intro/package.json videos/makerhub-intro/package-lock.json videos/makerhub-intro/.env.example videos/makerhub-intro/storyboard.md videos/makerhub-intro/scripts/storyboard.mjs
git commit -m "chore: scaffold intro video workflow"
```

---

### Task 2: Add Capture Plan And Tests

**Files:**
- Create: `videos/makerhub-intro/scripts/capture-plan.mjs`
- Create: `videos/makerhub-intro/scripts/capture-plan.test.mjs`

- [ ] **Step 1: Write failing capture-plan tests**

Create `videos/makerhub-intro/scripts/capture-plan.test.mjs`:

```js
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
  assert.equal(resolveCaptureUrl("https://demo.invalid/base", "/models?tag=__source_deleted__"), "https://demo.invalid/models?tag=__source_deleted__");
  assert.equal(resolveCaptureUrl("https://demo.invalid/base/", "/settings?tab=accounts"), "https://demo.invalid/settings?tab=accounts");
});

test("capture targets do not submit share creation or expose sensitive routes", () => {
  const sharing = captureTargets.find((target) => target.id === "sharing");
  assert.equal(sharing.actions.some((action) => action.type === "click" && /生成|复制|确定/.test(action.name || "")), false);
  for (const target of captureTargets) {
    assert.equal(/token|cookie|password|share_code/i.test(JSON.stringify(target)), false, target.id);
  }
});
```

- [ ] **Step 2: Run the failing tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: fails with a module-not-found error for `capture-plan.mjs`.

- [ ] **Step 3: Implement the capture plan**

Create `videos/makerhub-intro/scripts/capture-plan.mjs`:

```js
import { storyboardSegments, VIDEO_DURATION_SECONDS } from "./storyboard.mjs";

export const CAPTURE_VIEWPORT = { width: 1920, height: 1080, deviceScaleFactor: 1 };
export const CAPTURE_TIMEOUT_MS = 30000;
export const CAPTURE_SETTLE_MS = 1200;

export const captureTargets = [
  {
    order: 1,
    id: "model-library",
    route: "/models",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 2,
    id: "dashboard",
    route: "/",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 3,
    id: "online-sync",
    route: "/settings?tab=accounts",
    waitFor: ".online-account-card, .settings-panel",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 4,
    id: "subscriptions",
    route: "/subscriptions",
    waitFor: ".source-library-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 5,
    id: "remote-refresh",
    route: "/remote-refresh",
    waitFor: ".remote-refresh-layout, .remote-refresh-card",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 6,
    id: "source-deleted",
    route: "/models?tag=__source_deleted__",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 7,
    id: "local-upload",
    route: "/organizer",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [
      { type: "click", name: "open-local-import", selector: "button:has-text('导入本地模型')", optional: true },
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 8,
    id: "sharing",
    route: "/models",
    waitFor: ".model-grid, .empty-state",
    cropSelector: ".page-shell",
    actions: [
      { type: "click", name: "enter-select-mode", selector: "button:has-text('选择')", optional: true },
      { type: "click", name: "select-first-card", selector: ".gallery-card__select-toggle", optional: true },
      { type: "click", name: "open-share-dialog", selector: "button:has-text('分享')", optional: true },
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
  {
    order: 9,
    id: "verification",
    route: "/",
    waitFor: ".page-shell",
    cropSelector: ".page-shell",
    actions: [
      { type: "wait", ms: CAPTURE_SETTLE_MS },
    ],
  },
];

export function targetById(id) {
  return captureTargets.find((target) => target.id === id) || null;
}

export function safeCaptureFilename(id) {
  const target = targetById(id);
  if (!target) {
    throw new Error(`Unknown capture target: ${id}`);
  }
  return `${String(target.order).padStart(2, "0")}-${target.id}.png`;
}

export function resolveCaptureUrl(baseUrl, route) {
  const normalizedBase = String(baseUrl || "").trim();
  if (!normalizedBase) {
    throw new Error("MAKERHUB_VIDEO_BASE_URL is required");
  }
  return new URL(route, normalizedBase.endsWith("/") ? normalizedBase : `${normalizedBase}/`).toString();
}

export function validateCapturePlan() {
  const errors = [];
  const storyboardIds = new Set(storyboardSegments.map((segment) => segment.id));
  const targetIds = new Set(captureTargets.map((target) => target.id));
  const finalSegment = storyboardSegments.at(-1);
  if (!finalSegment || finalSegment.start + finalSegment.duration !== VIDEO_DURATION_SECONDS) {
    errors.push("storyboard duration must equal VIDEO_DURATION_SECONDS");
  }
  for (const id of storyboardIds) {
    if (!targetIds.has(id)) {
      errors.push(`missing capture target for ${id}`);
    }
  }
  for (const target of captureTargets) {
    if (!storyboardIds.has(target.id)) {
      errors.push(`capture target has no storyboard segment: ${target.id}`);
    }
    if (!Number.isInteger(target.order) || target.order < 1) {
      errors.push(`capture target has invalid order: ${target.id}`);
    }
    if (!target.route.startsWith("/")) {
      errors.push(`capture target route must be relative: ${target.id}`);
    }
  }
  return { valid: errors.length === 0, errors };
}
```

- [ ] **Step 4: Run capture-plan tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: all current tests pass.

- [ ] **Step 5: Commit the capture plan**

Run:

```bash
git add videos/makerhub-intro/scripts/capture-plan.mjs videos/makerhub-intro/scripts/capture-plan.test.mjs
git commit -m "test: add intro video capture plan"
```

---

### Task 3: Add Authenticated Page Capture

**Files:**
- Create: `videos/makerhub-intro/scripts/capture.mjs`

- [ ] **Step 1: Create the capture script**

Create `videos/makerhub-intro/scripts/capture.mjs`:

```js
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium, firefox, webkit } from "playwright";

import {
  CAPTURE_TIMEOUT_MS,
  CAPTURE_VIEWPORT,
  captureTargets,
  resolveCaptureUrl,
  safeCaptureFilename,
} from "./capture-plan.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const capturedDir = path.join(rootDir, "assets", "captured");
const authDir = path.join(rootDir, ".auth");
const manifestPath = path.join(capturedDir, "manifest.json");

const browsers = { chromium, firefox, webkit };

function env(name, { required = true, defaultValue = "" } = {}) {
  const value = String(process.env[name] || defaultValue).trim();
  if (required && !value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function booleanEnv(name, defaultValue) {
  const value = String(process.env[name] || "").trim().toLowerCase();
  if (!value) return defaultValue;
  return ["1", "true", "yes", "on"].includes(value);
}

function redactForError(message) {
  let output = String(message || "");
  for (const secretName of ["MAKERHUB_VIDEO_BASE_URL", "MAKERHUB_VIDEO_USERNAME", "MAKERHUB_VIDEO_PASSWORD"]) {
    const secret = String(process.env[secretName] || "").trim();
    if (secret) {
      output = output.split(secret).join(`[redacted:${secretName}]`);
    }
  }
  return output;
}

async function waitForAppReady(page, selector) {
  await page.waitForLoadState("domcontentloaded", { timeout: CAPTURE_TIMEOUT_MS });
  await page.waitForLoadState("networkidle", { timeout: CAPTURE_TIMEOUT_MS }).catch(() => {});
  await page.waitForSelector(selector, { timeout: CAPTURE_TIMEOUT_MS });
}

async function clickOptional(page, action) {
  const locator = page.locator(action.selector).first();
  const count = await locator.count();
  if (!count) {
    if (action.optional) return false;
    throw new Error(`Required action target not found: ${action.name}`);
  }
  await locator.click({ timeout: CAPTURE_TIMEOUT_MS });
  return true;
}

async function runActions(page, actions) {
  for (const action of actions || []) {
    if (action.type === "wait") {
      await page.waitForTimeout(action.ms);
    } else if (action.type === "click") {
      await clickOptional(page, action);
    } else {
      throw new Error(`Unsupported action type: ${action.type}`);
    }
  }
}

async function loginIfNeeded(page, baseUrl, username, password) {
  await page.goto(resolveCaptureUrl(baseUrl, "/login"), { waitUntil: "domcontentloaded", timeout: CAPTURE_TIMEOUT_MS });
  await page.waitForLoadState("networkidle", { timeout: CAPTURE_TIMEOUT_MS }).catch(() => {});

  if (!/\/login(?:\?|$)/.test(new URL(page.url()).pathname + new URL(page.url()).search)) {
    return;
  }

  await page.getByLabel("用户名").fill(username);
  await page.getByLabel("密码").fill(password);
  await Promise.all([
    page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: CAPTURE_TIMEOUT_MS }),
    page.getByRole("button", { name: "登录" }).click(),
  ]);
}

async function screenshotTarget(page, target, baseUrl) {
  const url = resolveCaptureUrl(baseUrl, target.route);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: CAPTURE_TIMEOUT_MS });
  await waitForAppReady(page, target.waitFor);
  await runActions(page, target.actions);

  const filename = safeCaptureFilename(target.id);
  const outputPath = path.join(capturedDir, filename);
  const crop = page.locator(target.cropSelector).first();
  const cropCount = await crop.count();
  if (cropCount) {
    await crop.screenshot({ path: outputPath, animations: "disabled" });
  } else {
    await page.screenshot({ path: outputPath, fullPage: false, animations: "disabled" });
  }

  return {
    id: target.id,
    order: target.order,
    route: target.route,
    filename,
    file: `assets/captured/${filename}`,
    captured_at: new Date().toISOString(),
  };
}

async function main() {
  const baseUrl = env("MAKERHUB_VIDEO_BASE_URL");
  const username = env("MAKERHUB_VIDEO_USERNAME");
  const password = env("MAKERHUB_VIDEO_PASSWORD");
  const browserName = env("MAKERHUB_VIDEO_BROWSER", { required: false, defaultValue: "chromium" });
  const headless = booleanEnv("MAKERHUB_VIDEO_HEADLESS", true);
  const saveAuthState = booleanEnv("MAKERHUB_VIDEO_SAVE_AUTH_STATE", false);
  const browserType = browsers[browserName];
  if (!browserType) {
    throw new Error(`Unsupported browser: ${browserName}`);
  }

  await fs.mkdir(capturedDir, { recursive: true });
  await fs.mkdir(authDir, { recursive: true });

  const browser = await browserType.launch({ headless });
  const context = await browser.newContext({
    viewport: { width: CAPTURE_VIEWPORT.width, height: CAPTURE_VIEWPORT.height },
    deviceScaleFactor: CAPTURE_VIEWPORT.deviceScaleFactor,
    locale: "zh-CN",
  });
  const page = await context.newPage();

  try {
    await loginIfNeeded(page, baseUrl, username, password);
    const captures = [];
    for (const target of captureTargets) {
      captures.push(await screenshotTarget(page, target, baseUrl));
    }
    if (saveAuthState) {
      await context.storageState({ path: path.join(authDir, "storage-state.json") });
    }
    await fs.writeFile(
      manifestPath,
      `${JSON.stringify({
        generated_at: new Date().toISOString(),
        viewport: CAPTURE_VIEWPORT,
        captures,
      }, null, 2)}\n`,
      "utf8",
    );
    console.log(`Captured ${captures.length} MakerHub intro assets.`);
  } catch (error) {
    console.error(redactForError(error instanceof Error ? error.stack || error.message : String(error)));
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

await main();
```

- [ ] **Step 2: Run capture without env and verify safe failure**

Run:

```bash
npm --prefix videos/makerhub-intro run capture
```

Expected: exits non-zero with `MAKERHUB_VIDEO_BASE_URL is required`. It must not print any real URL or credential value.

- [ ] **Step 3: Run capture with local environment variables**

Run with real values supplied by the user's shell, not committed files:

```bash
MAKERHUB_VIDEO_BASE_URL="$MAKERHUB_VIDEO_BASE_URL" \
MAKERHUB_VIDEO_USERNAME="$MAKERHUB_VIDEO_USERNAME" \
MAKERHUB_VIDEO_PASSWORD="$MAKERHUB_VIDEO_PASSWORD" \
npm --prefix videos/makerhub-intro run capture
```

Expected: creates `videos/makerhub-intro/assets/captured/01-model-library.png` through `09-verification.png` and `videos/makerhub-intro/assets/captured/manifest.json`.

- [ ] **Step 4: Inspect captured images before committing**

Run:

```bash
find videos/makerhub-intro/assets/captured -maxdepth 1 -type f | sort
```

Expected: captured files exist but are ignored by git.

Run:

```bash
git status --short videos/makerhub-intro/assets videos/makerhub-intro/.auth
```

Expected: no tracked or untracked output for captured files because generated material is ignored.

- [ ] **Step 5: Commit the capture script**

Run:

```bash
git add videos/makerhub-intro/scripts/capture.mjs
git commit -m "feat: add intro video capture script"
```

---

### Task 4: Add Redaction Scanner And Tests

**Files:**
- Create: `videos/makerhub-intro/scripts/redaction-check.mjs`
- Create: `videos/makerhub-intro/scripts/redaction-check.test.mjs`

- [ ] **Step 1: Write failing redaction tests**

Create `videos/makerhub-intro/scripts/redaction-check.test.mjs`:

```js
import assert from "node:assert/strict";
import { test } from "node:test";

import { buildSecretPatterns, scanTextForLeaks } from "./redaction-check.mjs";

test("buildSecretPatterns includes runtime secrets and skips empty values", () => {
  const patterns = buildSecretPatterns({
    MAKERHUB_VIDEO_BASE_URL: "https://private.invalid",
    MAKERHUB_VIDEO_USERNAME: "demo-user",
    MAKERHUB_VIDEO_PASSWORD: "",
    MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS: "custom-secret, second-secret ",
  });
  assert.deepEqual(patterns.map((item) => item.label), [
    "MAKERHUB_VIDEO_BASE_URL",
    "MAKERHUB_VIDEO_USERNAME",
    "MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS[0]",
    "MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS[1]",
  ]);
});

test("scanTextForLeaks reports configured secrets", () => {
  const findings = scanTextForLeaks("asset points at https://private.invalid/models", [
    { label: "MAKERHUB_VIDEO_BASE_URL", value: "https://private.invalid" },
  ]);
  assert.equal(findings.length, 1);
  assert.equal(findings[0].label, "MAKERHUB_VIDEO_BASE_URL");
});

test("scanTextForLeaks reports token-like strings", () => {
  const findings = scanTextForLeaks("token=mh_abcdefghijklmnopqrstuvwxyz1234567890", []);
  assert.equal(findings.some((finding) => finding.label === "token-like-string"), true);
});

test("scanTextForLeaks accepts clean storyboard text", () => {
  const findings = scanTextForLeaks("把 MakerWorld 模型，整理成你自己的私有模型库。", []);
  assert.deepEqual(findings, []);
});
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: fails with module-not-found for `redaction-check.mjs`.

- [ ] **Step 3: Implement the redaction scanner**

Create `videos/makerhub-intro/scripts/redaction-check.mjs`:

```js
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");

const scannedExtensions = new Set([".json", ".md", ".html", ".css", ".js", ".mjs", ".txt", ".svg"]);
const ignoredDirs = new Set(["node_modules", "output", ".auth"]);

export function buildSecretPatterns(env = process.env) {
  const patterns = [];
  for (const name of ["MAKERHUB_VIDEO_BASE_URL", "MAKERHUB_VIDEO_USERNAME", "MAKERHUB_VIDEO_PASSWORD"]) {
    const value = String(env[name] || "").trim();
    if (value) {
      patterns.push({ label: name, value });
    }
  }
  String(env.MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((value, index) => {
      patterns.push({ label: `MAKERHUB_VIDEO_EXTRA_SECRET_PATTERNS[${index}]`, value });
    });
  return patterns;
}

export function scanTextForLeaks(text, secretPatterns) {
  const findings = [];
  for (const pattern of secretPatterns) {
    if (pattern.value && text.includes(pattern.value)) {
      findings.push({ label: pattern.label });
    }
  }

  const tokenPatterns = [
    /\b(?:mh_|mk_|mw_)[A-Za-z0-9_-]{24,}\b/g,
    /\b[A-Za-z0-9_-]{36,}\.[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{24,}\b/g,
    /\bshare[_-]?(?:code|access)?["':=\s]+[A-Za-z0-9_-]{24,}\b/gi,
  ];
  for (const regex of tokenPatterns) {
    if (regex.test(text)) {
      findings.push({ label: "token-like-string" });
      break;
    }
  }
  return findings;
}

async function collectFiles(dir) {
  const entries = await fs.readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (entry.name.startsWith(".") && entry.name !== ".env.example") continue;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!ignoredDirs.has(entry.name)) {
        files.push(...await collectFiles(fullPath));
      }
    } else if (scannedExtensions.has(path.extname(entry.name))) {
      files.push(fullPath);
    }
  }
  return files;
}

async function main() {
  const secretPatterns = buildSecretPatterns();
  const files = await collectFiles(rootDir);
  const findings = [];
  for (const file of files) {
    const text = await fs.readFile(file, "utf8");
    const fileFindings = scanTextForLeaks(text, secretPatterns);
    for (const finding of fileFindings) {
      findings.push({
        file: path.relative(rootDir, file),
        label: finding.label,
      });
    }
  }

  if (findings.length) {
    console.error("Potential sensitive video data found:");
    for (const finding of findings) {
      console.error(`- ${finding.file}: ${finding.label}`);
    }
    process.exitCode = 1;
    return;
  }
  console.log(`Redaction check passed for ${files.length} files.`);
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await main();
}
```

- [ ] **Step 4: Run scanner tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: all tests pass.

- [ ] **Step 5: Run scanner against clean committed files**

Run:

```bash
npm --prefix videos/makerhub-intro run redaction:check
```

Expected: passes. If real environment variables are set, it must still pass because committed files do not include those values.

- [ ] **Step 6: Commit the scanner**

Run:

```bash
git add videos/makerhub-intro/scripts/redaction-check.mjs videos/makerhub-intro/scripts/redaction-check.test.mjs
git commit -m "test: add intro video redaction scanner"
```

---

### Task 5: Add Placeholder Assets For Safe Local Composition

**Files:**
- Create: `videos/makerhub-intro/scripts/ensure-placeholders.mjs`

- [ ] **Step 1: Create placeholder asset generator**

Create `videos/makerhub-intro/scripts/ensure-placeholders.mjs`:

```js
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { captureTargets, safeCaptureFilename } from "./capture-plan.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const placeholderDir = path.join(rootDir, "assets", "placeholders");
const capturedDir = path.join(rootDir, "assets", "captured");

function svgForTarget(target) {
  const colors = ["#13251a", "#1f3427", "#263f2e", "#1a2c22"];
  const bg = colors[(target.order - 1) % colors.length];
  return `<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900">
  <rect width="1600" height="900" fill="${bg}"/>
  <rect x="56" y="52" width="1488" height="94" rx="18" fill="#1f2a22" stroke="#395443"/>
  <text x="88" y="112" font-family="Arial, sans-serif" font-size="42" font-weight="700" fill="#dce8de">MakerHub</text>
  <text x="1320" y="108" font-family="Arial, sans-serif" font-size="24" fill="#8faf9a">${String(target.order).padStart(2, "0")}</text>
  <rect x="70" y="190" width="430" height="270" rx="16" fill="#25362b" stroke="#486651"/>
  <rect x="530" y="190" width="430" height="270" rx="16" fill="#203125" stroke="#486651"/>
  <rect x="990" y="190" width="430" height="270" rx="16" fill="#273a2d" stroke="#486651"/>
  <rect x="70" y="500" width="1350" height="64" rx="10" fill="#172019" stroke="#314536"/>
  <rect x="70" y="590" width="780" height="190" rx="14" fill="#1d2b22" stroke="#314536"/>
  <rect x="890" y="590" width="530" height="190" rx="14" fill="#1d2b22" stroke="#314536"/>
  <text x="92" y="670" font-family="Arial, sans-serif" font-size="60" font-weight="700" fill="#f2f7f0">${target.id}</text>
  <text x="94" y="724" font-family="Arial, sans-serif" font-size="28" fill="#a8b9aa">placeholder asset for local HyperFrames lint</text>
  <circle cx="1340" cy="685" r="54" fill="#65b978"/>
</svg>
`;
}

async function main() {
  await fs.mkdir(placeholderDir, { recursive: true });
  await fs.mkdir(capturedDir, { recursive: true });
  for (const target of captureTargets) {
    const filename = safeCaptureFilename(target.id);
    await fs.writeFile(path.join(placeholderDir, filename.replace(/\.png$/, ".svg")), svgForTarget(target), "utf8");
  }
  console.log(`Prepared ${captureTargets.length} placeholder assets.`);
}

await main();
```

- [ ] **Step 2: Run placeholder generation**

Run:

```bash
npm --prefix videos/makerhub-intro run placeholders
```

Expected: creates ignored SVG files under `videos/makerhub-intro/assets/placeholders/`.

- [ ] **Step 3: Verify placeholders are ignored**

Run:

```bash
git status --short videos/makerhub-intro/assets/placeholders
```

Expected: no output, because placeholders are generated artifacts.

- [ ] **Step 4: Run existing tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: all tests pass.

- [ ] **Step 5: Commit the placeholder generator**

Run:

```bash
git add videos/makerhub-intro/scripts/ensure-placeholders.mjs
git commit -m "chore: add intro video placeholder assets"
```

---

### Task 6: Add HyperFrames Compositions

**Files:**
- Create: `videos/makerhub-intro/compositions/styles.css`
- Create: `videos/makerhub-intro/compositions/intro-16x9.html`
- Create: `videos/makerhub-intro/compositions/intro-9x16.html`

- [ ] **Step 1: Add shared composition styles**

Create `videos/makerhub-intro/compositions/styles.css`:

```css
* {
  box-sizing: border-box;
}

html,
body {
  margin: 0;
  overflow: hidden;
  background: #111412;
  font-family: "Inter", "Noto Sans SC", "Microsoft YaHei", Arial, sans-serif;
  color: #edf5ef;
}

.composition {
  position: relative;
  overflow: hidden;
  background:
    linear-gradient(180deg, rgba(17, 20, 18, 0.98), rgba(12, 15, 13, 0.96)),
    #111412;
}

.stage {
  position: absolute;
  inset: 0;
}

.shot {
  position: absolute;
  inset: 0;
  overflow: hidden;
  background: #111412;
}

.shot img {
  position: absolute;
  width: 100%;
  height: 100%;
  object-fit: cover;
  filter: saturate(1.02) contrast(1.04);
}

.shot--vertical img {
  width: 150%;
  left: -25%;
  object-position: center center;
}

.shade {
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(12, 15, 13, 0.78), rgba(12, 15, 13, 0.08) 44%, rgba(12, 15, 13, 0.56)),
    linear-gradient(0deg, rgba(12, 15, 13, 0.84), transparent 34%);
  pointer-events: none;
}

.title-block {
  position: absolute;
  left: 72px;
  bottom: 126px;
  max-width: 720px;
}

.title-block--vertical {
  left: 64px;
  right: 64px;
  top: 84px;
  bottom: auto;
  max-width: none;
}

.eyebrow {
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 14px;
  border: 1px solid rgba(101, 185, 120, 0.48);
  border-radius: 999px;
  color: #a9d7b4;
  background: rgba(35, 56, 41, 0.82);
  font-size: 18px;
  font-weight: 700;
}

.title {
  margin: 18px 0 0;
  font-size: 68px;
  line-height: 1.04;
  font-weight: 800;
  letter-spacing: 0;
}

.title--vertical {
  font-size: 58px;
}

.caption {
  position: absolute;
  left: 72px;
  right: 72px;
  bottom: 46px;
  min-height: 54px;
  display: flex;
  align-items: center;
  padding: 0 24px;
  border-left: 4px solid #65b978;
  background: rgba(17, 24, 19, 0.88);
  color: #e3eee5;
  font-size: 30px;
  line-height: 1.35;
}

.caption--vertical {
  left: 54px;
  right: 54px;
  bottom: 72px;
  min-height: 120px;
  font-size: 31px;
  align-items: flex-start;
  padding: 22px 24px;
}

.highlight {
  position: absolute;
  border: 4px solid rgba(101, 185, 120, 0.92);
  border-radius: 16px;
  box-shadow: 0 0 0 999px rgba(8, 11, 9, 0.18), 0 0 44px rgba(101, 185, 120, 0.26);
}

.highlight--a {
  left: 58%;
  top: 16%;
  width: 29%;
  height: 42%;
}

.highlight--b {
  left: 10%;
  top: 18%;
  width: 42%;
  height: 24%;
}

.progress-line {
  position: absolute;
  left: 72px;
  right: 72px;
  bottom: 26px;
  height: 4px;
  background: rgba(255, 255, 255, 0.12);
}

.progress-line span {
  display: block;
  height: 100%;
  background: #65b978;
}
```

- [ ] **Step 2: Add the 16:9 HyperFrames composition**

Create `videos/makerhub-intro/compositions/intro-16x9.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=1920, height=1080">
    <link rel="stylesheet" href="./styles.css">
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  </head>
  <body>
    <div id="root" class="composition" data-composition-id="intro-16x9" data-start="0" data-duration="45" data-width="1920" data-height="1080">
      <div id="stage" class="stage"></div>
    </div>
    <script type="module">
      import { storyboardSegments, VIDEO_DURATION_SECONDS } from "../scripts/storyboard.mjs";

      const capturedPrefix = "../assets/captured/";
      const placeholderPrefix = "../assets/placeholders/";
      const assetName = (segment, index) => `${String(index + 1).padStart(2, "0")}-${segment.id}`;

      const stage = document.querySelector("#stage");
      storyboardSegments.forEach((segment, index) => {
        const name = assetName(segment, index);
        const shot = document.createElement("section");
        shot.className = "shot clip";
        shot.dataset.start = String(segment.start);
        shot.dataset.duration = String(segment.duration);
        shot.dataset.trackIndex = "0";
        shot.innerHTML = `
          <picture>
            <source srcset="${capturedPrefix}${name}.png">
            <img src="${placeholderPrefix}${name}.svg" alt="">
          </picture>
          <div class="shade"></div>
          <div class="highlight ${index % 2 ? "highlight--b" : "highlight--a"}"></div>
          <div class="title-block">
            <span class="eyebrow">MakerHub</span>
            <h1 class="title">${segment.title}</h1>
          </div>
          <div class="caption">${segment.voiceover}</div>
          <div class="progress-line"><span style="width: ${((segment.start + segment.duration) / VIDEO_DURATION_SECONDS) * 100}%"></span></div>
        `;
        stage.appendChild(shot);
      });

      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      storyboardSegments.forEach((segment, index) => {
        const shot = stage.children[index];
        const img = shot.querySelector("img");
        const title = shot.querySelector(".title-block");
        const caption = shot.querySelector(".caption");
        const highlight = shot.querySelector(".highlight");
        tl.fromTo(shot, { opacity: 0 }, { opacity: 1, duration: 0.16 }, segment.start);
        tl.fromTo(img, { scale: 1.05, xPercent: index % 2 ? -1.8 : 1.8 }, { scale: 1.12, xPercent: index % 2 ? 1.6 : -1.6, duration: segment.duration, ease: "none" }, segment.start);
        tl.fromTo(title, { opacity: 0, y: 22 }, { opacity: 1, y: 0, duration: 0.36, ease: "power2.out" }, segment.start + 0.16);
        tl.fromTo(caption, { opacity: 0, y: 18 }, { opacity: 1, y: 0, duration: 0.28, ease: "power2.out" }, segment.start + 0.36);
        tl.fromTo(highlight, { opacity: 0, scale: 0.98 }, { opacity: 1, scale: 1, duration: 0.32, ease: "power2.out" }, segment.start + 0.58);
        tl.to(shot, { opacity: 0, duration: 0.18 }, segment.start + segment.duration - 0.18);
      });
      window.__timelines["intro-16x9"] = tl;
    </script>
  </body>
</html>
```

- [ ] **Step 3: Add the 9:16 HyperFrames composition**

Create `videos/makerhub-intro/compositions/intro-9x16.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=1080, height=1920">
    <link rel="stylesheet" href="./styles.css">
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
  </head>
  <body>
    <div id="root" class="composition" data-composition-id="intro-9x16" data-start="0" data-duration="45" data-width="1080" data-height="1920">
      <div id="stage" class="stage"></div>
    </div>
    <script type="module">
      import { storyboardSegments, VIDEO_DURATION_SECONDS } from "../scripts/storyboard.mjs";

      const capturedPrefix = "../assets/captured/";
      const placeholderPrefix = "../assets/placeholders/";
      const assetName = (segment, index) => `${String(index + 1).padStart(2, "0")}-${segment.id}`;

      const stage = document.querySelector("#stage");
      storyboardSegments.forEach((segment, index) => {
        const name = assetName(segment, index);
        const shot = document.createElement("section");
        shot.className = "shot shot--vertical clip";
        shot.dataset.start = String(segment.start);
        shot.dataset.duration = String(segment.duration);
        shot.dataset.trackIndex = "0";
        shot.innerHTML = `
          <picture>
            <source srcset="${capturedPrefix}${name}.png">
            <img src="${placeholderPrefix}${name}.svg" alt="">
          </picture>
          <div class="shade"></div>
          <div class="highlight highlight--b"></div>
          <div class="title-block title-block--vertical">
            <span class="eyebrow">MakerHub</span>
            <h1 class="title title--vertical">${segment.title}</h1>
          </div>
          <div class="caption caption--vertical">${segment.voiceover}</div>
          <div class="progress-line"><span style="width: ${((segment.start + segment.duration) / VIDEO_DURATION_SECONDS) * 100}%"></span></div>
        `;
        stage.appendChild(shot);
      });

      window.__timelines = window.__timelines || {};
      const tl = gsap.timeline({ paused: true });
      storyboardSegments.forEach((segment, index) => {
        const shot = stage.children[index];
        const img = shot.querySelector("img");
        const title = shot.querySelector(".title-block");
        const caption = shot.querySelector(".caption");
        tl.fromTo(shot, { opacity: 0 }, { opacity: 1, duration: 0.16 }, segment.start);
        tl.fromTo(img, { scale: 1.08, yPercent: index % 2 ? -1 : 1 }, { scale: 1.17, yPercent: index % 2 ? 1 : -1, duration: segment.duration, ease: "none" }, segment.start);
        tl.fromTo(title, { opacity: 0, y: -22 }, { opacity: 1, y: 0, duration: 0.36, ease: "power2.out" }, segment.start + 0.16);
        tl.fromTo(caption, { opacity: 0, y: 22 }, { opacity: 1, y: 0, duration: 0.28, ease: "power2.out" }, segment.start + 0.36);
        tl.to(shot, { opacity: 0, duration: 0.18 }, segment.start + segment.duration - 0.18);
      });
      window.__timelines["intro-9x16"] = tl;
    </script>
  </body>
</html>
```

- [ ] **Step 4: Prepare placeholders and lint compositions**

Run:

```bash
npm --prefix videos/makerhub-intro run lint:hyperframes
```

Expected: HyperFrames lint succeeds for the composition directory.

- [ ] **Step 5: Run video workflow tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: all Node tests pass.

- [ ] **Step 6: Commit the compositions**

Run:

```bash
git add videos/makerhub-intro/compositions/styles.css videos/makerhub-intro/compositions/intro-16x9.html videos/makerhub-intro/compositions/intro-9x16.html
git commit -m "feat: add makerhub intro hyperframes compositions"
```

---

### Task 7: Add Render Script And Workflow Documentation

**Files:**
- Create: `videos/makerhub-intro/scripts/render.mjs`
- Create: `videos/makerhub-intro/README.md`

- [ ] **Step 1: Add the render orchestrator**

Create `videos/makerhub-intro/scripts/render.mjs`:

```js
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const outputDir = path.join(rootDir, "output");

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: rootDir,
      stdio: "inherit",
      shell: process.platform === "win32",
      ...options,
    });
    child.on("error", reject);
    child.on("exit", (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`${command} ${args.join(" ")} exited with ${code}`));
      }
    });
  });
}

function selectedOutputs() {
  const onlyArg = process.argv.find((arg) => arg.startsWith("--only="));
  const only = onlyArg ? onlyArg.slice("--only=".length) : "";
  const outputs = {
    "16x9": {
      input: "compositions/intro-16x9.html",
      output: "output/makerhub-intro-16x9.mp4",
    },
    "9x16": {
      input: "compositions/intro-9x16.html",
      output: "output/makerhub-intro-9x16.mp4",
    },
  };
  if (only) {
    if (!outputs[only]) {
      throw new Error(`Unknown render target: ${only}`);
    }
    return [outputs[only]];
  }
  return Object.values(outputs);
}

async function main() {
  await fs.mkdir(outputDir, { recursive: true });
  await run("node", ["scripts/ensure-placeholders.mjs"]);
  await run("node", ["scripts/redaction-check.mjs"]);
  await run("npx", ["hyperframes", "lint", "compositions"]);
  for (const item of selectedOutputs()) {
    await run("npx", [
      "hyperframes",
      "render",
      item.input,
      "-o",
      item.output,
      "-f",
      "30",
      "-q",
      "standard",
      "-w",
      "4",
    ]);
  }
}

await main();
```

- [ ] **Step 2: Add workflow documentation**

Create `videos/makerhub-intro/README.md`:

```markdown
# MakerHub Intro Video Workflow

This directory contains the repeatable workflow for the 45-second MakerHub intro video.

## Setup

Requirements:

- Node.js 22 or newer.
- FFmpeg available on `PATH`.
- Network access to the target MakerHub instance.

Install dependencies:

```bash
npm install
```

Install Playwright browser binaries if the local machine does not already have them:

```bash
npx playwright install chromium
```

## Runtime Secrets

Do not commit `.env` or real values. Export variables in the shell:

```bash
read -r -p "MakerHub URL: " MAKERHUB_VIDEO_BASE_URL
read -r -p "MakerHub username: " MAKERHUB_VIDEO_USERNAME
read -r -s -p "MakerHub password: " MAKERHUB_VIDEO_PASSWORD
echo
export MAKERHUB_VIDEO_BASE_URL MAKERHUB_VIDEO_USERNAME MAKERHUB_VIDEO_PASSWORD
```

The capture and redaction scripts treat these values as sensitive. The video must not show the browser address bar, full URL, login credentials, cookies, tokens, share codes, public sharing address, proxy values, or server paths.

## Capture

```bash
npm run capture
```

Captured images are written to `assets/captured/` and ignored by git.

## Preview

```bash
npm run preview
```

Open the HyperFrames preview URL printed by the CLI.

## Render

Render both versions:

```bash
npm run render
```

Render one version:

```bash
npm run render:16x9
npm run render:9x16
```

Outputs are written to `output/` and ignored by git.

## Verification Checklist

- No frame contains browser chrome or an address bar.
- `npm run redaction:check` passes with the runtime environment variables set.
- 16:9 and 9:16 subtitles do not cover key UI.
- The vertical version uses focused UI crops rather than a blind center crop.
- Sharing footage does not include a generated share code.
- Voiceover text can be replaced without changing storyboard timing.
```

- [ ] **Step 3: Run render-script redaction check**

Run:

```bash
npm --prefix videos/makerhub-intro run redaction:check
```

Expected: passes.

- [ ] **Step 4: Run HyperFrames lint through the package script**

Run:

```bash
npm --prefix videos/makerhub-intro run lint:hyperframes
```

Expected: passes.

- [ ] **Step 5: Render a draft 16:9 video**

Run:

```bash
npm --prefix videos/makerhub-intro run render:16x9
```

Expected: creates `videos/makerhub-intro/output/makerhub-intro-16x9.mp4`. If FFmpeg is missing, install FFmpeg and rerun this step.

- [ ] **Step 6: Confirm generated output is ignored**

Run:

```bash
git status --short videos/makerhub-intro/output
```

Expected: no output.

- [ ] **Step 7: Commit render tooling and docs**

Run:

```bash
git add videos/makerhub-intro/scripts/render.mjs videos/makerhub-intro/README.md
git commit -m "docs: add intro video render workflow"
```

---

### Task 8: Final Verification With Real Capture

**Files:**
- No committed file changes expected unless a previous task needs a focused fix.

- [ ] **Step 1: Run all video workflow tests**

Run:

```bash
npm --prefix videos/makerhub-intro test
```

Expected: all tests pass.

- [ ] **Step 2: Run redaction check with real environment variables set**

Run:

```bash
MAKERHUB_VIDEO_BASE_URL="$MAKERHUB_VIDEO_BASE_URL" \
MAKERHUB_VIDEO_USERNAME="$MAKERHUB_VIDEO_USERNAME" \
MAKERHUB_VIDEO_PASSWORD="$MAKERHUB_VIDEO_PASSWORD" \
npm --prefix videos/makerhub-intro run redaction:check
```

Expected: passes. If it fails, remove the leaked content from committed/generated text files before continuing.

- [ ] **Step 3: Capture real online assets**

Run:

```bash
MAKERHUB_VIDEO_BASE_URL="$MAKERHUB_VIDEO_BASE_URL" \
MAKERHUB_VIDEO_USERNAME="$MAKERHUB_VIDEO_USERNAME" \
MAKERHUB_VIDEO_PASSWORD="$MAKERHUB_VIDEO_PASSWORD" \
npm --prefix videos/makerhub-intro run capture
```

Expected: captures nine ignored images and an ignored manifest under `videos/makerhub-intro/assets/captured/`.

- [ ] **Step 4: Render both videos**

Run:

```bash
npm --prefix videos/makerhub-intro run render
```

Expected:

- `videos/makerhub-intro/output/makerhub-intro-16x9.mp4`
- `videos/makerhub-intro/output/makerhub-intro-9x16.mp4`

- [ ] **Step 5: Inspect frames for browser chrome and sensitive data**

Run:

```bash
ffmpeg -y -i videos/makerhub-intro/output/makerhub-intro-16x9.mp4 -vf fps=1 videos/makerhub-intro/output/check-16x9-%02d.png
ffmpeg -y -i videos/makerhub-intro/output/makerhub-intro-9x16.mp4 -vf fps=1 videos/makerhub-intro/output/check-9x16-%02d.png
```

Expected: extracted frames are created under ignored `output/`.

Manually inspect representative frames. Confirm:

- No browser address bar is visible.
- No online URL is visible.
- No account credential, Token, Cookie, proxy value, share code, or server path is visible.
- Model library, dashboard, online account sync, subscriptions, source refresh, source deletion, local upload, sharing, and verification retry all appear.
- Subtitles do not cover the highlighted UI.

- [ ] **Step 6: Verify git does not contain generated assets**

Run:

```bash
git status --short
```

Expected: no captured images, output MP4s, extracted frame PNGs, `.env`, `.auth`, or `node_modules` appear. Only deliberate source changes should be listed.

- [ ] **Step 7: Final commit if any focused fixes were needed**

If implementation fixes were required during final verification, stage only those files:

```bash
git add videos/makerhub-intro/scripts videos/makerhub-intro/compositions videos/makerhub-intro/README.md videos/makerhub-intro/package.json videos/makerhub-intro/package-lock.json .gitignore
git commit -m "fix: stabilize intro video workflow"
```

Expected: commit contains only workflow source files, not generated media or secrets.

---

## Self-Review

- Spec coverage: The plan covers real online capture, app-only screenshots, 45-second storyboard, 16:9 and 9:16 compositions, TTS-ready captions, generated artifact layout, privacy redaction, and final frame inspection.
- Scope check: The plan is focused on the video workflow only. It does not modify MakerHub product behavior.
- Placeholder scan: The plan uses no placeholder markers or unspecified implementation steps. Optional runtime values are documented in `.env.example` without real secrets.
- Type consistency: Shared ids come from `storyboardSegments` and are reused by `captureTargets`, placeholder assets, and composition asset names.
