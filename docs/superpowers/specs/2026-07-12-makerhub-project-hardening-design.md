# MakerHub Project Hardening Design

Date: 2026-07-12

## Goal

Resolve all findings from audit `ANL-001` without replacing MakerHub's current
App + Worker + Postgres deployment model. The work must secure new and existing
deployments, restore a trustworthy release gate, remove avoidable database and
frontend work, and make runtime/update state transitions fail closed.

The implementation is complete only when every finding `F01` through `F16` has
an implementation, a focused regression test, or an explicit compatibility
guard that removes the unsafe behavior.

## Chosen Approach

Use incremental, independently reversible batches. This is preferred over a
big-bang rewrite because archive, source refresh, browser login, and web update
are already production workflows. It is also preferred over isolated hotfixes
because several findings share the same root causes: non-atomic JSON state,
duplicated projections, and mutable release inputs.

The batches are:

1. Secure defaults and release gate.
2. Atomic state and bounded backend work.
3. Query and frontend data-flow reduction.
4. Runtime and self-update correctness.
5. Deployment/documentation convergence and release verification.

Each batch starts with a failing test, lands as a focused commit, and leaves the
application runnable. No code or generated output under
`videos/makerhub-intro/output/` is part of this work.

## Security Design

### Initial administrator credential

Fresh persistent configuration must not contain the shared `admin/admin`
credential. `MAKERHUB_ADMIN_PASSWORD` is the supported non-interactive
bootstrap secret. The canonical Compose configuration requires it and passes it
to every MakerHub process that can initialize configuration.

For direct development/file-mode startup without that variable, MakerHub
generates a cryptographically random one-time password, stores only its hash,
and writes the plaintext once to a mode-`0600` bootstrap file under the state
directory. Startup output tells the operator where to read it. Once the user
changes the password, the bootstrap file is removed.

Existing non-default password hashes are preserved. Existing deterministic
`admin/admin` hashes are rotated through the same one-time bootstrap mechanism
before credential authentication is accepted, so upgrades do not keep the
public shared secret.

### Client address and login limiting

The socket peer is authoritative by default. Uvicorn starts with proxy headers
disabled. It enables its native trusted-proxy parser only when
`MAKERHUB_TRUSTED_PROXIES` contains an explicit IP/CIDR list. Application code
never reads `X-Forwarded-For` or `X-Forwarded-Proto` directly; it uses the
normalized request client and scheme. Login failures are stored in Postgres
with an atomic key update so multiple web workers share the same counter. A
bounded in-memory fallback remains for local tests without Postgres.

### CloakBrowser exposure

`MAKERHUB_CLOAKBROWSER_AUTH_TOKEN` is required by the canonical Compose file
and by the bridge. The host port binds to
`MAKERHUB_CLOAKBROWSER_BIND` with a default of `127.0.0.1`; a LAN address must
be chosen explicitly when browser management is opened to another workstation.
The MakerHub-to-CloakBrowser service call continues to use the private Compose
network and the same token.

### API tokens

API token plaintext is returned only by the create response. Persisted records
and list responses contain only hash, prefix, permissions, and lifecycle
metadata. Loading old records removes `token_value`; those tokens remain valid
because validation already uses the hash.

## State And Database Design

### Connection lifecycle

Use one lazily opened `psycopg_pool.ConnectionPool` per process. The existing
`database_connection()` context manager remains the public boundary and keeps
commit/rollback semantics, so call sites do not need to own connections.
Connection pools close during application/worker shutdown and are resettable in
tests.

Multi-key dashboard/runtime reads use one connection and one query where the
same JSON state is currently loaded repeatedly. Synchronous session and config
lookups called by async routes remain behind the existing thread helpers.

### Atomic JSON state

Add a monotonic `revision` column and a database JSON state update primitive
that performs `SELECT ... FOR UPDATE`, validates the current object, invokes a
mutator, and writes the result inside the same transaction.
`JsonStore.update(mutator, expected_revision=None)` uses that primitive in
database mode and the existing process/file lock in file mode.

New write paths use `update`; compatibility `save(config)` performs a
three-way merge against the snapshot loaded for that `AppConfig`. Changes to
unrelated fields are retained, while a same-field conflict raises a clear
conflict error instead of silently overwriting newer data. The existing
subscription-only merge exception is removed after equivalent tests pass.

### Archive queue

`TaskStateStore.enqueue_archive_tasks()` accepts a list, normalizes and
deduplicates all items in memory, writes the queue once, reloads once, and emits
one state event. Single-item enqueue delegates to it. Batch discovery and
missing-3MF producers submit lists instead of looping over single-item writes.
This removes the current quadratic JSONB rewrite behavior without a risky queue
schema migration in this release.

### Cross-process resource limits

Every scarce resource class owns numbered `fcntl` lock files under the shared
state directory. The existing in-process FIFO limiter acquires one of those
global slots before entering a phase; process death releases the OS lock
automatically. Spawn payloads carry the current advanced limits so child
processes do not fall back to schema defaults. Lowering a limit uses drain
semantics and does not revoke an already-held slot. Platforms without `fcntl`
retain the current process-local fallback.

### Retention

Database cleanup deletes state events and business logs in bounded batches.
Defaults are 14 days for state events and 90 days for logs, configurable by
environment variables. Cleanup runs at startup and periodically in the Worker,
never on a UI request. The newest rows and active SSE cursor range remain
available.

## Query And Frontend Data Flow

### Model projection

