# Runtime Performance Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce live MakerHub CPU and Postgres pressure while keeping archive, subscription, and UI state refresh behavior correct.

**Architecture:** Cache database schema initialization per process, skip equivalent JSON state writes before touching Postgres, and make batch archive parent tasks visible without repeatedly occupying the active execution lane. Preserve semantic state events for task completion/failure, and keep user-facing counters understandable.

**Tech Stack:** Python FastAPI services, Postgres via psycopg, unittest/pytest, Docker worker/app runtime, existing `TaskStateStore` and `ArchiveTaskManager` patterns.

---

## Current Evidence

- Live queue on `2026-06-03 21:07:42 +0800`: `active=10`, `queued=775`, `failures=0`.
- Archive is progressing: `906391` completed at `20:57:49`, `899451` completed at `21:07:19`, `868104` started at `21:07:34`.
- CPU is still high: app about `131%`, worker about `194%`, Postgres about `133%`.
- Postgres showed repeated `CREATE INDEX IF NOT EXISTS ...` and `INSERT INTO makerhub_metadata ... ON CONFLICT` lock waits during normal runtime.
- Active queue contains 9 batch parent tasks with `running 0` children plus 1 actual single model task. This is operationally confusing and adds extra active-state churn.

## File Map

- Modify `app/core/database.py`: add per-process schema initialization guard and a test-only reset helper.
- Modify `app/services/task_state.py`: skip saving and publishing archive queue state when normalized payload is semantically unchanged.
- Modify `app/services/archive_worker.py`: keep restored/resumed batch parent tracking out of the active execution path where possible; add a helper that detects non-executable batch parent tasks.
- Modify `tests/test_database_json_state.py`: cover database initialization guard and state event behavior.
- Modify `tests/test_task_state.py`: cover unchanged archive queue save/update skip behavior.
- Modify `tests/test_archive_worker_batch_retry.py`: cover batch parent display/tracking without blocking queued single-model execution.
- Modify `README.md`: bump latest release notes.
- Modify `frontend/package.json`: bump patch version.

---

### Task 1: Cache Database Schema Initialization Per Process

**Files:**
- Modify: `app/core/database.py`
- Test: `tests/test_database_json_state.py`

- [ ] **Step 1: Write failing tests**

Append these tests near `DatabaseStatusTest` in `tests/test_database_json_state.py`.

```python
class DatabaseInitializationGuardTest(unittest.TestCase):
    def test_repeated_json_state_operations_initialize_database_once(self):
        calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row or {}

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "SELECT value FROM makerhub_json_state" in sql:
                    return FakeResult({"value": {"ok": True}})
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        database._reset_database_initialization_for_tests()
        with patch.object(database, "database_configured", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            database.load_json_state("archive_queue")
            database.save_json_state("archive_queue", {"ok": True})
            database.append_state_event("archive_queue", "state.changed", {"queued_count": 1})

        schema_calls = [
            sql for sql, _params in calls
            if "CREATE TABLE IF NOT EXISTS makerhub_json_state" in sql
            or "CREATE INDEX IF NOT EXISTS makerhub_state_events_created_idx" in sql
            or "INSERT INTO makerhub_metadata" in sql
        ]
        self.assertEqual(
            len([sql for sql in schema_calls if "CREATE TABLE IF NOT EXISTS makerhub_json_state" in sql]),
            1,
        )
        self.assertEqual(
            len([sql for sql in schema_calls if "CREATE INDEX IF NOT EXISTS makerhub_state_events_created_idx" in sql]),
            1,
        )
        self.assertEqual(
            len([sql for sql in schema_calls if "INSERT INTO makerhub_metadata" in sql]),
            1,
        )

    def test_initialization_guard_retries_after_failure(self):
        attempts = []

        class FakeContext:
            def __enter__(self):
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError("schema lock timeout")
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return {"value": {}}

        database._reset_database_initialization_for_tests()
        with patch.object(database, "database_configured", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            with self.assertRaises(RuntimeError):
                database.initialize_database()
            self.assertTrue(database.initialize_database())

        self.assertEqual(len(attempts), 2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_database_json_state.py::DatabaseInitializationGuardTest -q
```

