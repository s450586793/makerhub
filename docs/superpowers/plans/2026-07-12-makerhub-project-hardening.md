# MakerHub Project Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve audit findings F01-F16 with secure defaults, atomic state, bounded queries, simplified page loading, and rollback-safe updates.

**Architecture:** Keep App + Worker + Postgres and the current legacy archive managers. Add process-local pooling and transactional JSON-state primitives underneath existing APIs, turn light payloads into final projections, freeze the incomplete Runtime Engine, and update all managed containers as one release unit.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3/psycopg_pool, Postgres 16, Vue 3, Node test runner, Docker Compose, GitHub Actions.

## Global Constraints

- Preserve existing API paths and the default legacy archive workflow.
- Do not modify or commit `videos/makerhub-intro/output/`.
- Add a failing regression test before each behavior change.
- Use one focused Chinese commit per task and never push without an explicit request.
- Keep old secure passwords, sessions, cookies, subscriptions, and token hashes compatible.
- Keep full endpoints for one compatibility release even when the frontend stops calling them.
- Version and release notes are updated once, after all behavior and regression tests pass.

## File Map

- `app/core/database.py`: pooled connections, JSON-state revisions, multi-key reads, retention SQL.
- `app/core/database_json_state.py`: retrying typed wrappers for load/update operations.
- `app/core/store.py`: atomic validated AppConfig update and compatibility conflict handling.
- `app/services/auth.py`: shared login throttling, bootstrap password migration, hash-only API token lifecycle.
- `app/services/task_state.py`: dynamic state-key lookup and bulk archive enqueue.
- `app/services/resource_limiter.py`: process-wide file-backed resource slots.
- `app/services/archive_model_index.py`: SQL model projection and facet revision.
- `app/services/catalog.py`: SQL-first light payload with file/index fallback.
- `frontend/src/lib/useHydratedResource.js`: latest-wins final-projection controller.
- `app/services/self_update.py`: release-group prepare, activate, verify, commit, rollback.
- `compose.yaml`: canonical secure deployment and healthchecks.

---

### Task 1: Restore The Test Baseline

**Files:**
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`
- Test: `tests/test_remote_refresh.py`
- Test: `tests/test_subscriptions.py`

**Interfaces:**
- Produces: `_json_state_key_for_path(path: Path) -> str`, resolved from current module path constants rather than an import-time path snapshot.

- [ ] **Step 1: Add a failing dynamic-path regression**

```python
def test_runtime_state_key_follows_replaced_module_path():
    original = task_state.REMOTE_REFRESH_STATE_PATH
    try:
        task_state.REMOTE_REFRESH_STATE_PATH = Path("/tmp/replaced-remote.json")
        assert task_state._json_state_key_for_path(task_state.REMOTE_REFRESH_STATE_PATH) == REMOTE_REFRESH_STATE_KEY
    finally:
        task_state.REMOTE_REFRESH_STATE_PATH = original
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_task_state.py -k runtime_state_key_follows -q`

Expected: failure because `_JSON_STATE_KEYS` still contains the original resolved path.

- [ ] **Step 3: Resolve state keys dynamically**

Replace the static path-key map with a function that builds the map from the nine current module constants on each lookup. Keep unknown paths rejected.

- [ ] **Step 4: Verify the repaired baseline**

Run: `.venv/bin/python -m pytest tests/test_task_state.py tests/test_remote_refresh.py tests/test_subscriptions.py -q`

Expected: all tests pass, including the previous 48 failures.

- [ ] **Step 5: Commit**

```bash
git add app/services/task_state.py tests/test_task_state.py
git commit -m "test: 修复运行状态测试路径隔离"
```

### Task 2: Add Pooled And Atomic Database Primitives

**Files:**
- Modify: `requirements.txt`
- Modify: `app/core/database.py`
- Modify: `app/core/database_json_state.py`
- Modify: `app/core/store.py`
- Modify: `app/main.py`
- Modify: `app/worker.py`
- Test: `tests/test_database_json_state.py`
- Create: `tests/test_database_pool.py`

**Interfaces:**
- Produces: `close_database_pool() -> None`.
- Produces: `load_json_states(keys: Iterable[str]) -> dict[str, Any]`.
- Produces: `update_json_state(key, default, mutator, expected_revision=None) -> tuple[Any, int]`.
- Produces: `JsonStore.update(mutator, expected_revision=None) -> AppConfig`.

- [ ] **Step 1: Write pool lifecycle tests**

```python
def test_database_connection_reuses_one_pid_scoped_pool(monkeypatch):
    factory = MockPoolFactory()
    monkeypatch.setattr(database, "ConnectionPool", factory)
    with database.database_connection():
        pass
    with database.database_connection():
        pass
    assert factory.created == 1
    assert factory.connection_checkouts == 2
