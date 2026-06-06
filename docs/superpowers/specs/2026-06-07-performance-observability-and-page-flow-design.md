# MakerHub Performance Observability and Page Flow Design

Date: 2026-06-07
Status: Approved design for implementation planning

## Purpose

Recent fixes made login, global bootstrap, and the dashboard faster, but MakerHub still needs a repeatable way to answer: which page, API, or user action is slow on the live instance? This design adds lightweight performance observability for AI/operator inspection and fixes the most obvious page-flow bottlenecks without adding user-facing performance UI.

## Goals

- Record enough timing data for an operator or AI agent to diagnose slow API requests and slow page loads from logs or local state.
- Avoid adding new visible controls or charts to the log center.
- Reduce known avoidable serial request patterns in model and subscription pages.
- Split heavy settings-page data so the settings screen can show useful content before slower diagnostics finish.
- Keep performance logging low-volume and safe for credentials.

## Non-Goals

- No full APM system, tracing backend, dashboards, or external service integration.
- No user-facing performance page.
- No broad redesign of pages.
- No speculative optimization of archive or remote-refresh worker internals beyond request/page flow.

## Current Context

- `/api/bootstrap` was already reduced to a session snapshot and current app version.
- Login now uses SPA navigation instead of waiting for full bootstrap and a page reload.
- Dashboard source health prefers cached snapshots and refreshes in the background.
- Model list and subscription list preserve previously loaded cards, but returning to page N can still cause serial page requests from page 1 through page N.
- Settings still loads a broad `/api/config` payload that includes heavy diagnostics and state summaries.
- Existing logs are structured enough for AI/operator inspection, but there is no consistent request/page timing event.

## Design Overview

The implementation has two tracks:

1. Lightweight timing observability
2. Direct page-flow optimizations for known slow paths

These should be delivered together so each optimization can be validated with newly available timing data.

## Backend Timing Events

Add API request timing at the existing HTTP middleware boundary.

For each `/api/*` request:

- Measure elapsed time around `call_next`.
- Record only slow requests and failed requests by default.
- Use thresholds:
  - GET: slow when `duration_ms >= 800`
  - POST/PUT/PATCH/DELETE: slow when `duration_ms >= 1500`
- Use higher threshold or sampling for high-frequency endpoints:
  - `/api/logs`
  - `/api/state-events`
  - other event-stream/poll endpoints if present

Persist events through the existing structured logging path with:

- `category`: `performance`
- `event`: `slow_api_request` or `api_error_request`
- `method`
- `path`
- `status_code`
- `duration_ms`
- `query_keys`
- `response_size`

Do not record query values, request bodies, cookies, headers, tokens, or user-entered search text.

If timing-log persistence fails, it must not fail the API request.

## Frontend Timing Events

Add a small performance helper used by `apiRequest` and selected pages.

API request timing:

- Measure browser-side API duration in `apiRequest`.
- Track per-page counters in memory: total API count, slow API count, and max API duration.
- Do not report every request.

Page timing:

- Selected pages report slow initial loads only:
  - dashboard
  - models
  - model group
  - subscriptions
  - settings
  - tasks
  - logs
  - remote refresh
  - organizer
- Report only when the initial load exceeds a threshold, initially `1200ms`.
- Payload fields:
  - `page`
  - `route`
  - `duration_ms`
  - `api_count`
  - `slow_api_count`
  - `max_api_duration_ms`

Add a backend endpoint such as `/api/performance/events` to accept slow frontend timing events. It should sanitize payloads and write `category=performance`, `event=slow_page_load`.

This endpoint is for operator/AI diagnostics only. No UI is added.

## Model List Flow

Problem:

When opening `/models?page=N`, the frontend currently can request page 1, then 2, through N sequentially to reconstruct loaded card state.

Design:

- Add a backend-supported `limit` or `include_until_page` mode for `/api/models`.
- When page N is requested with this mode, backend returns the first `N * page_size` items in one response while preserving:
  - `page`
  - `page_size`
  - `has_more`
  - `filtered_total`
  - current filters and sort
- Frontend uses one request for initial restoration to page N.
- Infinite scroll still loads page N+1 normally.

This mirrors the existing group-page `limit` idea and avoids multiple serial network round trips.

## Subscription List Flow

Problem:

The subscription library page can also loop from page 1 through page N to restore loaded cards.

Design:

- Add a matching `limit` or `include_until_page` mode to `/api/subscriptions`.
- The backend returns subscription source cards up to `N * page_size` in one response.
- Frontend replaces serial page restoration with a single restoration request.
- Auto-load-more behavior remains unchanged.

## Model Group Flow

The model group page already has a `limit` parameter in the route. Verify its frontend path consistently uses this for page restoration and does not fall back to serial page requests. If gaps exist, align it with the model list behavior.

## Settings Page Flow

Problem:

`refreshConfig()` pulls a broad `/api/config` payload. Some data is useful immediately, while diagnostics and runtime summaries can lag behind.

Design:

- Keep `/api/config` compatible, but add or reuse lighter endpoints for heavy sections.
- Settings page first renders base configuration needed for forms and navigation.
- Load slower sections in the background:
  - GitHub/system update status
  - database status and migration markers
  - sharing records
  - remote refresh runtime state
  - other heavy inventories if confirmed by timing logs
- Save operations continue to return updated config payloads where needed.
- Existing form behavior must not regress.

## Tasks and Remote Refresh Pages

Initial change should be investigative and conservative:

- Use timing logs to confirm whether `/api/tasks` or `/api/remote-refresh` is slow.
- Do not split historical arrays in the first implementation pass.
- If timing data later proves these endpoints are slow due to large historical arrays, return summaries first and page historical lists separately in a follow-up change.
- Do not change task execution semantics.

## Error Handling

- Timing collection must be best-effort.
- Slow-event upload failures are ignored on the frontend.
- Backend timing logging must never expose sensitive data.
- Existing API error behavior remains unchanged.

## Testing

Backend tests:

- Slow API request emits a sanitized performance log.
- Fast successful API request does not emit a performance log by default.
- Failed API request emits an error performance log.
- `/api/models` limit/include-until-page returns correct counts and paging metadata.
- `/api/subscriptions` limit/include-until-page returns correct source card metadata.

Frontend tests:

- `apiRequest` exposes timing hooks without changing response/error behavior.
- Model list no longer serially loops page 1..N on initial page restoration.
- Subscription list no longer serially loops page 1..N on initial page restoration.
- Slow page load report is best-effort and non-blocking.

Build/verification:

- Python targeted pytest for performance, models, subscriptions, auth/bootstrap regressions.
- Node shape/unit tests for page flow.
- `npm run build`.
- `git diff --check`.

## Rollout

- Keep default logging sparse: slow and failed events only.
- Use existing logs or local state for AI/operator inspection.
- After deployment, inspect recent `performance` events from the live instance before deciding the next optimization batch.

## Open Decisions Resolved

- No user-facing log-center changes.
- No external telemetry.
- Thresholds are fixed initially and can later move into config only if needed.
