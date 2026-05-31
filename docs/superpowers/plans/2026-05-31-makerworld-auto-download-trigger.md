# MakerWorld Auto Download Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make browser verification prefer the original trigger URL, recover from JSON `403` API pages, and automatically try the MakerWorld `Download 3MF` flow.

**Architecture:** Keep `BrowserVerificationRuntime` as the coordinator. Add small helper functions in `app/services/browser_verification.py` for web/API URL selection, JSON denial detection, fallback navigation, and bounded download-trigger clicks.

**Tech Stack:** Python, Playwright-style page API through `cloakbrowser`, existing unittest/pytest tests in `tests/test_browser_verification.py`.

---

## File Structure

- Modify `app/services/browser_verification.py`
  - `_verification_start_url` should prefer original web trigger URLs and only synthesize API URLs for API triggers or instance-only sessions.
  - Add `_browser_verification_fallback_url`, `_looks_like_api_denial_text`, `_page_looks_like_api_denial`, and `_try_trigger_download_flow`.
  - Call the fallback and trigger helpers after the initial `page.goto`.
- Modify `tests/test_browser_verification.py`
  - Add tests for original web trigger selection.
  - Add tests for JSON `403` denial detection.
  - Add tests for fallback navigation and bounded automatic download click.

## Task 1: Start URL and API Denial Helpers

**Files:**
- Modify: `tests/test_browser_verification.py`
- Modify: `app/services/browser_verification.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert web URLs are preserved and observed JSON `403` text is classified as non-interactive:

```python
def test_verification_start_url_prefers_original_model_url_for_web_flow(self):
    session = {
        "platform": "cn",
        "target": {
            "model_url": "https://makerworld.com.cn/zh/models/12345-demo#profileId-1063416",
            "api_url": "",
            "instance_id": "1063416",
        },
    }

    start_url = browser_verification_module._verification_start_url(session)

    self.assertEqual(start_url, "https://makerworld.com.cn/zh/models/12345-demo#profileId-1063416")

