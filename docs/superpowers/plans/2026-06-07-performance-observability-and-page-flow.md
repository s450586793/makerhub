# Performance Observability and Page Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sparse AI/operator-readable performance timing and remove known serial page-load bottlenecks in MakerHub.

**Architecture:** Backend timing is collected at the existing FastAPI middleware boundary and persisted through existing structured business logs only for slow or failed requests. Frontend timing is centralized in `frontend/src/lib/performance.js` and called from `apiRequest` plus page load boundaries. Model and subscription list restoration changes use one backend request for "items through page N" instead of serial page loops.

**Tech Stack:** FastAPI, Python services/tests, Vue 3 SPA, Node test runner, existing `append_business_log`, existing pagination payload shapes.

---

## File Structure

- Create `app/services/performance.py`: backend timing thresholds, path classification, query-key sanitization, and best-effort logging helpers.
- Create `app/api/performance_routes.py`: frontend slow-page event ingestion endpoint.
- Modify `app/main.py`: wrap API requests with timing measurement and include the new router.
- Modify `app/api/config.py`: add `/api/models` `limit` query support and pass it to `build_models_payload`; add a light config payload endpoint for settings first paint.
- Modify `app/api/subscriptions_routes.py`: add `/api/subscriptions` `limit` query support.
- Modify `app/services/catalog.py`: implement `limit` semantics in `build_models_payload`.
- Modify `app/services/subscriptions.py`: implement `limit` semantics in `SubscriptionManager.list_payload`.
- Create or extend backend tests:
  - `tests/test_performance_logging.py`
  - `tests/test_source_library.py`
  - `tests/test_subscriptions.py`
- Create `frontend/src/lib/performance.js`: frontend timing scopes, API counters, and slow-page reporting.
- Modify `frontend/src/lib/api.js`: measure API duration and report it to the current page timing scope.
- Modify `frontend/src/pages/ModelsPage.vue`: use a single `limit` request on initial restoration to page N.
- Modify `frontend/src/pages/SubscriptionsPage.vue`: use a single `limit` request on initial restoration to page N.
- Modify `frontend/src/pages/SettingsPage.vue`: render base config first, then refresh heavy status sections in background.
- Extend frontend tests:
  - `frontend/src/lib/pageRefreshShape.test.mjs`
  - create `frontend/src/lib/performance.test.mjs`
- Modify `README.md`, `CHANGELOG.md`, `VERSION`, `frontend/package.json`, `frontend/package-lock.json` only during release task.

---

### Task 1: Backend Performance Logging

**Files:**
- Create: `app/services/performance.py`
- Create: `app/api/performance_routes.py`
- Modify: `app/main.py`
- Test: `tests/test_performance_logging.py`

- [ ] **Step 1: Write backend performance helper tests**

Create `tests/test_performance_logging.py` with these tests:

```python
from types import SimpleNamespace
from unittest.mock import patch

from app.services import performance


def test_slow_get_request_is_logged_without_query_values():
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        query_params={"q": "secret search", "page": "2"},
    )
    response = SimpleNamespace(status_code=200, headers={"content-length": "1234"})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=901)

    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "slow_api_request", "API 请求耗时较高。")
    assert kwargs["method"] == "GET"
    assert kwargs["path"] == "/api/models"
    assert kwargs["status_code"] == 200
    assert kwargs["duration_ms"] == 901
    assert kwargs["query_keys"] == ["page", "q"]
    assert "secret search" not in str(kwargs)


def test_fast_successful_request_is_not_logged():
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/models"),
        query_params={"page": "1"},
    )
    response = SimpleNamespace(status_code=200, headers={})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=120)

    log.assert_not_called()


def test_failed_request_is_logged_even_when_fast():
    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/archive"),
        query_params={},
    )
    response = SimpleNamespace(status_code=500, headers={})

    with patch.object(performance, "append_business_log") as log:
        performance.log_api_request_if_needed(request, response, duration_ms=50)

    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "api_error_request", "API 请求失败。")
    assert kwargs["method"] == "POST"
    assert kwargs["path"] == "/api/archive"
    assert kwargs["status_code"] == 500


def test_frontend_slow_page_event_is_sanitized_and_logged():
    payload = {
        "page": "settings",
        "route": "/settings?token=secret",
        "duration_ms": 1500.6,
        "api_count": 5,
        "slow_api_count": 2,
        "max_api_duration_ms": 900.2,
        "extra": "ignored",
    }

    with patch.object(performance, "append_business_log") as log:
        result = performance.log_frontend_page_event(payload)

    assert result == {"success": True, "recorded": True}
    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:3] == ("performance", "slow_page_load", "页面首屏加载较慢。")
    assert kwargs["page"] == "settings"
    assert kwargs["route"] == "/settings"
    assert kwargs["duration_ms"] == 1500.6
    assert "secret" not in str(kwargs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_performance_logging.py -q`

