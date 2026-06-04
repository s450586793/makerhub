# Subscription Source Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add model-library-style "加载更多" pagination to subscription source cards.

**Architecture:** The backend keeps the existing `/api/subscriptions` payload shape but paginates only the `subscription_sources` section. The frontend requests subscription pages with `page` and `page_size`, merges loaded source cards, stores loaded page depth in the route/cache, and keeps selection scoped to loaded cards.

**Tech Stack:** FastAPI, Python services/tests, Vue 3 Composition API, Node built-in test runner, Vite.

---

## File Structure

- Modify `app/services/subscriptions.py`: add section pagination helpers and extend `SubscriptionManager.list_payload()`.
- Modify `app/api/subscriptions_routes.py`: accept `page` and `page_size` query params and forward them.
- Modify `tests/test_subscriptions.py`: cover backend pagination and global summary preservation.
- Modify `frontend/src/lib/subscriptions.js`: preserve pagination metadata during payload normalization.
- Modify `frontend/src/pages/SubscriptionsPage.vue`: add page query parsing, page fetch/merge, cache restoration, load-more footer, and loaded-card selection behavior.
- Add `frontend/src/lib/subscriptionsPageShape.test.mjs`: lightweight source-shape regression tests for route paging, API query usage, load-more text, and selection copy.

## Task 1: Backend Pagination Contract

**Files:**
- Modify: `tests/test_subscriptions.py`
- Modify: `app/services/subscriptions.py`
- Modify: `app/api/subscriptions_routes.py`

- [ ] **Step 1: Write failing backend tests**

Append these tests inside `SubscriptionManagerTest` in `tests/test_subscriptions.py`:

```python
    def test_list_payload_paginates_subscription_source_cards_only(self):
        config = self.store.load()
        config.subscriptions = [
            SubscriptionRecord(id=f"sub-{index}", name=f"Source {index}", url=f"https://makerworld.com/zh/@user{index}/upload", mode="author_upload")
            for index in range(1, 6)
        ]
        self.store.save(config)

        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "label": "订阅来源",
                    "items": [{"key": f"source-{index}", "title": f"Source {index}"} for index in range(1, 6)],
                },
                {
                    "key": "states",
                    "label": "状态",
                    "items": [{"key": "source_deleted", "title": "源端删除"}],
                },
            ],
            "settings": {"card_sort": "recent"},
        }

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value=overview):
            payload = self.manager.list_payload(page=2, page_size=2)

        source_section = next(section for section in payload["sections"] if section["key"] == "subscription_sources")
        state_section = next(section for section in payload["sections"] if section["key"] == "states")
        self.assertEqual([item["key"] for item in source_section["items"]], ["source-3", "source-4"])
        self.assertEqual(source_section["count"], 2)
        self.assertEqual(source_section["total"], 5)
        self.assertEqual(source_section["page"], 2)
        self.assertEqual(source_section["page_size"], 2)
        self.assertTrue(source_section["has_more"])
        self.assertEqual(payload["count"], 5)
        self.assertEqual(payload["summary"]["enabled"], 5)
        self.assertEqual(state_section["items"], [{"key": "source_deleted", "title": "源端删除"}])

    def test_list_payload_subscription_source_last_page_has_no_more(self):
        overview = {
            "sections": [
                {
                    "key": "subscription_sources",
                    "label": "订阅来源",
                    "items": [{"key": f"source-{index}"} for index in range(1, 4)],
                }
            ],
            "settings": {},
        }

        with patch.object(subscriptions, "build_subscription_overview_payload", return_value=overview):
            payload = self.manager.list_payload(page=2, page_size=2)

        source_section = payload["sections"][0]
        self.assertEqual([item["key"] for item in source_section["items"]], ["source-3"])
        self.assertEqual(source_section["count"], 1)
        self.assertEqual(source_section["total"], 3)
        self.assertFalse(source_section["has_more"])
```

