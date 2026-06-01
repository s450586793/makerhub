# Manual MakerWorld Verification Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove MakerHub's built-in MakerWorld verification popup and replace all verification actions with external MakerWorld navigation.

**Architecture:** Keep the existing source-health and missing-3MF state classification, but change the action layer so verification states produce external links instead of browser-verification sessions. Delete the browser verification runtime, API, route, frontend page, and CloakBrowser install path while preserving Chromium/Scrapling because local preview rendering and source fetching still use them.

**Tech Stack:** Vue 3, Vite, Node test runner, FastAPI, Python unittest/pytest, Docker.

---

## File Map

- Modify `app/services/source_health.py`: verification status cards should use `url` + `action_label: "访问主页"`.
- Modify `frontend/src/lib/dashboardStatus.js`: remove browser-verification action classification.
- Modify `frontend/src/lib/dashboardStatus.test.mjs`: assert verification cards are external links.
- Modify `frontend/src/pages/DashboardPage.vue`: remove browser verification imports, state, helper functions, and button branch.
- Modify `frontend/src/pages/TasksPage.vue`: replace `去验证` session creation with external MakerWorld/model-page links.
- Modify `frontend/src/router.js`: remove standalone browser verification route/body class.
- Delete `frontend/src/pages/BrowserVerificationPage.vue`.
- Delete `frontend/src/lib/browserVerificationWindow.js`.
- Delete `frontend/src/lib/browserVerificationInput.js` if it exists.
- Delete browser-verification-only frontend tests: `frontend/src/lib/browserVerificationWindow.test.mjs`; rewrite `frontend/src/lib/routerShape.test.mjs` to assert the route is gone.
- Modify `app/api/config.py`: remove browser verification schemas/imports/store and `/api/browser-verification/*` endpoints.
- Modify `app/api/web.py`: remove `/browser-verification/{session_id}` SPA route.
- Modify `app/main.py`: remove browser verification cache allowlist entries.
- Modify `app/worker.py`: remove `BrowserVerificationRuntime` construction and polling.
- Modify `app/services/archive_worker.py`: remove proof import, retry proof argument, proof metadata injection, and proof consumption.
- Delete `app/services/browser_verification.py`.
- Modify `app/schemas/models.py`: remove browser verification request schemas.
- Delete or rewrite tests whose only covered behavior is removed: `tests/test_browser_verification.py`, `tests/test_browser_verification_api.py`, and `tests/test_web_routes.py`.
- Modify `requirements.txt`: remove `cloakbrowser==0.3.31`.
- Modify `Dockerfile`: remove CloakBrowser binary install line; keep `chromium`, Chromium libraries, and `RUN scrapling install`.
- Modify `VERSION` and `README.md`: bump `0.8.16` to `0.8.17` and add the latest release note.

## Task 1: Dashboard Source Cards Use Manual Verification Links

**Files:**
- Modify: `app/services/source_health.py`
- Modify: `tests/test_source_health.py`
- Modify: `frontend/src/lib/dashboardStatus.js`
- Modify: `frontend/src/lib/dashboardStatus.test.mjs`

- [ ] **Step 1: Write/update tests for dashboard manual verification**

In `tests/test_source_health.py`, add this test to `SourceHealthCardsTest` after `test_missing_3mf_verification_is_softened_when_probe_ok`:

```python
    def test_source_health_verification_card_opens_platform_homepage(self):
        original_probe = source_health._probe_platform_status
        source_health._probe_platform_status = lambda platform, *_args, **_kwargs: {
            "platform": platform,
            "state": "verification_required" if platform == "cn" else "ok",
            "status": "需要验证" if platform == "cn" else "连接正常",
            "detail": "需要完成 MakerWorld 验证。" if platform == "cn" else "",
        }

        class Config:
            cookies = []
            proxy = None

        try:
            cards = source_health.build_source_health_cards(Config(), [])
        finally:
            source_health._probe_platform_status = original_probe

        card_map = {item["key"]: item for item in cards}
        self.assertEqual(card_map["cn"]["state"], "verification_required")
        self.assertEqual(card_map["cn"].get("url"), "https://makerworld.com.cn")
        self.assertEqual(card_map["cn"].get("action_label"), "访问主页")
        self.assertNotIn("route", card_map["cn"])
```