Expected: fails with import or attribute errors for `app.services.performance`.

- [ ] **Step 3: Implement `app/services/performance.py`**

Create `app/services/performance.py`:

```python
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.services.business_logs import append_business_log


GET_SLOW_THRESHOLD_MS = 800
WRITE_SLOW_THRESHOLD_MS = 1500
HIGH_FREQUENCY_GET_SLOW_THRESHOLD_MS = 2500
FRONTEND_SLOW_PAGE_THRESHOLD_MS = 1200
HIGH_FREQUENCY_PATHS = {
    "/api/logs",
    "/api/state-events",
}


def _safe_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("/"):
        return "/"
    return path[:240]


def _query_keys(request: Any) -> list[str]:
    params = getattr(request, "query_params", {}) or {}
    try:
        keys = params.keys()
    except AttributeError:
        keys = []
    return sorted({str(key)[:80] for key in keys if str(key or "").strip()})


def _response_size(response: Any) -> int:
    headers = getattr(response, "headers", {}) or {}
    try:
        raw_value = headers.get("content-length") or headers.get("Content-Length") or ""
    except AttributeError:
        raw_value = ""
    try:
        return max(int(raw_value), 0)
    except (TypeError, ValueError):
        return 0


def _threshold_for(method: str, path: str) -> int:
    clean_method = str(method or "").upper()
    clean_path = _safe_path(path)
    if clean_method == "GET" and clean_path in HIGH_FREQUENCY_PATHS:
        return HIGH_FREQUENCY_GET_SLOW_THRESHOLD_MS
    if clean_method == "GET":
        return GET_SLOW_THRESHOLD_MS
    return WRITE_SLOW_THRESHOLD_MS


def log_api_request_if_needed(request: Any, response: Any, *, duration_ms: float) -> None:
    method = str(getattr(request, "method", "") or "").upper()
    path = _safe_path(getattr(getattr(request, "url", None), "path", ""))
    status_code = int(getattr(response, "status_code", 0) or 0)
    rounded_duration = round(float(duration_ms or 0), 1)
    failed = status_code >= 400
    slow = rounded_duration >= _threshold_for(method, path)
    if not failed and not slow:
        return
    event = "api_error_request" if failed else "slow_api_request"
    message = "API 请求失败。" if failed else "API 请求耗时较高。"
    try:
        append_business_log(
            "performance",
            event,
            message,
            level="warning" if failed else "info",
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=rounded_duration,
            slow=slow,
            query_keys=_query_keys(request),
            response_size=_response_size(response),
        )
    except Exception:
        return


def _safe_route(value: Any) -> str:
    raw_route = str(value or "").strip()[:240]
    if not raw_route:
        return ""
    parts = urlsplit(raw_route)
    path = parts.path or raw_route.split("?", 1)[0]
    return urlunsplit(("", "", path[:200], "", "")) or "/"


def _safe_int(value: Any, minimum: int = 0, maximum: int = 10000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(number, maximum))


def _safe_float(value: Any, minimum: float = 0.0, maximum: float = 600000.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return minimum
    return round(max(minimum, min(number, maximum)), 1)


def log_frontend_page_event(payload: dict[str, Any]) -> dict[str, bool]:
    if not isinstance(payload, dict):
        return {"success": True, "recorded": False}
    duration_ms = _safe_float(payload.get("duration_ms"))
    if duration_ms < FRONTEND_SLOW_PAGE_THRESHOLD_MS:
        return {"success": True, "recorded": False}
    page = str(payload.get("page") or "").strip()[:80] or "unknown"
    try:
        append_business_log(
            "performance",
            "slow_page_load",
            "页面首屏加载较慢。",
            page=page,
            route=_safe_route(payload.get("route")),
            duration_ms=duration_ms,
            api_count=_safe_int(payload.get("api_count"), maximum=500),
            slow_api_count=_safe_int(payload.get("slow_api_count"), maximum=500),
            max_api_duration_ms=_safe_float(payload.get("max_api_duration_ms")),
        )
        return {"success": True, "recorded": True}
    except Exception:
        return {"success": True, "recorded": False}
```

