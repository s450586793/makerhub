# Browser 3MF Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let MakerHub pause on MakerWorld 3MF verification, open an in-worker browser for the user to complete the challenge, then resume the blocked item and same-platform verification failures.

**Architecture:** The app process owns authenticated UI/API endpoints; the worker process owns CloakBrowser, Xvfb, screenshot capture, input replay, proof capture, and retry orchestration. Shared database JSON state coordinates sessions and input commands, while sensitive `x-bbl-captcha-result` values stay in worker memory behind short-lived proof ids.

**Tech Stack:** FastAPI, database JSON state, existing archive worker queue, CloakBrowser/Playwright, Xvfb, Vue 3.

---

### Task 1: 3MF Verification Metadata

**Files:**
- Modify: `app/services/legacy_archiver.py`
- Modify: `app/services/task_state.py`
- Test: `tests/test_browser_verification.py`

- [ ] Write a failing test that classifying `HTTP 418` JSON with `captchaId` returns `verification_required` plus a safe `verification` object containing `captcha_id`.
- [ ] Write a failing test that normalized missing 3MF items preserve safe fields: `api_url`, `captcha_id`, and `source`.
- [ ] Implement minimal metadata extraction and normalization.
- [ ] Run `python3 -m unittest tests.test_browser_verification`.

### Task 2: Proof Header Replay

**Files:**
- Modify: `app/services/legacy_archiver.py`
- Modify: `app/services/process_jobs.py`
- Modify: `app/services/archive_worker.py`
- Test: `tests/test_browser_verification.py`

- [ ] Write a failing test that `fetch_instance_3mf(..., captcha_result_header="proof")` sends `x-bbl-captcha-result` to Scrapling, requests, and curl command construction without logging proof content.
- [ ] Write a failing test that a missing-3MF retry can carry a `browser_verification_proof_id` into `run_archive_model_job`.
- [ ] Implement optional proof lookup in the worker and pass a resolved header only to the archive subprocess.
- [ ] Run `python3 -m unittest tests.test_browser_verification tests.test_missing_3mf`.

### Task 3: Verification Session State

**Files:**
- Create: `app/services/browser_verification.py`
- Modify: `app/worker.py`
- Test: `tests/test_browser_verification.py`

- [ ] Write failing tests for creating a session from a missing item, redacting sensitive fields, selecting same-platform verification failures only, and storing captured proofs by opaque id.
- [ ] Implement session CRUD, safe serialization, and retry selection.
- [ ] Add a worker poll hook that processes queued/active sessions.
- [ ] Run `python3 -m unittest tests.test_browser_verification`.

### Task 4: App API And Browser Stream

**Files:**
- Modify: `app/api/config.py`
- Test: `tests/test_browser_verification_api.py`

- [ ] Write failing API tests for create/get/cancel/complete/input/screenshot endpoints.
- [ ] Implement `/api/browser-verification/sessions` endpoints.
- [ ] Implement screenshot and input command endpoints backed by shared state files and database JSON state.
- [ ] Run `python3 -m unittest tests.test_browser_verification_api`.

### Task 5: Frontend Entry Points

**Files:**
- Modify: `frontend/src/router.js`
- Create: `frontend/src/pages/BrowserVerificationPage.vue`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/TasksPage.vue`

- [ ] Add compact “去验证” actions on dashboard health cards and verification-class missing 3MF rows.
- [ ] Add a dark compact verification page with screenshot viewer, status strip, retry/complete/cancel controls, and no marketing layout.
- [ ] Run `npm --prefix frontend run build`.

### Task 6: Image Dependencies

**Files:**
- Modify: `Dockerfile`
- Modify: `requirements.txt`

- [ ] Add `cloakbrowser[serve]`.
- [ ] Add Chromium headed runtime deps plus `xvfb`.
- [ ] Pre-download CloakBrowser binary at build time.
- [ ] Run focused backend tests and frontend build.