Expected: fails because `_reset_database_initialization_for_tests` does not exist or schema DDL runs more than once.

- [ ] **Step 3: Implement guarded initialization**

In `app/core/database.py`, add module globals after constants:

```python
import threading

_DATABASE_INITIALIZED = False
_DATABASE_INITIALIZE_LOCK = threading.Lock()
```

Replace `initialize_database()` with guarded structure:

```python
def _initialize_database_schema() -> None:
    with database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS makerhub_metadata (
                key TEXT PRIMARY KEY,
                value JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        # keep the existing CREATE TABLE / CREATE INDEX / metadata upsert body here unchanged


def initialize_database() -> bool:
    global _DATABASE_INITIALIZED
    if not database_configured():
        return False
    if _DATABASE_INITIALIZED:
        return True
    with _DATABASE_INITIALIZE_LOCK:
        if _DATABASE_INITIALIZED:
            return True
        _initialize_database_schema()
        _DATABASE_INITIALIZED = True
    return True


def _reset_database_initialization_for_tests() -> None:
    global _DATABASE_INITIALIZED
    with _DATABASE_INITIALIZE_LOCK:
        _DATABASE_INITIALIZED = False
```

Keep all existing DDL/index SQL exactly as it is inside `_initialize_database_schema()`.

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/test_database_json_state.py::DatabaseInitializationGuardTest -q
python -m pytest tests/test_database_json_state.py::JsonStateDatabaseRoutingTest::test_business_logs_store_structured_entries_in_database tests/test_database_json_state.py::StateEventsTest::test_append_state_event_inserts_event_and_notifies -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/core/database.py tests/test_database_json_state.py
git commit -m "perf: cache database schema initialization"
```

---

### Task 2: Skip Equivalent Archive Queue State Writes

**Files:**
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing tests**

Add this test class near existing task state tests.

```python
class ArchiveQueueWriteCoalescingTest(unittest.TestCase):
    def test_update_archive_queue_skips_save_and_event_when_payload_unchanged(self):
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "task-1",
                        "url": "https://makerworld.com.cn/zh/models/1",
                        "title": "https://makerworld.com.cn/zh/models/1",
                        "mode": "single_model",
                        "status": "queued",
                        "progress": 0,
                        "message": "等待归档",
                        "updated_at": "2026-06-03T20:00:00+08:00",
                    }
                ],
                "recent_failures": [],
            }
        }
        saves = []
        events = []
        store = TaskStateStore()

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: saves.append((key, value)) or state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store._update_archive_queue(lambda payload: payload)

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(saves, [])
        self.assertEqual(events, [])

    def test_update_archive_queue_saves_and_publishes_when_payload_changes(self):
        state = {"archive_queue": {"active": [], "queued": [], "recent_failures": []}}
        saves = []
        events = []
        store = TaskStateStore()

        def mutate(payload):
            payload["queued"] = [
                {
                    "id": "task-1",
                    "url": "https://makerworld.com.cn/zh/models/1",
                    "mode": "single_model",
                    "status": "queued",
                }
            ]
            return payload

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: saves.append((key, value)) or state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store._update_archive_queue(mutate)

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(len(saves), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "archive_queue")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_task_state.py::ArchiveQueueWriteCoalescingTest -q
```

Expected: first test fails because `_update_archive_queue` currently writes and publishes every time.

- [ ] **Step 3: Implement unchanged-payload detection**

In `app/services/task_state.py`, import `json` already exists. Add helper near `_normalize_archive_queue`:

```python
def _state_payload_signature(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
```

Change `_update_archive_queue` to compare normalized payloads under the lock:

```python
    def _update_archive_queue(self, updater) -> dict:
        publish = True
        with _STATE_LOCK, self._state_file_lock(ARCHIVE_QUEUE_PATH):
            payload = self._load_archive_queue_unlocked()
            before_payload = {
                "active": payload.get("active") or [],
                "queued": payload.get("queued") or [],
                "recent_failures": payload.get("recent_failures") or [],
            }
            before_signature = _state_payload_signature(_normalize_archive_queue(before_payload))
            updated = updater(payload)
            if updated is None:
                updated = payload
            normalized_updated = _normalize_archive_queue(updated)
            after_signature = _state_payload_signature(normalized_updated)
            if before_signature == after_signature:
                result = self._load_archive_queue_unlocked()
                publish = False
            else:
                result = self._save_archive_queue_unlocked(normalized_updated)
        if publish:
            self._publish_state_event(ARCHIVE_QUEUE_STATE_KEY, result)
        return result
```

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/test_task_state.py::ArchiveQueueWriteCoalescingTest tests/test_task_state.py::StateEventsTest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/task_state.py tests/test_task_state.py
git commit -m "perf: skip unchanged archive queue writes"
```

---

### Task 3: Keep Batch Parent Tracking From Blocking Actual Queue Progress

**Files:**
- Modify: `app/services/archive_worker.py`
- Modify: `app/services/task_state.py` if needed for helper naming only
- Test: `tests/test_archive_worker_batch_retry.py`

- [ ] **Step 1: Write failing tests**

Add tests to `ArchiveWorkerBatchRetryTest`.

```python
    def test_resume_keeps_batch_parents_tracking_but_prioritizes_single_model_child(self):
        state = {}
        manager = ArchiveTaskManager(background_enabled=False)

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch.object(manager, "_archived_task_keys", return_value=set()):
            manager.task_store.save_archive_queue(
                {
                    "active": [
                        {
                            "id": f"batch-{index}",
                            "url": f"https://makerworld.com.cn/zh/@author{index}/upload",
                            "mode": "author_upload",
                            "status": "running",
                            "message": "批量归档执行中：成功 0/1，运行中 0，排队中 1，失败 0。",
                            "meta": {
                                "batch_expected_items": [
                                    {
                                        "url": f"https://makerworld.com.cn/zh/models/{900000 + index}",
                                        "task_key": f"model:{900000 + index}",
                                        "model_id": str(900000 + index),
                                        "attempts": 1,
                                        "status": "queued",
                                        "last_task_id": f"child-{index}",
                                    }
                                ],
                                "batch_progress": {
                                    "total": 1,
                                    "completed": 0,
                                    "failed": 0,
                                    "running": 0,
                                    "queued": 1,
                                    "remaining": 1,
                                },
                            },
                        }
                        for index in range(10)
                    ],
                    "queued": [
                        {
                            "id": "child-0",
                            "url": "https://makerworld.com.cn/zh/models/900000",
                            "title": "https://makerworld.com.cn/zh/models/900000",
                            "mode": "single_model",
                            "status": "queued",
                            "meta": {
                                "batch_parent_id": "batch-0",
                                "batch_source_url": "https://makerworld.com.cn/zh/@author0/upload",
                            },
                        }
                    ],
                    "recent_failures": [],
                }
            )

            queue = manager.resume_pending_tasks()
            next_task = manager._next_executable_task(queue)

        self.assertEqual(next_task["id"], "child-0")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest tests/test_archive_worker_batch_retry.py::ArchiveWorkerBatchRetryTest::test_resume_keeps_batch_parents_tracking_but_prioritizes_single_model_child -q
```

Expected: fails because `_next_executable_task` does not exist.

- [ ] **Step 3: Add explicit executable-task selection**

In `app/services/archive_worker.py`, add helpers near `_queue_item_key`:

```python
def _is_batch_parent_waiting_for_children(item: dict[str, Any]) -> bool:
    if str(item.get("mode") or "") not in BATCH_TASK_MODES:
        return False
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    expected_items = meta.get("batch_expected_items")
    return bool(expected_items)
```

Add method to `ArchiveTaskManager`:

```python
    def _next_executable_task(self, queue: dict) -> Optional[dict]:
        queued = list(queue.get("queued") or [])
        for item in queued:
            if not _is_batch_parent_waiting_for_children(item):
                return item
        return queued[0] if queued else None
```

Change `_run_loop`:

```python
            task = self._next_executable_task(queue)
            if task is None:
                if has_active_batch:
                    time.sleep(ACTIVE_BATCH_IDLE_POLL_SECONDS)
                    continue
                return
```

This keeps batch parents as visible tracking records but prevents restored parent tasks from being selected ahead of executable single-model child tasks.

- [ ] **Step 4: Prefer child work after resuming parent tasks**

In `_run_batch_task`, keep the existing early return for `existing_expected_items`, but ensure it only updates parent status once and relies on `_refresh_batch_tasks()` for later progress. With Task 2, unchanged follow-up refreshes should not write repeatedly.

- [ ] **Step 5: Run tests**

Run:

```bash
python -m pytest tests/test_archive_worker_batch_retry.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add app/services/archive_worker.py tests/test_archive_worker_batch_retry.py
git commit -m "fix: prioritize executable archive child tasks"
```

---

### Task 4: Reduce State Event Write Volume Further

**Files:**
- Modify: `app/services/state_events.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing test**

Add to the existing `StateEventsTest` in `tests/test_task_state.py`.

```python
    def test_archive_queue_progress_message_only_changes_are_coalesced(self):
        rows = []

        def append_event(event_type, scope, payload):
            row = {
                "id": len(rows) + 1,
                "type": event_type,
                "scope": scope,
                "payload": payload,
                "created_at": "",
            }
            rows.append(row)
            return row

        with patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_AT", {}), \
                patch.object(state_events, "_LAST_STATE_CHANGED_EVENT_SIGNATURE", {}), \
                patch.object(state_events, "append_state_event", side_effect=append_event), \
                patch.object(state_events, "wake_state_event_subscribers"), \
                patch.object(state_events.time, "monotonic", side_effect=[100.0, 100.1, 100.2]):
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 777, "failed_count": 0, "last_message": "正在下载设计图片（1/6）"},
            )
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 777, "failed_count": 0, "last_message": "正在下载设计图片（2/6）"},
            )
            state_events.publish_state_event(
                "archive_queue",
                "state.changed",
                {"running_count": 10, "queued_count": 776, "failed_count": 0, "last_message": "下一个任务"},
            )

        self.assertEqual([row["payload"]["queued_count"] for row in rows], [777, 776])
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
python -m pytest tests/test_task_state.py::StateEventsTest::test_archive_queue_progress_message_only_changes_are_coalesced -q
```

Expected: fails if `last_message` participates in archive queue signature.

- [ ] **Step 3: Tune signature by scope**

In `app/services/state_events.py`, change `_state_changed_signature` to accept scope:

```python
def _state_changed_signature(scope: str, payload: dict[str, Any]) -> str:
    if scope == ARCHIVE_QUEUE_STATE_KEY:
        fields = ("running_count", "queued_count", "failed_count", "count", "status", "running")
    else:
        fields = (
            "running_count",
            "queued_count",
            "failed_count",
            "count",
            "status",
            "running",
            "last_message",
            "last_error_at",
            "last_success_at",
        )
    signature_payload = {key: payload.get(key) for key in fields if key in payload}
    return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True, default=str)