- [ ] **Step 4: Implement frontend event API route**

Create `app/api/performance_routes.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.config import _require_session_auth
from app.services.performance import log_frontend_page_event


router = APIRouter(prefix="/api")


@router.post("/performance/events")
async def record_performance_event(payload: dict, request: Request):
    _require_session_auth(request)
    return log_frontend_page_event(payload)
```

Modify `app/main.py` imports:

```python
import time
```

Add imports:

```python
from app.api.performance_routes import router as performance_router
from app.services.performance import log_api_request_if_needed
```

Include router near other API routers:

```python
app.include_router(performance_router)
```

At the start of `auth_guard`, add:

```python
    started_perf = time.perf_counter()
```

For every branch that returns a response in `auth_guard`, use a helper before returning. Add inside `auth_guard` after `path = request.url.path`:

```python
    def finish(response):
        response = _apply_cache_headers(path, response)
        if path.startswith("/api/"):
            duration_ms = (time.perf_counter() - started_perf) * 1000
            log_api_request_if_needed(request, response, duration_ms=duration_ms)
        return response
```

Then replace return patterns inside `auth_guard`:

```python
return _apply_cache_headers(path, response)
```

with:

```python
return finish(response)
```

and replace direct JSON/redirect returns with `return finish(...)`.

- [ ] **Step 5: Run backend performance tests**

Run: `.venv/bin/python -m pytest tests/test_performance_logging.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add app/services/performance.py app/api/performance_routes.py app/main.py tests/test_performance_logging.py
git commit -m "feat: 记录慢请求性能事件"
```

Expected: commit succeeds and unrelated `videos/makerhub-intro/output/` remains untracked.

---

### Task 2: Frontend Performance Timing

**Files:**
- Create: `frontend/src/lib/performance.js`
- Create: `frontend/src/lib/performance.test.mjs`
- Modify: `frontend/src/lib/api.js`
- Modify: selected page components listed below
- Test: `frontend/src/lib/performance.test.mjs`, `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Write frontend performance unit tests**

Create `frontend/src/lib/performance.test.mjs`:

```javascript
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createPagePerformanceTracker,
  normalizePagePerformancePayload,
  recordApiDuration,
  resetPerformanceStateForTests,
} from "./performance.js";

test("recordApiDuration tracks API count, slow count, and max duration", () => {
  resetPerformanceStateForTests();
  recordApiDuration("/api/models", 120);
  recordApiDuration("/api/dashboard", 900);
  const payload = normalizePagePerformancePayload({
    page: "models",
    route: "/models?page=2",
    durationMs: 1400,
  });
  assert.equal(payload.api_count, 2);
  assert.equal(payload.slow_api_count, 1);
  assert.equal(payload.max_api_duration_ms, 900);
});

test("createPagePerformanceTracker reports only slow page loads", async () => {
  resetPerformanceStateForTests();
  const calls = [];
  const tracker = createPagePerformanceTracker({
    page: "models",
    route: () => "/models",
    now: (() => {
      let value = 0;
      return () => {
        value += 700;
        return value;
      };
    })(),
    request: async (path, options) => calls.push([path, options]),
  });

  await tracker.finish();

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/performance/events");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].redirectOn401, false);
  assert.equal(calls[0][1].body.page, "models");
});
```

- [ ] **Step 2: Run frontend performance tests to verify failure**

Run: `node --test frontend/src/lib/performance.test.mjs`

Expected: fails because `frontend/src/lib/performance.js` does not exist.

- [ ] **Step 3: Implement `frontend/src/lib/performance.js`**

Create:

```javascript
const API_SLOW_THRESHOLD_MS = 800;
const PAGE_SLOW_THRESHOLD_MS = 1200;

let apiCount = 0;
let slowApiCount = 0;
let maxApiDurationMs = 0;

function safeNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, number) : 0;
}

export function resetPerformanceStateForTests() {
  apiCount = 0;
  slowApiCount = 0;
  maxApiDurationMs = 0;
}