```

Also cover rollback, URL/PID change, close idempotency, and an error that does not include the DSN.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_database_pool.py -q`

Expected: import/signature failures because pool APIs do not exist.

- [ ] **Step 3: Add `psycopg_pool` and preserve the connection context contract**

Use lazy `ConnectionPool(min_size=0, max_size=env, timeout=env, open=False)` keyed by `(pid, database_url)`. Continue explicit commit/rollback in `database_connection()` and leave LISTEN/NOTIFY listener connections dedicated.

- [ ] **Step 4: Write atomic update tests**

```python
def test_json_store_update_preserves_parallel_unrelated_changes():
    first = store.update(lambda cfg: cfg.model_copy(update={"user": changed_user}))
    second = store.update(lambda cfg: cfg.model_copy(update={"proxy": changed_proxy}))
    saved = store.load()
    assert saved.user == first.user
    assert saved.proxy == second.proxy
```

Cover file/database equivalence, expected revision conflict, validation failure rollback, and mutator called once.

- [ ] **Step 5: Verify RED, then implement revision-backed update**

Run: `.venv/bin/python -m pytest tests/test_database_json_state.py -k "atomic or revision or concurrent" -q`

Add `revision BIGINT NOT NULL DEFAULT 0`, lock the row with `FOR UPDATE`, update revision once, and validate `AppConfig` before commit. `save()` keeps compatibility through a three-way merge and raises a typed conflict on same-field changes.

- [ ] **Step 6: Close pools on App and Worker shutdown**

Register `close_database_pool()` in FastAPI lifespan cleanup and the Worker `finally` path.

- [ ] **Step 7: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_database_pool.py tests/test_database_json_state.py -q`

```bash
git add requirements.txt app/core/database.py app/core/database_json_state.py app/core/store.py app/main.py app/worker.py tests/test_database_pool.py tests/test_database_json_state.py
git commit -m "refactor: 增加数据库连接池与原子状态更新"
```

### Task 3: Secure Authentication And API Tokens

**Files:**
- Modify: `app/core/security.py`
- Modify: `app/schemas/models.py`
- Modify: `app/services/auth.py`
- Modify: `app/api/auth.py`
- Modify: `app/api/config.py`
- Modify: `app/main.py`
- Modify: `docker/entrypoint.sh`
- Modify: `frontend/src/pages/SettingsPage.vue`
- Modify: `frontend/src/lib/settingsPayloads.js`
- Test: `tests/test_auth_guard.py`
- Test: `tests/test_config_payloads.py`
- Test: `tests/test_database_json_state.py`
- Test: `frontend/src/lib/settingsPayloads.test.mjs`

**Interfaces:**
- Produces: `ensure_secure_admin_credential(store) -> BootstrapCredentialResult`.
- Produces: `AuthManager.login_failure_key(request, username)` based only on normalized `request.client.host`.
- Produces: token create response `{token, item}`, list response without plaintext.

- [ ] **Step 1: Add RED tests for fresh and legacy credentials**

```python
def test_fresh_config_never_accepts_shared_admin_password(tmp_path, monkeypatch):
    monkeypatch.delenv("MAKERHUB_ADMIN_PASSWORD", raising=False)
    store = JsonStore(tmp_path / "config.json")
    result = ensure_secure_admin_credential(store)
    assert not AuthManager(store).authenticate_credentials("admin", "admin")
    assert result.bootstrap_path.stat().st_mode & 0o077 == 0