In `frontend/src/lib/dashboardStatus.test.mjs`, replace the imports and verification test with:

```javascript
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  dashboardStatusAction,
  dashboardStatusElementKind,
} from "./dashboardStatus.js";
```

```javascript
test("verification status card opens the platform homepage", () => {
  const card = {
    key: "global",
    action_label: "访问主页",
    url: "https://makerworld.com",
    checks: [{ state: "verification_required" }],
  };

  assert.equal(dashboardStatusElementKind(card), "div");
  assert.deepEqual(dashboardStatusAction(card), {
    kind: "external",
    label: "访问主页",
    href: "https://makerworld.com",
  });
});
```

Remove the `needsBrowserVerification` assertion from the old test because that function will no longer exist.

- [ ] **Step 2: Run the targeted tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_source_health.py::SourceHealthCardsTest::test_source_health_verification_card_opens_platform_homepage -q
node --test frontend/src/lib/dashboardStatus.test.mjs
```

Expected before implementation:

- Python test fails because `action_label` is still `去验证`.
- Node test fails because `dashboardStatusAction()` still returns `kind: "browser-verification"` for verification cards.

- [ ] **Step 3: Implement dashboard manual verification behavior**

In `app/services/source_health.py`, change the verification branch in `build_source_health_cards()` from:

```python
        if any(item.get("state") in {"verification_required", "cloudflare"} for item in checks):
            card["url"] = PLATFORM_ORIGINS.get(platform, "")
            card["action_label"] = "去验证"
```

to:

```python
        if any(item.get("state") in {"verification_required", "cloudflare", "auth_required"} for item in checks):
            card["url"] = PLATFORM_ORIGINS.get(platform, "")
            card["action_label"] = "访问主页"
```

In `frontend/src/lib/dashboardStatus.js`, replace the file with:

```javascript
export function dashboardStatusElementKind(_item) {
  return "div";
}

export function dashboardStatusAction(item) {
  if (item?.route && item?.action_label) {
    return {
      kind: "route",
      label: item.action_label,
      to: item.route,
    };
  }
  if (item?.url && item?.action_label) {
    return {
      kind: "external",
      label: item.action_label,
      href: item.url,
    };
  }
  return null;
}
```

- [ ] **Step 4: Run the targeted tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_source_health.py::SourceHealthCardsTest::test_source_health_verification_card_opens_platform_homepage -q
node --test frontend/src/lib/dashboardStatus.test.mjs
```

Expected: all targeted tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add app/services/source_health.py tests/test_source_health.py frontend/src/lib/dashboardStatus.js frontend/src/lib/dashboardStatus.test.mjs
git commit -m "fix: open makerworld verification from status cards"
```

## Task 2: Replace Task-Page Verification Popup With External Links

**Files:**
- Modify: `frontend/src/pages/TasksPage.vue`

- [ ] **Step 1: Add a source-level frontend regression test**

Create `frontend/src/lib/tasksManualVerification.test.mjs`:

```javascript
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("../pages/TasksPage.vue", import.meta.url), "utf8");

test("missing 3MF verification action is an external link", () => {
  assert.match(source, /href="missingVerificationHref\(item\)"/);
  assert.match(source, />\s*访问源页面\s*</);
  assert.doesNotMatch(source, /api\/browser-verification\/sessions/);
  assert.doesNotMatch(source, /startBrowserVerification/);
  assert.doesNotMatch(source, /browserVerificationPath/);
  assert.doesNotMatch(source, /browserVerificationWindow/);
});

