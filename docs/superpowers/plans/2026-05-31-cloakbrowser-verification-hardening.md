# CloakBrowser Verification Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden MakerHub's existing CloakBrowser-based 3MF verification flow so Cloudflare/MakerWorld verification surfaces are cropped cleanly, session milestones are diagnosable, and sensitive proof/cookie data remains hidden.

**Architecture:** Keep `BrowserVerificationRuntime` as the only backend coordinator. Add focused helpers inside `app/services/browser_verification.py` for verification-surface clipping and redacted milestone logging, then cover them with unit tests in `tests/test_browser_verification.py`. The frontend popup is already chrome-free, so this plan does not change frontend files unless tests reveal a regression.

**Tech Stack:** Python 3, unittest/pytest, CloakBrowser persistent Chromium contexts, existing MakerHub JSON state, existing business log service.

---

## File Structure

- Modify: `app/services/browser_verification.py`
  - Extend `_verification_clip()` with Cloudflare Turnstile selectors and text fallback.
  - Add `_browser_verification_log()` to centralize low-volume session milestone logs and redact sensitive values.
  - Add milestone calls in `_run_session()` and `_capture_request()` without logging every screenshot or input command.
- Modify: `tests/test_browser_verification.py`
  - Add tests for Cloudflare clipping, clip preference, coordinate preservation, milestone logging, and sensitive-value redaction.
- No planned frontend changes.
- No planned database or state schema changes.

## Task 1: Cloudflare Verification Clipping

**Files:**
- Modify: `app/services/browser_verification.py`
- Test: `tests/test_browser_verification.py`

- [ ] **Step 1: Write failing tests for Turnstile clipping**

Add these tests to `BrowserVerificationSessionTest` after `test_try_trigger_download_flow_clicks_visible_download_control`:

```python
    def test_verification_clip_detects_cloudflare_turnstile_widget(self):
        class FakePage:
            def evaluate(self, _script):
                return {"x": 37, "y": 681, "width": 376, "height": 148}

        runtime = browser_verification_module.BrowserVerificationRuntime()

        self.assertEqual(
            runtime._verification_clip(FakePage()),
            {"x": 37, "y": 681, "width": 376, "height": 148},
        )

    def test_verification_clip_rejects_invalid_candidate_shape(self):
        class FakePage:
            def evaluate(self, _script):
                return {"x": 10, "y": 20, "width": 0, "height": 0}

        runtime = browser_verification_module.BrowserVerificationRuntime()

        self.assertEqual(runtime._verification_clip(FakePage()), {})
```

These tests make sure Python-side normalization accepts the clip shape used by the browser script and rejects impossible rectangles.