```

Cover env bootstrap, legacy deterministic hash rotation, bootstrap file deletion after password change, and minimum 12-character password.

- [ ] **Step 2: Add RED proxy-header tests**

Assert two requests with different spoofed XFF but the same socket peer receive the same failure key. Assert entrypoint uses `--no-proxy-headers` unless a non-wildcard trusted-proxy list is configured.

- [ ] **Step 3: Implement secure bootstrap and trusted proxy startup**

Generate 24+ random characters with `secrets`, persist only PBKDF2 hash, write the one-time value to a `0600` state file, and remove deterministic defaults. Application code uses `request.client` and `request.url.scheme`, never raw forwarding headers.

- [ ] **Step 4: Add RED token lifecycle tests**

```python
raw, created = manager.create_api_token("CI")
assert created.token_value == raw
assert all(not item.token_value for item in manager.list_api_tokens())
assert all(not item.token_value for item in store.load().api_tokens)
assert manager.validate_api_token(raw) is not None
```

Cover old `token_value` cleanup while retaining `token_hash`, and creation response one-time reveal.

- [ ] **Step 5: Persist hash-only records and update the UI**

Create the record through `JsonStore.update()`. Return plaintext only in the create response; settings lists render prefix/status and the creation dialog owns the one-time copy action.

- [ ] **Step 6: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_auth_guard.py tests/test_config_payloads.py tests/test_database_json_state.py -q`

Run: `npm --prefix frontend test -- --test-name-pattern="token|settings"`

```bash
git add app/core/security.py app/schemas/models.py app/services/auth.py app/api/auth.py app/api/config.py app/main.py docker/entrypoint.sh frontend/src/pages/SettingsPage.vue frontend/src/lib/settingsPayloads.js frontend/src/lib/settingsPayloads.test.mjs tests/test_auth_guard.py tests/test_config_payloads.py tests/test_database_json_state.py
git commit -m "fix: 收紧管理员登录与 API Token 存储"
```

### Task 4: Secure CloakBrowser And Restore The Release Gate

**Files:**
- Modify: `app/services/cloakbrowser_session.py`
- Modify: `app/services/cloakbrowser_bridge.mjs`
- Modify: `compose.yaml`
- Modify: `compose.external-flaresolverr.yaml`
- Modify: `.github/workflows/docker.yml`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Create: `.dockerignore`
- Create: `scripts/check_release_version.py`
- Test: `tests/test_config_cloakbrowser.py`
- Test: `tests/test_cloakbrowser_session.py`
- Test: `tests/test_self_update.py`
- Create: `tests/test_release_contract.py`

**Interfaces:**
- Produces: `cloakbrowser_configured()` true only for URL plus token.
- Produces: `npm test` running `node --test src/lib/*.test.mjs`.
- Produces: tag-only GHCR release after all quality jobs succeed.

- [ ] **Step 1: Add RED security/Compose tests**

Assert an empty token makes CloakBrowser unavailable, all bridge calls contain Bearer auth, Compose requires the token, and the default bind expression contains `127.0.0.1`.

- [ ] **Step 2: Implement token-required browser integration**

Fail with `CloakBrowserUnavailable` before network I/O when token is absent. Use `${MAKERHUB_CLOAKBROWSER_AUTH_TOKEN:?set ...}` and `${MAKERHUB_CLOAKBROWSER_BIND_ADDRESS:-127.0.0.1}:9050:8080`.

- [ ] **Step 3: Add RED workflow contract tests**

Parse the workflow and assert PR/main/tag verification, release job only on matching `v*`, no image push on main, and no mutable bare version tag.

- [ ] **Step 4: Implement CI and build-context controls**

