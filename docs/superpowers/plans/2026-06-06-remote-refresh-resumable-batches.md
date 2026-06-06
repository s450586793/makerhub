# Remote Refresh Resumable Batches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make source refresh batches recover after worker restart, stop stale runtime data from hiding real progress, and show clear scheduler state in the UI.

**Architecture:** Extend the existing `RemoteRefreshManager` and `TaskStateStore` contracts instead of adding new queue infrastructure. A durable manifest records the selected models for each batch, the existing NDJSON result file becomes the completion journal, and startup/manual trigger resume incomplete manifest entries before scheduling a fresh batch.

**Tech Stack:** Python standard library (`json`, `threading`, `Path`, `time`, `uuid`), existing FastAPI routes, existing `TaskStateStore`, Vue 3 single-file components, Node built-in test runner, pytest/unittest.

---

## File Structure

- Modify: `app/services/state_contracts.py`
  - Add remote-refresh runtime statuses used by backend state and UI.
- Modify: `app/services/task_state.py`
  - Normalize `remote_refresh_state.active_run`.
  - Preserve new scheduler timestamps and deferral/interruption fields.
  - Include the new fields in compact dashboard/task payloads.
- Modify: `tests/test_task_state.py`
  - Cover active-run normalization, compact payload fields, and archive queue stale semantics used by source refresh.
- Modify: `app/services/remote_refresh.py`
  - Add manifest helpers beside `_RemoteRefreshBatchBuffer`.
  - Add resume/finalize helpers inside `RemoteRefreshManager`.
  - Update `_ensure_state()`, `trigger_manual_refresh()`, `_tick()`, `_service_busy()`, and `_run_batch()`.
- Modify: `tests/test_remote_refresh.py`
  - Cover manifest creation, partial resume, completed journal finalization, missing manifest interruption, manual resume, and deferral fields.
- Modify: `app/services/database_migration.py`
  - Protect live runtime keys during forced JSON migration unless the source file explicitly opts into restore.
- Modify: `tests/test_database_json_state.py`
  - Cover protected runtime migration, first bootstrap, explicit restore, and per-key migration log payloads.
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
  - Split confusing time fields and add `继续源端刷新` / `修复队列状态` actions.