- [ ] **Step 2: Run tests to verify current behavior**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py::BrowserVerificationSessionTest::test_verification_clip_detects_cloudflare_turnstile_widget tests/test_browser_verification.py::BrowserVerificationSessionTest::test_verification_clip_rejects_invalid_candidate_shape -q
```

Expected: the first test already passes because it only validates Python normalization; the second test currently fails because `_verification_clip()` normalizes zero width/height to `1` instead of rejecting invalid candidates. This is acceptable as the red signal for the implementation.

- [ ] **Step 3: Implement robust browser-side candidate selection and stricter Python validation**

Replace the JavaScript body inside `_verification_clip()` with this version and keep the surrounding `try/except` structure:

```python
            raw_clip = page.evaluate(
                """
                () => {
                  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1024;
                  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 720;
                  const pad = 24;
                  const directSelectors = [
                    '.cf-turnstile',
                    '[data-sitekey]',
                    'iframe[src*="challenges.cloudflare.com"]',
                    '.geetest_panel',
                    '.geetest_box',
                    '.geetest_popup_box',
                    '.geetest_widget',
                    '[class*="geetest"]',
                    '[class*="captcha"]',
                    '[id*="captcha"]',
                    '[class*="verify"]'
                  ];
                  const textNeedles = [
                    'verify you are human',
                    'performing security verification',
                    'security verification',
                    'not a bot'
                  ];
                  function visibleRect(element) {
                    if (!element || !element.getBoundingClientRect) return null;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    if (
                      rect.width < 80 ||
                      rect.height < 40 ||
                      rect.right <= 0 ||
                      rect.bottom <= 0 ||
                      rect.left >= viewportWidth ||
                      rect.top >= viewportHeight ||
                      style.visibility === 'hidden' ||
                      style.display === 'none' ||
                      Number(style.opacity || '1') <= 0.05
                    ) {
                      return null;
                    }
                    return rect;
                  }
                  function paddedClip(rect) {
                    const x = Math.max(0, Math.floor(rect.left - pad));
                    const y = Math.max(0, Math.floor(rect.top - pad));
                    const right = Math.min(viewportWidth, Math.ceil(rect.right + pad));
                    const bottom = Math.min(viewportHeight, Math.ceil(rect.bottom + pad));
                    return { x, y, width: right - x, height: bottom - y };
                  }
                  const candidates = [];
                  for (const selector of directSelectors) {
                    for (const element of document.querySelectorAll(selector)) {
                      const rect = visibleRect(element);
                      if (rect) {
                        candidates.push({ clip: paddedClip(rect), score: rect.width * rect.height });
                      }
                    }
                  }
                  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
                  while (walker.nextNode()) {
                    const element = walker.currentNode;
                    const text = String(element.innerText || element.textContent || '').toLowerCase();
                    if (!text || !textNeedles.some((needle) => text.includes(needle))) continue;
                    const rect = visibleRect(element);
                    if (!rect) continue;
                    candidates.push({ clip: paddedClip(rect), score: rect.width * rect.height + 1000000 });
                  }
                  candidates.sort((a, b) => a.score - b.score);
                  return candidates.length ? candidates[0].clip : null;
                }
                """
            )
```

Then change the Python validation at the end of `_verification_clip()` to reject invalid shapes:

```python
        try:
            x = max(0, int(float(raw_clip.get("x") or 0)))
            y = max(0, int(float(raw_clip.get("y") or 0)))
            width = int(float(raw_clip.get("width") or 0))
            height = int(float(raw_clip.get("height") or 0))
        except (TypeError, ValueError):
            return {}
        if width < 80 or height < 40:
            return {}
        return {"x": x, "y": y, "width": width, "height": height}