export function recordApiDuration(path, durationMs) {
  const duration = safeNumber(durationMs);
  const cleanPath = String(path || "");
  if (!cleanPath.startsWith("/api/") || cleanPath === "/api/performance/events") {
    return;
  }
  apiCount += 1;
  if (duration >= API_SLOW_THRESHOLD_MS) {
    slowApiCount += 1;
  }
  maxApiDurationMs = Math.max(maxApiDurationMs, duration);
}

export function normalizePagePerformancePayload({ page, route, durationMs }) {
  return {
    page: String(page || "unknown").slice(0, 80),
    route: String(route || "").slice(0, 240),
    duration_ms: Math.round(safeNumber(durationMs) * 10) / 10,
    api_count: apiCount,
    slow_api_count: slowApiCount,
    max_api_duration_ms: Math.round(maxApiDurationMs * 10) / 10,
  };
}

export function createPagePerformanceTracker(options = {}) {
  const {
    page = "unknown",
    route = () => (typeof window === "undefined" ? "" : window.location.pathname),
    now = () => (typeof performance !== "undefined" ? performance.now() : Date.now()),
    request = async (path, requestOptions) => {
      const response = await fetch(path, {
        method: requestOptions.method || "GET",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(requestOptions.body || {}),
        credentials: "include",
        cache: "no-store",
      });
      return response.ok ? response : null;
    },
  } = options;
  const startedAt = now();
  return {
    async finish() {
      const durationMs = now() - startedAt;
      if (durationMs < PAGE_SLOW_THRESHOLD_MS) {
        return null;
      }
      const payload = normalizePagePerformancePayload({
        page,
        route: typeof route === "function" ? route() : route,
        durationMs,
      });
      try {
        await request("/api/performance/events", {
          method: "POST",
          body: payload,
          redirectOn401: false,
        });
      } catch {
        return null;
      }
      return payload;
    },
  };
}
```

- [ ] **Step 4: Wire API timing into `apiRequest`**

Modify `frontend/src/lib/api.js`:

```javascript
import { recordApiDuration } from "./performance.js";
```

At the start of `apiRequest`, before `fetch`:

```javascript
  const startedAt = typeof performance !== "undefined" ? performance.now() : Date.now();
```

After `fetch` resolves, before status handling:

```javascript
  recordApiDuration(path, (typeof performance !== "undefined" ? performance.now() : Date.now()) - startedAt);
```

Do not change response parsing or error behavior.

- [ ] **Step 5: Wire slow-page trackers into selected pages**

For each page, import:

```javascript
import { createPagePerformanceTracker } from "../lib/performance";
```

Then wrap initial load in `onMounted`.

For `frontend/src/pages/ModelsPage.vue`, change:

```javascript
onMounted(async () => {
  if (hydrateModelListFromCache()) {
    await nextTick();
    ensureObserver();
  } else {
    await load({ append: false });
  }
});
```

to:

```javascript
onMounted(async () => {
  const perf = createPagePerformanceTracker({ page: "models", route: () => route.fullPath });
  if (hydrateModelListFromCache()) {
    await nextTick();
    ensureObserver();
  } else {
    await load({ append: false });
  }
  void perf.finish();
});
```

Apply the same pattern to:

- `DashboardPage.vue`: page `"dashboard"`
- `SubscriptionsPage.vue`: page `"subscriptions"`
- `ModelLibraryGroupPage.vue`: page `"model_group"`
- `SettingsPage.vue`: page `"settings"`
- `TasksPage.vue`: page `"tasks"`
- `LogsPage.vue`: page `"logs"`
- `RemoteRefreshPage.vue`: page `"remote_refresh"`
- `OrganizerPage.vue`: page `"organizer"`

- [ ] **Step 6: Add shape test for selected pages**

Extend `frontend/src/lib/pageRefreshShape.test.mjs`:

```javascript
test("primary pages report slow first-load performance without UI changes", () => {
  for (const source of [
    dashboardPageSource,
    organizerPageSource,
    settingsPageSource,
    tasksPageSource,
    logsPageSource,
    remoteRefreshPageSource,
  ]) {
    assert.match(source, /createPagePerformanceTracker/);
    assert.match(source, /perf\.finish/);
  }
});
```

Add file reads for any missing page sources at the top.

- [ ] **Step 7: Run frontend timing tests**

Run:

```bash
node --test frontend/src/lib/performance.test.mjs frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add frontend/src/lib/performance.js frontend/src/lib/performance.test.mjs frontend/src/lib/api.js frontend/src/lib/pageRefreshShape.test.mjs frontend/src/pages/DashboardPage.vue frontend/src/pages/ModelsPage.vue frontend/src/pages/ModelLibraryGroupPage.vue frontend/src/pages/SubscriptionsPage.vue frontend/src/pages/SettingsPage.vue frontend/src/pages/TasksPage.vue frontend/src/pages/LogsPage.vue frontend/src/pages/RemoteRefreshPage.vue frontend/src/pages/OrganizerPage.vue
git commit -m "feat: 上报慢页面性能事件"
```

Expected: commit succeeds.

---

### Task 3: Single-Request Restoration for Model and Subscription Pages

**Files:**
- Modify: `app/services/catalog.py`
- Modify: `app/api/config.py`
- Modify: `app/services/subscriptions.py`
- Modify: `app/api/subscriptions_routes.py`
- Modify: `frontend/src/pages/ModelsPage.vue`
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Test: `tests/test_source_library.py`, `tests/test_subscriptions.py`, `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Write model limit backend test**

