import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import patch

from app.core.store import JsonStore
from app.services import remote_refresh
from app.services.remote_refresh import RemoteRefreshManager
import app.services.task_state as task_state_module
from app.services.task_state import TaskStateStore


class RemoteRefreshManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.original_remote_refresh_state_path = task_state_module.REMOTE_REFRESH_STATE_PATH
        self.original_append_remote_refresh_log = remote_refresh._append_remote_refresh_log
        self.original_append_business_log = remote_refresh.append_business_log
        self.original_invalidate_archive_snapshot = remote_refresh.invalidate_archive_snapshot

        task_state_module.REMOTE_REFRESH_STATE_PATH = self.temp_path / "remote_refresh_state.json"
        remote_refresh._append_remote_refresh_log = lambda *_args, **_kwargs: None
        remote_refresh.append_business_log = lambda *_args, **_kwargs: None
        remote_refresh.invalidate_archive_snapshot = lambda *_args, **_kwargs: None
        self.db_state = {}
        self.db_patches = [
            patch.object(
                task_state_module,
                "load_database_json_state",
                side_effect=lambda key, default: dict(self.db_state.get(key) or default),
            ),
            patch.object(
                task_state_module,
                "save_database_json_state",
                side_effect=lambda key, value: self.db_state.__setitem__(key, value) or value,
            ),
            patch.object(remote_refresh, "_read_three_mf_limit_guard", return_value={}),
        ]
        for item in self.db_patches:
            item.start()

        self.store = JsonStore(self.temp_path / "config.json")
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 0 * * *"
        self.store.save(config)

        self.task_store = TaskStateStore()
        self.manager = RemoteRefreshManager(
            store=self.store,
            task_store=self.task_store,
            archive_manager=None,
        )

    def tearDown(self):
        self.manager._set_batch_running(False)
        for item in reversed(self.db_patches):
            item.stop()
        task_state_module.REMOTE_REFRESH_STATE_PATH = self.original_remote_refresh_state_path
        remote_refresh._append_remote_refresh_log = self.original_append_remote_refresh_log
        remote_refresh.append_business_log = self.original_append_business_log
        remote_refresh.invalidate_archive_snapshot = self.original_invalidate_archive_snapshot
        self.temp_dir.cleanup()

    def test_manual_trigger_marks_state_running_when_accepted(self):
        self.manager._service_busy = lambda: False
        self.manager._start_batch_async = lambda _config, *, resume=False: True

        result = self.manager.trigger_manual_refresh()

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["mode"], "new")
        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])
        self.assertIn("已手动触发一轮源端同步", state["last_message"])

    def test_manual_trigger_resumes_existing_active_run(self):
        self.task_store.patch_remote_refresh_state(
            status="interrupted",
            running=False,
            active_run={
                "batch_id": "resume-batch",
                "status": "interrupted",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 3,
                "completed_total": 1,
                "remaining_total": 2,
                "manifest_path": "remote_refresh_batches/resume-batch.manifest.json",
                "result_path": "remote_refresh_batches/resume-batch.ndjson",
            },
        )
        self.manager._service_busy = lambda: False
        started_modes = []

        def fake_start(_config, *, resume=False):
            started_modes.append("resume" if resume else "new")
            return True

        self.manager._start_batch_async = fake_start

        result = self.manager.trigger_manual_refresh()

        state = self.task_store.load_remote_refresh_state()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["mode"], "resume")
        self.assertEqual(started_modes, ["resume"])
        self.assertEqual(state["status"], "resuming")
        self.assertTrue(state["running"])
        self.assertIn("恢复", state["last_message"])

    def test_manual_trigger_rejects_when_service_busy(self):
        self.manager._service_busy_reason = lambda: "archive_queue_busy"

        result = self.manager.trigger_manual_refresh()

        state = self.task_store.load_remote_refresh_state()
        self.assertFalse(result["accepted"])
        self.assertEqual(result["mode"], "rejected")
        self.assertEqual(state["status"], "idle")
        self.assertFalse(state["running"])
        self.assertIn("请稍后再试手动同步", state["last_message"])

    def test_state_payload_keeps_running_status_for_manual_sync_when_schedule_disabled(self):
        config = self.store.load()
        config.remote_refresh.enabled = False
        self.store.save(config)
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            last_message="已手动触发一轮源端同步，正在启动。",
        )
        self.manager._set_batch_running(True)

        state = self.manager.state_payload()

        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])
        self.assertIn("已手动触发一轮源端同步", state["last_message"])

    def test_remote_refresh_state_preserves_parallel_items_and_metrics(self):
        state = self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            current_items=[
                {"id": "m1", "title": "模型 1", "progress": 20, "message": "运行中"},
                {"id": "m2", "title": "模型 2", "progress": 40, "message": "运行中"},
            ],
            last_batch_metrics={"comments": 12, "replies": 5},
            last_resource_waits={"disk_io": {"wait_count": 1}},
            last_slow_models=[{"model_dir": "m1", "total_duration_ms": 1200}],
        )

        self.assertEqual(len(state["current_items"]), 2)
        self.assertEqual(state["current_item"]["id"], "m1")
        self.assertEqual(state["last_batch_metrics"]["comments"], 12)
        self.assertEqual(state["last_resource_waits"]["disk_io"]["wait_count"], 1)
        self.assertEqual(state["last_slow_models"][0]["model_dir"], "m1")

    def test_remote_refresh_model_workers_uses_advanced_config(self):
        config = SimpleNamespace(
            advanced=SimpleNamespace(remote_refresh_model_workers=3)
        )
        high_config = SimpleNamespace(
            advanced=SimpleNamespace(remote_refresh_model_workers=99)
        )

        self.assertEqual(remote_refresh._remote_refresh_model_workers(config), 3)
        self.assertEqual(remote_refresh._remote_refresh_model_workers(high_config), 4)

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

    def test_cleanup_remote_refresh_batch_buffers_keeps_newest_files(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        try:
            batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
            batch_dir.mkdir(parents=True, exist_ok=True)
            for index in range(7):
                path = batch_dir / f"old-{index}.ndjson"
                path.write_text(json.dumps({"index": index}, ensure_ascii=False), encoding="utf-8")
                old_time = time.time() - remote_refresh.REMOTE_REFRESH_BATCH_BUFFER_MAX_AGE_SECONDS - 100 + index
                os.utime(path, (old_time, old_time))

            remote_refresh._cleanup_remote_refresh_batch_buffers()

            remaining = sorted(path.name for path in batch_dir.glob("*.ndjson"))
            self.assertLessEqual(len(remaining), remote_refresh.REMOTE_REFRESH_BATCH_BUFFER_KEEP)
            self.assertIn("old-6.ndjson", remaining)
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir

    def test_state_payload_reschedules_when_cron_changes_from_app_container(self):
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 2 * * *"
        self.store.save(config)
        self.task_store.patch_remote_refresh_state(
            status="idle",
            running=False,
            next_run_at="2026-05-20T00:00:00+08:00",
            scheduled_cron="0 0 * * *",
            last_message="等待下一轮源端刷新。",
        )
        manager = RemoteRefreshManager(
            store=self.store,
            task_store=self.task_store,
            archive_manager=None,
            background_enabled=False,
        )
        original_now = remote_refresh._now
        remote_refresh._now = lambda: datetime.fromisoformat("2026-05-19T12:00:00+08:00")

        try:
            state = manager.state_payload()
        finally:
            remote_refresh._now = original_now

        self.assertEqual(state["next_run_at"], "2026-05-20T02:00:00+08:00")
        self.assertEqual(state["scheduled_cron"], "0 2 * * *")

    def test_app_container_state_payload_does_not_clear_worker_running_state(self):
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            next_run_at="2026-05-20T00:00:00+08:00",
            scheduled_cron="0 0 * * *",
            last_message="源端刷新进行中。",
            current_item={"id": "m1", "title": "模型 1", "progress": 40},
        )
        manager = RemoteRefreshManager(
            store=self.store,
            task_store=self.task_store,
            archive_manager=None,
            background_enabled=False,
        )

        state = manager.state_payload()

        self.assertEqual(state["status"], "running")
        self.assertTrue(state["running"])
        self.assertEqual(state["current_item"]["id"], "m1")

    def test_tick_when_service_busy_uses_configured_cron_without_scheduler_error(self):
        config = self.store.load()
        config.remote_refresh.enabled = True
        config.remote_refresh.cron = "0 2 * * *"
        self.store.save(config)
        self.task_store.patch_remote_refresh_state(
            status="idle",
            running=False,
            next_run_at="2026-05-19T10:00:00+08:00",
            scheduled_cron="0 2 * * *",
            last_message="等待下一轮源端刷新。",
        )
        self.manager._service_busy_reason = lambda: "archive_queue_busy"
        original_now = remote_refresh._now
        remote_refresh._now = lambda: datetime.fromisoformat("2026-05-19T12:00:00+08:00")
        original_now_iso = remote_refresh._now_iso
        remote_refresh._now_iso = lambda: "2026-05-19T12:00:00+08:00"

        try:
            self.manager._tick()
        finally:
            remote_refresh._now = original_now
            remote_refresh._now_iso = original_now_iso

        state = self.task_store.load_remote_refresh_state()
        self.assertEqual(state["status"], "deferred")
        self.assertEqual(state["scheduled_cron"], "0 2 * * *")
        self.assertEqual(state["last_message"], "等待下一轮源端刷新。")
        self.assertEqual(state["last_attempt_at"], "2026-05-19T12:00:00+08:00")
        self.assertEqual(state["last_deferred_at"], "2026-05-19T12:00:00+08:00")
        self.assertEqual(state["last_defer_reason"], "archive_queue_busy")

    def test_service_busy_reason_ignores_waiting_children_and_stale_active_queue(self):
        self.task_store.save_archive_queue(
            {
                "active": [
                    {
                        "id": "batch-parent",
                        "title": "批次父任务",
                        "status": "waiting_children",
                        "meta": {"batch_expected_items": [{"id": "child-1"}]},
                    },
                    {
                        "id": "stale-child",
                        "title": "过期子任务",
                        "status": "running",
                        "lease_expires_at": "2026-06-04T09:00:00+08:00",
                    },
                ],
                "queued": [],
                "recent_failures": [],
            }
        )

        with patch("app.services.remote_refresh.is_lease_expired", return_value=True):
            reason = self.manager._service_busy_reason()

        state = self.task_store.load_remote_refresh_state()
        self.assertEqual(reason, "")
        self.assertTrue(state["stale_archive_queue_detected"])

    def test_service_busy_reason_blocks_fresh_active_and_queued_work(self):
        self.task_store.save_archive_queue(
            {
                "active": [
                    {
                        "id": "fresh-child",
                        "title": "新鲜子任务",
                        "status": "running",
                        "lease_expires_at": "2099-06-04T09:00:00+08:00",
                    }
                ],
                "queued": [],
                "recent_failures": [],
            }
        )

        with patch("app.services.remote_refresh.is_lease_expired", return_value=False):
            reason = self.manager._service_busy_reason()

        self.assertEqual(reason, "archive_queue_busy")

        self.task_store.save_archive_queue(
            {
                "active": [],
                "queued": [{"id": "queued-1", "title": "等待归档", "status": "queued"}],
                "recent_failures": [],
            }
        )

        self.assertEqual(self.manager._service_busy_reason(), "archive_queue_busy")

    def test_tick_resumes_active_run_before_future_schedule(self):
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            next_run_at="2099-05-19T10:00:00+08:00",
            scheduled_cron="0 0 * * *",
            active_run={
                "batch_id": "resume-batch",
                "status": "running",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 2,
                "completed_total": 1,
                "remaining_total": 1,
                "manifest_path": "remote_refresh_batches/resume-batch.manifest.json",
                "result_path": "remote_refresh_batches/resume-batch.ndjson",
            },
        )
        resumed = []
        self.manager._service_busy = lambda: False
        self.manager._resume_active_run_if_possible = lambda _config: resumed.append(True) or True
        self.manager._run_batch = lambda _config: self.fail("resumable active run must not start a new batch")

        self.manager._tick()

        self.assertEqual(resumed, [True])

    def test_asset_signature_ignores_volatile_cdn_query_params(self):
        existing_meta = {
            "cover": {
                "url": "https://cdn.example.com/model/design/cover.jpg?x-oss-process=image/resize,w_512&Expires=100&Signature=old",
            },
            "designImages": [
                {
                    "index": 1,
                    "originalUrl": "https://cdn.example.com/model/design/cover.jpg?x-oss-process=image/resize,w_512&Expires=100&Signature=old",
                }
            ],
            "summaryImages": [
                {
                    "index": 1,
                    "originalUrl": "https://cdn.example.com/model/summary/a.png?token=old&w=800",
                }
            ],
            "author": {
                "avatarUrl": "https://cdn.example.com/avatar/user.jpg?auth_key=old",
            },
            "instances": [
                {
                    "id": "i1",
                    "pictures": [
                        {
                            "url": "https://cdn.example.com/model/instance/pic.png?X-Amz-Date=old&X-Amz-Signature=old",
                        }
                    ],
                    "plates": [
                        {
                            "thumbnailUrl": "https://cdn.example.com/model/instance/plate.png?image_process=resize,w_256",
                        }
                    ],
                }
            ],
            "comments": [
                {
                    "id": "c1",
                    "author": {
                        "avatarUrl": "https://cdn.example.com/avatar/commenter.jpg?token=old",
                    },
                    "images": [
                        {
                            "url": "https://cdn.example.com/comment/image.jpg?thumb=small&Expires=100",
                        }
                    ],
                }
            ],
        }
        fresh_meta = {
            "cover": {
                "url": "https://cdn.example.com/model/design/cover.jpg?x-oss-process=image/resize,w_1024&Expires=200&Signature=new",
            },
            "designImages": [
                {
                    "index": 1,
                    "originalUrl": "https://cdn.example.com/model/design/cover.jpg?x-oss-process=image/resize,w_1024&Expires=200&Signature=new",
                }
            ],
            "summaryImages": [
                {
                    "index": 1,
                    "originalUrl": "https://cdn.example.com/model/summary/a.png?token=new&w=1200",
                }
            ],
            "author": {
                "avatarUrl": "https://cdn.example.com/avatar/user.jpg?auth_key=new",
            },
            "instances": [
                {
                    "id": "i1",
                    "pictures": [
                        {
                            "url": "https://cdn.example.com/model/instance/pic.png?X-Amz-Date=new&X-Amz-Signature=new",
                        }
                    ],
                    "plates": [
                        {
                            "thumbnailUrl": "https://cdn.example.com/model/instance/plate.png?image_process=resize,w_512",
                        }
                    ],
                }
            ],
            "comments": [
                {
                    "id": "c1",
                    "author": {
                        "avatarUrl": "https://cdn.example.com/avatar/commenter.jpg?token=new",
                    },
                    "images": [
                        {
                            "url": "https://cdn.example.com/comment/image.jpg?thumb=large&Expires=200",
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            remote_refresh._asset_url_signature(existing_meta),
            remote_refresh._asset_url_signature(fresh_meta),
        )

        changed_meta = {
            **fresh_meta,
            "designImages": [
                {
                    "index": 1,
                    "originalUrl": "https://cdn.example.com/model/design/changed.jpg?x-oss-process=image/resize,w_1024",
                }
            ],
        }
        self.assertNotEqual(
            remote_refresh._asset_url_signature(existing_meta),
            remote_refresh._asset_url_signature(changed_meta),
        )

    def test_refresh_one_downloads_page_and_comment_assets(self):
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

        calls: list[dict[str, object]] = []
        original_job = remote_refresh.run_archive_model_job
        original_upsert = remote_refresh.upsert_archive_snapshot_model
        original_invalidate_snapshot = remote_refresh.invalidate_archive_snapshot
        original_invalidate_detail = remote_refresh.invalidate_model_detail_cache

        def fake_run_archive_model_job(**kwargs):
            calls.append(dict(kwargs))
            fresh_meta = {
                **existing_meta,
                "comments": [
                    {
                        "id": "c1",
                        "content": "新评论",
                        "author": {
                            "name": "用户",
                            "avatarUrl": "https://public-cdn.example.com/avatar/user.png",
                            "avatarRelPath": "_shared/avatars/avatar.png",
                        },
                        "images": [
                            {
                                "url": "https://public-cdn.example.com/comment/image.jpg",
                                "relPath": "images/comment_01_img_01.jpg",
                            }
                        ],
                    }
                ],
                "commentCount": 1,
            }
            meta_path.write_text(json.dumps(fresh_meta, ensure_ascii=False), encoding="utf-8")
            return {
                "stats": {
                    "comments": {
                        "comment_total": 1,
                        "comment_roots": 1,
                        "reply_total": 0,
                        "comment_images": 1,
                        "avatar_urls": 1,
                        "download_tasks": 2,
                    }
                },
                "missing_3mf": [],
            }

        remote_refresh.run_archive_model_job = fake_run_archive_model_job
        remote_refresh.upsert_archive_snapshot_model = lambda *_args, **_kwargs: True
        remote_refresh.invalidate_archive_snapshot = lambda *_args, **_kwargs: None
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
            remote_refresh.run_archive_model_job = original_job
            remote_refresh.upsert_archive_snapshot_model = original_upsert
            remote_refresh.invalidate_archive_snapshot = original_invalidate_snapshot
            remote_refresh.invalidate_model_detail_cache = original_invalidate_detail

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0]["download_assets"], False)
        self.assertIs(calls[0]["download_comment_assets"], False)
        self.assertIs(calls[1]["download_assets"], True)
        self.assertIs(calls[1]["download_comment_assets"], True)
        self.assertEqual(result["metrics"]["download_tasks"], 2)
        self.assertEqual(result["record"]["status"], "success")
        self.assertEqual(result["record"]["id"], "m1")

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

    def test_run_batch_buffers_model_results_and_publishes_only_batch_boundaries(self):
        original_workers = remote_refresh._remote_refresh_model_workers
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 2
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
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
            time.sleep(index * 0.01)
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
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir
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
        self.assertFalse(list((self.temp_path / "remote_refresh_batches").glob("*.ndjson")))

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

    def test_resume_active_run_skips_completed_manifest_entries(self):
        original_batch_dir = remote_refresh.REMOTE_REFRESH_BATCH_DIR
        original_workers = remote_refresh._remote_refresh_model_workers
        remote_refresh.REMOTE_REFRESH_BATCH_DIR = self.temp_path / "remote_refresh_batches"
        remote_refresh._remote_refresh_model_workers = lambda _config=None: 1
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

        def fake_refresh_one(item, *, index, total, config):
            refreshed.append(item["model_dir"])
            return {
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

        self.manager._refresh_one = fake_refresh_one

        try:
            resumed = self.manager._resume_active_run_if_possible(self.store.load())
        finally:
            remote_refresh.REMOTE_REFRESH_BATCH_DIR = original_batch_dir
            remote_refresh._remote_refresh_model_workers = original_workers

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

    def test_resume_active_run_marks_missing_manifest_interrupted(self):
        self.task_store.patch_remote_refresh_state(
            status="running",
            running=True,
            active_run={
                "batch_id": "missing-batch",
                "status": "running",
                "started_at": "2026-06-06T09:00:00+08:00",
                "candidate_total": 2,
                "completed_total": 1,
                "remaining_total": 1,
                "manifest_path": "remote_refresh_batches/missing-batch.manifest.json",
                "result_path": "remote_refresh_batches/missing-batch.ndjson",
            },
        )

        resumed = self.manager._resume_active_run_if_possible(self.store.load())

        state = self.task_store.load_remote_refresh_state()
        self.assertFalse(resumed)
        self.assertFalse(state["running"])
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["active_run"]["status"], "interrupted")
        self.assertEqual(state["last_interrupted_reason"], "manifest_missing")

    def test_run_batch_refreshes_models_concurrently(self):
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
        first_two_started = threading.Event()
        release = threading.Event()
        started: list[str] = []
        started_lock = threading.Lock()

        def fake_refresh_one(item, *, index, total, config):
            model_dir = str(item.get("model_dir") or "")
            with started_lock:
                started.append(model_dir)
                if len(started) == 2:
                    first_two_started.set()
            first_two_started.wait(timeout=2)
            release.wait(timeout=2)
            time.sleep(0.01)
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

        self.manager._refresh_one = fake_refresh_one
        try:
            runner = threading.Thread(target=lambda: self.manager._run_batch(config), daemon=True)
            runner.start()
            self.assertTrue(first_two_started.wait(timeout=2))
            with started_lock:
                self.assertEqual(len(started), 2)
            release.set()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive())
        finally:
            remote_refresh._remote_refresh_model_workers = original_workers
            release.set()

        state = self.task_store.load_remote_refresh_state()
        self.assertEqual(state["last_batch_succeeded"], 3)
        self.assertEqual(state["last_batch_failed"], 0)
        self.assertEqual(state["last_batch_metrics"]["comments"], 3)
        self.assertEqual(len(state["last_slow_models"]), 3)
        self.assertEqual(len(state["recent_items"]), 3)


if __name__ == "__main__":
    unittest.main()