Run backend pytest, `npm ci`, `npm test`, build, Compose validation, version consistency, Docker build, then publish `vX.Y.Z`, `sha-*`, and promote `latest` only in the tag job. `.dockerignore` excludes secrets, runtime data, VCS, venvs, dependencies, builds, workflow scratch, worktrees, docs/tests, and video output while retaining Docker build inputs.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_config_cloakbrowser.py tests/test_cloakbrowser_session.py tests/test_release_contract.py tests/test_self_update.py -q`

Run: `npm --prefix frontend test && npm --prefix frontend run build`

```bash
git add app/services/cloakbrowser_session.py app/services/cloakbrowser_bridge.mjs compose.yaml compose.external-flaresolverr.yaml .github/workflows/docker.yml frontend/package.json frontend/package-lock.json .dockerignore scripts/check_release_version.py tests/test_config_cloakbrowser.py tests/test_cloakbrowser_session.py tests/test_self_update.py tests/test_release_contract.py
git commit -m "fix: 收紧浏览器服务与镜像发布门禁"
```

### Task 5: Batch Archive Enqueue And Global Resource Slots

**Files:**
- Modify: `app/services/task_state.py`
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/resource_limiter.py`
- Modify: `app/services/process_jobs.py`
- Test: `tests/test_task_state.py`
- Test: `tests/test_archive_worker_speedup.py`
- Test: `tests/test_resource_limiter.py`
- Test: `tests/test_process_jobs.py`

**Interfaces:**
- Produces: `TaskStateStore.enqueue_archive_tasks(items) -> dict` with per-item results and summary.
- Produces: file-backed resource slots behind the existing `resource_slot(name)` context API.

- [ ] **Step 1: Add RED bulk-enqueue tests**

Cover batch-internal duplicates, queued/active duplicates, 3MF instance merging, stable existing IDs, and 1,000 input items causing exactly one save and one event.

- [ ] **Step 2: Implement bulk enqueue and delegate single enqueue**

Build one identity map, update it while walking inputs, write/decorate once, and return `{items, enqueued_count, merged_count, duplicate_count}`. Change batch discovery to call the bulk method once.

- [ ] **Step 3: Add RED real-process limiter tests**

Spawn more processes than capacity, record a shared active counter, assert `max_active <= capacity`, kill a holder and assert its slot becomes available, then cover shrink drain semantics.

- [ ] **Step 4: Add numbered `fcntl` slot files**

Acquire the process-local FIFO slot first, then nonblocking-scan global numbered lock files with bounded polling. Pass current advanced limits in the spawn payload and configure the child before work starts.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_task_state.py tests/test_archive_worker_speedup.py tests/test_resource_limiter.py tests/test_process_jobs.py -q`

```bash
git add app/services/task_state.py app/services/archive_worker.py app/services/resource_limiter.py app/services/process_jobs.py tests/test_task_state.py tests/test_archive_worker_speedup.py tests/test_resource_limiter.py tests/test_process_jobs.py
git commit -m "refactor: 批量写入归档队列并统一资源槽"
```

### Task 6: Add Bounded Database Retention

**Files:**
- Modify: `app/core/database.py`
- Create: `app/services/database_maintenance.py`
- Modify: `app/worker.py`
- Modify: `app/services/business_logs.py`
- Create: `tests/test_database_maintenance.py`
- Modify: `tests/test_business_logs.py`

**Interfaces:**
- Produces: `cleanup_expired_rows(event_days=14, log_days=90, batch_size=1000, max_batches=10) -> dict`.
- Produces: `run_database_maintenance_if_due(now=None) -> dict`.

- [ ] **Step 1: Add RED cleanup tests**

Assert cutoff is strict, each delete is bounded, `0` disables a table, event ID gaps do not break `list_state_events_after`, and one table failure does not skip the other.

- [ ] **Step 2: Implement indexed batched deletes and daily Worker scheduling**

Delete IDs selected by a CTE with `LIMIT`, add the global `(created_at, id)` log index, clamp environment values, and avoid explicit VACUUM or recursive per-batch logs.

- [ ] **Step 3: Add a bounded facet cache**

Cache log facet aggregates for five seconds and invalidate on new log writes/retention cleanup.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_database_maintenance.py tests/test_business_logs.py tests/test_state_contracts.py -q`