- [ ] **Step 2: Run backend tests to verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_subscriptions.py::SubscriptionManagerTest::test_list_payload_paginates_subscription_source_cards_only tests/test_subscriptions.py::SubscriptionManagerTest::test_list_payload_subscription_source_last_page_has_no_more -q
```

Expected: FAIL because `list_payload()` does not accept `page` and `page_size`.

- [ ] **Step 3: Implement backend pagination helpers**

In `app/services/subscriptions.py`, near `SubscriptionManager` or the existing payload helpers, add:

```python
DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE = 24
MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE = 120


def _normalize_subscription_source_page(page: int = 1, page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE) -> tuple[int, int]:
    try:
        safe_page = int(page or 1)
    except (TypeError, ValueError):
        safe_page = 1
    try:
        safe_page_size = int(page_size or DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE)
    except (TypeError, ValueError):
        safe_page_size = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE
    return max(safe_page, 1), max(1, min(safe_page_size, MAX_SUBSCRIPTION_SOURCE_PAGE_SIZE))


def _paginate_subscription_source_sections(sections: list[dict], *, page: int = 1, page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE) -> list[dict]:
    safe_page, safe_page_size = _normalize_subscription_source_page(page, page_size)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    paged_sections = []
    for section in sections or []:
        if not isinstance(section, dict) or section.get("key") != "subscription_sources":
            paged_sections.append(section)
            continue
        items = list(section.get("items") or [])
        visible_items = items[start:end]
        paged = dict(section)
        paged["items"] = visible_items
        paged["count"] = len(visible_items)
        paged["total"] = len(items)
        paged["page"] = safe_page
        paged["page_size"] = safe_page_size
        paged["has_more"] = end < len(items)
        paged_sections.append(paged)
    return paged_sections
```

- [ ] **Step 4: Extend `list_payload()`**

Change `SubscriptionManager.list_payload()` signature from:

```python
    def list_payload(self) -> dict:
```

to:

```python
    def list_payload(self, *, page: int = 1, page_size: int = DEFAULT_SUBSCRIPTION_SOURCE_PAGE_SIZE) -> dict:
```

Then change the returned `sections` line from:

```python
            "sections": overview.get("sections") or [],
```

to:

```python
            "sections": _paginate_subscription_source_sections(
                list(overview.get("sections") or []),
                page=page,
                page_size=page_size,
            ),
```

- [ ] **Step 5: Extend API route**

In `app/api/subscriptions_routes.py`, import `Query` if not already imported from FastAPI. Change `get_subscriptions_data()` to:

```python
@router.get("/subscriptions")
async def get_subscriptions_data(
    page: int = Query(1, ge=1, description="订阅来源分页页码"),
    page_size: int = Query(24, ge=1, le=120, description="每页订阅来源数量"),
):
    return await run_web_io(subscription_manager.list_payload, page=page, page_size=page_size)
```

- [ ] **Step 6: Run backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_subscriptions.py::SubscriptionManagerTest::test_list_payload_paginates_subscription_source_cards_only tests/test_subscriptions.py::SubscriptionManagerTest::test_list_payload_subscription_source_last_page_has_no_more -q
```

Expected: PASS.

- [ ] **Step 7: Commit backend contract**

Run:

```bash
git add app/services/subscriptions.py app/api/subscriptions_routes.py tests/test_subscriptions.py
git commit -m "feat: paginate subscription source payload"
```

## Task 2: Frontend Payload Normalization and Shape Tests

**Files:**
- Modify: `frontend/src/lib/subscriptions.js`
- Add: `frontend/src/lib/subscriptionsPageShape.test.mjs`

- [ ] **Step 1: Write failing frontend shape tests**

Create `frontend/src/lib/subscriptionsPageShape.test.mjs`:

```javascript
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { normalizeSubscriptionsPayload } from "./subscriptions.js";

const pageSource = readFileSync(new URL("../pages/SubscriptionsPage.vue", import.meta.url), "utf8");

test("subscription payload normalization preserves section pagination metadata", () => {
  const payload = normalizeSubscriptionsPayload({
    sections: [
      {
        key: "subscription_sources",
        items: [{ key: "source-1" }],
        count: 1,
        total: 25,
        page: 1,
        page_size: 24,
        has_more: true,
      },
    ],
  });

  assert.equal(payload.sections[0].total, 25);
  assert.equal(payload.sections[0].page, 1);
  assert.equal(payload.sections[0].page_size, 24);
  assert.equal(payload.sections[0].has_more, true);
});

test("subscriptions page requests paged subscription payloads and exposes load more", () => {
  assert.match(pageSource, /PAGE_SIZE\s*=\s*24/);
  assert.match(pageSource, /routePage\(/);
  assert.match(pageSource, /page_size/);
  assert.match(pageSource, /loadMoreSubscriptionSources/);
  assert.match(pageSource, />\s*加载更多\s*</);
  assert.match(pageSource, /全选(?:当前)?已加载/);
});
```

- [ ] **Step 2: Run frontend shape tests to verify failure**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs
```

Expected: FAIL because `SubscriptionsPage.vue` has no paging functions or load-more text yet.

- [ ] **Step 3: Make section metadata normalization explicit**

In `frontend/src/lib/subscriptions.js`, update `normalizeSubscriptionsPayload()` so `sections` maps through a helper:

```javascript
function normalizeSubscriptionSection(section = {}) {
  const items = Array.isArray(section.items) ? section.items : [];
  return {
    ...section,
    items,
    count: Number(section.count ?? items.length),
    total: Number(section.total ?? items.length),
    page: Number(section.page || 1),
    page_size: Number(section.page_size || items.length || 0),
    has_more: Boolean(section.has_more),
  };
}
```

Then replace:

```javascript
    sections: Array.isArray(response.sections) ? response.sections : [],
```

with:

```javascript
    sections: Array.isArray(response.sections) ? response.sections.map(normalizeSubscriptionSection) : [],
```

- [ ] **Step 4: Run normalization test**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs
```

Expected: still FAIL on page-source assertions until Task 3 updates the page.

## Task 3: Subscription Page Load-More UX

**Files:**
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Test: `frontend/src/lib/subscriptionsPageShape.test.mjs`

- [ ] **Step 1: Add route and paging state imports**

Change the import from `vue-router`:

```javascript
import { RouterLink, useRoute, useRouter } from "vue-router";
```

Create route and constants near existing `router` setup:

```javascript
const route = useRoute();
const router = useRouter();
const PAGE_SIZE = 24;
```

Keep the existing `router` constant as the same `useRouter()` instance.

- [ ] **Step 2: Add paging state**

Near the existing refs add:

```javascript
const loadingMore = ref(false);
let requestToken = 0;
```

Add helpers:

```javascript
function routePage() {
  const rawPage = Array.isArray(route.query.page) ? route.query.page[0] : route.query.page;
  const page = Number.parseInt(String(rawPage || ""), 10);
  if (!Number.isFinite(page) || page <= 1) {
    return 1;
  }
  return Math.min(page, 200);
}

function buildRouteQuery(page = 1) {
  const query = {};
  const safePage = Math.max(Number(page) || 1, 1);
  if (safePage > 1) {
    query.page = String(Math.floor(safePage));
  }
  return query;
}

function subscriptionSourcesSection(sourcePayload = payload.value) {
  return (sourcePayload.sections || []).find((section) => section?.key === "subscription_sources") || null;
}

function mergeSubscriptionSourceItems(existing = [], incoming = []) {
  const merged = [];
  const seen = new Set();
  for (const item of [...existing, ...incoming]) {
    const key = String(item?.key || "").trim();
    if (!key || seen.has(key)) {
      continue;
    }
    seen.add(key);
    merged.push(item);
  }
  return merged;
}

function replaceSubscriptionSourcesSection(basePayload, items, sectionMeta) {
  const sections = (basePayload.sections || []).map((section) => (
    section?.key === "subscription_sources"
      ? {
          ...section,
          ...sectionMeta,
          items,
          count: items.length,
        }
      : section
  ));
  return {
    ...basePayload,
    sections,
  };
}
```