Add to `tests/test_source_library.py`:

```python
def test_models_payload_limit_returns_items_through_requested_page():
    items = [
        {
            "model_dir": f"m-{index}",
            "id": str(index),
            "title": f"Model {index}",
            "author": {"name": "A"},
            "tags": [],
            "source": "cn",
            "collect_ts": 1000 - index,
            "local_flags": {},
            "subscription_flags": {},
        }
        for index in range(10)
    ]
    with patch("app.services.catalog.get_decorated_models", return_value=(items, items)):
        payload = catalog.build_models_payload(page=3, page_size=2, limit=6)

    assert payload["page"] == 3
    assert payload["page_size"] == 2
    assert payload["count"] == 6
    assert [item["model_dir"] for item in payload["items"]] == ["m-0", "m-1", "m-2", "m-3", "m-4", "m-5"]
    assert payload["has_more"] is True
    assert payload["filtered_total"] == 10
```

Ensure imports include:

```python
from unittest.mock import patch
from app.services import catalog
```

- [ ] **Step 2: Write subscription limit backend test**

Add to `tests/test_subscriptions.py`:

```python
def test_subscription_payload_limit_returns_source_cards_through_requested_page():
    manager = subscriptions.SubscriptionManager(
        archive_manager=SimpleNamespace(),
        store=JsonStore(),
        task_store=TaskStateStore(),
        background_enabled=False,
    )
    overview = {
        "sections": [
            {
                "key": "subscription_sources",
                "items": [{"key": f"source-{index}"} for index in range(10)],
            }
        ],
        "settings": {},
    }
    with patch.object(manager, "_ensure_state_records"), \
            patch.object(manager.store, "load", return_value=AppConfig()), \
            patch.object(manager.task_store, "load_subscriptions_state", return_value={"items": []}), \
            patch("app.services.subscriptions.build_subscription_overview_payload", return_value=overview):
        payload = manager.list_payload(page=3, page_size=2, limit=6)

    section = next(item for item in payload["sections"] if item["key"] == "subscription_sources")
    assert section["page"] == 3
    assert section["page_size"] == 2
    assert section["count"] == 6
    assert section["has_more"] is True
    assert [item["key"] for item in section["items"]] == [
        "source-0", "source-1", "source-2", "source-3", "source-4", "source-5"
    ]
```

Use real imports already present in `tests/test_subscriptions.py`; add `SimpleNamespace`, `patch`, `JsonStore`, `AppConfig`, and `TaskStateStore` only if missing.

- [ ] **Step 3: Run backend limit tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_source_library.py::test_models_payload_limit_returns_items_through_requested_page tests/test_subscriptions.py::test_subscription_payload_limit_returns_source_cards_through_requested_page -q
```

Expected: fails because `limit` is not accepted yet.

- [ ] **Step 4: Implement model `limit` support**

Modify `app/services/catalog.py` signature:

```python
def build_models_payload(
    q: str = "",
    source: str = "all",
    tag: str = "",
    sort_key: str = "collectDate",
    page: int = 1,
    page_size: int = 8,
    limit: int = 0,
) -> dict:
```

Replace paging calculation with:

```python
    safe_page_size = max(1, min(int(page_size or 8), 120))
    safe_page = max(int(page or 1), 1)
    total_filtered = len(items)
    safe_limit = max(int(limit or 0), 0)
    if safe_limit > 0:
        effective_limit = min(safe_limit, 2000)
        start = 0
        end = effective_limit
    else:
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
    paged_items = items[start:end]