```bash
git add app/core/database.py app/services/database_maintenance.py app/worker.py app/services/business_logs.py tests/test_database_maintenance.py tests/test_business_logs.py
git commit -m "feat: 增加日志与状态事件保留策略"
```

### Task 7: Freeze The Incomplete Runtime Engine

**Files:**
- Modify: `app/api/runtime_routes.py`
- Modify: `app/api/tasks_routes.py`
- Modify: `app/api/subscriptions_routes.py`
- Modify: `app/api/remote_refresh_routes.py`
- Modify: `app/worker.py`
- Modify: `app/services/runtime_diagnostics.py`
- Test: `tests/test_runtime_engine_api.py`
- Test: `tests/test_runtime_diagnostics.py`
- Test: `tests/test_runtime_engine_source_refresh_adapter.py`
- Test: `tests/test_runtime_engine_subscription_adapter.py`

**Interfaces:**
- Produces: read-only runtime payload `{enabled: false, writable: false, ...snapshots}`.
- Produces: every runtime POST returns HTTP 503 without state mutation.

- [ ] **Step 1: Add RED disabled-write tests**

For every old truthy environment value, assert POST submit/pause/resume/cancel/retry/repair returns 503 and the runtime store is untouched. Assert GET does not call `repair()`.

- [ ] **Step 2: Remove runtime execution branches**

Make compatibility routes always call legacy managers and remove the Worker tick. Keep only bounded read diagnostics and a clear disabled reason.

- [ ] **Step 3: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_runtime_engine_api.py tests/test_runtime_diagnostics.py tests/test_runtime_engine_source_refresh_adapter.py tests/test_runtime_engine_subscription_adapter.py -q`

```bash
git add app/api/runtime_routes.py app/api/tasks_routes.py app/api/subscriptions_routes.py app/api/remote_refresh_routes.py app/worker.py app/services/runtime_diagnostics.py tests/test_runtime_engine_api.py tests/test_runtime_diagnostics.py tests/test_runtime_engine_source_refresh_adapter.py tests/test_runtime_engine_subscription_adapter.py
git commit -m "fix: 冻结未完成的运行核心写入路径"
```

### Task 8: Push Model Filtering And Paging Into Postgres

**Files:**
- Modify: `app/services/archive_model_index.py`
- Modify: `app/services/catalog.py`
- Modify: `tests/test_archive_model_index.py`
- Modify: `tests/test_runtime_diagnostics.py`

**Interfaces:**
- Produces: `query_archive_model_index(q, source, tag, sort_key, page, page_size, limit) -> dict | None`.
- Produces: `load_archive_model_facets(revision, flags_signature) -> dict`.

- [ ] **Step 1: Add RED query contract tests**

Use a recording fake DB to assert page SQL contains `LIMIT/OFFSET`, current-page rows only, filtered count, all search/source/tag special cases, four sorts with stable `model_dir` tie-break, deep limit mode, and empty pages.

- [ ] **Step 2: Add index projection columns and revision invalidation**

Persist normalized `tags TEXT[]` and stats columns on upsert, add needed indexes, and bump a metadata revision after upsert/delete/rebuild.

- [ ] **Step 3: Implement SQL-first light payload**

Use relational columns for common filters and `EXISTS/unnest` for tags. Join/CTE model flags for special filters, decorate only returned rows, and return `None` on unavailable/unbootstrapped DB so the existing Python path remains the fallback.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_archive_model_index.py tests/test_runtime_diagnostics.py -q`

```bash
git add app/services/archive_model_index.py app/services/catalog.py tests/test_archive_model_index.py tests/test_runtime_diagnostics.py
git commit -m "perf: 将模型筛选分页下推到数据库"
```