test("task page can infer a MakerWorld homepage when model URL is missing", () => {
  assert.match(source, /function missingVerificationHref\(item\)/);
  assert.match(source, /https:\/\/makerworld\.com\.cn/);
  assert.match(source, /https:\/\/makerworld\.com/);
});
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
node --test frontend/src/lib/tasksManualVerification.test.mjs
```

Expected before implementation: fails because `TasksPage.vue` still contains `startBrowserVerification()` and `/api/browser-verification/sessions`.

- [ ] **Step 3: Replace the task-page action**

In `frontend/src/pages/TasksPage.vue`, replace the `去验证` `<button>` block with:

```vue
            <a
              v-if="needsManualVerification(item)"
              class="button button-primary button-small"
              :href="missingVerificationHref(item)"
              target="_blank"
              rel="noreferrer noopener"
              @click="missingStatus = '请在 MakerWorld 完成验证后回到 MakerHub 重试。'"
            >
              访问源页面
            </a>
```

Remove `useRouter` from the Vue Router import:

```javascript
import { RouterLink } from "vue-router";
```

Remove this import block:

```javascript
import {
  browserVerificationPath,
  closeBrowserVerificationWindow,
  navigateBrowserVerificationWindow,
  reserveBrowserVerificationWindow,
} from "../lib/browserVerificationWindow";
```

Remove:

```javascript
const router = useRouter();
```

Rename `needsBrowserVerification(item)` to:

```javascript
function needsManualVerification(item) {
  const status = String(item?.status || "").toLowerCase();
  return ["verification_required", "cloudflare", "auth_required"].includes(status);
}
```

Add these helpers near `needsManualVerification()`:

```javascript
function sourceHomepageForMissingItem(item) {
  const source = String(item?.source || "").toLowerCase();
  const url = String(item?.model_url || "");
  if (source === "global" || url.includes("makerworld.com/")) {
    return "https://makerworld.com";
  }
  return "https://makerworld.com.cn";
}

function missingVerificationHref(item) {
  const url = String(item?.model_url || "").trim();
  if (url.startsWith("http://") || url.startsWith("https://")) {
    return url;
  }
  return sourceHomepageForMissingItem(item);
}
```

Delete the entire `startBrowserVerification(item)` function.

- [ ] **Step 4: Run the task-page test**

Run:

```bash
node --test frontend/src/lib/tasksManualVerification.test.mjs
```

Expected: pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add frontend/src/pages/TasksPage.vue frontend/src/lib/tasksManualVerification.test.mjs
git commit -m "fix: use manual makerworld links for missing 3mf verification"
```

## Task 3: Remove Frontend Browser Verification Route And Popup Files

**Files:**
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/router.js`
- Modify: `frontend/src/lib/routerShape.test.mjs`
- Modify: `frontend/src/style.css`
- Delete: `frontend/src/pages/BrowserVerificationPage.vue`
- Delete: `frontend/src/lib/browserVerificationWindow.js`
- Delete: `frontend/src/lib/browserVerificationInput.js`
- Delete: `frontend/src/lib/browserVerificationWindow.test.mjs`

- [ ] **Step 1: Rewrite router shape test**

Replace `frontend/src/lib/routerShape.test.mjs` with:

```javascript
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

