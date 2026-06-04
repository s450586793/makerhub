# Subscription Source Pagination Design

## Goal

Add model-library-style incremental pagination to the subscription source card list on the subscription library page. The page should no longer render every author, collection, and favorites source card at once.

## Current State

The subscription page calls `GET /api/subscriptions` once and renders the `subscription_sources` section from the returned `sections` array. `SubscriptionManager.list_payload()` builds full subscription records, summary data, settings, and source overview sections in one payload.

The model library and source-library model views already use `page`, `page_size`, URL state, cache hydration, and incremental "load more" behavior. Subscription source pagination should follow that shape rather than introducing a separate interaction model.

## Requirements

- `GET /api/subscriptions` accepts `page` and `page_size` query parameters.
- Pagination applies only to the `subscription_sources` section.
- Existing top-level `items`, `count`, `summary`, and `settings` remain global and unpaged.
- The paged section includes `total`, `page`, `page_size`, and `has_more`.
- The frontend loads page 1 by default and appends additional pages when the user clicks "加载更多".
- The route query records the highest loaded page as `page`, matching the model library behavior.
- Returning to the subscription page restores cached loaded pages when possible.
- "全选当前" selects all shareable cards currently loaded on screen, not cards on unloaded pages.
- Existing selections remain selected after loading more cards. Newly loaded cards are not automatically selected.
- Creating a subscription resets to page 1 and reloads, because source ordering can change.
- State-event refresh reloads up to the currently loaded page so expanded lists do not collapse back to page 1.

## Backend Design

Update `SubscriptionManager.list_payload()` to accept:

- `page: int = 1`
- `page_size: int = 24`
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

- Define `PAGE_SIZE = 24`.
- Read `route.query.page`, clamp it to a practical maximum, and fetch pages from 1 through that page during initial load or refresh.
- Build API requests as `/api/subscriptions?page=<n>&page_size=24`.
- Merge `subscription_sources.items` across responses while preserving the latest global summary/settings from the newest response.
- Track `hasMoreSubscriptionSources` from the last response.
- Show a compact footer action below the grid when more cards are available.
- Clicking "加载更多" fetches the next page, appends its cards, updates route query, and writes the page cache.

The current cache key can remain subscription-scoped, but the cached payload should include the loaded page number. If route filters are later added, the cache key must include those filters; this design does not introduce filters.

## Selection Behavior

Selection remains keyed by source card key. The shareable card list uses the loaded source cards only. `selectAllShareableCards()` keeps its current meaning of "all cards currently available to the UI", and the button text should be updated from "全选当前" to "全选已加载" or "全选当前已加载".

When loading additional pages, do not clear existing selections. When a full reload occurs because state events changed, keep selections only if their card keys are still present in the reloaded payload.

## Error Handling

If loading an additional page fails, keep the existing loaded cards on screen and show the existing page status message. Do not drop the current list.

If page 1 fails during initial load, keep the current empty/loading behavior.

If the backend returns a page beyond the total, return an empty page with accurate `total` and `has_more: false`; the frontend should then stop showing the load-more action.

## Testing Plan

Backend tests:

- `SubscriptionManager.list_payload(page=2, page_size=24)` returns only the second slice for `subscription_sources`.
- `count`, `summary`, `settings`, and top-level `items` remain global.
- `subscription_sources.total` reflects the full section length and `has_more` is accurate.

Frontend tests:

- Subscription page source text includes `page_size`, route page parsing, and "加载更多".
- Selection copy says current loaded cards.
- Existing subscription normalization tests remain valid with section pagination metadata.

Manual verification:

- Open subscription library with more than one page of source cards.
- Confirm first page renders quickly.
- Click "加载更多" and verify cards append rather than replacing the list.
- Select cards, load more, and confirm previous selections remain.
- Trigger a subscription state refresh and confirm the loaded page depth is preserved.
