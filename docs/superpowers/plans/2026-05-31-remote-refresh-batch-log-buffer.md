# Remote Refresh Batch Log Buffer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce remote refresh database log/state-event write amplification by buffering per-model batch results in a temporary NDJSON file and publishing only batch-boundary state.

**Architecture:** Keep `TaskStateStore` compatible by adding explicit publish control for remote-refresh state writes. Add a small per-batch NDJSON buffer in `remote_refresh.py`; `_refresh_one()` returns compact result records instead of writing model-level logs/history, and `_run_batch()` aggregates the temp file at batch finish before one final durable state/log update.

**Tech Stack:** Python standard library (`json`, `threading`, `Path`, `uuid`), existing `TaskStateStore`, existing `RemoteRefreshManager`, unittest/pytest test suite.

---

## File Structure

- Modify: `app/services/task_state.py`
  - Add `publish_event` control to remote-refresh-specific save/update/patch/history methods.
  - Keep default behavior publishing events to avoid changing unrelated callers.
- Modify: `app/services/remote_refresh.py`
  - Add `REMOTE_REFRESH_BATCH_DIR`.
  - Add `_RemoteRefreshBatchBuffer` for thread-safe NDJSON append/read/delete.
  - Add helpers for compact model result records, batch aggregation, and stale temp-file cleanup.
  - Refactor `_refresh_one()` to return records instead of writing per-model state/log entries.
  - Refactor `_run_batch()` to write model records to the temp buffer and publish state only at batch start/finish.
- Modify: `tests/test_task_state.py`
  - Verify remote-refresh patching can skip event publication and still publishes by default.
- Modify: `tests/test_remote_refresh.py`
  - Verify batch-boundary publication, temp-file cleanup, summary aggregation, and no per-model success logging.
- No frontend code is expected. Existing SSE behavior should work because batch start and batch finish events are still published.

---

### Task 1: Add Publish Control To Remote Refresh State

**Files:**
- Modify: `app/services/task_state.py`
- Test: `tests/test_task_state.py`

- [ ] **Step 1: Write failing tests for remote-refresh publish control**

Add these tests to `ArchiveQueueStateTest` in `tests/test_task_state.py`, before the `if __name__ == "__main__":` block:

```python
    def test_remote_refresh_patch_can_skip_state_event_publish(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message="批次运行中。",
                publish_event=False,
            )

        self.assertEqual(result["status"], "running")
        self.assertTrue(result["running"])
        self.assertEqual(state["remote_refresh_state"]["status"], "running")
        self.assertEqual(events, [])

    def test_remote_refresh_patch_publishes_state_event_by_default(self):
        state = {}
        events = []
        store = TaskStateStore()
        with patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
            result = store.patch_remote_refresh_state(
                status="running",
                running=True,
                last_message="批次开始。",
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "remote_refresh_state")
        self.assertEqual(events[0][1], "state.changed")
        self.assertEqual(events[0][2]["scope"], "remote_refresh_state")
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py -k "remote_refresh_patch" -q
```

Expected: one failure with `TypeError: patch_remote_refresh_state() got an unexpected keyword argument 'publish_event'`.

- [ ] **Step 3: Add publish control to remote-refresh state internals**

In `app/services/task_state.py`, change `_update_remote_refresh_state()` to accept a keyword-only publish flag:

```python
    def _update_remote_refresh_state(self, updater, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            payload = self._load_remote_refresh_state_unlocked()
            updated = updater(payload)
            if updated is None:
                updated = payload
            result = self._save_remote_refresh_state_unlocked(updated)
        if publish_event:
            self._publish_state_event("remote_refresh_state", result)
        return result
```

Update `save_remote_refresh_state()`:

```python
    def save_remote_refresh_state(self, payload: dict, *, publish_event: bool = True) -> dict:
        with _STATE_LOCK, self._state_file_lock(REMOTE_REFRESH_STATE_PATH):
            result = self._save_remote_refresh_state_unlocked(payload)
        if publish_event:
            self._publish_state_event("remote_refresh_state", result)
        return result
```

Update `patch_remote_refresh_state()`:

```python
    def patch_remote_refresh_state(self, publish_event: bool = True, **changes: Any) -> dict:
        def _mutate(payload: dict) -> dict:
            merged = dict(_normalize_remote_refresh_state(payload))
            for key, value in changes.items():
                if value is None:
                    continue
                if key == "current_item":
                    if isinstance(value, dict) and value:
                        merged[key] = _normalize_task_item(value, "running")
                    elif not value:
                        merged[key] = {}
                        if "current_items" not in changes:
                            merged["current_items"] = []
                    continue
                if key == "current_items":
                    if isinstance(value, list):
                        merged[key] = [
                            _normalize_task_item(item, "running")
                            for item in value
                            if isinstance(item, (dict, str))
                        ][:8]
                    elif not value:
                        merged[key] = []
                    continue
                merged[key] = value
            return merged

        return self._update_remote_refresh_state(_mutate, publish_event=publish_event)
```