test("browser verification route is removed", () => {
  const routerSource = readFileSync(new URL("../router.js", import.meta.url), "utf8");
  const dashboardSource = readFileSync(new URL("../pages/DashboardPage.vue", import.meta.url), "utf8");

  assert.doesNotMatch(routerSource, /browser-verification/);
  assert.doesNotMatch(routerSource, /BrowserVerificationPage/);
  assert.doesNotMatch(routerSource, /browser-verification-page/);
  assert.doesNotMatch(dashboardSource, /browserVerificationWindow/);
  assert.doesNotMatch(dashboardSource, /startBrowserVerificationFromCard/);
  assert.doesNotMatch(dashboardSource, /api\/browser-verification\/sessions/);
});
```

- [ ] **Step 2: Run frontend route tests and verify failure**

Run:

```bash
node --test frontend/src/lib/routerShape.test.mjs frontend/src/lib/browserVerificationWindow.test.mjs
```

Expected before implementation:

- `routerShape.test.mjs` fails because the route still exists.
- `browserVerificationWindow.test.mjs` still passes before deletion, but it will be removed because the helper is no longer part of the product.

- [ ] **Step 3: Remove route and popup files**

In `frontend/src/router.js`:

- remove `browser-verification-page` from `BODY_CLASSES`
- remove the `BrowserVerificationPage` import
- remove the `/browser-verification/:sessionId` route object

The top of the file should begin like:

```javascript
import { createRouter, createWebHistory } from "vue-router";

import AppShell from "./layouts/AppShell.vue";
import { appState, bootstrapApp } from "./lib/appState";


const BODY_CLASSES = ["login-page", "detail-page", "detail-page--makerworld"];
const DashboardPage = () => import("./pages/DashboardPage.vue");
```

In `frontend/src/pages/DashboardPage.vue`, delete the browser-verification button branch:

```vue
            <button
              v-if="dashboardStatusAction(item)?.kind === 'browser-verification'"
              class="dashboard-hero__status-action dashboard-hero__status-action--button"
              type="button"
              :disabled="verifyingPlatform === item.key"
              @click="startBrowserVerificationFromCard(item)"
            >
              {{ verifyingPlatform === item.key ? "创建中..." : dashboardStatusAction(item).label }}
            </button>
```

Then change the following `RouterLink` branch from `v-else-if` to `v-if` so route and external actions still render:

```vue
            <RouterLink
              v-if="dashboardStatusAction(item)?.kind === 'route'"
```

Remove these imports:

```javascript
import {
  browserVerificationPath,
  closeBrowserVerificationWindow,
  navigateBrowserVerificationWindow,
  reserveBrowserVerificationWindow,
} from "../lib/browserVerificationWindow";
```

Delete the `verifyingPlatform` ref, `verificationItemForPlatform()`, `needsMissingVerification()`, `platformFromModelUrl()`, `platformOrigin()`, and `startBrowserVerificationFromCard()` helpers. Keep `useRouter()` only if another dashboard function still uses `router`; otherwise remove it from the import and delete `const router = useRouter();`.

Delete these files:

```bash
rm frontend/src/pages/BrowserVerificationPage.vue
rm frontend/src/lib/browserVerificationWindow.js
rm -f frontend/src/lib/browserVerificationInput.js
rm frontend/src/lib/browserVerificationWindow.test.mjs
```

In `frontend/src/style.css`, remove the browser verification CSS block from `.browser-verification-page` through `.browser-verification-empty strong`, and remove the responsive browser verification rules near the end of the file. Use `rg -n "browser-verification" frontend/src/style.css` after editing; it must return no matches.

- [ ] **Step 4: Run frontend tests**

Run:

```bash
node --test frontend/src/lib/dashboardStatus.test.mjs frontend/src/lib/tasksManualVerification.test.mjs frontend/src/lib/routerShape.test.mjs
rg -n "browser-verification|BrowserVerification|browserVerification" frontend/src
```

Expected:

- Node tests pass.
- `rg` returns no frontend source matches.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add frontend/src/pages/DashboardPage.vue frontend/src/router.js frontend/src/style.css frontend/src/lib/routerShape.test.mjs
git rm frontend/src/pages/BrowserVerificationPage.vue frontend/src/lib/browserVerificationWindow.js frontend/src/lib/browserVerificationWindow.test.mjs
git rm -f frontend/src/lib/browserVerificationInput.js
git commit -m "refactor: remove browser verification frontend"
```

## Task 4: Remove Backend Browser Verification API, Worker, And Proof Flow