- [ ] **Step 3: Add fetch and cache support**

Change `rememberSubscriptionsPage()` to cache the loaded page:

```javascript
function rememberSubscriptionsPage() {
  setPageCache("subscriptions", {
    payload: payload.value,
    page: Number(subscriptionSourcesSection()?.page || 1),
  });
}
```

Change `hydrateSubscriptionsPageFromCache()` so it also restores route page if needed:

```javascript
function hydrateSubscriptionsPageFromCache() {
  const cached = getPageCache("subscriptions");
  if (!cached?.payload) {
    return false;
  }
  payload.value = normalizeSubscriptionsPayload(cached.payload);
  initialLoaded.value = true;
  return true;
}
```

Add:

```javascript
function buildSubscriptionsQuery(page = 1) {
  const query = new URLSearchParams();
  query.set("page", String(Math.max(Number(page) || 1, 1)));
  query.set("page_size", String(PAGE_SIZE));
  return query;
}

async function fetchSubscriptionsPage(page = 1) {
  return normalizeSubscriptionsPayload(await apiRequest(`/api/subscriptions?${buildSubscriptionsQuery(page).toString()}`));
}
```

- [ ] **Step 4: Replace `load()` with visible-page reload**

Replace the current `load()` implementation with:

```javascript
async function load({ silent = false, pages = routePage() } = {}) {
  const currentToken = ++requestToken;
  const pagesToLoad = Math.max(Number(pages) || 1, 1);
  try {
    let mergedPayload = null;
    let mergedItems = [];
    let latestSection = null;
    for (let page = 1; page <= pagesToLoad; page += 1) {
      const response = await fetchSubscriptionsPage(page);
      if (currentToken !== requestToken) {
        return;
      }
      const section = subscriptionSourcesSection(response);
      mergedItems = mergeSubscriptionSourceItems(mergedItems, section?.items || []);
      latestSection = section || latestSection;
      mergedPayload = response;
    }
    if (!mergedPayload) {
      return;
    }
    payload.value = replaceSubscriptionSourcesSection(
      mergedPayload,
      mergedItems,
      {
        ...(latestSection || {}),
        page: pagesToLoad,
        page_size: PAGE_SIZE,
        has_more: Boolean(latestSection?.has_more),
        total: Number(latestSection?.total || mergedItems.length),
      },
    );
    initialLoaded.value = true;
    pruneSelectionsToLoadedCards();
    rememberSubscriptionsPage();
  } catch (error) {
    if (!silent) {
      status.value = error instanceof Error ? error.message : "订阅数据加载失败。";
    }
  }
}
```

- [ ] **Step 5: Add load-more behavior**

Add computed values:

```javascript
const subscriptionSources = computed(() => subscriptionSourcesSection());
const hasMoreSubscriptionSources = computed(() => Boolean(subscriptionSources.value?.has_more));
```

Add:

```javascript
async function updateRoutePage(page) {
  await router.replace({
    path: route.path,
    query: buildRouteQuery(page),
  });
}

async function loadMoreSubscriptionSources() {
  if (loadingMore.value || !hasMoreSubscriptionSources.value) {
    return;
  }
  const nextPage = Math.max(Number(subscriptionSources.value?.page || 1), 1) + 1;
  loadingMore.value = true;
  try {
    const response = await fetchSubscriptionsPage(nextPage);
    const incomingSection = subscriptionSourcesSection(response);
    const mergedItems = mergeSubscriptionSourceItems(subscriptionSources.value?.items || [], incomingSection?.items || []);
    payload.value = replaceSubscriptionSourcesSection(
      response,
      mergedItems,
      {
        ...(incomingSection || {}),
        page: nextPage,
        page_size: PAGE_SIZE,
        has_more: Boolean(incomingSection?.has_more),
        total: Number(incomingSection?.total || mergedItems.length),
      },
    );
    pruneSelectionsToLoadedCards();
    rememberSubscriptionsPage();
    await updateRoutePage(nextPage);
  } catch (error) {
    status.value = error instanceof Error ? error.message : "加载更多订阅来源失败。";
  } finally {
    loadingMore.value = false;
  }
}
```