When the Postgres model index is ready, the light model endpoint pushes search,
source/tag filters, sort, count, limit, and offset into SQL. Relational index
columns are used for common fields and JSONB operators only for flags/tags.
Facet aggregation is separate and short-TTL cached. File-based full snapshot is
kept only as a fallback when the index is unavailable.

The returned light projection is sufficient to render the current list page.
It is not followed automatically by the full model payload.

### Page loading and state events

A single `useHydratedResource` controller owns request revision, cancellation,
cache-first display, and optional explicit enrichment. Dashboard, tasks,
subscriptions, organizer, and models use the light/final projection as their
normal load. Full payloads are requested only by a user action that needs fields
not present in the projection.

Organizer and subscription pages ignore archive progress events. They refresh
only on terminal archive completion/failure events that can change their data.
Latest-wins behavior prevents a stale response from replacing newer state.

### Source refresh

One remote fetch produces both the metadata delta and an asset plan. The asset
stage consumes that plan and does not fetch/parse the model a second time.
Remote-refresh responses return bounded counts and recent items; they do not
embed a complete source queue that the page discards. Task light responses use
the existing SQL JSON-array summary for source refresh state.

### Performance telemetry

API timing separates time-to-first-byte, body transfer, JSON parse, light-ready,
and optional enrichment-ready durations. Payload byte size is recorded when it
is available without cloning large bodies. Existing slow-request logging stays
backward compatible.

## Runtime Engine Design

Runtime write routes fail with `503` and a clear disabled message for every
configuration. Read endpoints are side-effect free and return
`enabled: false`. The Worker does not tick the runtime engine, and archive,
subscription, source-refresh, and missing-3MF compatibility routes always use
their proven legacy managers.

This release deliberately freezes the incomplete engine instead of pretending
that an enqueue acknowledgment is a real archive completion receipt. Enabling
it again requires a separate relation-backed lease and cooperative-cancel
migration with end-to-end adapter receipts. Keeping read-only bounded snapshots
preserves diagnostics while removing the half-active dual-track behavior.

## Self-Update Design

The update helper treats App, Web, and Worker as one release unit:

1. Resolve a versioned image reference from the requested version and pull it.
2. Inspect every managed role and keep every old container available.
3. Create replacement containers under temporary names without deleting old
   containers.
4. Validate App/Web through HTTP readiness and Worker through a database-backed
   heartbeat/readiness probe, not container `running` alone.
5. If every candidate is ready, stop old roles, rename/start candidates under
   canonical names, and verify the final group.
6. If any preparation or commit step fails, remove candidates and restore every
   old role before reporting failure.
7. Only after group commit succeeds are old containers and old images scheduled
   for cleanup. The helper container keeps Docker `AutoRemove` plus the existing
   delayed cleanup fallback.

The canonical Compose file adds matching healthchecks. A short service pause
during the group commit is acceptable; mixed application versions are not.

## Deployment And Release

`compose.yaml` is the canonical deployment definition. The external
FlareSolverr variant is a small override rather than a full copy. The in-app
migration example reads the packaged canonical Compose file, and structure
tests check service/environment parity.

CI has separate verification and release paths:

- Pull requests and `main` run Python tests, frontend tests, frontend build,
  version consistency, Compose parsing, and Docker build smoke checks.
- Only a signed/intentional `v*` Git tag whose value matches `VERSION` publishes
  immutable `version` and `sha` image tags.
- `latest` is promoted only from that tested release job. A version tag is
  rejected if it already exists in GHCR rather than overwritten.

`.dockerignore` excludes VCS data, virtual environments, runtime state,
dependencies/build output, caches, workflow scratch data, and video output.

## Compatibility And Error Handling

- Existing API paths and current default legacy archive behavior stay intact.
- Existing secure passwords, sessions, cookies, subscriptions, and token hashes
  remain valid.
- Security migration errors fail closed and produce sanitized operator logs;
  plaintext passwords, cookies, tokens, proxy credentials, and upstream HTML
  never enter business logs.
- Database pool exhaustion and update conflicts return bounded, actionable
  errors rather than silently dropping state.
- SQL model queries fall back to the existing file/index snapshot path when the
  index is unavailable.
- Update rollback preserves the old release and reports which readiness check
  failed.

## Verification

Focused tests cover every new public function and each failure branch. Final
verification must include:

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend audit --audit-level=high
```

Compose files must parse and pass structural parity tests. If Docker is
available, CI also builds the image and runs an authenticated health smoke test.
Local completion may report Docker smoke as unavailable, but cannot skip the
Python/frontend/build/structure gates.

## Finding Coverage

| Finding | Design owner |
|---|---|
| F01 | Initial credential, trusted proxy parsing, shared login limiter |
| F02 | Required CloakBrowser token and loopback bind |
| F03 | Test baseline and tag-only release gate |
| F04 | Runtime write freeze, atomic lease, terminal semantics |
| F05 | Group update prepare/readiness/commit/rollback |
| F06 | Process connection pool and combined reads |
| F07 | Batch archive enqueue |
| F08 | SQL model filtering/paging/facets |
| F09 | Final light projection, terminal-only SSE, shared controller |
| F10 | Single-fetch asset plan and compact source state |
| F11 | Parent-owned cross-process resource slots |
| F12 | Atomic `JsonStore.update` and conflict detection |
| F13 | Hash-only persisted API token |
| F14 | Batched log/event retention |
| F15 | Canonical Compose and shared hydration controller |
| F16 | Complete frontend timing and `.dockerignore` |