### Task 9: Make Light Payloads Final And Narrow SSE Refreshes

**Files:**
- Create: `frontend/src/lib/useHydratedResource.js`
- Create: `frontend/src/lib/useHydratedResource.test.mjs`
- Modify: `frontend/src/lib/stateRefresh.js`
- Modify: `frontend/src/lib/stateRefresh.test.mjs`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/pages/ModelsPage.vue`
- Modify: `frontend/src/pages/TasksPage.vue`
- Modify: `frontend/src/pages/SubscriptionsPage.vue`
- Modify: `frontend/src/pages/OrganizerPage.vue`
- Modify: `app/services/catalog.py`
- Modify: `app/services/source_library.py`
- Modify: `app/services/subscriptions.py`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`
- Modify: `frontend/src/lib/subscriptionsPageShape.test.mjs`

**Interfaces:**
- Produces: `createHydratedResource({load, enrich?, merge?, cache?})` with revision/cancel/latest-wins semantics.
- Produces: `eventRules: [{scopes: string[], types: string[]}]` matcher.

- [ ] **Step 1: Add RED controller and page-shape tests**

Assert stale responses never win, cancellation is silent, each of five pages makes one initial primary request, and no idle callback starts a full request.

- [ ] **Step 2: Implement the shared final-projection controller**

Expose `load`, `cancel`, `enrich`, `revision`, and state callbacks. Enrichment is explicit only; normal mount/refresh resolves after the light/final payload.

- [ ] **Step 3: Complete final backend projections**

Add missing dashboard aggregates, task compact counts, subscription card summaries, and organizer local/state summaries. Keep full routes unchanged for compatibility.

- [ ] **Step 4: Replace page-local hydration schedulers**

Remove automatic full calls from Models, Dashboard, Tasks, Subscriptions, and Organizer. Task detail enrichment is user-triggered. Organizer stops requesting unused config during data refresh.

- [ ] **Step 5: Add and apply scoped event rules**

Subscriptions and Organizer ignore archive progress and refresh once on `archive.completed`/`archive.failed`; publish `source_library.changed` after source-library mutation.

- [ ] **Step 6: Verify and commit**

Run: `npm --prefix frontend test`

Run: `.venv/bin/python -m pytest tests/test_config_payloads.py tests/test_source_library.py tests/test_subscriptions.py tests/test_task_state.py -q`

```bash
git add frontend/src/lib/useHydratedResource.js frontend/src/lib/useHydratedResource.test.mjs frontend/src/lib/stateRefresh.js frontend/src/lib/stateRefresh.test.mjs frontend/src/pages/DashboardPage.vue frontend/src/pages/ModelsPage.vue frontend/src/pages/TasksPage.vue frontend/src/pages/SubscriptionsPage.vue frontend/src/pages/OrganizerPage.vue frontend/src/lib/pageRefreshShape.test.mjs frontend/src/lib/subscriptionsPageShape.test.mjs app/services/catalog.py app/services/source_library.py app/services/subscriptions.py
git commit -m "perf: 合并页面首屏请求并收窄状态刷新"
```

### Task 10: Remove Duplicate Source Fetches And Payloads

**Files:**
- Modify: `app/services/remote_refresh.py`
- Modify: `app/services/source_refresh_jobs.py`
- Modify: `app/api/remote_refresh_routes.py`
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `tests/test_remote_refresh.py`
- Modify: `tests/test_source_refresh.py`
- Modify: `tests/test_remote_refresh_summary.py`

**Interfaces:**
- Produces: one `run_source_refresh_model_job(..., download_assets=True)` invocation per model.
- Produces: source-refresh route containing only `config`, `state`, and read-only runtime diagnostics.

- [ ] **Step 1: Change the two-fetch assertion to RED single-fetch cases**

Cover changed, missing, unchanged, and failure cases. Assert one job invocation, unchanged assets are not redownloaded, and existing metadata survives failure.