```

Keep returned `page` as `safe_page` and `page_size` as `safe_page_size`.

Modify `app/api/config.py` `get_models_data` parameters:

```python
    limit: int = Query(0, ge=0, le=2000, description="从第一页起一次返回的数量"),
```

Pass `limit=limit` to `build_models_payload`.

- [ ] **Step 5: Implement subscription `limit` support**

Modify `app/services/subscriptions.py` `list_payload` signature:

```python
    def list_payload(
        self,
        *,
        page: int = 1,
        page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE,
        limit: int = 0,
    ) -> dict:
```

Before returning, compute:

```python
        effective_page_size = max(1, min(int(page_size or DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE), 120))
        effective_page = max(int(page or 1), 1)
        effective_limit = max(int(limit or 0), 0)
        pagination_page_size = effective_limit if effective_limit > 0 else effective_page_size
        pagination_page = 1 if effective_limit > 0 else effective_page
```

Call:

```python
            "sections": _paginate_subscription_source_sections(
                list(overview.get("sections") or []),
                page=pagination_page,
                page_size=pagination_page_size,
            ),
```

After pagination, if `effective_limit > 0`, update the subscription source section metadata:

```python
        sections = _paginate_subscription_source_sections(...)
        if effective_limit > 0:
            for section in sections:
                if section.get("key") == "subscription_sources":
                    section["page"] = effective_page
                    section["page_size"] = effective_page_size
                    section["has_more"] = effective_limit < int(section.get("total") or 0)
```

Return `sections`.

Modify `app/api/subscriptions_routes.py`:

```python
    limit: int = Query(0, ge=0, le=2000, description="从第一页起一次返回的数量"),
```

and call:

```python
return await run_web_io(subscription_manager.list_payload, page=page, page_size=page_size, limit=limit)
```

- [ ] **Step 6: Change model frontend restoration to one request**

Modify `frontend/src/pages/ModelsPage.vue` `buildQuery`:

```javascript
  if (options.includeUntilPage) {
    query.set("limit", String(Math.max(1, Math.floor(Number(page) || 1)) * PAGE_SIZE));
  }
```

Modify `load` initial branch:

```javascript
  const response = suppressLocallyDeletedItems(await fetchPage(nextPage, {
    cacheKey: cacheKeyBase ? `${cacheKeyBase}-${nextPage}` : "",
    includeUntilPage: !append && nextPage > 1,
  }));
```

Then remove the `for` loop and `responses` array. For non-append, set payload from the single response:

```javascript
  if (append) {
    const mergedItems = mergeUniqueModelItems(payload.value.items, response.items || []);
    payload.value = {
      ...response,
      items: mergedItems,
      count: mergedItems.length,
    };
  } else {
    payload.value = {
      ...response,
      count: (response.items || []).length,
      page: nextPage,
    };
  }
```

Apply the same single-request pattern in `reloadVisiblePages`.

- [ ] **Step 7: Change subscription frontend restoration to one request**

Modify `frontend/src/pages/SubscriptionsPage.vue` `buildSubscriptionsQuery`:

```javascript
function buildSubscriptionsQuery(page = 1, options = {}) {
  const query = new URLSearchParams();
  const safePage = Math.max(Number(page) || 1, 1);
  query.set("page", String(safePage));
  query.set("page_size", String(PAGE_SIZE));
  if (options.includeUntilPage) {
    query.set("limit", String(Math.max(1, Math.floor(safePage)) * PAGE_SIZE));
  }
  return query;
}
```

Modify `fetchSubscriptionsPage`:

```javascript
async function fetchSubscriptionsPage(page = 1, options = {}) {
  return normalizeSubscriptionsPayload(
    await apiRequest(`/api/subscriptions?${buildSubscriptionsQuery(page, options).toString()}`),
  );
}
```

Modify `load` to remove the `for` loop:

```javascript
    const response = await fetchSubscriptionsPage(pagesToLoad, { includeUntilPage: pagesToLoad > 1 });
    if (currentToken !== requestToken) {
      return;
    }
    const section = subscriptionSourcesSection(response);
    payload.value = replaceSubscriptionSourcesSection(
      response,
      section?.items || [],
      {
        ...(section || {}),
        page: pagesToLoad,
        page_size: PAGE_SIZE,
        has_more: Boolean(section?.has_more),
        total: Number(section?.total || section?.items?.length || 0),
      },
    );