def test_browser_verification_detects_json_api_denial_text(self):
    self.assertTrue(
        browser_verification_module._looks_like_api_denial_text(
            '{"code":403,"error":"The client does not have access rights to the content."}'
        )
    )
    self.assertFalse(
        browser_verification_module._looks_like_api_denial_text(
            '<html><body><button>Download 3MF</button></body></html>'
        )
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_browser_verification.py -k "start_url_prefers_original_model_url_for_web_flow or detects_json_api_denial_text" -q`

Expected: FAIL because `_verification_start_url` still prefers synthesized API URLs and `_looks_like_api_denial_text` does not exist.

- [ ] **Step 3: Implement minimal helpers**

Update `_verification_start_url` so `target.model_url` web URLs are returned before synthesizing Bambu API URLs. Add `_looks_like_api_denial_text(text: str) -> bool` that detects JSON/plain text `403` denial without matching normal HTML.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_browser_verification.py -k "start_url_prefers_original_model_url_for_web_flow or detects_json_api_denial_text" -q`

Expected: PASS.

## Task 2: Fallback Navigation and Automatic Trigger

**Files:**
- Modify: `tests/test_browser_verification.py`
- Modify: `app/services/browser_verification.py`

- [ ] **Step 1: Write failing tests**

Add focused fake-page tests:

```python
class _FakeLocator:
    def __init__(self, visible=True):
        self.clicked = False
        self.visible = visible

    def first(self):
        return self

    def is_visible(self, timeout=0):
        return self.visible

    def click(self, timeout=0):
        self.clicked = True


class _FakePage:
    def __init__(self, body_text):
        self.body_text = body_text
        self.goto_calls = []
        self.locators = {}

    def locator(self, selector):
        return self.locators.setdefault(selector, _FakeLocator(visible=False))

    def goto(self, url, wait_until="domcontentloaded", timeout=45000):
        self.goto_calls.append((url, wait_until, timeout))

    def text_content(self, selector, timeout=0):
        if selector == "body":
            return self.body_text
        return ""


def test_browser_verification_falls_back_to_model_url_after_api_denial(self):
    page = _FakePage('{"code":403,"error":"The client does not have access rights to the content."}')
    session = {
        "platform": "cn",
        "target": {
            "model_url": "https://makerworld.com.cn/zh/models/12345-demo",
            "api_url": "https://api.bambulab.cn/v1/design-service/instance/1063416/f3mf?type=download&fileType=3mf",
            "instance_id": "1063416",
        },
    }

    result = browser_verification_module._fallback_from_api_denial_if_needed(page, session)

    self.assertTrue(result)
    self.assertEqual(page.goto_calls[-1][0], "https://makerworld.com.cn/zh/models/12345-demo")


def test_try_trigger_download_flow_clicks_visible_download_control(self):
    page = _FakePage("")
    locator = _FakeLocator(visible=True)
    page.locators['button:has-text("Download 3MF")'] = locator

    result = browser_verification_module._try_trigger_download_flow(page)

    self.assertTrue(result)
    self.assertTrue(locator.clicked)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_browser_verification.py -k "falls_back_to_model_url_after_api_denial or try_trigger_download_flow_clicks_visible_download_control" -q`

Expected: FAIL because the helper functions do not exist.

- [ ] **Step 3: Implement fallback and trigger helpers**

Add:

- `_page_looks_like_api_denial(page) -> bool`: reads `body` text with a short timeout and calls `_looks_like_api_denial_text`.
- `_browser_verification_fallback_url(session) -> str`: returns normalized `target.model_url` if present, otherwise origin for platform.
- `_fallback_from_api_denial_if_needed(page, session) -> bool`: if current page is API denial, navigate to fallback URL and return `True`.
- `_try_trigger_download_flow(page) -> bool`: tries a bounded list of Playwright selectors for `Download 3MF`, `下载 3MF`, `Download`, and `下载`; clicks the first visible locator and returns `True`.

- [ ] **Step 4: Wire helpers into runtime**

After `page.goto(start_url, wait_until="domcontentloaded", timeout=45000)` in `_run_session`, call:

```python
fell_back = _fallback_from_api_denial_if_needed(page, session)
if fell_back or "/f3mf" not in urlparse(start_url).path:
    _try_trigger_download_flow(page)
```

Keep failures best-effort by catching helper exceptions and continuing the session.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_browser_verification.py -k "falls_back_to_model_url_after_api_denial or try_trigger_download_flow_clicks_visible_download_control" -q`

Expected: PASS.

## Task 3: Regression Suite, Version, and Release Notes

**Files:**
- Modify: version/release note files used by the project for patch releases

- [ ] **Step 1: Run browser verification tests**

Run: `pytest tests/test_browser_verification.py tests/test_web_routes.py -q`

Expected: PASS.

- [ ] **Step 2: Run frontend popup tests if frontend files changed**

Run the existing node tests that cover browser verification popup shape only if frontend files changed.

Expected: PASS or skipped because no frontend files changed.

- [ ] **Step 3: Update patch version and release notes**

Find the existing version and README release-note pattern, bump patch version, and add a concise note that browser verification now recovers from JSON API denial and tries the original MakerWorld download flow.

- [ ] **Step 4: Run final verification**

Run: `pytest tests/test_browser_verification.py tests/test_web_routes.py -q`

Expected: PASS.

- [ ] **Step 5: Commit only touched files**

Run:

```bash
git status --short
git add app/services/browser_verification.py tests/test_browser_verification.py README.md pyproject.toml package.json package-lock.json docs/superpowers/specs/2026-05-31-makerworld-auto-download-trigger-design.md docs/superpowers/plans/2026-05-31-makerworld-auto-download-trigger.md
git status --short
git commit -m "Trigger MakerWorld download verification flow"
```

If one of the listed version files is not touched or does not exist, omit it from `git add`.

## Self-Review

- Spec coverage: original trigger URL preference is Task 1; JSON `403` detection and fallback is Task 2; automatic download click is Task 2; tests and release notes are Task 3.
- Placeholder scan: no deferred implementation placeholders are present.
- Type consistency: all helpers are module-level functions in `app/services/browser_verification.py` and tests call them through `browser_verification_module`.