- [ ] **Step 2: Execute metadata and asset planning once**

Run the existing job once with asset download enabled, consume its diff/result once, and finalize once. Remove the second call and intermediate full state write.

- [ ] **Step 3: Add RED compact route test, then remove unused queue projection**

Assert `/api/source-refresh` has no full `queue/items/runs` projection and the page continues to render from config/state.

- [ ] **Step 4: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_source_refresh.py tests/test_remote_refresh_summary.py -q`

Run: `npm --prefix frontend test -- --test-name-pattern="remote|source refresh"`

```bash
git add app/services/remote_refresh.py app/services/source_refresh_jobs.py app/api/remote_refresh_routes.py frontend/src/pages/RemoteRefreshPage.vue tests/test_remote_refresh.py tests/test_source_refresh.py tests/test_remote_refresh_summary.py
git commit -m "perf: 合并源端刷新抓取并缩小响应"
```

### Task 11: Complete Performance Milestones

**Files:**
- Modify: `frontend/src/lib/api.js`
- Modify: `frontend/src/lib/performance.js`
- Modify: `frontend/src/lib/api.test.mjs`
- Modify: `frontend/src/lib/performance.test.mjs`
- Modify: five page files from Task 9
- Modify: `app/services/performance.py`
- Modify: `tests/test_performance_logging.py`

**Interfaces:**
- Produces: request metrics `{ttfb_ms, body_parse_ms, total_ms, response_bytes, status}`.
- Produces: page metrics `{data_ready_ms, enrichment_ready_ms, max_ttfb_ms, max_parse_ms, max_total_ms}`.

- [ ] **Step 1: Add RED timing tests with controlled clocks**

Assert TTFB ends at response headers, total includes JSON parse, parse failure still records total/status, and final projection readiness is included in page payload.

- [ ] **Step 2: Instrument response parsing and page milestones**

Record only bounded numeric fields; do not clone large responses or include URL query/body. Mark each page ready after its final projection and optional diagnostics only after explicit enrichment.

- [ ] **Step 3: Whitelist and clamp backend metrics**

Persist the new numeric fields while retaining old duration fields for compatibility.

- [ ] **Step 4: Verify and commit**

Run: `npm --prefix frontend test`

Run: `.venv/bin/python -m pytest tests/test_performance_logging.py -q`

```bash
git add frontend/src/lib/api.js frontend/src/lib/performance.js frontend/src/lib/api.test.mjs frontend/src/lib/performance.test.mjs frontend/src/pages/DashboardPage.vue frontend/src/pages/ModelsPage.vue frontend/src/pages/TasksPage.vue frontend/src/pages/SubscriptionsPage.vue frontend/src/pages/OrganizerPage.vue app/services/performance.py tests/test_performance_logging.py
git commit -m "perf: 补齐响应解析与数据就绪埋点"
```

### Task 12: Make Self-Update A Release-Group Transaction

**Files:**
- Modify: `app/services/self_update.py`
- Modify: `app/api/system.py`
- Modify: `app/main.py`
- Modify: `app/worker.py`
- Modify: `compose.yaml`
- Modify: `tests/test_self_update.py`
- Modify: `tests/test_system_bootstrap.py`
- Create: `tests/test_health_ready.py`

**Interfaces:**
- Produces: `GET /api/public/health/ready` with DB/version/role readiness.
- Produces: Worker heartbeat containing version, start token, and fresh timestamp.
- Produces: `prepare_release_group`, `activate_release_group`, `rollback_release_group`, and `commit_release_group` helpers.

- [ ] **Step 1: Add RED public readiness tests**

Assert DB unavailable returns 503 even when the process is running, version/role are returned on success, and no secret/DSN appears in errors. Assert stale or wrong-token Worker heartbeat is not ready.

- [ ] **Step 2: Add RED release-group failure injection tests**

Inject Web/App/Worker prepare, start, readiness, and commit failures. For every pre-commit failure assert all old containers are restored, none were removed, candidates are removed, and status names the failed role.

- [ ] **Step 3: Split replacement into prepare/activate/verify/commit**

Pull distinct versioned images first. Keep old role containers under backup names, start all candidates under canonical names, probe App/Web HTTP and Worker DB heartbeat, then remove backups only after the whole group is healthy. Roll back in reverse order on any failure.

- [ ] **Step 4: Add canonical healthchecks**

Enable Postgres healthcheck and App/Worker dependency conditions. Add App HTTP healthcheck and Worker heartbeat command/contract. Cleanup failure after a successful commit is warning-only and retains stopped backups for delayed cleanup.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/python -m pytest tests/test_self_update.py tests/test_health_ready.py tests/test_system_bootstrap.py -q`