**Files:**
- Modify: `app/api/config.py`
- Modify: `app/api/web.py`
- Modify: `app/main.py`
- Modify: `app/worker.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/schemas/models.py`
- Delete: `app/services/browser_verification.py`
- Delete: `tests/test_browser_verification_api.py`
- Delete: `tests/test_browser_verification.py`
- Modify: `tests/test_web_routes.py`
- Add: `tests/test_manual_verification_rollback.py`

- [ ] **Step 1: Add backend regression tests for removal**

Create `tests/test_manual_verification_rollback.py`:

```python
import inspect
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main as main_app
from app.services import archive_worker


class ManualVerificationRollbackTest(unittest.TestCase):
    def test_browser_verification_api_routes_are_removed(self):
        paths = {route.path for route in main_app.app.routes}
        self.assertNotIn("/api/browser-verification/sessions", paths)
        self.assertFalse(any(str(path).startswith("/api/browser-verification") for path in paths))

    def test_browser_verification_spa_route_is_removed(self):
        response = TestClient(main_app.app).get("/browser-verification/bv_test")
        self.assertIn(response.status_code, {302, 404})

    def test_archive_worker_no_longer_consumes_browser_verification_proofs(self):
        source = inspect.getsource(archive_worker)
        self.assertNotIn("consume_browser_verification_proof", source)
        self.assertNotIn("browser_verification_proof_id", source)

    def test_browser_verification_service_file_is_deleted(self):
        self.assertFalse(Path("app/services/browser_verification.py").exists())
```

Replace `tests/test_web_routes.py` with:

```python
import unittest

from fastapi.testclient import TestClient

from app import main as main_app


class RemovedBrowserVerificationWebRouteTest(unittest.TestCase):
    def test_browser_verification_direct_url_is_not_spa_route(self):
        response = TestClient(main_app.app).get("/browser-verification/bv_test", follow_redirects=False)
        self.assertIn(response.status_code, {302, 404})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run backend removal tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_manual_verification_rollback.py tests/test_web_routes.py -q
```

Expected before implementation: failure because the API route, SPA route, service file, and proof handling still exist.

- [ ] **Step 3: Remove API endpoints and schemas**

In `app/api/config.py`:

- remove `BrowserVerificationInputRequest` and `BrowserVerificationSessionRequest` from the `app.schemas.models` import list
- remove `from app.services.browser_verification import browser_verification_store`
- delete all functions from `list_browser_verification_sessions()` through `cancel_browser_verification_session()`

In `app/schemas/models.py`, delete:

```python
class BrowserVerificationSessionRequest(BaseModel):
    model_id: str = ""
    model_url: str = ""
    title: str = ""
    instance_id: str = ""
    api_url: str = ""
    captcha_id: str = ""
    source: str = "cn"


class BrowserVerificationInputRequest(BaseModel):
    type: Literal["click", "mousemove", "mousedown", "mouseup", "wheel", "key", "text"]
    x: int = 0
    y: int = 0
    delta_x: int = 0
    delta_y: int = 0
    key: str = ""
    text: str = ""
```

If `Literal` becomes unused in `app/schemas/models.py`, remove it from the typing import.

- [ ] **Step 4: Remove web route and cache allowlist**

In `app/api/web.py`, delete:

```python
@router.get("/browser-verification/{session_id}", response_class=HTMLResponse)
async def browser_verification(_: Request, session_id: str):
    return _serve_spa()
```

In `app/main.py`:

- remove `"/browser-verification"` from `SPA_SHELL_PATHS`
- remove `or path.startswith("/browser-verification/")` from `_apply_cache_headers()`

- [ ] **Step 5: Remove worker runtime polling**

In `app/worker.py`, delete:

```python
from app.services.browser_verification import BrowserVerificationRuntime
```

Delete the construction:

```python
    browser_verification_runtime = BrowserVerificationRuntime(
        archive_manager=archive_manager,
        json_store=store,
    )
```

Delete this polling block:

```python
            try:
                browser_verification_runtime.poll_once()
            except Exception as exc:
                append_business_log(
                    "missing_3mf",
                    "browser_verification_worker_poll_failed",
                    "浏览器验证 worker 轮询失败。",
                    level="warning",
                    error=str(exc),
                )
```

- [ ] **Step 6: Remove archive proof flow**

In `app/services/archive_worker.py`, delete:

```python
from app.services.browser_verification import consume_browser_verification_proof
```

Change the `retry_missing_3mf()` signature by removing:

```python
        browser_verification_proof_id: str = "",
```

Inside `retry_missing_3mf()`, remove this metadata field:

```python
                "browser_verification_proof_id": str(browser_verification_proof_id or "").strip(),
```

Change `retry_verification_missing_3mf()` signature from:

```python
    def retry_verification_missing_3mf(self, *, platform: str, primary: Optional[dict] = None, proof_id: str = "") -> dict:
```

to:

```python
    def retry_verification_missing_3mf(self, *, platform: str, primary: Optional[dict] = None) -> dict:
```

Remove:

```python
        proof_key = str(proof_id or "").strip()
```

and remove this argument from the `retry_missing_3mf()` call:

```python
                browser_verification_proof_id=proof_key,
```

In the archive task execution code, remove:

```python
        browser_verification_proof_id = str(meta.get("browser_verification_proof_id") or "").strip()
        if browser_verification_proof_id:
            scrubbed_meta = dict(meta)
            scrubbed_meta["browser_verification_proof_id"] = ""
            meta = scrubbed_meta
            self.task_store.update_active_task(task_id, meta=meta)
        three_mf_captcha_result_header = (
            consume_browser_verification_proof(browser_verification_proof_id)
            if browser_verification_proof_id
            else ""
        )
```

Replace it with:

```python
        three_mf_captcha_result_header = ""
```

This keeps the existing `run_archive_model_job(..., three_mf_captcha_result_header=three_mf_captcha_result_header)` call stable without passing deleted proof data.

- [ ] **Step 7: Delete removed backend service/tests**

Run:

```bash
git rm app/services/browser_verification.py tests/test_browser_verification.py tests/test_browser_verification_api.py
```

- [ ] **Step 8: Run backend removal tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_manual_verification_rollback.py tests/test_web_routes.py tests/test_missing_3mf.py -q
rg -n "browser_verification|BrowserVerification|browser-verification|cloakbrowser" app tests
```

Expected:

- Python tests pass.
- `rg` may still find `cf-browser-verification` detector tokens in source health, task state, three_mf, remote_refresh, and scrapling fetch. Those are expected.
- `rg` must not find app imports, API paths, worker names, proof IDs, or `cloakbrowser`.

- [ ] **Step 9: Commit Task 4**

Run:

```bash
git add app/api/config.py app/api/web.py app/main.py app/worker.py app/services/archive_worker.py app/schemas/models.py tests/test_web_routes.py tests/test_manual_verification_rollback.py
git add -u app/services/browser_verification.py tests/test_browser_verification.py tests/test_browser_verification_api.py
git commit -m "refactor: remove browser verification backend"
```

## Task 5: Remove CloakBrowser Image Dependency And Update Release Metadata

**Files:**
- Modify: `requirements.txt`
- Modify: `Dockerfile`
- Modify: `VERSION`
- Modify: `README.md`

- [ ] **Step 1: Update dependency tests by source search**

Run before implementation:

```bash
rg -n "cloakbrowser|from cloakbrowser|ensure_binary" requirements.txt Dockerfile app tests
```

Expected before implementation: finds `requirements.txt`, `Dockerfile`, and the browser verification service if Task 4 has not been completed. After Task 4, it should only find requirements/Dockerfile before this task.

- [ ] **Step 2: Remove CloakBrowser from runtime image**

In `requirements.txt`, delete:

```text
cloakbrowser==0.3.31
```

In `Dockerfile`, delete:

```dockerfile
RUN python -c "from cloakbrowser import ensure_binary; ensure_binary()"
```

Do not delete:

```dockerfile
        chromium \