```

- [ ] **Step 8: Add frontend shape tests for no serial restoration loops**

Extend `frontend/src/lib/pageRefreshShape.test.mjs`:

```javascript
const modelsPageSource = readFileSync(new URL("../pages/ModelsPage.vue", import.meta.url), "utf8");
const subscriptionsPageSource = readFileSync(new URL("../pages/SubscriptionsPage.vue", import.meta.url), "utf8");

test("ModelsPage restores deep pages with a single include-until-page request", () => {
  assert.match(modelsPageSource, /includeUntilPage/);
  assert.match(modelsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(modelsPageSource, /for \(let page = append \? nextPage : 1; page <= nextPage;/);
});

test("SubscriptionsPage restores deep pages with a single include-until-page request", () => {
  assert.match(subscriptionsPageSource, /includeUntilPage/);
  assert.match(subscriptionsPageSource, /query\.set\("limit"/);
  assert.doesNotMatch(subscriptionsPageSource, /for \(let page = 1; page <= pagesToLoad;/);
});
```

- [ ] **Step 9: Run restoration tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_source_library.py tests/test_subscriptions.py -q
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: all tests pass.

- [ ] **Step 10: Commit Task 3**

Run:

```bash
git add app/services/catalog.py app/api/config.py app/services/subscriptions.py app/api/subscriptions_routes.py frontend/src/pages/ModelsPage.vue frontend/src/pages/SubscriptionsPage.vue frontend/src/lib/pageRefreshShape.test.mjs tests/test_source_library.py tests/test_subscriptions.py
git commit -m "fix: 减少列表深页串行加载"
```

Expected: commit succeeds.

---

### Task 4: Settings First-Paint Split

**Files:**
- Modify: `app/api/config.py`
- Modify: `frontend/src/lib/appState.js`
- Modify: `frontend/src/pages/SettingsPage.vue`
- Test: `tests/test_config_payloads.py`, `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Write backend light config test**

Create `tests/test_config_payloads.py`:

```python
from app.api import config as config_api
from app.schemas.models import AppConfig


def test_public_config_light_payload_excludes_heavy_runtime_sections():
    payload = config_api._public_config_light_payload(AppConfig())

    assert payload["app_version"]
    assert "cookies" in payload
    assert "proxy" in payload
    assert "user" in payload
    assert "subscriptions" in payload
    assert "database" not in payload
    assert "remote_refresh_state" not in payload
    assert "cookie_source_inventory" not in payload
    assert "cookie_source_sync_state" not in payload
```

- [ ] **Step 2: Run light config test to verify failure**

Run: `.venv/bin/python -m pytest tests/test_config_payloads.py -q`

Expected: fails because `_public_config_light_payload` does not exist.

- [ ] **Step 3: Implement light config payload**

Modify `app/api/config.py` after `_public_config_payload`:

```python
def _public_config_light_payload(config) -> dict:
    payload = _public_config_payload(config)
    for key in (
        "database",
        "remote_refresh_state",
        "cookie_source_inventory",
        "cookie_source_sync_state",
    ):
        payload.pop(key, None)
    return payload
```

Add route:

```python
@router.get("/config/light")
async def get_config_light():
    config = await run_ui_io(store.load)
    return _with_version_status(
        await run_ui_io(_public_config_light_payload, config),
        await _get_github_version_status(proxy_config=config.proxy),
    )
```

- [ ] **Step 4: Add frontend config refresh helper**

Modify `frontend/src/lib/appState.js`:

```javascript
export async function refreshLightConfig() {
  return applyConfigPayload(await apiRequest("/api/config/light"));
}
```

- [ ] **Step 5: Modify SettingsPage first load**

In `frontend/src/pages/SettingsPage.vue`, import `refreshLightConfig` alongside `refreshConfig`.

Change:

```javascript
async function load() {
  const payload = await refreshConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "system");
}
```

to:

```javascript
async function load() {
  const payload = await refreshLightConfig();
  applyConfigToForms(payload);
  setActiveTab(typeof route.query.tab === "string" ? route.query.tab : "system");
  void refreshSettingsDiagnostics();
}
```

Add:

```javascript
async function refreshSettingsDiagnostics() {
  try {
    const payload = await refreshConfig();
    applyConfigToForms(payload);
  } catch (error) {
    statuses.system = error instanceof Error ? error.message : "系统诊断刷新失败。";
  }
}
```

Do not block first paint on `refreshSettingsDiagnostics`.

- [ ] **Step 6: Add frontend shape test for settings light config**

Extend `frontend/src/lib/pageRefreshShape.test.mjs`:

```javascript
test("SettingsPage renders from light config before background diagnostics", () => {
  assert.match(settingsPageSource, /refreshLightConfig/);
  assert.match(settingsPageSource, /refreshSettingsDiagnostics/);
  assert.match(settingsPageSource, /void refreshSettingsDiagnostics\(\)/);
});
```

- [ ] **Step 7: Run settings tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config_payloads.py -q
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 4**

Run:

```bash
git add app/api/config.py frontend/src/lib/appState.js frontend/src/pages/SettingsPage.vue frontend/src/lib/pageRefreshShape.test.mjs tests/test_config_payloads.py
git commit -m "fix: 加快设置页首屏配置加载"
```

Expected: commit succeeds.

---

### Task 5: Verification, Version, and Release Notes

**Files:**
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run full targeted verification**

Run:

```bash
.venv/bin/python -m pytest tests/test_performance_logging.py tests/test_config_payloads.py tests/test_source_library.py tests/test_subscriptions.py tests/test_auth_guard.py tests/test_system_bootstrap.py -q
node --test frontend/src/lib/performance.test.mjs frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/loginFlow.test.mjs
.venv/bin/python -m py_compile app/main.py app/services/performance.py app/api/performance_routes.py app/api/config.py app/api/subscriptions_routes.py app/services/catalog.py app/services/subscriptions.py
npm run build
git diff --check
```

Expected:

- pytest passes
- node tests pass
- py_compile exits 0
- frontend build exits 0
- diff check exits 0

- [ ] **Step 2: Bump patch version**

If current version is `0.9.9`, update:

- `VERSION` to `0.9.10`
- `frontend/package.json` version to `0.9.10`
- `frontend/package-lock.json` version fields to `0.9.10`
- README current version to `v0.9.10`

Use existing version-file style exactly.

- [ ] **Step 3: Update release notes**

Add top entries to `CHANGELOG.md` and README release notes:

```markdown
## 2026-06-07 · v0.9.10

- 新增稀疏性能观测，慢 API、失败 API 和慢页面会写入结构化性能事件，便于线上排查访问时间和响应时间。
- 模型库和订阅库深页恢复改为单次请求返回已加载范围，避免从第 1 页串行拉到当前页。
- 设置页先加载轻量配置，系统诊断和运行状态改为后台刷新，减少首屏等待。
```

Keep README latest-three-release rule intact: show latest three directly and move older notes into the collapsed section if needed.

- [ ] **Step 4: Run release verification again**

Run:

```bash
.venv/bin/python -m pytest tests/test_performance_logging.py tests/test_config_payloads.py tests/test_source_library.py tests/test_subscriptions.py tests/test_auth_guard.py tests/test_system_bootstrap.py -q
node --test frontend/src/lib/performance.test.mjs frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/loginFlow.test.mjs
npm run build
git diff --check
```

Expected: all pass.

- [ ] **Step 5: Commit release**

Run:

```bash
git add VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md
git commit -m "chore: 发布 v0.9.10"
```

Expected: commit succeeds.

Do not push unless the user explicitly says `推送`.

---

## Self-Review Checklist

- Spec coverage:
  - Backend slow/failed API timing: Task 1
  - Frontend slow page timing: Task 2
  - Model deep-page single request: Task 3
  - Subscription deep-page single request: Task 3
  - Settings first-paint split: Task 4
  - No log center UI: no task adds UI
  - Tasks/remote-refresh conservative handling: no task splits their payload
- Plan quality scan: every task names exact files, commands, and expected outcomes.
- Type consistency:
  - Backend uses `duration_ms`, `query_keys`, `response_size`.
  - Frontend uses `duration_ms`, `api_count`, `slow_api_count`, `max_api_duration_ms`.
  - Pagination uses `limit` consistently for models and subscriptions.