```

Update caller:

```python
signature = _state_changed_signature(scope, payload)
```

- [ ] **Step 4: Run tests**

Run:

```bash
python -m pytest tests/test_task_state.py::StateEventsTest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/state_events.py tests/test_task_state.py
git commit -m "perf: coalesce archive queue progress events"
```

---

### Task 5: Runtime Diagnostics Verification Script

**Files:**
- Create: `scripts/check_runtime_pressure.sh`
- Test: shell syntax check only

- [ ] **Step 1: Create script**

Create `scripts/check_runtime_pressure.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'makerhub|self-update' || true
docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' makerhub-app makerhub-worker makerhub-postgres || true
docker exec -i makerhub-postgres psql -U makerhub -d makerhub -P pager=off -x <<'SQL'
SELECT updated_at AT TIME ZONE 'Asia/Shanghai' AS cn_updated_at,
       jsonb_array_length(value->'active') AS active,
       jsonb_array_length(value->'queued') AS queued,
       jsonb_array_length(value->'recent_failures') AS failures
FROM makerhub_json_state WHERE key='archive_queue';
SELECT state, wait_event_type, wait_event, count(*)
FROM pg_stat_activity
WHERE datname=current_database()
GROUP BY state, wait_event_type, wait_event
ORDER BY count(*) DESC;
SELECT created_at AT TIME ZONE 'Asia/Shanghai' AS cn_time, event, left(message, 160) AS message
FROM makerhub_logs
WHERE file_name = 'business.log' AND category = 'archive'
ORDER BY created_at DESC, id DESC LIMIT 8;
SQL
```

- [ ] **Step 2: Make executable and check syntax**

Run:

```bash
chmod +x scripts/check_runtime_pressure.sh
bash -n scripts/check_runtime_pressure.sh
```

Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/check_runtime_pressure.sh
git commit -m "chore: add runtime pressure diagnostic script"
```