```

Do not delete:

```dockerfile
RUN scrapling install
```

- [ ] **Step 3: Bump version and README**

Change `VERSION` from:

```text
0.8.16
```

to:

```text
0.8.17
```

In `README.md`, change the current version line from:

```markdown
> 当前版本：`v0.8.16`
```

to:

```markdown
> 当前版本：`v0.8.17`
```

Add a new release entry above `v0.8.16`:

```markdown
### 2026-06-01 · v0.8.17

- 回退 MakerWorld 内置浏览器验证流程：首页和任务页改为外跳官网/模型页，由用户在 MakerWorld 手动完成验证后回到 MakerHub 重试。
- 删除未使用的 CloakBrowser 验证运行时、接口、弹窗页面和镜像预安装步骤，保留本地预览与 Scrapling 抓取仍需使用的 Chromium/Scrapling 组件。
```

If README's visible release list exceeds three direct entries, move older entries into the existing collapsed historical section while keeping only the latest three visible.

- [ ] **Step 4: Verify dependency cleanup**

Run:

```bash
rg -n "cloakbrowser|from cloakbrowser|ensure_binary" requirements.txt Dockerfile app tests
rg -n "scrapling|chromium|puppeteer-core" Dockerfile requirements.txt app/services/local_preview_renderer.mjs app/services/scrapling_fetch.py frontend/package.json
```

Expected:

- First `rg` returns no matches.
- Second `rg` still shows Scrapling, Chromium, local preview, and puppeteer-core usage.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add requirements.txt Dockerfile VERSION README.md
git commit -m "chore: release v0.8.17"
```

## Task 6: Full Verification Pass

**Files:**
- No planned edits unless verification exposes a regression.

- [ ] **Step 1: Run frontend tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all frontend source-level tests pass.

- [ ] **Step 2: Build frontend**

Run:

```bash
cd frontend && npm run build
```

Expected: Vite build completes successfully.

- [ ] **Step 3: Run focused backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_manual_verification_rollback.py tests/test_web_routes.py tests/test_source_health.py tests/test_missing_3mf.py tests/test_archive_worker_batch_retry.py tests/test_resource_limiter.py -q
```

Expected: all selected backend tests pass.

- [ ] **Step 4: Compile impacted backend modules**

Run:

```bash
.venv/bin/python -m py_compile app/api/config.py app/api/web.py app/main.py app/worker.py app/services/archive_worker.py app/services/source_health.py app/services/task_state.py app/schemas/models.py
```

Expected: no syntax errors.

- [ ] **Step 5: Final source search**

Run:

```bash
rg -n "browser_verification|BrowserVerification|browser-verification|browserVerification|cloakbrowser|ensure_binary" app frontend tests Dockerfile requirements.txt
```

Expected: only allowed matches are Cloudflare detector literals such as `cf-browser-verification`. There should be no browser verification route, API, frontend helper, worker runtime, proof ID, or CloakBrowser dependency.

- [ ] **Step 6: Check git status**

Run:

```bash
git status --short --branch
```

Expected: clean working tree on `main`, ahead of origin by the new implementation commits until the user asks to push.

## Self-Review

- Spec coverage: dashboard cards, homepage component cleanup, task-page actions, frontend popup deletion, backend API/runtime deletion, proof-flow removal, dependency cleanup, and release metadata are each assigned to a task.
- Dependency boundary: plan explicitly keeps Chromium, Chromium libraries, `puppeteer-core`, Scrapling, and `RUN scrapling install`.
- Testing coverage: plan covers new manual verification behavior, route/API removal, proof removal, frontend build, targeted backend tests, and final source search.
- Scope: no unrelated Docker image diet or Scrapling behavior changes are included.
