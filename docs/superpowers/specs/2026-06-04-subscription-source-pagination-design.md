# Subscription Source Pagination Design

## Goal

Add model-library-style incremental pagination to the subscription source card list on the subscription library page. The page should no longer render every author, collection, and favorites source card at once; it should show 8 source cards per batch and load the next batch automatically when the user reaches the bottom.

## Current State

The subscription page calls `GET /api/subscriptions` once and renders the `subscription_sources` section from the returned `sections` array. `SubscriptionManager.list_payload()` builds full subscription records, summary data, settings, and source overview sections in one payload.

The model library and source-library model views already use `page`, `page_size`, URL state, cache hydration, and incremental "load more" behavior. Subscription source pagination should follow that shape rather than introducing a separate interaction model.

## Requirements

- `GET /api/subscriptions` accepts `page` and `page_size` query parameters.
- Pagination applies only to the `subscription_sources` section.
- Existing top-level `items`, `count`, `summary`, and `settings` remain global and unpaged.
- The paged section includes `total`, `page`, `page_size`, and `has_more`.
- The frontend loads page 1 by default with exactly 8 source cards per page.
- Additional pages append 8 cards at a time.
- The bottom loader auto-triggers like the model library list when the sentinel nears the viewport.
- A manual "加载更多" button is only a fallback for environments without `IntersectionObserver`.
- The route query records the highest loaded page as `page`, matching the model library behavior.
- Returning to the subscription page restores cached loaded pages when possible.
- "全选当前" selects all shareable cards currently loaded on screen, not cards on unloaded pages.
- Existing selections remain selected after loading more cards. Newly loaded cards are not automatically selected.
- Creating a subscription resets to page 1 and reloads, because source ordering can change.
- State-event refresh reloads up to the currently loaded page so expanded lists do not collapse back to page 1.

## Backend Design

Update `SubscriptionManager.list_payload()` to accept:

- `page: int = 1`
- `page_size: int = 8`
- Optional `limit: int = 0` if implementation needs the same "load until page" pattern as source library endpoints.

The manager should still call `build_subscription_overview_payload()` once to get the complete sections. It should then locate the `subscription_sources` section, slice its `items`, and copy the section with pagination metadata:

- `items`: current page slice or combined slice if using a limit path
- `count`: visible item count
- `total`: total item count before slicing
- `page`: normalized page
- `page_size`: normalized page size
- `has_more`: whether more cards exist after the returned slice

All other sections pass through unchanged. The route `GET /api/subscriptions` forwards `page` and `page_size` to the manager.

## Frontend Design

Update `SubscriptionsPage.vue` to mirror the model list paging pattern:

- Define `PAGE_SIZE = 8`.
- Read `route.query.page`, clamp it to a practical maximum, and fetch pages from 1 through that page during initial load or refresh.
- Build API requests as `/api/subscriptions?page=<n>&page_size=8`.
- Merge `subscription_sources.items` across responses while preserving the latest global summary/settings from the newest response.
- Track `hasMoreSubscriptionSources` from the last response.
- Show a compact footer below the grid when at least one source card is loaded:
  - `正在加载更多订阅来源...` while loading.
  - `下拉到底自动加载下一页` while more pages exist.
  - `已经到底了` when there are no more pages.
- Use `IntersectionObserver` with the same near-bottom trigger shape as `ModelsPage.vue`. When the loader sentinel intersects, fetch the next page, append its cards, update the route query, and write the page cache.
- If `IntersectionObserver` is unavailable, show a compact `加载更多` button in the same footer so the page remains usable.

The current cache key can remain subscription-scoped, but the cached payload should include the loaded page number. If route filters are later added, the cache key must include those filters; this design does not introduce filters.

## Selection Behavior

Selection remains keyed by source card key. The shareable card list uses the loaded source cards only. `selectAllShareableCards()` keeps its current meaning of "all cards currently available to the UI", and the button text should be updated from "全选当前" to "全选已加载" or "全选当前已加载".

When loading additional pages, do not clear existing selections. When a full reload occurs because state events changed, keep selections only if their card keys are still present in the reloaded payload.

## Error Handling

If loading an additional page fails, keep the existing loaded cards on screen, show the existing page status message, and reconnect the bottom observer so the user can retry by scrolling after the error is visible.

If page 1 fails during initial load, keep the current empty/loading behavior.

If the backend returns a page beyond the total, return an empty page with accurate `total` and `has_more: false`; the frontend should then stop showing the load-more action.

## Testing Plan

Backend tests:

- `SubscriptionManager.list_payload(page=2, page_size=8)` returns only the second slice for `subscription_sources`.
- `count`, `summary`, `settings`, and top-level `items` remain global.
- `subscription_sources.total` reflects the full section length and `has_more` is accurate.

Frontend tests:

- Subscription page source text includes `PAGE_SIZE = 8`, route page parsing, `IntersectionObserver`, and the automatic loader copy.
- Selection copy says current loaded cards.
- Existing subscription normalization tests remain valid with section pagination metadata.

Manual verification:

- Open subscription library with more than one page of source cards.
- Confirm first page renders quickly.
- Scroll to the bottom and verify cards append automatically rather than replacing the list.
- In a browser without `IntersectionObserver`, use the fallback "加载更多" button and verify the same append behavior.
- Select cards, load more, and confirm previous selections remain.
- Trigger a subscription state refresh and confirm the loaded page depth is preserved.