- Modify: `frontend/src/pages/DashboardPage.vue`
  - Keep the dashboard source-refresh card aligned with the new state labels.
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`
  - Cover the new source-refresh labels and actions with static UI shape assertions.
- Modify only during release/push stage: `VERSION`, `frontend/package.json`, `frontend/package-lock.json`, `README.md`, `CHANGELOG.md`
  - Bump patch version once when the user asks to push and add focused release notes.

---

### Task 1: Normalize Remote Refresh Active Run State

**Files:**
- Modify: `app/services/state_contracts.py`
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing tests for new remote-refresh state fields**

In `tests/test_task_state.py`, update the import:

```python
from app.services.task_state import TaskStateStore, _ORGANIZER_TERMINAL_LOG_CACHE, _normalize_organize_tasks, compact_remote_refresh_state
```

Then add these tests to `ArchiveQueueStateTest`, before the `if __name__ == "__main__":` block:

```python
    def test_remote_refresh_state_normalizes_active_run_and_scheduler_fields(self):
        state = {}
        store = TaskStateStore()
        active_run = {
            "batch_id": "batch-1",
            "status": "running",
            "started_at": "2026-06-06T10:00:00+08:00",
            "scheduled_cron": "0 2 * * *",
            "manual": True,
            "candidate_total": "3",
            "completed_total": "1",
            "remaining_total": "2",
            "manifest_path": "state/remote_refresh_batches/batch-1.manifest.json",
            "result_path": "state/remote_refresh_batches/batch-1.ndjson",
        }

        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value):
            saved = store.patch_remote_refresh_state(
                status="resuming",
                running=True,
                active_run=active_run,
                last_attempt_at="2026-06-06T10:01:00+08:00",
                last_deferred_at="2026-06-06T09:59:00+08:00",
                last_defer_reason="archive_queue_busy",
                last_interrupted_at="2026-06-06T09:58:00+08:00",
                last_interrupted_reason="worker_stopped",
                last_completed_at="2026-06-05T05:32:26+08:00",
                stale_archive_queue_detected=True,
            )

        self.assertEqual(saved["status"], "resuming")
        self.assertEqual(saved["active_run"]["batch_id"], "batch-1")
        self.assertEqual(saved["active_run"]["candidate_total"], 3)
        self.assertEqual(saved["active_run"]["completed_total"], 1)
        self.assertEqual(saved["active_run"]["remaining_total"], 2)
        self.assertTrue(saved["active_run"]["manual"])
        self.assertEqual(saved["last_attempt_at"], "2026-06-06T10:01:00+08:00")
        self.assertEqual(saved["last_defer_reason"], "archive_queue_busy")
        self.assertTrue(saved["stale_archive_queue_detected"])

    def test_compact_remote_refresh_state_includes_scheduler_and_active_run(self):
        compact = compact_remote_refresh_state(
            {
                "status": "interrupted",
                "running": False,
                "last_run_at": "2026-06-06T10:00:00+08:00",
                "last_completed_at": "2026-06-05T05:32:26+08:00",
                "last_attempt_at": "2026-06-06T10:10:00+08:00",
                "last_deferred_at": "2026-06-06T10:09:00+08:00",
                "last_defer_reason": "stale_runtime_state",
                "last_interrupted_at": "2026-06-06T10:08:00+08:00",
                "last_interrupted_reason": "manifest_missing",
                "stale_archive_queue_detected": True,
                "active_run": {
                    "batch_id": "batch-2",
                    "status": "interrupted",
                    "candidate_total": 8,
                    "completed_total": 5,
                    "remaining_total": 3,
                },
            },
            include_current=False,
        )

        self.assertEqual(compact["status"], "interrupted")
        self.assertEqual(compact["last_completed_at"], "2026-06-05T05:32:26+08:00")
        self.assertEqual(compact["last_attempt_at"], "2026-06-06T10:10:00+08:00")
        self.assertEqual(compact["last_defer_reason"], "stale_runtime_state")
        self.assertEqual(compact["active_run"]["remaining_total"], 3)
        self.assertTrue(compact["stale_archive_queue_detected"])
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py -k "remote_refresh_state_normalizes_active_run or compact_remote_refresh_state_includes" -q
```

Expected: fails because `active_run`, `last_attempt_at`, `last_completed_at`, and `stale_archive_queue_detected` are missing from normalized state.

- [ ] **Step 3: Extend remote-refresh status contracts**

In `app/services/state_contracts.py`, replace `REMOTE_REFRESH_STATUSES` with:

```python
REMOTE_REFRESH_STATUSES = frozenset(
    {
        "idle",
        "running",
        "resuming",
        "deferred",
        "interrupted",
        "success",
        "error",
        "disabled",
    }
)
```

- [ ] **Step 4: Add active-run normalization helpers**

In `app/services/task_state.py`, add this helper near `_normalize_remote_refresh_state()`:

```python
def _normalize_remote_refresh_active_run(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    batch_id = str(payload.get("batch_id") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if not batch_id and not status:
        return {}
    if status not in {"running", "resuming", "interrupted", "completed", "abandoned"}:
        status = "running" if batch_id else ""
    return {
        "batch_id": batch_id,
        "status": status,
        "started_at": str(payload.get("started_at") or ""),
        "resumed_at": str(payload.get("resumed_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "scheduled_cron": str(payload.get("scheduled_cron") or ""),
        "manual": bool(payload.get("manual", False)),
        "candidate_total": _safe_int(payload.get("candidate_total") or 0),
        "completed_total": _safe_int(payload.get("completed_total") or 0),
        "remaining_total": _safe_int(payload.get("remaining_total") or 0),
        "manifest_path": str(payload.get("manifest_path") or ""),
        "result_path": str(payload.get("result_path") or ""),
        "interrupted_reason": _normalize_source_refresh_text(_sanitize_message_text(payload.get("interrupted_reason") or "")),
    }
```

- [ ] **Step 5: Preserve the new fields in normalized and compact state**

In `_normalize_remote_refresh_state()`, add `active_run = _normalize_remote_refresh_active_run(payload.get("active_run"))` before the returned dict, then include:

```python
        "last_attempt_at": str(payload.get("last_attempt_at") or ""),
        "last_deferred_at": str(payload.get("last_deferred_at") or ""),
        "last_defer_reason": str(payload.get("last_defer_reason") or ""),
        "last_interrupted_at": str(payload.get("last_interrupted_at") or ""),
        "last_interrupted_reason": _normalize_source_refresh_text(_sanitize_message_text(payload.get("last_interrupted_reason") or "")),
        "last_completed_at": str(payload.get("last_completed_at") or ""),
        "stale_archive_queue_detected": bool(payload.get("stale_archive_queue_detected", False)),
        "active_run": active_run,
```

In `compact_remote_refresh_state()`, add the same scalar fields and:

```python
        "active_run": state["active_run"],
        "stale_archive_queue_detected": state["stale_archive_queue_detected"],
```

- [ ] **Step 6: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py -k "remote_refresh_state_normalizes_active_run or compact_remote_refresh_state_includes" -q
```

Expected: both tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add app/services/state_contracts.py app/services/task_state.py tests/test_task_state.py
git commit -m "feat: 扩展源端刷新运行状态"
```

---

### Task 2: Add Durable Batch Manifest Helpers

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing tests for manifest creation and completion keys**

Add these tests to `RemoteRefreshManagerTest` in `tests/test_remote_refresh.py`:

```python
    def test_remote_refresh_manifest_writes_safe_candidate_fields(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        try:
            manifest = remote_refresh._RemoteRefreshBatchManifest.create(
                batch_id="batch-safe",
                candidates=[
                    {
                        "model_dir": "MW_1",
                        "title": "模型 1",
                        "origin_url": "https://makerworld.com.cn/zh/models/1?from=share",
                        "meta_path": str(self.temp_path / "MW_1" / "meta.json"),
                        "cookie": "secret=must-not-persist",
                        "raw_html": "<html>secret</html>",
                    }
                ],
                stats={"eligible_total": 1, "selected_total": 1, "remaining_total": 0},
                cron="0 2 * * *",
                manual=True,
                directory=remote_refresh.REMOTE_REFRESH_BATCH_DIR,
            )
            payload = json.loads(manifest.path.read_text(encoding="utf-8"))
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["batch_id"], "batch-safe")
        self.assertTrue(payload["manual"])
        self.assertEqual(payload["candidates"][0]["model_dir"], "MW_1")
        self.assertEqual(payload["candidates"][0]["url"], "https://makerworld.com.cn/zh/models/1?from=share")
        self.assertNotIn("cookie", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("raw_html", json.dumps(payload, ensure_ascii=False))

    def test_completed_keys_from_batch_records_uses_model_dir_and_url(self):
        records = [
            {"model_dir": "MW_1", "url": "https://makerworld.com.cn/zh/models/1", "status": "success"},
            {"model_dir": "MW_2", "url": "https://makerworld.com/zh/models/2", "status": "failed"},
            {"model_dir": "", "url": "", "status": "success"},
        ]

        keys = remote_refresh._completed_remote_refresh_keys(records)

        self.assertEqual(
            keys,
            {
                "MW_1|https://makerworld.com.cn/zh/models/1",
                "MW_2|https://makerworld.com/zh/models/2",
            },
        )
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "manifest_writes_safe_candidate_fields or completed_keys_from_batch_records" -q
```

Expected: fails because `_RemoteRefreshBatchManifest` and `_completed_remote_refresh_keys()` do not exist.

- [ ] **Step 3: Add manifest constants and key helpers**

In `app/services/remote_refresh.py`, add near the batch constants:

```python
REMOTE_REFRESH_BATCH_MANIFEST_VERSION = 1
REMOTE_REFRESH_BATCH_RETRY_SECONDS = 60
```

Add these helpers after `_remote_refresh_batch_id()`:

```python
def _remote_refresh_relative_state_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(STATE_DIR.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _remote_refresh_path_from_state(value: Any) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return Path()
    path = Path(raw)
    if path.is_absolute():
        return path
    return STATE_DIR / path


def _remote_refresh_candidate_key(item: dict[str, Any]) -> str:
    model_dir = str(item.get("model_dir") or "").strip().strip("/")
    url = normalize_source_url(str(item.get("url") or item.get("origin_url") or ""))
    if not model_dir or not url:
        return ""
    return f"{model_dir}|{url}"


def _completed_remote_refresh_keys(records: list[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = _remote_refresh_candidate_key(record)
        if key:
            keys.add(key)
    return keys
```

- [ ] **Step 4: Add `_RemoteRefreshBatchManifest`**

Add this class after `_RemoteRefreshBatchBuffer`:

```python
class _RemoteRefreshBatchManifest:
    def __init__(self, *, batch_id: str, path: Path, payload: dict[str, Any]) -> None:
        self.batch_id = str(batch_id or "").strip()
        self.path = Path(path)
        self.payload = payload if isinstance(payload, dict) else {}

    @classmethod
    def create(
        cls,
        *,
        batch_id: str,
        candidates: list[dict[str, Any]],
        stats: dict[str, Any],
        cron: str,
        manual: bool,
        directory: Optional[Path] = None,
    ) -> "_RemoteRefreshBatchManifest":
        batch_dir = Path(directory or REMOTE_REFRESH_BATCH_DIR)
        batch_dir.mkdir(parents=True, exist_ok=True)
        clean_batch_id = str(batch_id or _remote_refresh_batch_id()).strip()
        path = batch_dir / f"{clean_batch_id}.manifest.json"
        entries = [_remote_refresh_manifest_entry(item) for item in candidates or []]
        payload = {
            "schema_version": REMOTE_REFRESH_BATCH_MANIFEST_VERSION,
            "batch_id": clean_batch_id,
            "created_at": _now_iso(),
            "scheduled_cron": str(cron or ""),
            "manual": bool(manual),
            "stats": _json_safe_remote_refresh_value(stats if isinstance(stats, dict) else {}),
            "candidate_total": len(entries),
            "candidates": entries,
        }
        _write_json(path, payload)
        return cls(batch_id=clean_batch_id, path=path, payload=payload)

    @classmethod
    def load(cls, path: Path) -> "_RemoteRefreshBatchManifest":
        payload = _load_json(path)
        return cls(batch_id=str(payload.get("batch_id") or ""), path=path, payload=payload)

    def compatible(self) -> bool:
        return (
            int(self.payload.get("schema_version") or 0) == REMOTE_REFRESH_BATCH_MANIFEST_VERSION
            and bool(self.batch_id)
            and isinstance(self.payload.get("candidates"), list)
        )

    def candidates(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.payload.get("candidates") or [] if isinstance(item, dict)]
```

Add the entry builder before the class:

```python
def _remote_refresh_manifest_entry(item: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    return {
        "model_dir": str(item.get("model_dir") or "").strip().strip("/"),
        "title": str(item.get("title") or item.get("model_dir") or "").strip(),
        "origin_url": normalize_source_url(str(item.get("origin_url") or item.get("url") or "")),
        "url": normalize_source_url(str(item.get("origin_url") or item.get("url") or "")),
        "source": str(item.get("source") or "").strip(),
        "meta_path": str(item.get("meta_path") or "").strip(),
        "collect_ts": _parse_ts(item.get("collect_ts")),
        "remote_sync": item.get("remote_sync") if isinstance(item.get("remote_sync"), dict) else {},
    }
```

- [ ] **Step 5: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "manifest_writes_safe_candidate_fields or completed_keys_from_batch_records" -q
```

Expected: both tests pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "feat: 添加源端刷新批次清单"
```

---

### Task 3: Start New Batches With Active Run And Manifest

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write a failing test for new-batch active-run metadata**

Add this test to `RemoteRefreshManagerTest`:

```python
    def test_run_batch_writes_active_run_manifest_and_completion_state(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 1
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
            {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
        ]
        self.manager._pick_candidates = lambda: (
            items,
            {"eligible_total": 2, "selected_total": 2, "remaining_total": 0, "missing_cookie": 0, "local_or_invalid": 0},
        )
        self.manager._refresh_one = lambda item, *, index, total, config: {
            "ok": True,
            "metrics": {"model_dir": item["model_dir"], "title": item["title"], "comments": 1, "total_duration_ms": index},
            "record": remote_refresh._remote_refresh_result_record(
                model_dir=item["model_dir"],
                title=item["title"],
                url=item["origin_url"],
                status="success",
                message="完成",
                metrics={"comments": 1, "total_duration_ms": index},
                change_labels=["已检查，无远端变化"],
            ),
        }

        try:
            self.manager._run_batch(config)
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir
            remote_refresh._remote_refresh_model_workers = original_workers

        state = self.task_store.load_remote_refresh_state()
        active_run = state["active_run"]
        self.assertEqual(active_run["status"], "completed")
        self.assertEqual(active_run["candidate_total"], 2)
        self.assertEqual(active_run["completed_total"], 2)
        self.assertEqual(active_run["remaining_total"], 0)
        self.assertTrue(active_run["manifest_path"].endswith(".manifest.json"))
        self.assertEqual(state["last_completed_at"], state["last_success_at"])
        manifest_path = remote_refresh._remote_refresh_path_from_state(active_run["manifest_path"])
        self.assertTrue(manifest_path.exists())
```

- [ ] **Step 2: Run the test and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_run_batch_writes_active_run_manifest_and_completion_state -q
```

Expected: fails because `_run_batch()` does not write `active_run` or a manifest.

- [ ] **Step 3: Add active-run patch helpers to `RemoteRefreshManager`**

Inside `RemoteRefreshManager`, add:

```python
    def _batch_paths(self, batch_id: str) -> tuple[Path, Path]:
        batch_dir = REMOTE_REFRESH_BATCH_DIR
        return batch_dir / f"{batch_id}.manifest.json", batch_dir / f"{batch_id}.ndjson"

    def _active_run_payload(
        self,
        *,
        batch_id: str,
        status: str,
        started_at: str,
        scheduled_cron: str,
        manual: bool,
        candidate_total: int,
        completed_total: int,
        manifest_path: Path,
        result_path: Path,
        resumed_at: str = "",
        finished_at: str = "",
        interrupted_reason: str = "",
    ) -> dict[str, Any]:
        remaining_total = max(int(candidate_total or 0) - int(completed_total or 0), 0)
        return {
            "batch_id": batch_id,
            "status": status,
            "started_at": started_at,
            "resumed_at": resumed_at,
            "finished_at": finished_at,
            "scheduled_cron": scheduled_cron,
            "manual": bool(manual),
            "candidate_total": int(candidate_total or 0),
            "completed_total": int(completed_total or 0),
            "remaining_total": remaining_total,
            "manifest_path": _remote_refresh_relative_state_path(manifest_path),
            "result_path": _remote_refresh_relative_state_path(result_path),
            "interrupted_reason": _sanitize_remote_refresh_message(interrupted_reason, ""),
        }
```

- [ ] **Step 4: Write manifest and active-run state at batch start**

In `_run_batch()`, after `started_at` and before creating the manifest, read the manual flag once:

```python
        current_state = self.task_store.load_remote_refresh_state()
        manual_run = bool(str(current_state.get("manual_requested_at") or "").strip())
```

Before calling `patch_remote_refresh_state()` for batch start, create the batch ID, manifest, and buffer:

```python
            batch_id = _remote_refresh_batch_id()
            manifest = _RemoteRefreshBatchManifest.create(
                batch_id=batch_id,
                candidates=candidates,
                stats=stats,
                cron=normalized_cron,
                manual=manual_run,
            )
            _, result_path = self._batch_paths(batch_id)
            active_run = self._active_run_payload(
                batch_id=batch_id,
                status="running",
                started_at=started_at,
                scheduled_cron=normalized_cron,
                manual=manual_run,
                candidate_total=len(candidates),
                completed_total=0,
                manifest_path=manifest.path,
                result_path=result_path,
            )
```

In the batch-start state patch, add:

```python
                active_run=active_run,
                last_attempt_at=started_at,
                last_interrupted_at="",
                last_interrupted_reason="",
                stale_archive_queue_detected=False,
```

When constructing the batch buffer, use the same batch ID:

```python
            batch_buffer = _RemoteRefreshBatchBuffer(batch_id=batch_id)
```

- [ ] **Step 5: Mark active-run completion at batch finish and no-candidate finish**

In the no-candidate state patch, include:

```python
                    last_completed_at=started_at,
                    active_run=self._active_run_payload(
                        batch_id=batch_id,
                        status="completed",
                        started_at=started_at,
                        scheduled_cron=normalized_cron,
                        manual=active_run["manual"],
                        candidate_total=0,
                        completed_total=0,
                        manifest_path=manifest.path,
                        result_path=result_path,
                        finished_at=started_at,
                    ),
```

In the normal finish state patch, include:

```python
                last_completed_at=finished_at,
                active_run=self._active_run_payload(
                    batch_id=batch_id,
                    status="completed",
                    started_at=started_at,
                    scheduled_cron=normalized_cron,
                    manual=active_run["manual"],
                    candidate_total=len(candidates),
                    completed_total=processed_total,
                    manifest_path=manifest.path,
                    result_path=result_path,
                    finished_at=finished_at,
                ),
```

- [ ] **Step 6: Keep the existing batch-boundary event behavior**

Update `tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_run_batch_buffers_model_results_and_publishes_only_batch_boundaries` so it still asserts two `remote_refresh_state` events and adds:

```python
        self.assertEqual(state["active_run"]["status"], "completed")
        self.assertEqual(state["last_completed_at"], state["last_error_at"])
```

Keep its existing NDJSON cleanup assertion if `_run_batch()` still deletes the result file after successful finalization. If the implementation keeps the last result journal for diagnostics, replace that assertion with:

```python
        self.assertLessEqual(len(list((self.temp_path / "remote_refresh_batches").glob("*.ndjson"))), 1)
```

- [ ] **Step 7: Run the focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "active_run_manifest or buffers_model_results" -q
```

Expected: selected tests pass and state events are still batch-boundary only.

- [ ] **Step 8: Commit Task 3**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "feat: 记录源端刷新批次运行态"
```

---

### Task 4: Resume Interrupted Batches

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing tests for partial resume and completed journal finalization**

Add these tests to `RemoteRefreshManagerTest`:

```python
    def test_resume_active_run_skips_completed_manifest_entries(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        batch_dir.mkdir(parents=True, exist_ok=True)
        manifest = remote_refresh._RemoteRefreshBatchManifest.create(
            batch_id="resume-batch",
            candidates=[
                {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
                {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
                {"model_dir": "m3", "title": "模型 3", "origin_url": "https://makerworld.com.cn/model/3", "meta_path": str(self.temp_path / "m3" / "meta.json")},
            ],
            stats={"eligible_total": 3, "selected_total": 3, "remaining_total": 0},
            cron="0 0 * * *",
            manual=False,
            directory=batch_dir,
        )
        buffer = remote_refresh._RemoteRefreshBatchBuffer(batch_id="resume-batch", directory=batch_dir)
        buffer.append(remote_refresh._remote_refresh_result_record(
            model_dir="m1",
            title="模型 1",
            url="https://makerworld.com.cn/model/1",
            status="success",
            message="已完成",
            metrics={"comments": 1},
            change_labels=["已检查，无远端变化"],
        ))
        buffer.close()
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            active_run={
                "batch_id": "resume-batch",
                "status": "running",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 3,
                "completed_total": 1,
                "remaining_total": 2,
                "manifest_path": remote_refresh._remote_refresh_relative_state_path(manifest.path),
                "result_path": "remote_refresh_batches/resume-batch.ndjson",
            },
        )
        refreshed = []
        self.manager._refresh_one = lambda item, *, index, total, config: refreshed.append(item["model_dir"]) or {
            "ok": True,
            "metrics": {"model_dir": item["model_dir"], "title": item["title"], "comments": 1},
            "record": remote_refresh._remote_refresh_result_record(
                model_dir=item["model_dir"],
                title=item["title"],
                url=item["origin_url"],
                status="success",
                message="完成",
                metrics={"comments": 1},
                change_labels=["已检查，无远端变化"],
            ),
        }

        try:
            resumed = self.manager._resume_active_run_if_possible(self.store.load())
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(resumed)
        self.assertEqual(refreshed, ["m2", "m3"])
        self.assertEqual(state["active_run"]["status"], "completed")
        self.assertEqual(state["active_run"]["completed_total"], 3)
        self.assertEqual(state["last_batch_succeeded"], 3)

    def test_resume_active_run_finalizes_when_all_manifest_entries_have_records(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        batch_dir.mkdir(parents=True, exist_ok=True)
        manifest = remote_refresh._RemoteRefreshBatchManifest.create(
            batch_id="done-batch",
            candidates=[
                {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
                {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
            ],
            stats={"eligible_total": 2, "selected_total": 2, "remaining_total": 0},
            cron="0 0 * * *",
            manual=False,
            directory=batch_dir,
        )
        buffer = remote_refresh._RemoteRefreshBatchBuffer(batch_id="done-batch", directory=batch_dir)
        for model_dir in ("m1", "m2"):
            buffer.append(remote_refresh._remote_refresh_result_record(
                model_dir=model_dir,
                title=model_dir,
                url=f"https://makerworld.com.cn/model/{model_dir[-1]}",
                status="success",
                message="完成",
                metrics={"comments": 1},
                change_labels=["已检查，无远端变化"],
            ))
        buffer.close()
        self.task_store.patch_remote_refresh_state(
            active_run={
                "batch_id": "done-batch",
                "status": "running",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 2,
                "completed_total": 2,
                "remaining_total": 0,
                "manifest_path": remote_refresh._remote_refresh_relative_state_path(manifest.path),
                "result_path": "remote_refresh_batches/done-batch.ndjson",
            },
        )
        self.manager._refresh_one = lambda *_args, **_kwargs: self.fail("completed journal must not rerun models")

        try:
            resumed = self.manager._resume_active_run_if_possible(self.store.load())
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(resumed)
        self.assertEqual(state["active_run"]["status"], "completed")
        self.assertEqual(state["last_batch_succeeded"], 2)
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "resume_active_run_skips_completed or resume_active_run_finalizes" -q
```

Expected: fails because `_resume_active_run_if_possible()` does not exist.

- [ ] **Step 3: Add resumable active-run detection**

Inside `RemoteRefreshManager`, add:

```python
    def _resumable_active_run(self) -> dict[str, Any]:
        state = self.task_store.load_remote_refresh_state()
        active_run = state.get("active_run") if isinstance(state.get("active_run"), dict) else {}
        if str(active_run.get("status") or "") not in {"running", "resuming", "interrupted"}:
            return {}
        if not str(active_run.get("batch_id") or "").strip():
            return {}
        return dict(active_run)
```

- [ ] **Step 4: Add finalize-from-records helper**

Inside `RemoteRefreshManager`, add:

```python
    def _finalize_batch_from_records(
        self,
        *,
        config,
        active_run: dict[str, Any],
        records: list[dict[str, Any]],
        normalized_cron: str,
        started_at: str,
        resumed: bool,
    ) -> None:
        finished_at = _now_iso()
        previous_state = self.task_store.load_remote_refresh_state()
        summary = _remote_refresh_batch_summary(records)
        succeeded = summary["succeeded"]
        failed = summary["failed"]
        skipped = summary["skipped"]
        candidate_total = int(active_run.get("candidate_total") or len(records))
        remaining_total = max(candidate_total - len(records), 0)
        batch_metrics = _empty_batch_metrics()
        for record in records:
            _merge_batch_metrics(batch_metrics, record.get("metrics") if isinstance(record.get("metrics"), dict) else {})
        message_prefix = "源端刷新恢复完成" if resumed else "源端刷新完成"
        message = f"{message_prefix}，成功 {succeeded} 个，失败 {failed} 个。"
        manifest_path = _remote_refresh_path_from_state(active_run.get("manifest_path"))
        result_path = _remote_refresh_path_from_state(active_run.get("result_path"))
        completed_run = self._active_run_payload(
            batch_id=str(active_run.get("batch_id") or ""),
            status="completed",
            started_at=started_at,
            resumed_at=str(active_run.get("resumed_at") or ""),
            scheduled_cron=normalized_cron,
            manual=bool(active_run.get("manual")),
            candidate_total=candidate_total,
            completed_total=len(records),
            manifest_path=manifest_path,
            result_path=result_path,
            finished_at=finished_at,
        )
        self.task_store.patch_remote_refresh_state(
            status="idle" if failed == 0 else "error",
            running=False,
            current_item={},
            current_items=[],
            next_run_at=_next_run_at(normalized_cron),
            scheduled_cron=normalized_cron,
            last_success_at=finished_at if succeeded else str(previous_state.get("last_success_at") or ""),
            last_error_at=finished_at if failed else str(previous_state.get("last_error_at") or ""),
            last_completed_at=finished_at,
            last_message=message,
            last_batch_succeeded=succeeded,
            last_batch_failed=failed,
            last_batch_skipped=skipped,
            last_batch_total=candidate_total,
            last_eligible_total=candidate_total,
            last_remaining_total=remaining_total,
            last_batch_metrics=batch_metrics,
            recent_items=summary["recent_items"],
            active_run=completed_run,
        )
        invalidate_archive_snapshot("remote_refresh_batch_finished")
```

- [ ] **Step 5: Add resume implementation**

Inside `RemoteRefreshManager`, add:

```python
    def _resume_active_run_if_possible(self, config) -> bool:
        active_run = self._resumable_active_run()
        if not active_run:
            return False
        normalized_cron = _validate_cron(getattr(config.remote_refresh, "cron", "") or active_run.get("scheduled_cron") or DEFAULT_REMOTE_REFRESH_CRON)
        manifest_path = _remote_refresh_path_from_state(active_run.get("manifest_path"))
        result_path = _remote_refresh_path_from_state(active_run.get("result_path"))
        manifest = _RemoteRefreshBatchManifest.load(manifest_path) if manifest_path else _RemoteRefreshBatchManifest(batch_id="", path=Path(), payload={})
        if not manifest.compatible() or not result_path:
            self._mark_active_run_interrupted(active_run, reason="manifest_missing" if not manifest.compatible() else "result_path_missing")
            return False
        buffer = _RemoteRefreshBatchBuffer(batch_id=str(active_run.get("batch_id") or manifest.batch_id), directory=result_path.parent)
        records = buffer.read_records()
        completed_keys = _completed_remote_refresh_keys(records)
        candidates = manifest.candidates()
        remaining = [item for item in candidates if _remote_refresh_candidate_key(item) not in completed_keys]
        resumed_at = _now_iso()
        self.task_store.patch_remote_refresh_state(
            status="resuming",
            running=True,
            last_attempt_at=resumed_at,
            active_run={
                **active_run,
                "status": "resuming",
                "resumed_at": resumed_at,
                "completed_total": len(completed_keys),
                "remaining_total": len(remaining),
            },
        )
        if not remaining:
            self._finalize_batch_from_records(
                config=config,
                active_run={**active_run, "resumed_at": resumed_at},
                records=records,
                normalized_cron=normalized_cron,
                started_at=str(active_run.get("started_at") or resumed_at),
                resumed=True,
            )
            return True
        stats = dict(manifest.payload.get("stats") if isinstance(manifest.payload.get("stats"), dict) else {})
        stats["eligible_total"] = int(stats.get("eligible_total") or len(candidates))
        stats["selected_total"] = len(remaining)
        stats["remaining_total"] = max(stats["eligible_total"] - len(records) - len(remaining), 0)
        self._run_batch(
            config,
            prelocked=True,
            resume_active_run={**active_run, "resumed_at": resumed_at},
            resume_candidates=remaining,
            resume_stats=stats,
            resume_existing_records=records,
        )
        return True
```

Also add `_mark_active_run_interrupted()`:

```python
    def _mark_active_run_interrupted(self, active_run: dict[str, Any], *, reason: str) -> None:
        now = _now_iso()
        manifest_path = _remote_refresh_path_from_state(active_run.get("manifest_path"))
        result_path = _remote_refresh_path_from_state(active_run.get("result_path"))
        candidate_total = int(active_run.get("candidate_total") or 0)
        completed_total = int(active_run.get("completed_total") or 0)
        interrupted_run = self._active_run_payload(
            batch_id=str(active_run.get("batch_id") or ""),
            status="interrupted",
            started_at=str(active_run.get("started_at") or ""),
            resumed_at=str(active_run.get("resumed_at") or ""),
            scheduled_cron=str(active_run.get("scheduled_cron") or ""),
            manual=bool(active_run.get("manual")),
            candidate_total=candidate_total,
            completed_total=completed_total,
            manifest_path=manifest_path,
            result_path=result_path,
            interrupted_reason=reason,
        )
        self.task_store.patch_remote_refresh_state(
            status="interrupted",
            running=False,
            last_interrupted_at=now,
            last_interrupted_reason=reason,
            last_message=f"源端刷新批次中断：{reason}",
            active_run=interrupted_run,
            current_item={},
            current_items=[],
        )
```

- [ ] **Step 6: Allow `_run_batch()` to run resume candidates**

Change the `_run_batch()` signature:

```python
    def _run_batch(
        self,
        config,
        *,
        prelocked: bool = False,
        resume_active_run: Optional[dict[str, Any]] = None,
        resume_candidates: Optional[list[dict[str, Any]]] = None,
        resume_stats: Optional[dict[str, int]] = None,
        resume_existing_records: Optional[list[dict[str, Any]]] = None,
    ) -> None:
```

At candidate selection, use:

```python
        if resume_candidates is not None:
            candidates = list(resume_candidates)
            stats = dict(resume_stats or {})
        else:
            candidates, stats = self._pick_candidates()
```

At batch ID and manifest setup, reuse resume paths when `resume_active_run` exists:

```python
            if resume_active_run:
                batch_id = str(resume_active_run.get("batch_id") or _remote_refresh_batch_id())
                manifest_path, result_path = self._batch_paths(batch_id)
                stored_manifest_path = _remote_refresh_path_from_state(resume_active_run.get("manifest_path"))
                if stored_manifest_path:
                    manifest_path = stored_manifest_path
                manifest = _RemoteRefreshBatchManifest.load(manifest_path)
                started_at = str(resume_active_run.get("started_at") or started_at)
                manual_run = bool(resume_active_run.get("manual"))
            else:
                batch_id = _remote_refresh_batch_id()
                manifest = _RemoteRefreshBatchManifest.create(
                    batch_id=batch_id,
                    candidates=candidates,
                    stats=stats,
                    cron=normalized_cron,
                    manual=manual_run,
                )
                _, result_path = self._batch_paths(batch_id)
```

When computing `batch_records`, merge previous records with new records:

```python
            existing_records = list(resume_existing_records or [])
            new_records = batch_buffer.read_records() if batch_buffer is not None else []
            batch_records = existing_records + [
                record
                for record in new_records
                if _remote_refresh_candidate_key(record) not in _completed_remote_refresh_keys(existing_records)
            ]
```

- [ ] **Step 7: Run focused resume tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "resume_active_run_skips_completed or resume_active_run_finalizes or active_run_manifest" -q
```

Expected: selected tests pass.

- [ ] **Step 8: Commit Task 4**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "feat: 恢复中断的源端刷新批次"
```

---

### Task 5: Wire Resume Into Startup, Manual Trigger, And Failure Paths

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing tests for manual resume and missing manifest interruption**

Add these tests to `RemoteRefreshManagerTest`:

```python
    def test_manual_trigger_resumes_existing_active_run(self):
        config = self.store.load()
        self.task_store.patch_remote_refresh_state(
            active_run={
                "batch_id": "manual-resume",
                "status": "interrupted",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 5,
                "completed_total": 2,
                "remaining_total": 3,
                "manifest_path": "remote_refresh_batches/manual-resume.manifest.json",
                "result_path": "remote_refresh_batches/manual-resume.ndjson",
            }
        )
        called = []
        self.manager._resume_active_run_if_possible = lambda _config: called.append(True) or True

        result = self.manager.trigger_manual_refresh()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["mode"], "resume")
        self.assertEqual(called, [True])
        self.assertIn("继续", result["message"])

    def test_missing_manifest_marks_active_run_interrupted(self):
        self.task_store.patch_remote_refresh_state(
            active_run={
                "batch_id": "missing-manifest",
                "status": "running",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 5,
                "completed_total": 2,
                "remaining_total": 3,
                "manifest_path": "remote_refresh_batches/missing.manifest.json",
                "result_path": "remote_refresh_batches/missing.ndjson",
            }
        )

        resumed = self.manager._resume_active_run_if_possible(self.store.load())

        state = self.task_store.load_remote_refresh_state()
        self.assertFalse(resumed)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["active_run"]["status"], "interrupted")
        self.assertEqual(state["last_interrupted_reason"], "manifest_missing")
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "manual_trigger_resumes_existing_active_run or missing_manifest_marks" -q
```

Expected: manual trigger does not return `mode="resume"` and missing manifest is not marked interrupted.

- [ ] **Step 3: Resume before scheduling a fresh worker batch**

In `start()`, call resume after `_ensure_state()`:

```python
    def start(self) -> None:
        self._ensure_state()
        if self.background_enabled:
            self._resume_active_run_if_possible(self.store.load())
        if not self.background_enabled:
            return
```

In `_tick()`, before checking `next_run_at`, add:

```python
        if self._resume_active_run_if_possible(config):
            return
```

- [ ] **Step 4: Update manual trigger response modes**

In `trigger_manual_refresh()`, after the batch-running check and before `_service_busy()`, add:

```python
        if self._resumable_active_run():
            if self._resume_active_run_if_possible(config):
                state = self.task_store.load_remote_refresh_state()
                message = "已继续上次中断的源端刷新批次。"
                return {
                    "accepted": True,
                    "mode": "resume",
                    "message": message,
                    "config": config.remote_refresh.model_dump(),
                    "state": state,
                }
```

For existing return payloads, add:

```python
                "mode": "queued_for_worker",
```

for background-disabled queued requests, add:

```python
                "mode": "rejected",
```

for busy/running rejects, and add:

```python
            "mode": "new",
```

for accepted fresh runs.

- [ ] **Step 5: Mark unexpected batch-level exceptions as interrupted**

Wrap `_run_batch()` internals so exceptions outside per-model handling patch interruption before re-raising:

```python
        except Exception as exc:
            active_run = self.task_store.load_remote_refresh_state().get("active_run") or {}
            if isinstance(active_run, dict) and active_run.get("batch_id"):
                self._mark_active_run_interrupted(
                    active_run,
                    reason=_sanitize_remote_refresh_message(exc, exc.__class__.__name__),
                )
            raise
```

Keep the existing `finally` block that closes the buffer and clears `_batch_running`.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "manual_trigger_resumes_existing_active_run or missing_manifest_marks or manual_trigger" -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "feat: 接入源端刷新断点续跑"
```

---

### Task 6: Fix Busy-State And Deferral Semantics

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing tests for stale archive queue behavior**

Add these tests to `RemoteRefreshManagerTest`:

```python
    def test_service_busy_ignores_waiting_children_parent_and_stale_active_task(self):
        state = {
            "archive_queue": {
                "active": [
                    {
                        "id": "parent",
                        "status": "waiting_children",
                        "meta": {"batch_expected_items": 3},
                    },
                    {
                        "id": "stale-child",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                    },
                ],
                "queued": [],
                "recent_failures": [],
            },
            "organize_tasks": {"items": []},
        }
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.remote_refresh.is_lease_expired", return_value=True):
            reason = self.manager._service_busy_reason()

        self.assertEqual(reason["reason"], "")
        self.assertTrue(reason["stale_archive_queue_detected"])

    def test_tick_deferral_records_attempt_without_overwriting_last_completed_message(self):
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 2 * * *"
        self.store.save(config)
        self.task_store.patch_remote_refresh_state(
            next_run_at="2026-05-19T11:00:00+08:00",
            last_message="源端刷新完成，成功 2 个，失败 0 个。",
            last_completed_at="2026-05-18T05:00:00+08:00",
        )
        self.manager._service_busy_reason = lambda: {"busy": True, "reason": "archive_queue_busy", "stale_archive_queue_detected": False}
        original_now = remote_refresh._now
        remote_refresh._now = lambda: datetime.fromisoformat("2026-05-19T12:00:00+08:00")
        try:
            self.manager._tick()
        finally:
            remote_refresh._now = original_now

        state = self.task_store.load_remote_refresh_state()
        self.assertEqual(state["status"], "deferred")
        self.assertEqual(state["last_attempt_at"], "2026-05-19T12:00:00+08:00")
        self.assertEqual(state["last_deferred_at"], "2026-05-19T12:00:00+08:00")
        self.assertEqual(state["last_defer_reason"], "archive_queue_busy")
        self.assertEqual(state["last_completed_at"], "2026-05-18T05:00:00+08:00")
        self.assertEqual(state["last_message"], "源端刷新完成，成功 2 个，失败 0 个。")
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "service_busy_ignores_waiting_children or tick_deferral_records_attempt" -q
```

Expected: fails because `_service_busy_reason()` does not exist and `_tick()` overwrites `last_message`.

- [ ] **Step 3: Import lease helper and add busy-state helpers**

In `app/services/remote_refresh.py`, add:

```python
from app.services.task_runtime import is_lease_expired
```

Inside `RemoteRefreshManager`, replace `_service_busy()` with:

```python
    def _archive_queue_busy_reason(self) -> dict[str, Any]:
        queue = self.task_store.load_archive_queue()
        stale_detected = False
        for item in queue.get("active") or []:
            status = str(item.get("status") or "").strip().lower()
            if status == "waiting_children":
                continue
            if status == "running" and is_lease_expired(item.get("lease_expires_at")):
                stale_detected = True
                continue
            if status in {"running", "queued", "blocked", "paused"}:
                return {"busy": True, "reason": "archive_queue_busy", "stale_archive_queue_detected": stale_detected}
        if queue.get("queued"):
            return {"busy": True, "reason": "archive_queue_busy", "stale_archive_queue_detected": stale_detected}
        return {"busy": False, "reason": "", "stale_archive_queue_detected": stale_detected}

    def _service_busy_reason(self) -> dict[str, Any]:
        archive_reason = self._archive_queue_busy_reason()
        if archive_reason.get("busy"):
            return archive_reason
        organize_tasks = self.task_store.load_organize_tasks()
        for item in organize_tasks.get("items") or []:
            if str(item.get("status") or "").strip().lower() in {"pending", "queued", "running"}:
                return {
                    "busy": True,
                    "reason": "local_organizer_busy",
                    "stale_archive_queue_detected": bool(archive_reason.get("stale_archive_queue_detected")),
                }
        return archive_reason

    def _service_busy(self) -> bool:
        return bool(self._service_busy_reason().get("busy"))
```

- [ ] **Step 4: Update deferral state patching**

In `_tick()`, replace the busy block with:

```python
        busy_reason = self._service_busy_reason()
        if busy_reason.get("busy"):
            now = _now()
            retry_at = now.timestamp() + REMOTE_REFRESH_BATCH_RETRY_SECONDS
            self.task_store.patch_remote_refresh_state(
                status="deferred",
                running=False,
                next_run_at=china_from_timestamp(retry_at).isoformat(),
                scheduled_cron=normalized_cron,
                last_attempt_at=now.isoformat(),
                last_deferred_at=now.isoformat(),
                last_defer_reason=str(busy_reason.get("reason") or "archive_queue_busy"),
                stale_archive_queue_detected=bool(busy_reason.get("stale_archive_queue_detected")),
                current_item={},
            )
            return
```

Do not set `last_message` in this deferral patch.

- [ ] **Step 5: Update manual busy rejection**

In `trigger_manual_refresh()`, use `busy_reason = self._service_busy_reason()` and patch:

```python
                last_attempt_at=_now_iso(),
                last_deferred_at=_now_iso(),
                last_defer_reason=str(busy_reason.get("reason") or "archive_queue_busy"),
                stale_archive_queue_detected=bool(busy_reason.get("stale_archive_queue_detected")),
```

Keep the rejection message user-facing, but do not overwrite last completed batch fields.

- [ ] **Step 6: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_task_state.py -k "service_busy_ignores_waiting_children or tick_deferral_records_attempt or repair_archive_queue" -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py tests/test_task_state.py
git commit -m "fix: 修正源端刷新阻塞判断"
```

---

### Task 7: Protect Runtime State During Forced JSON Migration

**Files:**
- Modify: `app/services/database_migration.py`
- Test: `tests/test_database_json_state.py`

- [ ] **Step 1: Write failing migration protection tests**

Add these tests to `JsonStateDatabaseRoutingTest` in `tests/test_database_json_state.py`:

```python
    def test_force_json_migration_protects_existing_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote_state_path = Path(tmp) / "remote_refresh_state.json"
            remote_state_path.write_text(
                '{"last_run_at":"2026-05-25T03:42:02+08:00","last_success_at":"2026-05-25T05:32:26+08:00"}',
                encoding="utf-8",
            )
            self.state["remote_refresh_state"] = {
                "last_run_at": "2026-06-05T17:53:44+08:00",
                "active_run": {"batch_id": "live", "status": "running"},
            }
            with patch.object(database_migration, "JSON_STATE_FILE_MIGRATIONS", (("remote_refresh_state", remote_state_path, {}),)):
                result = database_migration.migrate_json_files_to_database(force=True)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.state["remote_refresh_state"]["active_run"]["batch_id"], "live")
        self.assertEqual(result["items"][0]["status"], "protected_runtime_state")

    def test_force_json_migration_allows_first_runtime_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "archive_queue.json"
            queue_path.write_text('{"active":[],"queued":[{"id":"q1"}],"recent_failures":[]}', encoding="utf-8")
            with patch.object(database_migration, "JSON_STATE_FILE_MIGRATIONS", (("archive_queue", queue_path, {"active": [], "queued": [], "recent_failures": []}),)):
                result = database_migration.migrate_json_files_to_database(force=True)

        self.assertEqual(result["items"][0]["status"], "updated")
        self.assertEqual(self.state["archive_queue"]["queued"][0]["id"], "q1")

    def test_force_json_migration_restore_marker_allows_runtime_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "remote_refresh_state.json"
            state_path.write_text(
                '{"__makerhub_restore__":true,"last_run_at":"2026-06-06T10:00:00+08:00"}',
                encoding="utf-8",
            )
            self.state["remote_refresh_state"] = {"last_run_at": "2026-06-05T10:00:00+08:00"}
            with patch.object(database_migration, "JSON_STATE_FILE_MIGRATIONS", (("remote_refresh_state", state_path, {}),)):
                result = database_migration.migrate_json_files_to_database(force=True)

        self.assertEqual(result["items"][0]["status"], "restored_runtime_state")
        self.assertEqual(self.state["remote_refresh_state"]["last_run_at"], "2026-06-06T10:00:00+08:00")
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_database_json_state.py -k "force_json_migration_protects_existing_runtime_state or force_json_migration_allows_first_runtime_bootstrap or force_json_migration_restore_marker" -q
```

Expected: first test fails because forced migration overwrites `remote_refresh_state`.

- [ ] **Step 3: Add runtime-key and restore-marker helpers**

In `app/services/database_migration.py`, add after `LOG_FILE_IMPORT_LIMIT`:

```python
RUNTIME_JSON_STATE_KEYS = {
    "archive_queue",
    "missing_3mf",
    "organize_tasks",
    "subscriptions_state",
    "remote_refresh_state",
    "three_mf_limit_guard",
    "three_mf_daily_quota",
    "archive_repair_status",
    "archive_profile_backfill_status",
    "system_update",
}
RESTORE_MARKER_FIELD = "__makerhub_restore__"


def _payload_has_restore_marker(payload: dict[str, Any]) -> bool:
    return bool(isinstance(payload, dict) and payload.get(RESTORE_MARKER_FIELD) is True)


def _strip_restore_marker(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    clean.pop(RESTORE_MARKER_FIELD, None)
    return clean


def _should_protect_runtime_state(key: str, payload: dict[str, Any], default: dict[str, Any]) -> bool:
    if key not in RUNTIME_JSON_STATE_KEYS:
        return False
    if _payload_has_restore_marker(payload):
        return False
    existing = _load_existing_json_state(key)
    existing_payload = existing if isinstance(existing, dict) else {}
    return _payload_has_user_data(existing_payload, default)
```

- [ ] **Step 4: Apply protection inside `migrate_json_files_to_database()`**

Inside the loop, after `payload, source_path = _read_json_migration_payload(key, path, default)`, add:

```python
            restore_runtime_state = key in RUNTIME_JSON_STATE_KEYS and _payload_has_restore_marker(payload)
            if restore_runtime_state:
                payload = _strip_restore_marker(payload)
            if force and _should_protect_runtime_state(key, payload, default):
                result["skipped"] += 1
                item["status"] = "protected_runtime_state"
                item["path"] = source_path.as_posix()
                item["count"] = len(payload.get("items") or []) if isinstance(payload.get("items"), list) else len(payload)
                continue
```

After `save_json_state(key, payload)`, before the default `updated` status, add:

```python
            if restore_runtime_state:
                item["status"] = "restored_runtime_state"
```

- [ ] **Step 5: Include per-key statuses in the business log payload**

Update the `append_business_log()` call at the end of `migrate_json_files_to_database()`:

```python
        items=result["items"],
```

- [ ] **Step 6: Run focused migration tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_database_json_state.py -k "force_json_migration_protects_existing_runtime_state or force_json_migration_allows_first_runtime_bootstrap or force_json_migration_restore_marker or database_migration_preserves_legacy_text_marker_files" -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit Task 7**

```bash
git add app/services/database_migration.py tests/test_database_json_state.py
git commit -m "fix: 保护数据库运行态迁移"
```

---

### Task 8: Update Source Refresh UI And Dashboard Labels

**Files:**
- Modify: `frontend/src/pages/RemoteRefreshPage.vue`
- Modify: `frontend/src/pages/DashboardPage.vue`
- Modify: `frontend/src/lib/pageRefreshShape.test.mjs`

- [ ] **Step 1: Add failing UI shape tests**

Append these tests to `frontend/src/lib/pageRefreshShape.test.mjs`:

```javascript
test("RemoteRefreshPage separates scheduler timestamps and recovery actions", () => {
  assert.match(remoteRefreshPageSource, /上次尝试/);
  assert.match(remoteRefreshPageSource, /上次批次开始/);
  assert.match(remoteRefreshPageSource, /上次完成/);
  assert.match(remoteRefreshPageSource, /最近阻塞/);
  assert.match(remoteRefreshPageSource, /最近中断/);
  assert.match(remoteRefreshPageSource, /继续源端刷新/);
  assert.match(remoteRefreshPageSource, /修复队列状态/);
  assert.match(remoteRefreshPageSource, /\/api\/tasks\/archive-queue\/repair/);
});

test("DashboardPage labels source refresh completion separately from run start", () => {
  assert.match(dashboardPageSource, /最近完成/);
  assert.match(dashboardPageSource, /last_completed_at/);
});
```

- [ ] **Step 2: Run the UI shape tests and verify the expected failure**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: fails because the new labels and repair endpoint are not present on the page.

- [ ] **Step 3: Add computed state helpers in `RemoteRefreshPage.vue`**

In the `<script setup>` section, add:

```javascript
const activeRun = computed(() => remoteRefreshState.value?.active_run || {});
const activeRunRemaining = computed(() => Number(activeRun.value?.remaining_total || 0));
const hasResumableRun = computed(() => {
  const runStatus = String(activeRun.value?.status || "").trim();
  return ["running", "resuming", "interrupted"].includes(runStatus) && activeRunRemaining.value > 0;
});
const showQueueRepairAction = computed(() => Boolean(remoteRefreshState.value?.stale_archive_queue_detected));
const manualActionLabel = computed(() => hasResumableRun.value ? "继续源端刷新" : "手动同步");
const manualBusyLabel = computed(() => hasResumableRun.value ? "继续中..." : "同步中...");
```

Add formatter helpers:

```javascript
function deferReasonLabel(value) {
  const mapping = {
    archive_queue_busy: "归档队列运行中",
    local_organizer_busy: "本地整理运行中",
    stale_runtime_state: "运行态需要修复",
  };
  return mapping[String(value || "").trim()] || "-";
}

function interruptedReasonLabel(value) {
  const clean = String(value || "").trim();
  if (!clean) {
    return "-";
  }
  const mapping = {
    worker_stopped: "Worker 已停止",
    manifest_missing: "批次清单缺失",
    result_path_missing: "结果日志缺失",
  };
  return mapping[clean] || clean;
}
```

- [ ] **Step 4: Replace the settings time grid**

In `RemoteRefreshPage.vue`, replace the three-field settings time grid with:

```vue
      <div class="settings-grid settings-grid--three">
        <label class="field-card">
          <span>下次运行</span>
          <strong>{{ formatDateTime(remoteRefreshState.next_run_at) }}</strong>
        </label>
        <label class="field-card">
          <span>上次尝试</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_attempt_at) }}</strong>
        </label>
        <label class="field-card">
          <span>上次批次开始</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_run_at) }}</strong>
        </label>
        <label class="field-card">
          <span>上次完成</span>
          <strong>{{ formatDateTime(remoteRefreshState.last_completed_at || remoteRefreshState.last_success_at) }}</strong>
        </label>
        <label class="field-card">
          <span>最近阻塞</span>
          <strong>{{ remoteRefreshState.last_deferred_at ? `${formatDateTime(remoteRefreshState.last_deferred_at)} · ${deferReasonLabel(remoteRefreshState.last_defer_reason)}` : "-" }}</strong>
        </label>
        <label class="field-card">
          <span>最近中断</span>
          <strong>{{ remoteRefreshState.last_interrupted_at ? `${formatDateTime(remoteRefreshState.last_interrupted_at)} · ${interruptedReasonLabel(remoteRefreshState.last_interrupted_reason)}` : "-" }}</strong>
        </label>
      </div>
```

- [ ] **Step 5: Update manual and repair action buttons**

Replace the manual button text block with:

```vue
          <button
            class="button button-secondary"
            type="button"
            :disabled="loading || manualSyncing || (remoteRefreshState.running && !hasResumableRun)"
            @click="runRemoteRefreshManually"
          >
            {{ (manualSyncing || remoteRefreshState.running) ? manualBusyLabel : manualActionLabel }}
          </button>
          <button
            v-if="showQueueRepairAction"
            class="button button-secondary"
            type="button"
            :disabled="loading || repairingQueue"
            @click="repairArchiveQueue"
          >
            {{ repairingQueue ? "修复中..." : "修复队列状态" }}
          </button>
```

Add `const repairingQueue = ref(false);` near existing refs.

Add the action:

```javascript
async function repairArchiveQueue() {
  if (repairingQueue.value) {
    return;
  }
  repairingQueue.value = true;
  try {
    const payload = await apiRequest("/api/tasks/archive-queue/repair", {
      method: "POST",
    });
    status.value = payload?.message || "队列状态修复完成。";
    await load({ silent: true });
  } catch (error) {
    status.value = error instanceof Error ? error.message : "队列状态修复失败。";
  } finally {
    repairingQueue.value = false;
  }
}
```

- [ ] **Step 6: Expand source-refresh status labels**

Update `formatRemoteRefreshStatus()` mapping:

```javascript
  const mapping = {
    idle: "空闲",
    running: "运行中",
    resuming: "继续中",
    deferred: "已延后",
    interrupted: "已中断",
    error: "异常",
    disabled: "已停用",
  };
```

- [ ] **Step 7: Update dashboard card labels**

In `frontend/src/pages/DashboardPage.vue`, update the source-refresh meta labels:

```vue
              <span>
                <strong>最近完成</strong>
                {{ formatDateTime(automation.remote_refresh.last_completed_at || automation.remote_refresh.last_success_at) }}
              </span>
              <span>
                <strong>最近状态</strong>
                {{ automation.remote_refresh.last_message || "当前没有源端刷新记录。" }}
              </span>
```

Add defaults to `defaultAutomationOverview.remote_refresh`:

```javascript
    last_attempt_at: "",
    last_deferred_at: "",
    last_defer_reason: "",
    last_interrupted_at: "",
    last_interrupted_reason: "",
    last_completed_at: "",
    active_run: {},
    stale_archive_queue_detected: false,
```

Update `remoteRefreshStatusLabel()` mapping:

```javascript
    resuming: "继续中",
    deferred: "已延后",
    interrupted: "已中断",
```

- [ ] **Step 8: Run UI tests**

Run:

```bash
node --test frontend/src/lib/pageRefreshShape.test.mjs
```

Expected: tests pass.

- [ ] **Step 9: Commit Task 8**

```bash
git add frontend/src/pages/RemoteRefreshPage.vue frontend/src/pages/DashboardPage.vue frontend/src/lib/pageRefreshShape.test.mjs
git commit -m "feat: 优化源端刷新状态展示"
```

---

### Task 9: Regression Suite And Release Metadata

**Files:**
- Modify only when releasing/pushing: `VERSION`
- Modify only when releasing/pushing: `frontend/package.json`
- Modify only when releasing/pushing: `frontend/package-lock.json`
- Modify only when releasing/pushing: `README.md`
- Modify only when releasing/pushing: `CHANGELOG.md`

- [ ] **Step 1: Run backend regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_task_state.py tests/test_database_json_state.py tests/test_archive_worker_batch_retry.py -q
```

Expected: all selected backend tests pass.

- [ ] **Step 2: Run frontend shape tests**

Run:

```bash
node --test frontend/src/lib/*.test.mjs
```

Expected: all frontend unit/shape tests pass.

- [ ] **Step 3: Build frontend**

Run:

```bash
npm --prefix frontend run build
```

Expected: Vite build completes and writes `frontend/dist`.

- [ ] **Step 4: Verify working tree before release metadata**

Run:

```bash
git status --short
```

Expected: only implementation files and generated build output expected by the project are changed. Do not stage unrelated files such as `videos/makerhub-intro/output/`.

- [ ] **Step 5: Bump patch version only when the user asks to push**

Read current versions:

```bash
cat VERSION
node -e "console.log(require('./frontend/package.json').version)"
```

If the current version is `0.9.7`, use `0.9.8`. If it is newer at execution time, increment that version's patch number by one and use that exact new version in:

```text
VERSION
frontend/package.json
frontend/package-lock.json
README.md
CHANGELOG.md
```

The release note entry should use this shape:

```markdown
## 2026-06-06 · v0.9.8

- 源端刷新新增可恢复批次，Worker 重启后会优先继续未完成批次并跳过已完成模型。
- 源端刷新页拆分上次尝试、批次开始、完成、阻塞和中断状态，并提供继续刷新与队列修复动作。
- 强制 JSON 迁移会保护已有 Postgres 运行态，避免旧 JSON 状态覆盖线上最新批次状态。
```

In `README.md`, also update:

```markdown
> 当前版本：`v0.9.8`
```

Keep only the latest three release entries visible above `<details>` and move older visible entries into the existing history block.

- [ ] **Step 6: Verify version consistency when release metadata is changed**

Run:

```bash
python - <<'PY'
from pathlib import Path
import json
version = Path("VERSION").read_text(encoding="utf-8").strip()
package = json.loads(Path("frontend/package.json").read_text(encoding="utf-8"))["version"]
lock = json.loads(Path("frontend/package-lock.json").read_text(encoding="utf-8"))["version"]
readme = Path("README.md").read_text(encoding="utf-8")
changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
assert package == version
assert lock == version
assert f"v{version}" in readme
assert f"v{version}" in changelog
print(version)
PY
```

Expected: prints the new version and exits successfully.

- [ ] **Step 7: Commit release metadata only when it was changed**

```bash
git add VERSION frontend/package.json frontend/package-lock.json README.md CHANGELOG.md
git commit -m "chore: 发布 v0.9.8"
```

If execution-time version is not `0.9.8`, replace the commit message with the actual new version.

- [ ] **Step 8: Final status check**

Run:

```bash
git status --short --branch
```

Expected: branch is ahead by the implementation commits; only unrelated pre-existing untracked files remain.

---

## Self-Review

- Spec coverage:
  - Resumable batch manifest and NDJSON journal: Tasks 2, 3, 4, and 5.
  - Manual resume behavior and startup recovery: Task 5.
  - Busy-state stale archive queue handling and deferral fields: Task 6.
  - Forced JSON migration runtime-state protection: Task 7.
  - UI timestamp split and recovery actions: Task 8.
  - Regression and release metadata with one patch bump at push time: Task 9.
- Placeholder scan:
  - No task uses vague future work language.
  - Every code-changing step includes exact file paths, concrete snippets, commands, and expected results.
- Type consistency:
  - `active_run` field names match the design: `batch_id`, `status`, `started_at`, `resumed_at`, `finished_at`, `scheduled_cron`, `manual`, `candidate_total`, `completed_total`, `remaining_total`, `manifest_path`, `result_path`, `interrupted_reason`.
  - Manual trigger mode values match the API design: `new`, `resume`, `queued_for_worker`, and `rejected`.