- [ ] **Step 6: Preserve refresh depth and reset after create**

Change state-event callback from:

```javascript
void load({ silent: true });
```

to:

```javascript
void load({ silent: true, pages: Number(subscriptionSources.value?.page || routePage()) });
```

After creating a subscription, before `await load({ silent: true });`, add:

```javascript
await updateRoutePage(1);
```

and call:

```javascript
await load({ silent: true, pages: 1 });
```

- [ ] **Step 7: Add load-more footer**

After the source grid section in the template, add:

```vue
      <div v-if="section.key === 'subscription_sources' && section.items?.length" class="list-loader-anchor">
        <button
          v-if="section.has_more"
          class="button button-secondary"
          type="button"
          :disabled="loadingMore"
          @click="loadMoreSubscriptionSources"
        >
          {{ loadingMore ? "加载中..." : "加载更多" }}
        </button>
        <span v-else>已经到底了</span>
      </div>
```

Place it after the `source-library-grid` div and before the empty-state branch.

- [ ] **Step 8: Run frontend shape test**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs
```

Expected: PASS.

## Task 4: Selection Behavior and Regression Verification

**Files:**
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Test: `frontend/src/lib/subscriptionsPageShape.test.mjs`

- [ ] **Step 1: Update selection copy**

Change the select-all button text from:

```vue
全选当前
```

to:

```vue
全选当前已加载
```

- [ ] **Step 2: Prune stale selections after reload**

Add:

```javascript
function pruneSelectionsToLoadedCards() {
  const loadedKeys = new Set(shareableCards.value.map((card) => String(card.key || "")).filter(Boolean));
  const nextSet = new Set();
  for (const key of selectedCardKeySet.value) {
    if (loadedKeys.has(key)) {
      nextSet.add(key);
    }
  }
  selectedCardKeySet.value = nextSet;
}
```

This function is called by the Task 3 load paths.

- [ ] **Step 3: Run frontend shape test**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs
```

Expected: PASS.

- [ ] **Step 4: Run existing frontend subscriptions-adjacent tests**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs src/lib/modelNavigation.test.mjs
```

Expected: PASS.

- [ ] **Step 5: Commit frontend pagination**

Run:

```bash
git add frontend/src/lib/subscriptions.js frontend/src/pages/SubscriptionsPage.vue frontend/src/lib/subscriptionsPageShape.test.mjs
git commit -m "feat: add subscription source load more"
```

## Task 5: Full Verification

**Files:**
- No code edits unless verification reveals defects.

- [ ] **Step 1: Run targeted backend tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_subscriptions.py tests/test_source_library.py -q
```

Expected: PASS.

- [ ] **Step 2: Run targeted frontend tests**

Run:

```bash
node --test src/lib/subscriptionsPageShape.test.mjs src/lib/modelNavigation.test.mjs
```

from `frontend/`.

Expected: PASS.

- [ ] **Step 3: Build frontend**

Run:

```bash
npm run build
```

from `frontend/`.

Expected: PASS with Vite build output.

- [ ] **Step 4: Check diff hygiene**

Run:

```bash
git diff --check
git status --short --branch
```

Expected: no whitespace errors and only intended committed or uncommitted files.

- [ ] **Step 5: Release note and version bump when pushing**

Only when the user asks to push, bump patch version and update `README.md` latest release notes. The expected version after `v0.9.1` is `v0.9.2`.