---

### Task 6: Version Bump and Release Notes

**Files:**
- Modify: `README.md`
- Modify: `frontend/package.json`

- [ ] **Step 1: Bump version**

Update:

- `README.md` current version from `v0.8.20` to `v0.8.21`
- `frontend/package.json` from `0.8.20` to `0.8.21`

- [ ] **Step 2: Add release notes**

Insert this section above `v0.8.20`:

```markdown
### 2026-06-03 · v0.8.21

- 降低运行期 Postgres 压力：数据库 schema 初始化改为进程内只执行一次，避免日志和状态写入反复触发建表/建索引检查。
- 归档队列状态写入增加等价内容跳过，减少批量归档和单模型进度更新带来的数据库写入与前端事件风暴。
- 批量归档恢复后优先推进可执行的单模型子任务，避免批量父任务长期占据运行视图导致误判卡死。
```

Keep README showing only latest three release sections before the collapsed history. Move `v0.8.18` into the collapsed section if needed.

- [ ] **Step 3: Commit**

```bash
git add README.md frontend/package.json
git commit -m "chore: release v0.8.21"
```

---

## Final Verification

- [ ] Run focused backend tests:

```bash
python -m pytest tests/test_database_json_state.py tests/test_task_state.py tests/test_archive_worker_batch_retry.py -q
```

Expected: pass.

- [ ] Run full backend tests:

```bash
python -m pytest -q
```

Expected: pass.

- [ ] Run frontend build:

```bash
npm --prefix frontend run build
```

Expected: build succeeds.

- [ ] Check git status:

```bash
git status --short
```

Expected: only intended files changed before commits; clean after commits.

- [ ] After deploy/update, verify live:

```bash
scripts/check_runtime_pressure.sh
```

Expected after warm-up:

- No long-lived `CREATE INDEX IF NOT EXISTS` lock waits in `pg_stat_activity`.
- Archive queue `updated_at` continues to advance when active single-model task progresses/completes.
- Worker/Postgres CPU is lower than the pre-fix sampled `~190% / ~130%` during idle or light queue progression.

## Self-Review

- Spec coverage: addresses live CPU pressure, Postgres schema initialization locks, archive queue write pressure, state event volume, and batch parent active confusion.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: uses existing `TaskStateStore`, `ArchiveTaskManager`, `publish_state_event`, and database module names.