```bash
git add app/services/self_update.py app/api/system.py app/main.py app/worker.py compose.yaml tests/test_self_update.py tests/test_system_bootstrap.py tests/test_health_ready.py
git commit -m "fix: 将网页更新改为整组验证与回滚"
```

### Task 13: Converge Deployment Docs And Release Metadata

**Files:**
- Modify: `compose.external-flaresolverr.yaml`
- Modify: `app/services/self_update.py`
- Modify: `Dockerfile`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/modules/deployment_update.md`
- Modify: `docs/modules/core.md`
- Modify: `docs/modules/archive.md`
- Modify: `VERSION`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `tests/test_self_update.py`
- Modify: `tests/test_release_contract.py`

**Interfaces:**
- Produces: `compose.yaml` as the only full deployment definition.
- Produces: external FlareSolverr as an override and in-app example loaded from packaged canonical YAML.
- Produces: release `0.11.0` with README showing only the latest three releases directly.

- [ ] **Step 1: Add RED Compose parity and version tests**

Assert packaged migration YAML equals canonical YAML, override contains only differences, required security/readiness fields exist, and VERSION/frontend/README latest heading match.

- [ ] **Step 2: Package and load canonical Compose**

Copy `compose.yaml` in the image and have self-update diagnostics read that file. Convert external Compose to an override rather than a second full copy.

- [ ] **Step 3: Write migration/release documentation**

Document required admin/Cloak/Postgres secrets, trusted proxy opt-in, Cloak LAN binding, the first updater migration caveat, hash-only API tokens, runtime freeze, retention defaults, and rollback behavior. Keep only `0.11.0`, `0.10.3`, and `0.10.2` expanded in README.

- [ ] **Step 4: Bump all release metadata to `0.11.0`**

Update VERSION, frontend package and lock, CHANGELOG, and README once all behavior tests are green.

- [ ] **Step 5: Run complete verification**

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend audit --audit-level=high
.venv/bin/python scripts/check_release_version.py
```

Parse both Compose inputs with PyYAML and run `docker compose config -q` plus image smoke when Docker is available. Inspect `git diff --check`, `git status`, and ensure no video output is staged.

- [ ] **Step 6: Commit without pushing**

```bash
git add compose.external-flaresolverr.yaml app/services/self_update.py Dockerfile README.md CHANGELOG.md docs/modules/deployment_update.md docs/modules/core.md docs/modules/archive.md VERSION frontend/package.json frontend/package-lock.json tests/test_self_update.py tests/test_release_contract.py
git commit -m "chore: 发布 v0.11.0 项目治理更新"
```

## Plan Self-Review

- F01-F16 each map to Tasks 1-13 and to the design coverage table.
- Shared files have a single sequential owner: database/store (Task 2), task/archive worker (Task 5), catalog (Tasks 8 then 9), Worker (Tasks 2/6/7/12), self-update/Compose (Tasks 4 then 12/13).
- Runtime is intentionally frozen; there is no unsupported promise that legacy enqueue acknowledgment equals terminal completion.
- Queue table migration, Runtime Engine reactivation, and zero-downtime updates are explicitly outside this release because they require separate data migrations.
- The plan contains no unresolved implementation placeholders.