```

- [ ] **Step 4: Run focused clipping tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py::BrowserVerificationSessionTest::test_verification_clip_detects_cloudflare_turnstile_widget tests/test_browser_verification.py::BrowserVerificationSessionTest::test_verification_clip_rejects_invalid_candidate_shape tests/test_browser_verification.py::BrowserVerificationSessionTest::test_write_screenshot_crops_verification_box_and_exposes_offset_viewport tests/test_browser_verification.py::BrowserVerificationSessionTest::test_apply_input_offsets_coordinates_for_cropped_viewport -q
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit clipping change**

Run:

```bash
git add app/services/browser_verification.py tests/test_browser_verification.py
git commit -m "Improve browser verification clipping"
```

## Task 2: Redacted Browser Verification Milestone Logs

**Files:**
- Modify: `app/services/browser_verification.py`
- Test: `tests/test_browser_verification.py`

- [ ] **Step 1: Write failing tests for redacted milestone logging**

Add this test to `BrowserVerificationWorkerTest` after `test_runtime_logs_when_worker_accepts_browser_verification_session`:

```python
    def test_browser_verification_log_redacts_sensitive_values(self):
        log_calls = []

        with patch.object(browser_verification_module, "append_business_log", side_effect=lambda *args, **kwargs: log_calls.append((args, kwargs))):
            browser_verification_module._browser_verification_log(
                "browser_verification_proof_captured",
                "已捕获验证结果。",
                session_id="bv_secret",
                platform="cn",
                cookie="cf_clearance=secret-cookie",
                token="secret-token",
                proof="proof-secret",
                headers={
                    "Cookie": "token=secret-token; cf_clearance=secret-cookie",
                    "x-bbl-captcha-result": "proof-secret",
                    "User-Agent": "UnitTest",
                },
            )

        self.assertEqual(log_calls[0][0][:3], ("missing_3mf", "browser_verification_proof_captured", "已捕获验证结果。"))
        serialized = str(log_calls[0])
        self.assertNotIn("secret-cookie", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("proof-secret", serialized)
        self.assertIn("[redacted]", serialized)
        self.assertIn("UnitTest", serialized)
```

This test should fail because `_browser_verification_log()` does not exist yet.

- [ ] **Step 2: Run the failing redaction test**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py::BrowserVerificationWorkerTest::test_browser_verification_log_redacts_sensitive_values -q
```

Expected: FAIL with `AttributeError: module 'app.services.browser_verification' has no attribute '_browser_verification_log'`.

- [ ] **Step 3: Add redaction helpers and centralized milestone logger**

Add these helpers near `SENSITIVE_REQUEST_HEADER_KEYS`:

```python
SENSITIVE_LOG_KEYS = SENSITIVE_REQUEST_HEADER_KEYS | {
    "cf_clearance",
    "__cf_bm",
    "proof",
    "proof_id",
    "browser_verification_proof_id",
}


def _redact_browser_verification_value(key: str, value: Any) -> Any:
    lowered = str(key or "").lower()
    if any(secret_key in lowered for secret_key in SENSITIVE_LOG_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _redact_browser_verification_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_browser_verification_value(lowered, item) for item in value]
    return value


def _browser_verification_log(event: str, message: str, **fields: Any) -> None:
    safe_fields = {
        str(key): _redact_browser_verification_value(str(key), value)
        for key, value in fields.items()
    }
    append_business_log("missing_3mf", event, message, **safe_fields)
```

- [ ] **Step 4: Replace direct browser verification session logs with the helper**

In `poll_once()`, replace the existing `append_business_log(...)` call for `browser_verification_session_worker_started` with:

```python
                    _browser_verification_log(
                        "browser_verification_session_worker_started",
                        "浏览器验证 worker 已接收会话。",
                        session_id=session_id,
                        platform=str(session.get("platform") or ""),
                        model_id=str(target.get("model_id") or ""),
                    )
```

In `_run_session_guarded()`, replace the existing failure `append_business_log(...)` call with:

```python
            _browser_verification_log(
                "browser_verification_session_failed",
                "浏览器验证会话失败。",
                level="warning",
                session_id=session_id,
                error=str(exc),
            )
```

- [ ] **Step 5: Add low-volume milestone logs in `_run_session()`**

Add `_browser_verification_log()` calls at these exact points:

After `context = self._launch_context(session, config)`:

```python
            _browser_verification_log(
                "browser_verification_context_launched",
                "浏览器验证 CloakBrowser 上下文已启动。",
                session_id=session_id,
                platform=platform,
            )
```

After `page.goto(start_url, wait_until="domcontentloaded", timeout=45000)`:

```python
            _browser_verification_log(
                "browser_verification_start_url_loaded",
                "浏览器验证起始页面已加载。",
                session_id=session_id,
                platform=platform,
                start_url=start_url,
            )
```

Inside the `if fell_back_to_web or not _is_f3mf_url(start_url):` block, replace the current bare trigger call:

```python
                try:
                    _try_trigger_download_flow(page)
                except Exception:
                    pass
```

with:

```python
                try:
                    triggered = _try_trigger_download_flow(page)
                except Exception as exc:
                    triggered = False
                    _browser_verification_log(
                        "browser_verification_download_trigger_failed",
                        "浏览器验证自动触发下载失败，等待用户手动操作。",
                        level="warning",
                        session_id=session_id,
                        platform=platform,
                        error=str(exc),
                    )
                else:
                    _browser_verification_log(
                        "browser_verification_download_trigger_attempted",
                        "浏览器验证已尝试自动触发下载。",
                        session_id=session_id,
                        platform=platform,
                        triggered=triggered,
                    )
```

Inside `_capture_request()`, immediately after `captured_proof["id"] = proof_id`, add:

```python
                    _browser_verification_log(
                        "browser_verification_proof_captured",
                        "已捕获浏览器验证结果。",
                        session_id=session_id,
                        platform=platform,
                        proof_id=proof_id,
                    )
```

Immediately after `retry_result = self._retry_after_verification(current, proof_id)`, add:

```python
                    _browser_verification_log(
                        "browser_verification_retry_submitted",
                        "验证完成后已提交缺失 3MF 重试。",
                        session_id=session_id,
                        platform=platform,
                        retry_result=retry_result,
                    )
```

Immediately before `return` after the completed `update_session(...)`, add:

```python
                    _browser_verification_log(
                        "browser_verification_session_completed",
                        "浏览器验证会话已完成。",
                        session_id=session_id,
                        platform=platform,
                    )
```

Immediately after the timeout `self.store.update_session(... status="expired" ...)`, add:

```python
            _browser_verification_log(
                "browser_verification_session_expired",
                "浏览器验证会话已超时。",
                level="warning",
                session_id=session_id,
                platform=platform,
            )
```

Do not add logs inside the screenshot loop for each screenshot and do not log input commands.

- [ ] **Step 6: Run logging tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py::BrowserVerificationWorkerTest::test_browser_verification_log_redacts_sensitive_values tests/test_browser_verification.py::BrowserVerificationWorkerTest::test_runtime_logs_when_worker_accepts_browser_verification_session tests/test_browser_verification.py::BrowserVerificationWorkerTest::test_worker_main_logs_browser_verification_poll_errors_and_continues -q
```

Expected: all 3 tests pass.

- [ ] **Step 7: Commit logging change**

Run:

```bash
git add app/services/browser_verification.py tests/test_browser_verification.py
git commit -m "Add browser verification milestone logs"
```

## Task 3: Full Verification And Documentation Check

**Files:**
- Modify only if tests reveal a missing note: `README.md`
- Test: `tests/test_browser_verification.py`, `tests/test_browser_verification_api.py`

- [ ] **Step 1: Run browser verification backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_browser_verification.py tests/test_browser_verification_api.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git diff --stat HEAD~2..HEAD
git diff -- app/services/browser_verification.py tests/test_browser_verification.py
```

Expected: only focused browser verification clipping/logging changes are present. No frontend, source refresh, account login, or concurrency changes should appear.

- [ ] **Step 4: Decide whether docs need an implementation note**

Open `README.md` release notes only if the user has asked to push in the current turn. If the user has not asked to push, do not change version or README release notes. This follows the current project rule from the user: version/release notes are updated once at push time.

- [ ] **Step 5: Commit documentation only if changed**

If Step 4 changes docs, run:

```bash
git add README.md VERSION
git commit -m "Document browser verification hardening release"
```

Expected: commit created only when release docs/version were intentionally changed for push.

## Self-Review Checklist

- Spec coverage:
  - CloakBrowser persistent context remains unchanged.
  - Platform profile reuse remains unchanged.
  - Cloudflare/MakerWorld verification clipping is implemented in Task 1.
  - Chrome-free popup remains unchanged because no frontend code is touched.
  - Proof capture and retry remain unchanged, with Task 2 adding milestone logs around them.
  - Sensitive Cookie/token/proof redaction is implemented in Task 2.
- Placeholder scan:
  - No unresolved placeholder markers or vague catch-all implementation steps are used.
- Type consistency:
  - New helper names are `_redact_browser_verification_value()` and `_browser_verification_log()`.
  - Existing names `_verification_clip()`, `_write_screenshot()`, `_apply_input()`, `_try_trigger_download_flow()`, and `_retry_after_verification()` remain unchanged.