Update `append_remote_refresh_history()`:

```python
    def append_remote_refresh_history(self, item: dict, limit: int = 50, *, publish_event: bool = True) -> dict:
        normalized_list = _normalize_remote_refresh_state({"recent_items": [item]}).get("recent_items", [])
        if not normalized_list:
            return self.load_remote_refresh_state()
        target = normalized_list[0]

        def _mutate(payload: dict) -> dict:
            recent_items = [target]
            for existing in payload.get("recent_items") or []:
                normalized = _normalize_task_item(existing, "idle")
                if normalized["id"] and normalized["id"] == target["id"]:
                    continue
                recent_items.append(normalized)
            normalized_payload = dict(payload)
            normalized_payload["recent_items"] = recent_items[: max(int(limit or 0), 1)]
            return normalized_payload

        return self._update_remote_refresh_state(_mutate, publish_event=publish_event)
```

- [ ] **Step 4: Run tests and verify the state API still behaves**

Run:

```bash
.venv/bin/python -m pytest tests/test_task_state.py -k "remote_refresh_patch or state_update_publishes" -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add app/services/task_state.py tests/test_task_state.py
git commit -m "Add remote refresh state publish control"
```

---

### Task 2: Add The Remote Refresh Batch Buffer Helper

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing tests for NDJSON append/read/delete**

Add this test method to `RemoteRefreshManagerTest` in `tests/test_remote_refresh.py`:

```python
    def test_remote_refresh_batch_buffer_writes_reads_and_deletes_records(self):
        buffer = remote_refresh._RemoteRefreshBatchBuffer(
            batch_id="test-batch",
            directory=self.temp_path / "remote_refresh_batches",
        )

        buffer.append(
            {
                "model_dir": "m1",
                "title": "模型 1",
                "url": "https://makerworld.com.cn/model/1",
                "status": "success",
                "message": "完成",
                "updated_at": "2026-05-31T12:00:00+08:00",
                "change_labels": ["已检查，无远端变化"],
                "metrics": {"total_duration_ms": 10},
            }
        )
        buffer.append(
            {
                "model_dir": "m2",
                "title": "模型 2",
                "url": "https://makerworld.com.cn/model/2",
                "status": "failed",
                "message": "失败",
                "updated_at": "2026-05-31T12:01:00+08:00",
                "change_labels": ["刷新失败"],
                "metrics": {"total_duration_ms": 20},
            }
        )
        buffer.close()

        records = buffer.read_records()

        self.assertEqual([item["model_dir"] for item in records], ["m1", "m2"])
        self.assertEqual(records[1]["status"], "failed")
        self.assertTrue(buffer.path.exists())
        buffer.delete()
        self.assertFalse(buffer.path.exists())
```

- [ ] **Step 2: Run the new buffer test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_remote_refresh_batch_buffer_writes_reads_and_deletes_records -q
```

Expected: failure with `AttributeError: module 'app.services.remote_refresh' has no attribute '_RemoteRefreshBatchBuffer'`.

- [ ] **Step 3: Add imports and constants**

In `app/services/remote_refresh.py`, change the imports:

```python
import uuid
```

Add `STATE_DIR` to the settings import:

```python
from app.core.settings import ARCHIVE_DIR, BACKGROUND_TASKS_ENABLED, LOGS_DIR, STATE_DIR
```

Add constants near `REMOTE_REFRESH_LOG_PATH`:

```python
REMOTE_REFRESH_LOG_PATH = LOGS_DIR / "remote_refresh.log"
REMOTE_REFRESH_BATCH_DIR = STATE_DIR / "remote_refresh_batches"
REMOTE_REFRESH_BATCH_BUFFER_KEEP = 5
REMOTE_REFRESH_BATCH_BUFFER_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
```

- [ ] **Step 4: Add JSON-safe and batch buffer helpers**

Add this helper code after `_write_json()` in `app/services/remote_refresh.py`:

```python
def _json_safe_remote_refresh_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_remote_refresh_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_remote_refresh_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_remote_refresh_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _remote_refresh_batch_id() -> str:
    return f"{_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"


class _RemoteRefreshBatchBuffer:
    def __init__(self, *, batch_id: str = "", directory: Optional[Path] = None) -> None:
        self.batch_id = str(batch_id or _remote_refresh_batch_id()).strip()
        self.directory = Path(directory or REMOTE_REFRESH_BATCH_DIR)
        self.path = self.directory / f"{self.batch_id}.ndjson"
        self._lock = threading.Lock()
        self._closed = False
        self.directory.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            return
        line = json.dumps(
            _json_safe_remote_refresh_value(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("remote refresh batch buffer is closed")
            self._handle.write(f"{line}\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._handle.close()
            self._closed = True

    def read_records(self) -> list[dict[str, Any]]:
        self.close()
        records: list[dict[str, Any]] = []
        if not self.path.exists():
            return records
        for raw_line in self.path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def delete(self) -> None:
        self.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
```

- [ ] **Step 5: Run the buffer test**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_remote_refresh_batch_buffer_writes_reads_and_deletes_records -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 2**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "Add remote refresh batch buffer"
```

---

### Task 3: Add Model Record And Batch Aggregation Helpers

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing aggregation tests**

Add these tests to `RemoteRefreshManagerTest`:

```python
    def test_remote_refresh_batch_summary_uses_recent_newest_first_and_failure_samples(self):
        records = [
            remote_refresh._remote_refresh_result_record(
                model_dir="m1",
                title="模型 1",
                url="https://makerworld.com.cn/model/1",
                status="success",
                message="完成",
                metrics={"total_duration_ms": 10},
                change_labels=["已检查，无远端变化"],
            ),
            remote_refresh._remote_refresh_result_record(
                model_dir="m2",
                title="模型 2",
                url="https://makerworld.com.cn/model/2",
                status="failed",
                message="失败",
                metrics={"total_duration_ms": 20},
                change_labels=["刷新失败"],
            ),
            remote_refresh._remote_refresh_result_record(
                model_dir="m3",
                title="模型 3",
                url="https://makerworld.com.cn/model/3",
                status="source_deleted",
                message="源端模型已删除",
                metrics={"total_duration_ms": 30},
                change_labels=["模型源端已删除"],
            ),
        ]

        summary = remote_refresh._remote_refresh_batch_summary(records)

        self.assertEqual(summary["recent_items"][0]["id"], "m3")
        self.assertEqual(summary["recent_items"][1]["id"], "m2")
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["source_deleted"], 1)
        self.assertEqual(summary["failure_samples"][0]["model_dir"], "m2")

    def test_remote_refresh_batch_summary_limits_recent_and_failures(self):
        records = [
            remote_refresh._remote_refresh_result_record(
                model_dir=f"m{index}",
                title=f"模型 {index}",
                url=f"https://makerworld.com.cn/model/{index}",
                status="failed",
                message=f"失败 {index}",
                metrics={"total_duration_ms": index},
                change_labels=["刷新失败"],
            )
            for index in range(60)
        ]

        summary = remote_refresh._remote_refresh_batch_summary(records)

        self.assertEqual(len(summary["recent_items"]), 50)
        self.assertEqual(summary["recent_items"][0]["id"], "m59")
        self.assertEqual(len(summary["failure_samples"]), 10)
```

- [ ] **Step 2: Run the aggregation tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "batch_summary" -q
```

Expected: failures with missing helper attributes.

- [ ] **Step 3: Add result-record and aggregation helpers**

Add this code after `_RemoteRefreshBatchBuffer`:

```python
def _remote_refresh_result_record(
    *,
    model_dir: str,
    title: str,
    url: str,
    status: str,
    message: str,
    metrics: Optional[dict[str, Any]] = None,
    change_labels: Optional[list[Any]] = None,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    clean_model_dir = str(model_dir or "").strip()
    clean_status = str(status or "success").strip() or "success"
    clean_metrics = dict(metrics or {}) if isinstance(metrics, dict) else {}
    clean_meta = dict(meta or {}) if isinstance(meta, dict) else {}
    clean_meta.setdefault("model_dir", clean_model_dir)
    if clean_metrics:
        clean_meta["metrics"] = clean_metrics
    labels = [str(item).strip() for item in (change_labels or []) if str(item).strip()]
    if labels:
        clean_meta["change_labels"] = labels
        clean_meta["change_summary"] = "，".join(labels)
    return {
        "id": clean_model_dir,
        "title": str(title or clean_model_dir or "未命名模型"),
        "url": str(url or ""),
        "status": clean_status,
        "progress": 100 if clean_status in {"success", "source_deleted"} else 0,
        "message": _sanitize_remote_refresh_message(message, clean_status),
        "updated_at": _now_iso(),
        "meta": clean_meta,
    }


def _remote_refresh_batch_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = [item for item in records if isinstance(item, dict)]
    failed_records = [item for item in normalized if str(item.get("status") or "") == "failed"]
    source_deleted_records = [item for item in normalized if str(item.get("status") or "") == "source_deleted"]
    skipped_records = [item for item in normalized if str(item.get("status") or "") == "skipped"]
    failure_samples: list[dict[str, Any]] = []
    for item in failed_records[:10]:
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        failure_samples.append(
            {
                "model_dir": str(meta.get("model_dir") or item.get("id") or ""),
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "message": str(item.get("message") or ""),
            }
        )
    return {
        "records": normalized,
        "recent_items": list(reversed(normalized))[:50],
        "failed": len(failed_records),
        "skipped": len(skipped_records),
        "source_deleted": len(source_deleted_records),
        "failure_samples": failure_samples,
    }
```

- [ ] **Step 4: Run aggregation tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "batch_summary" -q
```

Expected: pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "Add remote refresh batch summary helpers"
```

---

### Task 4: Refactor `_refresh_one()` To Return Records Without Per-Model DB Writes

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Add a failing test for no per-model success logs/history**

Add this test to `RemoteRefreshManagerTest`:

```python
    def test_refresh_one_returns_record_without_model_level_logs_or_history(self):
        config = self.store.load()
        config.cookies[0].cookie = "session=ok"
        self.store.save(config)

        model_root = self.temp_path / "m1"
        model_root.mkdir()
        meta_path = model_root / "meta.json"
        existing_meta = {
            "id": "1",
            "title": "模型 1",
            "url": "https://makerworld.com.cn/zh/models/1",
            "comments": [],
            "instances": [],
            "attachments": [],
        }
        meta_path.write_text(json.dumps(existing_meta, ensure_ascii=False), encoding="utf-8")

        structured_events = []
        business_events = []
        original_structured = remote_refresh._append_remote_refresh_log
        original_business = remote_refresh.append_business_log
        original_job = remote_refresh.run_archive_model_job
        original_upsert = remote_refresh.upsert_archive_snapshot_model
        original_invalidate_detail = remote_refresh.invalidate_model_detail_cache

        def fake_run_archive_model_job(**kwargs):
            fresh_meta = {
                **existing_meta,
                "remoteSync": {"lastMessage": "源端刷新完成。"},
            }
            meta_path.write_text(json.dumps(fresh_meta, ensure_ascii=False), encoding="utf-8")
            return {"stats": {"comments": {"comment_total": 0}}, "missing_3mf": []}

        remote_refresh._append_remote_refresh_log = lambda event, **payload: structured_events.append(event)
        remote_refresh.append_business_log = lambda category, event, message="", **fields: business_events.append(event)
        remote_refresh.run_archive_model_job = fake_run_archive_model_job
        remote_refresh.upsert_archive_snapshot_model = lambda *_args, **_kwargs: True
        remote_refresh.invalidate_model_detail_cache = lambda *_args, **_kwargs: None
        try:
            result = self.manager._refresh_one(
                {
                    "model_dir": "m1",
                    "title": "模型 1",
                    "origin_url": "https://makerworld.com.cn/zh/models/1",
                    "meta_path": str(meta_path),
                },
                index=1,
                total=1,
                config=config,
            )
        finally:
            remote_refresh._append_remote_refresh_log = original_structured
            remote_refresh.append_business_log = original_business
            remote_refresh.run_archive_model_job = original_job
            remote_refresh.upsert_archive_snapshot_model = original_upsert
            remote_refresh.invalidate_model_detail_cache = original_invalidate_detail

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(result["ok"])
        self.assertEqual(result["record"]["status"], "success")
        self.assertEqual(result["record"]["id"], "m1")
        self.assertEqual(state["recent_items"], [])
        self.assertNotIn("model_succeeded", structured_events)
        self.assertNotIn("model_succeeded", business_events)
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_refresh_one_returns_record_without_model_level_logs_or_history -q
```

Expected: fail because `_refresh_one()` writes history/logs and does not return `record`.

- [ ] **Step 3: Remove persisted current-item writes from `_refresh_one()`**

In `_refresh_one()`, replace the initial `current_item` construction and `self._set_current_item(...)` call with local progress only:

```python
        progress_state: dict[str, Any] = {
            "progress": 0,
            "message": f"源端刷新中 {index}/{total}，等待资源",
        }
```

Replace `progress_callback()` with:

```python
        def progress_callback(payload: dict[str, Any]) -> None:
            progress_state["progress"] = int(payload.get("percent") or 0)
            progress_state["message"] = _sanitize_remote_refresh_message(
                payload.get("message") or f"源端刷新中 {index}/{total}",
                f"源端刷新中 {index}/{total}",
            )
```

When asset sync starts, replace the `_set_current_item(...)` call with:

```python
                progress_state["progress"] = 78
                progress_state["message"] = "检测到远端资源变化，正在同步图片、头像和附件资源"
```

Remove the `finally: self._remove_current_item(model_dir)` block at the end of `_refresh_one()`.

- [ ] **Step 4: Return a skipped record instead of writing skipped history/logs**

In the missing-cookie branch, replace `append_remote_refresh_history(...)`, `_append_remote_refresh_log("model_skipped", ...)`, and `_remove_current_item(...)` with:

```python
            model_metrics = {
                "model_dir": model_dir,
                "title": title,
                "total_duration_ms": round((time.perf_counter() - model_started_perf) * 1000, 1),
            }
            record = _remote_refresh_result_record(
                model_dir=model_dir,
                title=title,
                url=origin_url,
                status="skipped",
                message=message,
                metrics=model_metrics,
                change_labels=["缺少 Cookie"],
                meta={
                    "checked_at": _now_iso(),
                },
            )
            return {"ok": True, "skipped": True, "metrics": model_metrics, "record": record}
```

- [ ] **Step 5: Return a success record instead of writing success history/logs**

In the success branch, keep building `history_item` if that is the least invasive change, but rename it to `record` and remove:

```python
            self.task_store.append_remote_refresh_history(history_item)
            _append_remote_refresh_log(...)
            append_business_log(...)
```

The return should become:

```python
            return {"ok": True, "metrics": model_metrics, "record": record}
```

If using the new helper instead of preserving the existing dict shape, build the record as:

```python
            record = _remote_refresh_result_record(
                model_dir=model_dir,
                title=title,
                url=origin_url,
                status="success",
                message=message,
                metrics=model_metrics,
                change_labels=change_labels,
                meta={
                    "checked_at": finalized.get("checked_at"),
                    "added_comments": finalized.get("added_comments"),
                    "preserved_comments": finalized.get("preserved_comments"),
                    "added_instances": finalized.get("added_instances"),
                    "deleted_instances": finalized.get("deleted_instances"),
                    "attachments_added": finalized.get("attachments_added"),
                    "summary_changed": finalized.get("summary_changed"),
                    "new_3mf_download_queued": len(new_3mf_download_items),
                    "new_3mf_download_task_id": str(new_3mf_download_result.get("task_id") or ""),
                },
            )
            return {"ok": True, "metrics": model_metrics, "record": record}
```

- [ ] **Step 6: Return source-deleted and failed records instead of writing model-level logs**

In the `deleted_on_source` branch, remove `append_remote_refresh_history(...)`, `_append_remote_refresh_log("model_deleted_on_source", ...)`, and `append_business_log(...)`. Return:

```python
                record = _remote_refresh_result_record(
                    model_dir=model_dir,
                    title=title,
                    url=origin_url,
                    status="source_deleted",
                    message=message,
                    metrics=model_metrics,
                    change_labels=["模型源端已删除"],
                    meta={
                        "checked_at": _now_iso(),
                    },
                )
                return {"ok": True, "source_deleted": True, "metrics": model_metrics, "record": record}
```

In the generic failure branch, remove `append_remote_refresh_history(...)`, `_append_remote_refresh_log("model_failed", ...)`, and `append_business_log(...)`. Return:

```python
            record = _remote_refresh_result_record(
                model_dir=model_dir,
                title=title,
                url=origin_url,
                status="failed",
                message=message,
                metrics=model_metrics,
                change_labels=["刷新失败"],
                meta={
                    "checked_at": _now_iso(),
                },
            )
            return {"ok": False, "error": message, "metrics": model_metrics, "record": record}
```

- [ ] **Step 7: Update existing `_refresh_one()` test expectations**

In `test_refresh_one_downloads_page_and_comment_assets`, add:

```python
        self.assertEqual(result["record"]["status"], "success")
        self.assertEqual(result["record"]["id"], "m1")
```

Keep the existing assertions for two archive job calls and metrics.

- [ ] **Step 8: Run focused `_refresh_one()` tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "refresh_one" -q
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit Task 4**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "Return remote refresh model records"
```

---

### Task 5: Refactor `_run_batch()` To Use The Temp Buffer And Boundary Events

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Add a failing test for batch-boundary state events and logs**

Add this test to `RemoteRefreshManagerTest`:

```python
    def test_run_batch_buffers_model_results_and_publishes_only_batch_boundaries(self):
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 2
        config = self.store.load()
        items = [
            {"model_dir": "m1", "title": "模型 1", "origin_url": "https://makerworld.com.cn/model/1", "meta_path": str(self.temp_path / "m1" / "meta.json")},
            {"model_dir": "m2", "title": "模型 2", "origin_url": "https://makerworld.com.cn/model/2", "meta_path": str(self.temp_path / "m2" / "meta.json")},
            {"model_dir": "m3", "title": "模型 3", "origin_url": "https://makerworld.com.cn/model/3", "meta_path": str(self.temp_path / "m3" / "meta.json")},
        ]
        self.manager._pick_candidates = lambda: (
            items,
            {
                "eligible_total": 3,
                "selected_total": 3,
                "remaining_total": 0,
                "missing_cookie": 0,
                "local_or_invalid": 0,
            },
        )
        events = []
        structured_events = []
        business_events = []

        def fake_refresh_one(item, *, index, total, config):
            model_dir = str(item.get("model_dir") or "")
            return {
                "ok": model_dir != "m2",
                "error": "失败" if model_dir == "m2" else "",
                "metrics": {
                    "model_dir": model_dir,
                    "title": str(item.get("title") or ""),
                    "comments": 1,
                    "total_duration_ms": 10 + index,
                },
                "record": remote_refresh._remote_refresh_result_record(
                    model_dir=model_dir,
                    title=str(item.get("title") or ""),
                    url=str(item.get("origin_url") or ""),
                    status="failed" if model_dir == "m2" else "success",
                    message="失败" if model_dir == "m2" else "完成",
                    metrics={"comments": 1, "total_duration_ms": 10 + index},
                    change_labels=["刷新失败"] if model_dir == "m2" else ["已检查，无远端变化"],
                ),
            }

        self.manager._refresh_one = fake_refresh_one
        original_structured = remote_refresh._append_remote_refresh_log
        original_business = remote_refresh.append_business_log
        remote_refresh._append_remote_refresh_log = lambda event, **payload: structured_events.append((event, payload))
        remote_refresh.append_business_log = lambda category, event, message="", **fields: business_events.append((event, fields))
        try:
            with patch("app.services.task_state.publish_state_event", side_effect=lambda scope, event_type, payload: events.append((scope, event_type, payload))):
                self.manager._run_batch(config)
        finally:
            remote_refresh._remote_refresh_model_workers = original_workers
            remote_refresh._append_remote_refresh_log = original_structured
            remote_refresh.append_business_log = original_business

        state = self.task_store.load_remote_refresh_state()
        remote_events = [item for item in events if item[0] == "remote_refresh_state"]
        self.assertEqual(len(remote_events), 2)
        self.assertEqual(state["last_batch_succeeded"], 2)
        self.assertEqual(state["last_batch_failed"], 1)
        self.assertEqual(state["last_batch_metrics"]["comments"], 3)
        self.assertEqual(state["recent_items"][0]["id"], "m3")
        self.assertTrue(any(event == "batch_started" for event, _payload in structured_events))
        self.assertTrue(any(event == "batch_finished" for event, _payload in structured_events))
        self.assertFalse(any(event == "model_succeeded" for event, _payload in structured_events))
        finish_payloads = [payload for event, payload in structured_events if event == "batch_finished"]
        self.assertEqual(finish_payloads[-1]["failure_samples"][0]["model_dir"], "m2")
        self.assertFalse(list(remote_refresh.REMOTE_REFRESH_BATCH_DIR.glob("*.ndjson")))
```

- [ ] **Step 2: Run the new batch test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_run_batch_buffers_model_results_and_publishes_only_batch_boundaries -q
```

Expected: failure because `_run_batch()` still patches state after every completed model and does not use the buffer.

- [ ] **Step 3: Patch the test setup to isolate the batch directory**

In `RemoteRefreshManagerTest.setUp()`, store and override the batch directory:

```python
        self.original_remote_refresh_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
```

In `tearDown()`, restore it:

```python
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.original_remote_refresh_batch_dir
```

- [ ] **Step 4: Make `_reset_current_items()` optionally quiet**

Change `_reset_current_items()`:

```python
    def _reset_current_items(self, *, publish_event: bool = True) -> None:
        with self._current_items_lock:
            self._current_items = {}
        self.task_store.patch_remote_refresh_state(current_item={}, current_items=[], publish_event=publish_event)
```

Leave `_set_current_item()` and `_remove_current_item()` unchanged for compatibility, but `_refresh_one()` should no longer call them during batch execution.

- [ ] **Step 5: Create the batch buffer in `_run_batch()`**

At the beginning of `_run_batch()`, after `workers = ...`, add:

```python
        batch_buffer: Optional[_RemoteRefreshBatchBuffer] = None
```

Inside the `try:` block, replace:

```python
            self._reset_current_items()
```

with:

```python
            self._reset_current_items(publish_event=False)
            batch_buffer = _RemoteRefreshBatchBuffer()
```

Keep the following start `patch_remote_refresh_state(...)` call publishing normally. Change its `last_message` text to make the frontend expectation explicit:

```python
                last_message=(
                    f"源端刷新开始，本轮计划处理 {len(candidates)} 个模型，并发 {workers}。"
                    f"运行中不逐个刷新模型结果，批次完成后统一刷新。"
                    f"{_batch_scope_message(eligible_total=int(stats.get('eligible_total') or 0), remaining_total=int(stats.get('remaining_total') or 0))}"
                    if candidates
                    else "当前没有可执行源端刷新的模型。"
                ),
```

- [ ] **Step 6: Replace per-completion state patching with buffer append**

In the `for future in as_completed(future_map):` loop, after building `result`, append the record:

```python
                    record = result.get("record") if isinstance(result.get("record"), dict) else None
                    if record is not None and batch_buffer is not None:
                        batch_buffer.append(record)
```

Keep in-memory counters and `_merge_batch_metrics(...)`, but delete this per-model state write block:

```python
                    processed_total = succeeded + failed
                    remaining_total = max(int(stats.get("eligible_total") or 0) - processed_total, 0)
                    self.task_store.patch_remote_refresh_state(...)
```

Do not replace it with another state update.

- [ ] **Step 7: Aggregate the temp file at batch finish**

Before the final `self.task_store.patch_remote_refresh_state(...)`, add:

```python
            batch_records = batch_buffer.read_records() if batch_buffer is not None else []
            batch_summary = _remote_refresh_batch_summary(batch_records)
            failure_samples = batch_summary["failure_samples"]
            recent_items = batch_summary["recent_items"]
```

In the final state patch, add `recent_items=recent_items` and keep existing final metrics:

```python
                recent_items=recent_items,
```

In the `batch_finished` structured log payload, add:

```python
                source_deleted=batch_summary["source_deleted"],
                failure_samples=failure_samples,
```

In the `append_business_log("remote_refresh", "batch_finished", ...)` fields, add:

```python
                source_deleted=batch_summary["source_deleted"],
                failure_samples=failure_samples,
```

After both final logs succeed, delete the temp file:

```python
            if batch_buffer is not None:
                batch_buffer.delete()
                batch_buffer = None
```

- [ ] **Step 8: Preserve temp files on batch-level error**

In `_run_batch()` there is an outer `finally` that clears `_batch_running`. Keep it. Before leaving `_run_batch()`, ensure the buffer closes without deleting if an exception escapes:

```python
        finally:
            if batch_buffer is not None:
                batch_buffer.close()
            self._set_batch_running(False)
```

If the existing function already has a `finally`, merge this behavior into it rather than adding a second conflicting `finally`.

- [ ] **Step 9: Update the existing concurrency test**

In `test_run_batch_refreshes_models_concurrently()`, update `fake_refresh_one()` return value so each successful result includes a `record`:

```python
            model_dir = str(item.get("model_dir") or "")
            return {
                "ok": True,
                "metrics": {
                    "model_dir": model_dir,
                    "title": str(item.get("title") or ""),
                    "comments": 1,
                    "total_duration_ms": 10,
                },
                "record": remote_refresh._remote_refresh_result_record(
                    model_dir=model_dir,
                    title=str(item.get("title") or ""),
                    url=str(item.get("origin_url") or ""),
                    status="success",
                    message="完成",
                    metrics={"comments": 1, "total_duration_ms": 10},
                    change_labels=["已检查，无远端变化"],
                ),
            }
```

Keep the existing assertions, and add:

```python
        self.assertEqual(len(state["recent_items"]), 3)
```

- [ ] **Step 10: Run batch tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py -k "run_batch" -q
```

Expected: pass.

- [ ] **Step 11: Commit Task 5**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "Buffer remote refresh batch results"
```

---

### Task 6: Add Stale Buffer Recovery And Retention

**Files:**
- Modify: `app/services/remote_refresh.py`
- Test: `tests/test_remote_refresh.py`

- [ ] **Step 1: Write failing tests for stale file retention**

Add this test to `RemoteRefreshManagerTest`:

```python
    def test_cleanup_remote_refresh_batch_buffers_keeps_newest_files(self):
        batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        batch_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for index in range(7):
            path = batch_dir / f"old-{index}.ndjson"
            path.write_text(json.dumps({"index": index}, ensure_ascii=False), encoding="utf-8")
            old_time = time.time() - remote_refresh.REMOTE_REFRESH_BATCH_BUFFER_MAX_AGE_SECONDS - 100 + index
            os.utime(path, (old_time, old_time))
            paths.append(path)

        remote_refresh._cleanup_remote_refresh_batch_buffers()

        remaining = sorted(path.name for path in batch_dir.glob("*.ndjson"))
        self.assertLessEqual(len(remaining), remote_refresh.REMOTE_REFRESH_BATCH_BUFFER_KEEP)
        self.assertIn("old-6.ndjson", remaining)
```

This test uses `os.utime`. Add this import at the top of `tests/test_remote_refresh.py` with the other standard-library imports:

```python
import os
```

- [ ] **Step 2: Run stale cleanup test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_cleanup_remote_refresh_batch_buffers_keeps_newest_files -q
```

Expected: failure because `_cleanup_remote_refresh_batch_buffers` does not exist.

- [ ] **Step 3: Add cleanup helper**

Add this helper after `_RemoteRefreshBatchBuffer`:

```python
def _cleanup_remote_refresh_batch_buffers(
    *,
    directory: Optional[Path] = None,
    keep: int = REMOTE_REFRESH_BATCH_BUFFER_KEEP,
    max_age_seconds: int = REMOTE_REFRESH_BATCH_BUFFER_MAX_AGE_SECONDS,
) -> None:
    batch_dir = Path(directory or REMOTE_REFRESH_BATCH_DIR)
    if not batch_dir.exists():
        return
    now = time.time()
    files = sorted(
        [path for path in batch_dir.glob("*.ndjson") if path.is_file()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for index, path in enumerate(files):
        try:
            age = now - path.stat().st_mtime
            keep_count = max(int(keep or 0), 0)
            if index >= keep_count and age > max_age_seconds:
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue
```

- [ ] **Step 4: Call cleanup at safe boundaries**

At the start of `_run_batch()`, before creating the new buffer, call:

```python
        _cleanup_remote_refresh_batch_buffers()
```

In `_ensure_state()`, do not delete stale files when `stale_running` is detected. Existing state recovery can continue to mark the task idle/error; retained files are for diagnostics.

- [ ] **Step 5: Run stale cleanup test**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py::RemoteRefreshManagerTest::test_cleanup_remote_refresh_batch_buffers_keeps_newest_files -q
```

Expected: pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add app/services/remote_refresh.py tests/test_remote_refresh.py
git commit -m "Clean stale remote refresh batch buffers"
```

---

### Task 7: Full Verification And Release Notes

**Files:**
- Modify: `VERSION`
- Modify: `README.md` if it contains release notes for the latest three releases.
- Test: no new test file.

- [ ] **Step 1: Run focused test suites**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_task_state.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run related log/state tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_business_logs.py tests/test_database_json_state.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run syntax check**

Run:

```bash
.venv/bin/python -m py_compile app/services/remote_refresh.py app/services/task_state.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Run diff whitespace check**

Run:

```bash
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 5: Bump patch version**

Inspect current version:

```bash
cat VERSION
```

If the current version is `0.8.12`, change it to:

```text
0.8.13
```

Use the next patch version if `VERSION` has already moved.

- [ ] **Step 6: Update release notes**

If `README.md` has a release notes section, add one latest entry:

```markdown
### v0.8.13

- Reduced source refresh database write pressure by buffering per-model results in a temporary NDJSON file during a batch.
- Source refresh now publishes running state at batch start and final counts/results at batch completion instead of refreshing per model.
- Preserved failure summaries and stale batch buffers for interrupted refresh diagnostics.
```

Keep only the latest three releases expanded if the README already follows that convention.

- [ ] **Step 7: Run final focused verification after docs/version changes**

Run:

```bash
.venv/bin/python -m pytest tests/test_remote_refresh.py tests/test_task_state.py tests/test_business_logs.py tests/test_database_json_state.py -q
git diff --check
```

Expected: tests pass and diff check is clean.

- [ ] **Step 8: Review staged files explicitly**

Run:

```bash
git status --short
git diff --stat
```

Expected changed files are limited to the implementation, tests, version, and release notes:

- `app/services/remote_refresh.py`
- `app/services/task_state.py`
- `tests/test_remote_refresh.py`
- `tests/test_task_state.py`
- `VERSION`
- `README.md` if release notes exist there

- [ ] **Step 9: Commit final verification/docs**

```bash
git add VERSION README.md app/services/remote_refresh.py app/services/task_state.py tests/test_remote_refresh.py tests/test_task_state.py
git commit -m "Reduce remote refresh batch write frequency"
```

If `README.md` was not changed, omit it from `git add`.

---

## Self-Review Checklist

- Spec coverage:
  - Temporary NDJSON buffer: Task 2 and Task 5.
  - Batch start/finish state boundaries: Task 5.
  - No per-model success/progress DB logs: Task 4 and Task 5.
  - Frontend refresh only after batch completion: Task 5 final state-event behavior.
  - Failure summary and stale temp files: Task 3, Task 5, Task 6.
  - No concurrency throttling: no task changes worker count or request limits.
- Placeholder scan:
  - The plan contains no unresolved placeholders or open-ended implementation instructions.
- Type consistency:
  - `publish_event` is used consistently in `TaskStateStore`.
  - `_RemoteRefreshBatchBuffer` consistently exposes `append()`, `read_records()`, `close()`, and `delete()`.
  - `_refresh_one()` returns `record` dictionaries consumed by `_run_batch()`.
