import json
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app.services.archive_worker as archive_worker_module
import app.services.legacy_archiver as legacy_archiver_module
import app.services.task_state as task_state_module
import app.services.three_mf_quota as three_mf_quota_module
from app.services.legacy_archiver import (
    _build_instance_api_candidates,
    _missing_3mf_instances,
    _missing_3mf_failure_for_skipped_fetch,
    _should_pause_three_mf_fetch,
    download_file,
    fetch_instance_3mf,
    rebuild_once,
)
from app.services.archive_worker import ArchiveTaskManager
from app.services.remote_refresh import _build_missing_3mf_items
from app.services.task_state import _normalize_missing_3mf


class Missing3mfTest(unittest.TestCase):
    def test_daily_limit_guard_uses_full_24_hour_cooldown(self):
        fixed_now = datetime(2026, 8, 8, 9, 24, 55, tzinfo=timezone(timedelta(hours=8)))

        with patch.object(archive_worker_module, "_three_mf_limit_now", return_value=fixed_now):
            limited_until = archive_worker_module._three_mf_limit_until()

        self.assertEqual(limited_until, "2026-08-09T09:24:55+08:00")

    def test_daily_limit_guard_default_message_matches_rolling_cooldown(self):
        message = archive_worker_module._three_mf_limit_message({})

        self.assertEqual(message, "已达到 MakerWorld 每日下载上限，暂时停止自动重试。")

    def test_terminal_and_technical_failures_pause_following_instance_fetches(self):
        for state in (
            "verification_required",
            "cloudflare",
            "auth_required",
            "cookie_invalid",
            "download_limited",
            "http_error",
        ):
            with self.subTest(state=state):
                self.assertTrue(_should_pause_three_mf_fetch({"state": state, "message": "blocked"}))

        self.assertFalse(_should_pause_three_mf_fetch({"state": "missing", "message": "no download address"}))

    def test_skipped_refresh_does_not_reuse_stale_download_limited_state(self):
        failure = _missing_3mf_failure_for_skipped_fetch(
            skip_state="pending_download",
            existing_state="download_limited",
            existing_message="国区返回了每日下载上限，今日暂停自动重试。",
            fetch_url="https://makerworld.com.cn/zh/models/2351687",
        )

        self.assertEqual(failure["state"], "pending_download")
        self.assertNotIn("每日下载上限", failure["message"])

    def test_active_limit_guard_can_preserve_download_limited_state(self):
        failure = _missing_3mf_failure_for_skipped_fetch(
            skip_state="download_limited",
            skip_message="国区返回了每日下载上限，今日暂停自动重试。",
            existing_state="download_limited",
            existing_message="旧的每日下载上限消息",
            fetch_url="https://makerworld.com.cn/zh/models/2351687",
        )

        self.assertEqual(failure["state"], "download_limited")
        self.assertEqual(failure["message"], "旧的每日下载上限消息")

    def test_normalize_keeps_missing_3mf_items(self):
        payload = {
            "items": [
                {
                    "model_id": "1686848",
                    "title": "仪羽毛球（球头无挖孔）",
                    "status": "missing",
                    "message": "未获取到 3MF 下载地址。",
                },
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["model_id"], "1686848")

    def test_remote_refresh_missing_builder_keeps_missing_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meta_path = Path(temp_dir) / "model" / "meta.json"
            meta_path.parent.mkdir(parents=True)
            meta = {
                "id": "1686848",
                "url": "https://makerworld.com.cn/zh/models/1686848",
                "title": "仪羽毛球（球头无挖孔）",
                "instances": [
                    {
                        "id": "profile-1",
                        "title": "缺失配置",
                        "downloadState": "missing",
                        "downloadMessage": "未获取到 3MF 下载地址。",
                    },
                ],
            }

            items = _build_missing_3mf_items(meta_path, meta, resolved_files={"matches": {}})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["instance_id"], "profile-1")

    def test_remote_refresh_missing_builder_skips_platform_print_only_instances(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            meta_path = Path(temp_dir) / "model" / "meta.json"
            meta_path.parent.mkdir(parents=True)
            meta = {
                "id": "2618766",
                "url": "https://makerworld.com.cn/zh/models/2618766",
                "title": "Print only demo",
                "license": "Standard Digital File License - Platform Print Only (SDFL-PPO)",
                "threeMfDownloadAllowed": False,
                "instances": [
                    {
                        "id": "3020206",
                        "title": "0.2mm profile",
                        "downloadState": "missing",
                        "downloadMessage": "未获取到 3MF 下载地址。",
                    },
                ],
            }

            items = _build_missing_3mf_items(meta_path, meta, resolved_files={"matches": {}})

        self.assertEqual(items, [])

    def test_normalize_infers_verification_status_from_message(self):
        payload = {
            "items": [
                {
                    "model_id": "1759345",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "missing",
                    "model_url": "https://makerworld.com.cn/zh/models/1759345",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                }
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "verification_required")
        self.assertEqual(normalized["items"][0]["message"], "MakerWorld 需要验证，前往官网任意下载一个模型。")

    def test_normalize_infers_not_found_status_from_makerworld_404_message(self):
        payload = {
            "items": [
                {
                    "model_id": "1590150",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "missing",
                    "model_url": "https://makerworld.com.cn/zh/models/1590150",
                    "message": "模型页面返回 404，可能已下架、设为私有或转为草稿。",
                }
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "not_found")
        self.assertIn("404", normalized["items"][0]["message"])

    def test_retry_state_is_not_reclassified_by_old_verification_message(self):
        payload = {
            "items": [
                {
                    "model_id": "1759345",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "queued",
                    "model_url": "https://makerworld.com.cn/zh/models/1759345",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                }
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "queued")

    def test_download_limited_message_uses_current_limit_guard_date(self):
        guard_state = {
            "active": True,
            "limited_until": "2099-01-02T00:00:00+08:00",
            "last_hit_at": "2099-01-01T01:22:00+08:00",
            "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
            "reason": "download_limited",
            "model_url": "https://makerworld.com.cn/zh/models/2193050",
        }
        payload = {
            "items": [
                {
                    "model_id": "2193050",
                    "title": "0.2mm 层高, 2 层墙, 15% 填充",
                    "status": "download_limited",
                    "model_url": "https://makerworld.com.cn/zh/models/2193050",
                    "message": "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2026-04-26 00:00。",
                }
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value=guard_state):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["status"], "download_limited")
        self.assertEqual(
            normalized["items"][0]["message"],
            "国区返回了每日下载上限，今日暂停自动重试，自动重试暂停至 2099-01-02 00:00。",
        )

    def test_manual_missing_retry_keeps_active_limit_guard_and_does_not_queue(self):
        original_select_cookie = archive_worker_module._select_cookie
        try:
            guard_state = {
                "active": True,
                "limited_until": "2099-01-02T00:00:00+08:00",
                "last_hit_at": "2099-01-01T01:22:00+08:00",
                "message": "国区返回了每日下载上限，今日暂停自动重试。",
                "reason": "download_limited",
                "model_url": "https://makerworld.com.cn/zh/models/2193050",
            }

            def load_guard(_key, default):
                return dict(guard_state or default)

            def save_guard(_key, payload):
                guard_state.clear()
                guard_state.update(payload)
                return payload

            with patch.object(archive_worker_module, "load_database_json_state", side_effect=load_guard), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=save_guard), \
                    patch.object(three_mf_quota_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "cn"}) as reset_quota:
                manager = ArchiveTaskManager()
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                updates = []
                manager.task_store = SimpleNamespace(
                    update_missing_3mf_status=lambda **payload: updates.append(payload)
                )
                submitted = []
                manager.submit = lambda url, force=False, meta=None, **_: submitted.append(
                    {"url": url, "force": force, "meta": meta}
                ) or {"accepted": True, "task_id": "task-1", "message": "queued"}
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="https://makerworld.com.cn/zh/models/2193050",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-1",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertFalse(result["accepted"])
        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "download_limited")
        self.assertEqual(submitted, [])
        self.assertTrue(guard_state["active"])
        self.assertEqual(updates[-1]["status"], "download_limited")
        reset_quota.assert_not_called()

    def test_active_limit_guard_takes_priority_over_missing_cookie(self):
        manager = ArchiveTaskManager()
        manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
        updates = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **payload: updates.append(payload)
        )
        guard = {
            "active": True,
            "limited_until": "2099-01-02T00:00:00+08:00",
            "model_url": "https://makerworld.com.cn/zh/models/2193050",
            "message": "国区返回了每日下载上限，暂时停止自动重试。",
        }

        with patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value=guard), \
                patch.object(archive_worker_module, "_select_cookie", return_value=""), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_missing_3mf(
                model_url="https://makerworld.com.cn/zh/models/2193050",
                model_id="2193050",
            )

        self.assertTrue(result["paused"])
        self.assertEqual(result["state"], "download_limited")
        self.assertEqual(updates[-1]["status"], "download_limited")

    def test_manual_missing_retry_does_not_reset_daily_quota_before_queueing(self):
        original_select_cookie = archive_worker_module._select_cookie
        quota_calls = []
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                    patch.object(three_mf_quota_module, "reset_three_mf_daily_quota", side_effect=lambda **kwargs: quota_calls.append(kwargs) or {"reset": True, "source": "global", "previous": {"used": 100}}):
                manager = ArchiveTaskManager()
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store = SimpleNamespace(
                    update_missing_3mf_status=lambda **_payload: None
                )
                submitted = []
                manager.submit = lambda url, force=False, meta=None, **_: submitted.append(
                    {"url": url, "force": force, "meta": meta}
                ) or {"accepted": True, "task_id": "task-1", "message": "queued"}
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="https://makerworld.com/zh/models/2193050",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-1",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertTrue(result["accepted"])
        self.assertEqual(quota_calls, [])
        self.assertEqual(len(submitted), 1)
        self.assertTrue(submitted[0]["force"])

    def test_manual_missing_retry_merges_distinct_instances_for_same_model_in_queue(self):
        original_select_cookie = archive_worker_module._select_cookie
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "retry-profile-1",
                        "url": "https://makerworld.com/zh/models/2193050",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {
                            "missing_3mf_retry": True,
                            "model_id": "2193050",
                            "instance_id": "profile-1",
                            "instance_ids": ["profile-1"],
                        },
                    }
                ],
                "recent_failures": [],
            }
        }
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda key, payload: state.__setitem__(key, payload) or payload), \
                    patch.object(task_state_module, "load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                    patch.object(task_state_module, "save_database_json_state", side_effect=lambda key, payload: state.__setitem__(key, payload) or payload), \
                    patch.object(archive_worker_module, "get_archive_snapshot", return_value={"archived_keys": []}), \
                    patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                    patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                    patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True}), \
                    patch.object(archive_worker_module, "threading") as threading_mock:
                threading_mock.Thread.side_effect = AssertionError("worker should not start in this test")
                manager = ArchiveTaskManager(background_enabled=False)
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store.update_missing_3mf_status = lambda **_payload: None
                manager._deleted_task_lookup = lambda candidates=None: {}
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="https://makerworld.com/zh/models/2193050",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-2",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertFalse(result["accepted"])
        self.assertTrue(result["merged"])
        self.assertEqual(result["task_id"], "retry-profile-1")
        self.assertEqual([item["id"] for item in state["archive_queue"]["queued"]], ["retry-profile-1"])
        self.assertEqual(state["archive_queue"]["queued"][0]["meta"]["instance_ids"], ["profile-1", "profile-2"])

    def test_manual_missing_retry_reports_existing_queue_item_without_new_instance_as_queued(self):
        original_select_cookie = archive_worker_module._select_cookie
        state = {
            "archive_queue": {
                "active": [],
                "queued": [
                    {
                        "id": "retry-profile-1",
                        "url": "https://makerworld.com/zh/models/2193050",
                        "mode": "single_model",
                        "status": "queued",
                        "meta": {
                            "missing_3mf_retry": True,
                            "model_id": "2193050",
                            "instance_id": "profile-1",
                            "instance_ids": ["profile-1"],
                        },
                    }
                ],
                "recent_failures": [],
            }
        }
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda key, payload: state.__setitem__(key, payload) or payload), \
                    patch.object(task_state_module, "load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                    patch.object(task_state_module, "save_database_json_state", side_effect=lambda key, payload: state.__setitem__(key, payload) or payload), \
                    patch.object(archive_worker_module, "get_archive_snapshot", return_value={"archived_keys": []}), \
                    patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                    patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                    patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True}), \
                    patch.object(archive_worker_module, "threading") as threading_mock:
                threading_mock.Thread.side_effect = AssertionError("worker should not start in this test")
                manager = ArchiveTaskManager(background_enabled=False)
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store.update_missing_3mf_status = lambda **_payload: None
                manager._deleted_task_lookup = lambda candidates=None: {}
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="https://makerworld.com/zh/models/2193050",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-1",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertFalse(result["accepted"])
        self.assertTrue(result["queued"])
        self.assertFalse(result.get("merged", False))
        self.assertEqual(result["task_id"], "retry-profile-1")
        self.assertEqual([item["id"] for item in state["archive_queue"]["queued"]], ["retry-profile-1"])

    def test_duplicate_missing_retry_log_reports_existing_queue_item(self):
        manager = ArchiveTaskManager(background_enabled=False)
        logged = []

        with patch.object(manager, "_deleted_task_lookup", return_value={}), \
                patch.object(manager, "_archived_task_keys", return_value=set()), \
                patch.object(
                    manager,
                    "_enqueue_single_task_with_queue",
                    return_value=(
                        "retry-profile-1",
                        {
                            "enqueued": False,
                            "merged": False,
                            "existing_task_id": "retry-profile-1",
                            "task_identity_key": "missing_3mf_retry:model:2193050",
                        },
                    ),
                ), \
                patch.object(manager, "_ensure_worker"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(
                    archive_worker_module,
                    "_log_archive",
                    side_effect=lambda *args, **kwargs: logged.append((args, kwargs)),
                ):
            result = manager._submit_single(
                "https://makerworld.com.cn/zh/models/2193050",
                force=True,
                meta={
                    "missing_3mf_retry": True,
                    "model_id": "2193050",
                    "instance_id": "profile-1",
                },
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["task_id"], "retry-profile-1")
        self.assertEqual(result["message"], "该模型的缺失 3MF 重试已在队列中。")
        self.assertTrue(logged)
        event_args, event_kwargs = logged[-1]
        self.assertEqual(event_args[0], "single_submit_skipped")
        self.assertEqual(event_args[1], "缺失 3MF 重试已在队列中。")
        self.assertFalse(event_kwargs["enqueued"])
        self.assertEqual(event_kwargs["existing_task_id"], "retry-profile-1")

    def test_manual_missing_retry_keeps_existing_queue_item_status_queued(self):
        original_select_cookie = archive_worker_module._select_cookie
        updates = []
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                    patch.object(three_mf_quota_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}):
                manager = ArchiveTaskManager()
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store = SimpleNamespace(
                    update_missing_3mf_status=lambda **payload: updates.append(payload)
                )
                manager.submit = lambda *_args, **_kwargs: {
                    "accepted": False,
                    "queued": True,
                    "task_id": "retry-profile-1",
                    "message": "该模型的缺失 3MF 重试已在队列中。",
                }
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="https://makerworld.com/zh/models/2193050",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-1",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertFalse(result["accepted"])
        self.assertTrue(result["queued"])
        self.assertEqual(updates[-1]["status"], "queued")
        self.assertEqual(updates[-1]["message"], "已存在于重新下载队列")

    def test_manual_missing_retry_uses_source_when_model_url_is_missing(self):
        original_select_cookie = archive_worker_module._select_cookie
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                    patch.object(three_mf_quota_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}):
                manager = ArchiveTaskManager()
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store = SimpleNamespace(
                    update_missing_3mf_status=lambda **_payload: None
                )
                submitted = []
                manager.submit = lambda url, force=False, meta=None, **_: submitted.append(
                    {"url": url, "force": force, "meta": meta}
                ) or {"accepted": True, "task_id": "task-1", "message": "queued"}
                archive_worker_module._select_cookie = lambda *_: "cookie"

                result = manager.retry_missing_3mf(
                    model_url="",
                    model_id="2193050",
                    title="Demo",
                    instance_id="profile-1",
                    source="global",
                )
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertTrue(result["accepted"])
        self.assertEqual(submitted[0]["url"], "https://makerworld.com/zh/models/2193050")
        self.assertEqual(submitted[0]["meta"]["source"], "global")

    def test_retry_all_missing_preserves_source_for_url_rebuild(self):
        manager = ArchiveTaskManager()
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "2193050",
                        "title": "Demo",
                        "instance_id": "profile-1",
                        "source": "global",
                    }
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        calls = []
        manager.retry_missing_3mf = lambda **payload: calls.append(payload) or {"accepted": True, "message": "queued"}

        with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_all_missing_3mf()

        self.assertTrue(result["accepted"])
        self.assertEqual(calls[0]["source"], "global")

    def test_retry_all_missing_keeps_daily_limit_items_paused(self):
        manager = ArchiveTaskManager()
        marked = []
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com.cn/zh/models/2193050",
                        "title": "Demo",
                        "instance_id": "profile-1",
                        "source": "cn",
                        "status": "download_limited",
                    }
                ]
            },
            mark_missing_3mf_retrying=lambda *args, **kwargs: marked.append((args, kwargs)),
        )
        manager.retry_missing_3mf = Mock(return_value={"accepted": True, "message": "queued"})
        guard = {
            "active": True,
            "limited_until": "2099-01-02T00:00:00+08:00",
            "model_url": "https://makerworld.com.cn/zh/models/1",
            "message": "国区返回了每日下载上限，暂时停止自动重试。",
        }

        with patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value=guard), \
                patch.object(three_mf_quota_module, "reset_three_mf_daily_quota") as reset_quota, \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_all_missing_3mf()

        self.assertFalse(result["accepted"])
        self.assertEqual(result["paused_count"], 1)
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0][1]["status"], "download_limited")
        self.assertIn("2099-01-02 00:00", marked[0][1]["message"])
        manager.retry_missing_3mf.assert_not_called()
        reset_quota.assert_not_called()

    def test_retry_all_missing_groups_instances_by_model_before_queueing(self):
        manager = ArchiveTaskManager()
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com/zh/models/2193050",
                        "title": "Profile A",
                        "instance_id": "profile-1",
                        "source": "global",
                    },
                    {
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com/zh/models/2193050",
                        "title": "Profile B",
                        "instance_id": "profile-2",
                        "source": "global",
                    },
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        calls = []
        manager.retry_missing_3mf = lambda **payload: calls.append(payload) or {"accepted": True, "message": "queued"}

        with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_all_missing_3mf()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model_id"], "2193050")
        self.assertEqual(calls[0]["instance_id"], "")

    def test_retry_all_missing_counts_merged_retry_as_queued(self):
        manager = ArchiveTaskManager()
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com/zh/models/2193050",
                        "title": "Demo",
                        "instance_id": "profile-1",
                        "source": "global",
                    }
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        manager.retry_missing_3mf = lambda **_payload: {
            "accepted": False,
            "queued": True,
            "merged": True,
            "message": "该模型的缺失 3MF 重试已在队列中，已合并缺失实例。",
        }

        with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_all_missing_3mf()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["failed_count"], 0)

    def test_idle_missing_retry_queues_small_fair_batch_and_respects_gate_and_cooldown(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_archive_queue_compact=lambda **_kwargs: {
                "queued_count": 0,
                "running_count": 0,
                "queued": [],
                "active": [],
            },
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "global-old",
                        "model_url": "https://makerworld.com/zh/models/global-old",
                        "source": "global",
                        "status": "missing",
                        "updated_at": "2026-08-25T10:00:00+08:00",
                    },
                    {
                        "model_id": "global-old",
                        "model_url": "https://makerworld.com/zh/models/global-old?from=profile",
                        "instance_id": "profile-2",
                        "source": "global",
                        "status": "missing",
                        "updated_at": "2026-08-25T10:00:00+08:00",
                    },
                    {
                        "model_id": "cn-old",
                        "model_url": "https://makerworld.com.cn/zh/models/cn-old",
                        "source": "cn",
                        "status": "verification_required",
                        "updated_at": "2026-08-25T10:01:00+08:00",
                    },
                    {
                        "model_id": "global-recent",
                        "model_url": "https://makerworld.com/zh/models/global-recent",
                        "source": "global",
                        "status": "missing",
                        "updated_at": "2026-08-25T11:55:00+08:00",
                    },
                    {
                        "model_id": "cn-gated",
                        "model_url": "https://makerworld.com.cn/zh/models/cn-gated",
                        "source": "cn",
                        "status": "missing",
                        "updated_at": "2026-08-25T07:00:00+08:00",
                    },
                    {
                        "model_id": "cn-stale-queued",
                        "model_url": "https://makerworld.com.cn/zh/models/cn-stale-queued",
                        "source": "cn",
                        "status": "queued",
                        "updated_at": "2026-08-25T08:00:00+08:00",
                    },
                ]
            },
        )
        retried = []
        manager.retry_missing_3mf = lambda **payload: retried.append(payload) or {
            "accepted": True,
            "message": "queued",
        }

        def gate_for_url(url, _meta=None):
            if "cn-gated" in url:
                return {"open": False, "state": "daily_limit", "platform": "cn", "message": "limited"}
            return {"open": True, "state": "open", "platform": "cn" if ".com.cn" in url else "global"}

        with patch.object(archive_worker_module, "three_mf_gate_for_url", side_effect=gate_for_url), \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_idle_missing_3mf(
                limit=4,
                now=datetime.fromisoformat("2026-08-25T12:00:00+08:00"),
            )

        self.assertEqual(result["accepted_count"], 2)
        self.assertEqual(result["cooldown_count"], 1)
        self.assertEqual(result["gated_count"], 1)
        self.assertEqual(
            [item["model_id"] for item in retried],
            ["cn-stale-queued", "global-old"],
        )
        self.assertTrue(all(item["instance_id"] == "" for item in retried))

    def test_idle_missing_retry_schedules_at_most_one_model_per_platform(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_archive_queue_compact=lambda **_kwargs: {
                "queued_count": 0,
                "running_count": 0,
                "queued": [],
                "active": [],
            },
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": model_id,
                        "model_url": f"https://makerworld.com.cn/zh/models/{model_id}",
                        "source": "cn",
                        "status": "http_error",
                        "updated_at": "2026-08-25T10:00:00+08:00",
                    }
                    for model_id in ("cn-1", "cn-2", "cn-3")
                ]
            },
        )
        retried = []
        manager.retry_missing_3mf = lambda **payload: retried.append(payload) or {
            "accepted": True,
            "message": "queued",
        }

        with patch.object(
            archive_worker_module,
            "three_mf_gate_for_url",
            return_value={"open": True, "state": "open", "platform": "cn"},
        ), patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_idle_missing_3mf(
                limit=4,
                now=datetime.fromisoformat("2026-08-25T12:00:00+08:00"),
            )

        self.assertEqual(result["attempted_count"], 1)
        self.assertEqual(result["accepted_count"], 1)
        self.assertEqual([item["model_id"] for item in retried], ["cn-1"])

    def test_idle_missing_retry_does_not_enqueue_while_archive_queue_has_work(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_archive_queue_compact=lambda **_kwargs: {
                "queued_count": 1,
                "running_count": 0,
                "queued": [{"status": "queued"}],
                "active": [],
            },
            load_missing_3mf=Mock(),
        )
        manager.retry_missing_3mf = Mock()

        result = manager.retry_idle_missing_3mf(limit=4)

        self.assertEqual(result["reason"], "archive_queue_busy")
        manager.task_store.load_missing_3mf.assert_not_called()
        manager.retry_missing_3mf.assert_not_called()

    def test_retry_verification_missing_counts_merged_retry_as_queued(self):
        manager = ArchiveTaskManager()
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com/zh/models/2193050",
                        "title": "Demo",
                        "instance_id": "profile-1",
                        "source": "global",
                        "status": "verification_required",
                        "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    }
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        manager.retry_missing_3mf = lambda **_payload: {
            "accepted": False,
            "queued": True,
            "merged": True,
            "message": "该模型的缺失 3MF 重试已在队列中，已合并缺失实例。",
        }

        with patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_verification_missing_3mf(platform="global")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["failed_count"], 0)

    def test_targeted_verification_retry_selects_one_platform_blocker_when_primary_is_missing(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {
                        "model_id": "auth-model",
                        "model_url": "https://makerworld.com.cn/zh/models/auth-model",
                        "title": "Auth blocked",
                        "instance_id": "auth-instance",
                        "source": "cn",
                        "status": "auth_required",
                        "message": "国区下载 3MF 需要有效登录态。",
                    },
                    {
                        "model_id": "verify-model",
                        "model_url": "https://makerworld.com.cn/zh/models/verify-model",
                        "title": "Verification blocked",
                        "instance_id": "verify-instance",
                        "source": "cn",
                        "status": "verification_required",
                        "message": "MakerWorld 需要验证。",
                    },
                    {
                        "model_id": "global-model",
                        "model_url": "https://makerworld.com/zh/models/global-model",
                        "title": "Global blocked",
                        "instance_id": "global-instance",
                        "source": "global",
                        "status": "verification_required",
                        "message": "MakerWorld 需要验证。",
                    },
                ]
            },
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        retry_calls = []
        manager.retry_missing_3mf = lambda **payload: retry_calls.append(payload) or {
            "accepted": False,
            "queued": True,
            "message": "该模型的缺失 3MF 重试已在队列中。",
        }

        with patch.object(manager, "_resume_browser_session_recovery_task", return_value=True) as resume_mock, \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_verification_missing_3mf(
                platform="cn",
                primary=None,
                retry_all=False,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["resumed_count"], 1)
        self.assertEqual(len(retry_calls), 1)
        self.assertEqual(retry_calls[0]["model_id"], "verify-model")
        self.assertTrue(retry_calls[0]["browser_session_recovery"])
        resume_mock.assert_called_once_with(
            model_url="https://makerworld.com.cn/zh/models/verify-model",
            model_id="verify-model",
            source="cn",
            title="Verification blocked",
            instance_id="verify-instance",
        )

    def test_retry_verification_missing_resumes_paused_queue_items(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "task-paused-cn",
                    "status": "paused",
                    "title": "https://makerworld.com.cn/zh/models/1590150",
                    "url": "https://makerworld.com.cn/zh/models/1590150",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "blocked_reason": "needs_verification",
                    "meta": {
                        "missing_3mf_retry": True,
                        "model_id": "1590150",
                        "model_url": "https://makerworld.com.cn/zh/models/1590150",
                        "source": "cn",
                    },
                },
                {
                    "id": "task-paused-global",
                    "status": "paused",
                    "title": "https://makerworld.com/zh/models/2193050",
                    "url": "https://makerworld.com/zh/models/2193050",
                    "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                    "blocked_reason": "needs_verification",
                    "meta": {
                        "missing_3mf_retry": True,
                        "model_id": "2193050",
                        "model_url": "https://makerworld.com/zh/models/2193050",
                        "source": "global",
                    },
                },
            ],
            "active": [],
            "recent_failures": [],
        }
        ensured = []
        resumed_queue = {
            **queue,
            "queued": [
                {
                    **queue["queued"][0],
                    "status": "queued",
                    "message": "验证已完成，正在探测 3MF 下载权限",
                    "meta": {**queue["queued"][0]["meta"], "browser_session_recovery": True},
                },
                queue["queued"][1],
            ],
            "resumed_count": 1,
        }
        resumed_queue["resumed_items"] = [resumed_queue["queued"][0]]
        resume_mock = Mock(return_value=resumed_queue)
        retrying_calls = []
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {"items": []},
            mark_missing_3mf_retrying=lambda *args, **kwargs: retrying_calls.append((args, kwargs)),
            resume_verification_paused_archive_tasks=resume_mock,
        )
        manager._ensure_worker = lambda: ensured.append(True)

        with patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_verification_missing_3mf(platform="cn")

        self.assertTrue(result["accepted"])
        self.assertEqual(result["resumed_count"], 1)
        self.assertEqual(resumed_queue["queued"][0]["status"], "queued")
        self.assertTrue(resumed_queue["queued"][0]["meta"]["browser_session_recovery"])
        self.assertEqual(resumed_queue["queued"][1]["status"], "paused")
        self.assertEqual(ensured, [True])
        self.assertEqual(resume_mock.call_args.kwargs["limit"], 1)
        self.assertTrue(resume_mock.call_args.kwargs["include_daily_limit"])
        self.assertEqual(resume_mock.call_args.kwargs["meta_updates"], {"browser_session_recovery": True})
        self.assertEqual(len(retrying_calls), 1)
        self.assertEqual(retrying_calls[0][1]["status"], "queued")

    def test_manual_verification_resumes_one_paused_probe_before_retrying_missing_records(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager._ensure_worker = Mock()

        with patch.object(manager, "_resume_paused_missing_3mf_retry_tasks_for_platform", return_value=1) as resume_mock:
            result = manager.retry_verification_missing_3mf(
                platform="cn",
                retry_all=False,
                resume_paused_probe_first=True,
            )

        resume_mock.assert_called_once_with("cn")
        manager._ensure_worker.assert_called_once_with()
        self.assertEqual(result["resumed_count"], 1)
        self.assertEqual(result["total_count"], 0)
        self.assertTrue(result["accepted"])

    def test_verified_browser_authorization_resumes_paused_three_mf_stage(self):
        manager = ArchiveTaskManager(background_enabled=False)
        queue = {
            "queued": [
                {
                    "id": "task-stage-cn",
                    "status": "paused",
                    "url": "https://makerworld.com.cn/zh/models/1590150",
                    "blocked_reason": "needs_verification",
                    "meta": {
                        "three_mf_download": True,
                        "model_url": "https://makerworld.com.cn/zh/models/1590150",
                        "source": "cn",
                    },
                }
            ],
            "active": [],
            "recent_failures": [],
        }
        resumed_item = {
            **queue["queued"][0],
            "status": "queued",
            "meta": {**queue["queued"][0]["meta"], "browser_session_recovery": True},
        }
        resume_mock = Mock(
            return_value={
                **queue,
                "queued": [resumed_item],
                "resumed_count": 1,
                "resumed_items": [resumed_item],
            }
        )
        manager.task_store = SimpleNamespace(
            resume_verification_paused_archive_tasks=resume_mock,
        )

        with patch.object(archive_worker_module, "append_business_log"):
            resumed = manager._resume_paused_missing_3mf_retry_tasks_for_platform("cn")

        self.assertEqual(resumed, 1)
        self.assertEqual(resumed_item["status"], "queued")
        self.assertTrue(resumed_item["meta"]["browser_session_recovery"])
        self.assertEqual(resume_mock.call_args.kwargs["limit"], 1)

    def test_targeted_verification_retry_only_resumes_the_current_model(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.task_store = SimpleNamespace(
            load_missing_3mf=lambda: {"items": []},
            mark_missing_3mf_retrying=lambda *_args, **_kwargs: None,
        )
        manager.retry_missing_3mf = lambda **_payload: {
            "accepted": False,
            "queued": True,
            "message": "该模型的缺失 3MF 重试已在队列中。",
        }

        with patch.object(manager, "_resume_browser_session_recovery_task", return_value=True) as resume_mock, \
                patch.object(archive_worker_module, "append_business_log"):
            result = manager.retry_verification_missing_3mf(
                platform="cn",
                primary={
                    "model_url": "https://makerworld.com.cn/zh/models/123",
                    "model_id": "123",
                    "title": "Demo",
                    "instance_id": "profile-1",
                    "source": "cn",
                    "status": "verification_required",
                },
                retry_all=False,
            )

        resume_mock.assert_called_once_with(
            model_url="https://makerworld.com.cn/zh/models/123",
            model_id="123",
            source="cn",
            title="Demo",
            instance_id="profile-1",
        )
        self.assertEqual(result["resumed_count"], 1)
        self.assertTrue(result["accepted"])

    def test_run_single_task_marks_account_ok_after_missing_3mf_retry_success(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        active_updates = []
        completed = []
        replaced_missing = []
        removed_failures = []
        logged = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda model_id, items: replaced_missing.append((model_id, items)),
            remove_recent_failures_for_model=lambda model_id, url="": removed_failures.append((model_id, url)),
            update_active_task=lambda task_id, **payload: active_updates.append((task_id, payload)),
            complete_archive_task=lambda task_id, **payload: completed.append((task_id, payload)),
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "973599",
                        "base_name": "Demo Model",
                        "work_dir": "",
                        "missing_3mf": [],
                        "stats": {
                            "instances": {
                                "api_fetch_attempts": 1,
                                "api_fetch": 1,
                                "three_mf_downloaded": 1,
                                "three_mf_existing": 0,
                            }
                        },
                    },
                ), \
                patch.object(archive_worker_module, "mark_account_ok") as mark_account_ok_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(
                    archive_worker_module,
                    "_log_archive",
                    side_effect=lambda *args, **kwargs: logged.append((args, kwargs)),
                ):
            manager._run_single_task(
                "task-1",
                "https://makerworld.com/zh/models/973599",
                {"missing_3mf_retry": True, "instance_id": "profile-1"},
            )

        mark_account_ok_mock.assert_called_once_with(
            "global",
            source="missing_3mf_retry",
            model_url="https://makerworld.com/zh/models/973599",
            model_id="973599",
            instance_id="profile-1",
        )
        self.assertEqual(replaced_missing, [("973599", [])])
        self.assertEqual(
            removed_failures,
            [("973599", "https://makerworld.com/zh/models/973599")],
        )
        self.assertEqual(
            completed,
            [("task-1", {"progress": 100, "message": "3MF 下载检查完成：Demo Model，当前没有缺失文件。"})],
        )
        self.assertEqual(logged[-1][0][0], "three_mf_download_completed")
        self.assertEqual(logged[-1][1]["three_mf_authorization_attempts"], 1)
        self.assertEqual(logged[-1][1]["three_mf_authorized"], 1)
        self.assertEqual(logged[-1][1]["three_mf_downloaded"], 1)
        self.assertFalse(active_updates)

    def test_run_single_task_reports_metadata_stage_before_three_mf_child(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        completed = []
        logged = []
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda task_id, **payload: completed.append((task_id, payload)),
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "973599",
                        "base_name": "Demo Model",
                        "work_dir": "",
                        "instances": [{"id": "profile-1"}],
                        "missing_3mf": [
                            {
                                "id": "profile-1",
                                "title": "Profile",
                                "downloadState": "pending_download",
                                "downloadMessage": "等待下载 3MF",
                            }
                        ],
                    },
                ), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_result", return_value=None), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(manager, "_enqueue_three_mf_stage_task_from_result", return_value="three-mf-task") as enqueue_three_mf, \
                patch.object(
                    archive_worker_module,
                    "_log_archive",
                    side_effect=lambda *args, **kwargs: logged.append((args, kwargs)),
                ):
            manager._run_single_task(
                "task-1",
                "https://makerworld.com/zh/models/973599",
                {},
            )

        enqueue_three_mf.assert_called_once()
        self.assertEqual(
            completed,
            [("task-1", {"progress": 100, "message": "元数据归档完成：Demo Model，3MF 下载任务已入队。"})],
        )
        self.assertEqual(logged[-1][0][0], "single_metadata_completed")

    def test_run_single_task_updates_three_mf_gate_for_verification_required_missing_3mf(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        manager.task_store = SimpleNamespace(
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "1759345",
                        "base_name": "CN Model",
                        "work_dir": "",
                        "missing_3mf": [
                            {
                                "id": "profile-9",
                                "title": "0.2mm",
                                "downloadState": "verification_required",
                                "downloadMessage": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                            }
                        ],
                    },
                ), \
                patch.object(archive_worker_module, "mark_account_ok") as mark_account_ok_mock, \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_three_mf_gate_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_single_task(
                "task-2",
                "https://makerworld.com.cn/zh/models/1759345",
            )

        mark_account_ok_mock.assert_not_called()
        update_three_mf_gate_mock.assert_called_once_with(
            "cn",
            gate="verification_required",
            reason="three_mf_download_failed",
            source="archive_download",
            detail="MakerWorld 需要验证，前往官网任意下载一个模型。",
            model_url="https://makerworld.com.cn/zh/models/1759345",
            model_id="1759345",
            instance_id="profile-9",
        )

    def test_run_single_task_clears_not_found_missing_3mf_result_without_requeueing_it(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        replaced_missing = []
        removed_missing = []
        removed_failures = []
        log_calls = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda model_id, items: replaced_missing.append((model_id, items)),
            remove_missing_3mf_item=lambda **payload: removed_missing.append(payload) or {"items": []},
            remove_recent_failures_for_model=lambda model_id, url="": removed_failures.append((model_id, url)),
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda *_args, **_kwargs: None,
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "1590150",
                        "base_name": "Gone Model",
                        "work_dir": "",
                        "missing_3mf": [
                            {
                                "id": "profile-gone",
                                "title": "Gone profile",
                                "downloadState": "not_found",
                                "downloadMessage": "源端没有返回该打印配置的 3MF 下载地址。",
                            },
                            {
                                "id": "profile-blocked",
                                "title": "Blocked profile",
                                "downloadState": "verification_required",
                                "downloadMessage": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                            },
                        ],
                    },
                ), \
                patch.object(archive_worker_module, "mark_account_ok") as mark_account_ok_mock, \
                patch.object(archive_worker_module, "update_three_mf_gate") as update_three_mf_gate_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "append_business_log", side_effect=lambda *args, **kwargs: log_calls.append((args, kwargs))):
            manager._run_single_task(
                "task-not-found",
                "https://makerworld.com.cn/zh/models/1590150",
                {"missing_3mf_retry": True, "source": "cn", "model_id": "1590150"},
            )

        self.assertEqual(removed_missing, [
            {
                "model_id": "1590150",
                "model_url": "https://makerworld.com.cn/zh/models/1590150",
                "title": "Gone profile",
                "instance_id": "profile-gone",
            }
        ])
        self.assertEqual(replaced_missing, [
            (
                "1590150",
                [
                    {
                        "model_id": "1590150",
                        "model_url": "https://makerworld.com.cn/zh/models/1590150",
                        "title": "Blocked profile",
                        "instance_id": "profile-blocked",
                        "status": "verification_required",
                        "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
                        "updated_at": replaced_missing[0][1][0]["updated_at"] if replaced_missing else "",
                    }
                ],
            )
        ])
        self.assertEqual(
            removed_failures,
            [("1590150", "https://makerworld.com.cn/zh/models/1590150")],
        )
        mark_account_ok_mock.assert_not_called()
        update_three_mf_gate_mock.assert_called_once()
        self.assertEqual(update_three_mf_gate_mock.call_args.kwargs["instance_id"], "profile-blocked")
        cleared_log = next(
            (kwargs for args, kwargs in log_calls if len(args) > 1 and args[1] == "missing_3mf_not_found_cleared"),
            None,
        )
        self.assertIsNotNone(cleared_log)
        self.assertEqual(cleared_log["detail"], "源端没有返回该打印配置的 3MF 下载地址。")

    def test_run_single_task_completes_when_account_health_sync_fails(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        completed = []
        log_calls = []
        manager.task_store = SimpleNamespace(
            update_missing_3mf_status=lambda **_payload: None,
            replace_missing_3mf_for_model=lambda *_args, **_kwargs: None,
            remove_recent_failures_for_model=lambda *_args, **_kwargs: None,
            update_active_task=lambda *_args, **_kwargs: None,
            complete_archive_task=lambda task_id, **_payload: completed.append(task_id),
        )

        with patch.object(archive_worker_module, "_select_cookie", return_value="cookie"), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                patch.object(archive_worker_module, "_temporary_proxy_env", side_effect=lambda *_args, **_kwargs: nullcontext()), \
                patch.object(
                    archive_worker_module,
                    "run_archive_model_job",
                    return_value={
                        "model_id": "973599",
                        "base_name": "Demo Model",
                        "work_dir": "",
                        "missing_3mf": [],
                    },
                ), \
                patch.object(archive_worker_module, "mark_account_ok", side_effect=RuntimeError("db down")), \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive", side_effect=lambda *args, **kwargs: log_calls.append((args, kwargs))):
            manager._run_single_task(
                "task-sync-fail",
                "https://makerworld.com/zh/models/973599",
                {"missing_3mf_retry": True, "instance_id": "profile-1"},
            )

        self.assertEqual(completed, ["task-sync-fail"])
        self.assertTrue(
            any(args and args[0] == "account_health_sync_failed" for args, _kwargs in log_calls)
        )

    def test_run_loop_clears_missing_3mf_retry_when_model_is_not_found(self):
        manager = ArchiveTaskManager(background_enabled=False)
        task = {
            "id": "task-404",
            "url": "https://makerworld.com.cn/zh/models/1590150",
            "mode": "single_model",
            "meta": {
                "missing_3mf_retry": True,
                "model_id": "1590150",
                "model_url": "https://makerworld.com.cn/zh/models/1590150",
                "instance_id": "1738489",
                "title": "按颜色分盘No AMS",
                "source": "cn",
                "browser_session_recovery": True,
            },
        }
        calls = []
        load_calls = []
        lease_calls = []

        def load_archive_queue():
            load_calls.append(True)
            if len(load_calls) == 1:
                return {"queued": [task], "active": [], "recent_failures": []}
            return {"queued": [], "active": [], "recent_failures": []}

        def lease_next_archive_task(_selector):
            lease_calls.append(True)
            if len(lease_calls) == 1:
                return task
            return None

        manager.task_store = SimpleNamespace(
            load_archive_queue=load_archive_queue,
            lease_next_archive_task=lease_next_archive_task,
            update_active_task=lambda *args, **kwargs: calls.append(("update_active", args, kwargs)),
            update_missing_3mf_status=lambda *args, **kwargs: calls.append(("update_missing", args, kwargs)),
            remove_missing_3mf_item=lambda **kwargs: calls.append(("remove_missing", (), kwargs)) or {"items": []},
            remove_recent_failures_for_model=lambda *args, **kwargs: calls.append(("remove_failures", args, kwargs)),
            complete_archive_task=lambda *args, **kwargs: calls.append(("complete", args, kwargs)),
            fail_archive_task=lambda *args, **kwargs: calls.append(("fail", args, kwargs)),
        )

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(
                    manager,
                    "_run_single_task",
                    side_effect=RuntimeError("模型页面返回 404，可能已下架、设为私有或转为草稿。"),
                ), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_exception") as sync_health, \
                patch.object(archive_worker_module, "append_business_log") as business_log, \
                patch.object(manager, "_resume_paused_missing_3mf_retry_tasks_for_platform", return_value=1) as resume_paused:
            manager._run_loop()

        self.assertIn(
            (
                "remove_missing",
                (),
                {
                    "model_id": "1590150",
                    "model_url": "https://makerworld.com.cn/zh/models/1590150",
                    "title": "按颜色分盘No AMS",
                    "instance_id": "1738489",
                },
            ),
            calls,
        )
        self.assertIn(
            ("remove_failures", ("1590150",), {"url": "https://makerworld.com.cn/zh/models/1590150"}),
            calls,
        )
        self.assertIn(
            (
                "complete",
                ("task-404",),
                {
                    "progress": 100,
                    "message": "源端已不可用，已停止缺失 3MF 重试：模型页面返回 404，可能已下架、设为私有或转为草稿。",
                },
            ),
            calls,
        )
        self.assertNotIn("fail", [call[0] for call in calls])
        self.assertNotIn("update_missing", [call[0] for call in calls])
        sync_health.assert_not_called()
        business_log.assert_any_call(
            "archive",
            "missing_3mf_not_found_cleared",
            "源端已不可用，已停止缺失 3MF 重试。",
            level="info",
            task_id="task-404",
            url="https://makerworld.com.cn/zh/models/1590150",
            model_id="1590150",
            instance_id="1738489",
            detail="模型页面返回 404，可能已下架、设为私有或转为草稿。",
        )
        resume_paused.assert_called_once_with("cn")

    def test_run_loop_keeps_regular_archive_not_found_as_failure(self):
        manager = ArchiveTaskManager(background_enabled=False)
        task = {
            "id": "task-regular-404",
            "url": "https://makerworld.com.cn/zh/models/1590150",
            "mode": "single_model",
            "meta": {},
        }
        calls = []
        load_calls = []
        lease_calls = []

        def load_archive_queue():
            load_calls.append(True)
            if len(load_calls) == 1:
                return {"queued": [task], "active": [], "recent_failures": []}
            return {"queued": [], "active": [], "recent_failures": []}

        def lease_next_archive_task(_selector):
            lease_calls.append(True)
            if len(lease_calls) == 1:
                return task
            return None

        manager.task_store = SimpleNamespace(
            load_archive_queue=load_archive_queue,
            lease_next_archive_task=lease_next_archive_task,
            update_active_task=lambda *args, **kwargs: calls.append(("update_active", args, kwargs)),
            update_missing_3mf_status=lambda *args, **kwargs: calls.append(("update_missing", args, kwargs)),
            remove_missing_3mf_item=lambda **kwargs: calls.append(("remove_missing", (), kwargs)),
            remove_recent_failures_for_model=lambda *args, **kwargs: calls.append(("remove_failures", args, kwargs)),
            complete_archive_task=lambda *args, **kwargs: calls.append(("complete", args, kwargs)),
            fail_archive_task=lambda *args, **kwargs: calls.append(("fail", args, kwargs)),
        )

        with patch.object(manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                patch.object(
                    manager,
                    "_run_single_task",
                    side_effect=RuntimeError("模型页面返回 404，可能已下架、设为私有或转为草稿。"),
                ), \
                patch.object(archive_worker_module, "_sync_account_health_for_archive_exception"), \
                patch.object(archive_worker_module, "_log_archive"):
            manager._run_loop()

        self.assertIn(
            ("fail", ("task-regular-404", "模型页面返回 404，可能已下架、设为私有或转为草稿。"), {}),
            calls,
        )
        self.assertNotIn("remove_missing", [call[0] for call in calls])
        self.assertNotIn("complete", [call[0] for call in calls])

    def test_cn_instance_api_candidates_prefer_bambulab_api(self):
        candidates = _build_instance_api_candidates(
            2864062,
            "https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
            "https://makerworld.com.cn",
            None,
        )

        self.assertTrue(candidates[0].startswith("https://api.bambulab.cn/v1/design-service/instance/2864062/f3mf"))
        self.assertFalse(any("api.bambulab.cn/api/v1/design-service" in item for item in candidates))
        self.assertFalse(any("api.bambulab.cn/makerworld/v1/design-service" in item for item in candidates))
        self.assertIn(
            "https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
            candidates,
        )

    def test_fetch_instance_3mf_stops_after_auth_required_candidate(self):
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        try:
            legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
            session = SimpleNamespace(headers={"User-Agent": "test-agent"})
            calls = []

            def fake_browser_get(url, **_kwargs):
                calls.append(url)
                if len(calls) == 1:
                    return {"code": 1, "error": "Please log in to download models."}
                return {"name": "ok.3mf", "url": "https://example.test/ok.3mf"}

            with patch.object(legacy_archiver_module, "makerworld_browser_get_json", side_effect=fake_browser_get):
                name, url, used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=abc",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(name, "")
        self.assertEqual(url, "")
        self.assertEqual(failure["state"], "auth_required")
        self.assertEqual(len(calls), 1)
        self.assertEqual(used_api_url, calls[0])

    def test_fetch_instance_3mf_uses_browser_authorization_without_direct_retry(self):
        session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                return_value={
                    "status_code": 200,
                    "payload": {"name": "browser.3mf", "url": "https://download.example.test/browser.3mf"},
                    "text": "",
                },
            ) as browser_mock, patch.object(legacy_archiver_module, "makerworld_browser_get_json") as direct_mock:
                name, url, used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=abc",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    model_page_url="https://makerworld.com.cn/zh/models/123456",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(name, "browser.3mf")
        self.assertEqual(url, "https://download.example.test/browser.3mf")
        self.assertIn("api.bambulab.cn", used_api_url)
        self.assertEqual(failure["state"], "available")
        browser_mock.assert_called_once()
        self.assertEqual(browser_mock.call_args.kwargs["model_url"], "https://makerworld.com.cn/zh/models/123456")
        self.assertEqual(browser_mock.call_args.kwargs["instance_id"], "2864062")
        direct_mock.assert_not_called()

    def test_fetch_instance_3mf_keeps_browser_confirmation_when_browser_authorization_is_rejected(self):
        session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                return_value={"status_code": 401, "payload": {"message": "Please log in to download models."}, "text": ""},
            ), patch.object(legacy_archiver_module, "makerworld_browser_get_json") as direct_mock:
                _name, _url, _used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=abc",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(failure["state"], "verification_required")
        self.assertIn("浏览器", failure["message"])
        direct_mock.assert_not_called()

    def test_fetch_instance_3mf_keeps_manual_fallback_and_redacts_auto_verification_diagnostics(self):
        session = SimpleNamespace(
            headers={"User-Agent": "test-agent", "Cookie": "token=session-secret"},
            get=lambda *_args, **_kwargs: None,
        )
        raw_cookie = "token=account-secret; refreshToken=refresh-secret"
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                return_value={
                    "status_code": 418,
                    "payload": {"captchaId": "safe-id"},
                    "verification": {
                        "attempted": True,
                        "completed": False,
                        "provider": "geetest4",
                        "challenge_type": "slider",
                        "attempts": 2,
                        "reason": "low_confidence",
                        "confidence": 0.614,
                        "captchaId": "verification-id",
                        "url": "https://verification.example.test/private",
                        "cookie": "verification-cookie",
                        "headers": {"X-Secret": "secret"},
                        "image": "verification-image",
                    },
                },
            ), patch.object(legacy_archiver_module, "makerworld_browser_get_json") as direct_mock, \
                    patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
                _name, _url, _used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    raw_cookie,
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(failure["state"], "verification_required")
        self.assertEqual(failure["message"], "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。")
        self.assertEqual(raw_cookie, "token=account-secret; refreshToken=refresh-secret")
        direct_mock.assert_not_called()
        business_log_mock.assert_called_once_with(
            "archive",
            "cloakbrowser_auto_verification_fallback",
            "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
            level="warning",
            provider="geetest4",
            challenge_type="slider",
            attempts=2,
            reason="low_confidence",
            confidence=0.61,
        )
        serialized_calls = json.dumps(business_log_mock.call_args_list, default=str, ensure_ascii=False)
        for sensitive_value in (
            "captchaId",
            "safe-id",
            "verification-id",
            "https://",
            "account-secret",
            "refresh-secret",
            "verification-cookie",
            "base64-image-data",
            "verification-image",
        ):
            self.assertNotIn(sensitive_value, serialized_calls)

    def test_auto_verification_log_replaces_untrusted_nested_diagnostics_with_safe_codes(self):
        sensitive_value = {
            "url": "https://verification.example.test/private",
            "token": "provider-token",
            "cookie": "verification-cookie",
            "headers": {"Authorization": "Bearer header-secret"},
            "image": "base64-image-data",
            "captcha": "captcha-secret",
            "payload": {"secret": "payload-secret"},
        }
        diagnostics = legacy_archiver_module._normalized_auto_verification_diagnostics(
            {
                "attempted": True,
                "completed": False,
                "provider": "https://verification.example.test/?token=provider-token",
                "challenge_type": [sensitive_value],
                "attempts": True,
                "reason": "cookie=verification-cookie",
                "confidence": False,
            }
        )

        with patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
            legacy_archiver_module._log_auto_verification_result(
                diagnostics,
                signed_url_available=False,
            )

        business_log_mock.assert_called_once_with(
            "archive",
            "cloakbrowser_auto_verification_fallback",
            "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
            level="warning",
            provider="unknown",
            challenge_type="unknown",
            attempts=0,
            reason="unknown",
            confidence=0.0,
        )
        serialized_calls = json.dumps(business_log_mock.call_args_list, default=str, ensure_ascii=False)
        for sensitive_marker in (
            "https://",
            "provider-token",
            "verification-cookie",
            "Authorization",
            "base64-image-data",
            "captcha-secret",
            "payload-secret",
        ):
            self.assertNotIn(sensitive_marker, serialized_calls)

    def test_auto_verification_diagnostics_accept_only_native_numeric_values(self):
        cases = (
            (True, True, 0, 0.0),
            (False, False, 0, 0.0),
            ("2", "0.614", 0, 0.0),
            (None, None, 0, 0.0),
            (float("nan"), float("inf"), 0, 0.0),
            (float("inf"), float("-inf"), 0, 0.0),
            (-2, -0.1, 0, 0.0),
            (100, 1.4, 99, 1.0),
            (2.9, 0.614, 2, 0.61),
            (-(10**400), 0.25, 0, 0.25),
            (10**400, 0.25, 99, 0.25),
            (0, -(10**400), 0, 0.0),
            (0, 10**400, 0, 1.0),
        )

        for attempts, confidence, expected_attempts, expected_confidence in cases:
            with self.subTest(attempts=attempts, confidence=confidence):
                diagnostics = legacy_archiver_module._normalized_auto_verification_diagnostics(
                    {
                        "provider": "geetest4",
                        "challenge_type": "slider",
                        "reason": "low_confidence",
                        "attempts": attempts,
                        "confidence": confidence,
                    }
                )

                self.assertEqual(diagnostics["fields"]["attempts"], expected_attempts)
                self.assertEqual(diagnostics["fields"]["confidence"], expected_confidence)

    def test_auto_verification_diagnostics_preserve_known_bridge_reason_codes_only(self):
        known_reason_codes = (
            "aborted",
            "ambiguous_candidates",
            "ambiguous_gap",
            "attempts_exhausted",
            "candidate_count_invalid",
            "challenge_unchanged",
            "challenge_unsupported",
            "checkbox_unavailable",
            "cleanup_failed",
            "click_layout_invalid",
            "click_target_unavailable",
            "completed",
            "confidence_too_low",
            "discovery_failed",
            "distance_invalid",
            "empty_screenshot",
            "gap_not_found",
            "geometry_invalid",
            "image_base64_invalid",
            "image_decode_failed",
            "image_dimensions_invalid",
            "image_format_invalid",
            "image_foreground_missing",
            "image_size_invalid",
            "image_width_invalid",
            "input_too_large",
            "interaction_failed",
            "json_invalid",
            "low_confidence",
            "mode_invalid",
            "no_challenge",
            "outcome_failed",
            "payload_invalid",
            "piece_restore_failed",
            "piece_unavailable",
            "request_invalid",
            "slider_geometry_invalid",
            "solved",
            "timeout",
            "trajectory_out_of_bounds",
            "unsupported_fields",
            "verification_failed",
            "vision_rejected",
        )
        for reason in known_reason_codes:
            with self.subTest(reason=reason):
                diagnostics = legacy_archiver_module._normalized_auto_verification_diagnostics(
                    {"reason": reason}
                )
                self.assertEqual(diagnostics["fields"]["reason"], reason)

        for reason in ("https://example.test/?token=secret", "token=secret", {"reason": "payload-secret"}):
            with self.subTest(reason=reason):
                diagnostics = legacy_archiver_module._normalized_auto_verification_diagnostics(
                    {"reason": reason}
                )
                self.assertEqual(diagnostics["fields"]["reason"], "unknown")

    def test_auto_verification_logs_a_real_production_solver_reason_end_to_end(self):
        diagnostics = legacy_archiver_module._normalized_auto_verification_diagnostics(
            {
                "attempted": True,
                "completed": False,
                "provider": "geetest4",
                "challenge_type": "slider",
                "attempts": 1,
                "reason": "confidence_too_low",
                "confidence": 0.61,
            }
        )

        with patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
            legacy_archiver_module._log_auto_verification_result(
                diagnostics,
                signed_url_available=False,
            )

        business_log_mock.assert_called_once_with(
            "archive",
            "cloakbrowser_auto_verification_fallback",
            "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。",
            level="warning",
            provider="geetest4",
            challenge_type="slider",
            attempts=1,
            reason="confidence_too_low",
            confidence=0.61,
        )

    def test_fetch_instance_3mf_keeps_manual_fallback_when_attempts_is_an_extreme_integer(self):
        session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                return_value={
                    "status_code": 418,
                    "payload": {"captchaId": "safe-id"},
                    "verification": {
                        "attempted": True,
                        "completed": False,
                        "provider": "geetest4",
                        "challenge_type": "slider",
                        "attempts": 10**400,
                        "reason": "verification_failed",
                        "confidence": 0.5,
                    },
                },
            ), patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
                _name, _url, _used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=account-secret",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(failure["state"], "verification_required")
        self.assertEqual(failure["message"], "指纹浏览器未取得 3MF 授权，请在官网完成验证后点击“已验证”继续归档。")
        self.assertEqual(business_log_mock.call_args.kwargs["attempts"], 99)
        self.assertEqual(business_log_mock.call_args.kwargs["reason"], "verification_failed")

    def test_fetch_instance_3mf_logs_auto_verification_completion_once_after_signed_url(self):
        session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                return_value={
                    "status_code": 200,
                    "payload": {
                        "name": "browser.3mf",
                        "url": "https://download.example.test/browser.3mf?signature=signed-secret",
                    },
                    "verification": {
                        "attempted": True,
                        "completed": True,
                        "provider": "geetest4",
                        "challenge_type": "slider",
                        "attempts": 2,
                        "reason": "solved",
                        "confidence": 0.986,
                    },
                },
            ), patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
                name, url, _used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=account-secret",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(name, "browser.3mf")
        self.assertTrue(url.startswith("https://download.example.test/browser.3mf"))
        self.assertEqual(failure["state"], "available")
        business_log_mock.assert_called_once_with(
            "archive",
            "cloakbrowser_auto_verification_completed",
            "指纹浏览器已完成 3MF 自动验证。",
            level="info",
            provider="geetest4",
            challenge_type="slider",
            attempts=2,
            reason="solved",
            confidence=0.99,
        )
        serialized_calls = json.dumps(business_log_mock.call_args_list, default=str, ensure_ascii=False)
        self.assertNotIn("signed-secret", serialized_calls)

    def test_fetch_instance_3mf_reports_browser_bridge_timeout_as_http_error(self):
        session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
        try:
            from app.services.cloakbrowser_session import CloakBrowserBridgeError

            with patch.object(
                legacy_archiver_module,
                "browser_authorize_3mf_download",
                side_effect=CloakBrowserBridgeError("指纹浏览器 CDP 操作超时。"),
            ), patch.object(legacy_archiver_module, "makerworld_browser_get_json") as direct_mock, \
                    patch.object(legacy_archiver_module, "append_business_log") as business_log_mock:
                _name, _url, _used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=abc",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                    model_page_url="https://makerworld.com.cn/zh/models/123456",
                    browser_authorization=True,
                    browser_profile_id="profile-cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(failure["state"], "http_error")
        self.assertIn("暂时无法完成", failure["message"])
        direct_mock.assert_not_called()
        business_log_mock.assert_called_once()
        self.assertEqual(business_log_mock.call_args.args[:2], ("archive", "cloakbrowser_3mf_authorization_error"))
        self.assertEqual(business_log_mock.call_args.kwargs["error"], "指纹浏览器 CDP 操作超时。")

    def test_fetch_instance_3mf_uses_cloakbrowser_download_payload(self):
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        try:
            legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
            session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
            calls = []

            def fake_browser_get(url, **kwargs):
                calls.append((url, kwargs))
                return {"name": "from-cloakbrowser.3mf", "url": "https://example.test/from-cloakbrowser.3mf"}

            with patch.object(legacy_archiver_module, "makerworld_browser_get_json", side_effect=fake_browser_get):
                name, url, used_api_url, failure = fetch_instance_3mf(
                    session,
                    2864062,
                    "token=abc",
                    api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                    origin="https://makerworld.com.cn",
                )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait

        self.assertEqual(name, "from-cloakbrowser.3mf")
        self.assertEqual(url, "https://example.test/from-cloakbrowser.3mf")
        self.assertEqual(failure["state"], "available")
        self.assertEqual(used_api_url, calls[0][0])
        self.assertIn("Cookie", calls[0][1]["headers"])

    def test_fake_three_mf_fetch_skips_remote_calls(self):
        class FailingSession:
            headers = {"User-Agent": "test-agent"}

            def get(self, *_args, **_kwargs):
                raise AssertionError("fake 3MF mode must not call the remote API")

        with patch.dict("os.environ", {"MAKERHUB_FAKE_THREE_MF_DOWNLOADS": "true"}):
            name, url, used_api_url, failure = fetch_instance_3mf(
                FailingSession(),
                2864062,
                "token=abc",
                api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                origin="https://makerworld.com.cn",
            )

        self.assertEqual(name, "2864062.3mf")
        self.assertEqual(url, "makerhub://fake-3mf/2864062.3mf")
        self.assertIn("/instance/2864062/f3mf", used_api_url)
        self.assertEqual(failure["state"], "available")

    def test_fake_three_mf_download_writes_placeholder_zip(self):
        class FailingSession:
            def get(self, *_args, **_kwargs):
                raise AssertionError("fake 3MF mode must not download the binary")

        with tempfile.TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / "fake.3mf"
            with patch.dict("os.environ", {"MAKERHUB_FAKE_THREE_MF_DOWNLOADS": "true"}):
                download_file(FailingSession(), "https://example.test/real.3mf", dest)

            self.assertTrue(dest.exists())
            with zipfile.ZipFile(dest) as archive:
                self.assertIn("3D/3dmodel.model", archive.namelist())
                metadata = archive.read("Metadata/makerhub_fake_download.json").decode("utf-8")
            self.assertIn("MakerHub", metadata)
            self.assertIn("real.3mf", metadata)

    def test_rebuild_marks_three_mf_static_download_failure_as_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir) / "LOCAL_Model"
            model_dir.mkdir()
            meta_path = model_dir / "meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "id": "1686848",
                        "url": "https://makerworld.com.cn/zh/models/1686848",
                        "title": "Demo model",
                        "baseName": "LOCAL_Model",
                        "instances": [
                            {
                                "id": "profile-1",
                                "title": "0.2mm profile",
                                "name": "profile.3mf",
                                "fileName": "profile.3mf",
                                "downloadUrl": "https://cdn.example.test/profile.3mf",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch.object(legacy_archiver_module, "download_file", side_effect=RuntimeError("403 Cloudflare challenge")), patch.object(
                legacy_archiver_module, "_wait_before_three_mf_download", return_value=0
            ):
                rebuild_once(meta_path)

            updated_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            instance = updated_meta["instances"][0]
            self.assertEqual(instance["downloadState"], "cloudflare")
            self.assertIn("403 Cloudflare challenge", instance["downloadMessage"])
            self.assertFalse((model_dir / "instances" / "profile.3mf").exists())

            items = _build_missing_3mf_items(meta_path, updated_meta, resolved_files={"matches": {}})
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["instance_id"], "profile-1")
            self.assertIn("MakerWorld 需要验证", items[0]["message"])

    def test_rebuild_reports_only_new_three_mf_files_as_downloaded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            model_dir = Path(temp_dir) / "LOCAL_Model"
            model_dir.mkdir()
            meta_path = model_dir / "meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "id": "1686848",
                        "url": "https://makerworld.com.cn/zh/models/1686848",
                        "title": "Demo model",
                        "baseName": "LOCAL_Model",
                        "instances": [
                            {
                                "id": "profile-1",
                                "title": "0.2mm profile",
                                "name": "profile.3mf",
                                "fileName": "profile.3mf",
                                "downloadUrl": "https://cdn.example.test/profile.3mf",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            def write_download(_session, _url, dest, **_kwargs):
                dest.write_bytes(b"3mf")

            with patch.object(legacy_archiver_module, "download_file", side_effect=write_download), \
                    patch.object(legacy_archiver_module, "_wait_before_three_mf_download", return_value=0):
                result = rebuild_once(meta_path)

            self.assertEqual(result["three_mf_downloaded"], 1)
            self.assertEqual(result["three_mf_existing"], 0)

            with patch.object(legacy_archiver_module, "download_file") as download_again:
                second_result = rebuild_once(meta_path)

            download_again.assert_not_called()
            self.assertEqual(second_result["three_mf_downloaded"], 0)
            self.assertEqual(second_result["three_mf_existing"], 1)

    def test_missing_three_mf_local_file_check_is_only_for_rebuild(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            instance = {
                "id": "profile-1",
                "title": "0.2mm profile",
                "fileName": "profile.3mf",
                "downloadUrl": "https://cdn.example.test/profile.3mf",
            }

            self.assertEqual(_missing_3mf_instances([dict(instance)], work_dir, require_local_file=False), [])
            missing = _missing_3mf_instances([dict(instance)], work_dir, require_local_file=True)

            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["downloadState"], "missing")
            self.assertEqual(missing[0]["downloadMessage"], "3MF 文件尚未保存到本地，等待重新下载。")

    def test_download_file_does_not_use_cloakbrowser(self):
        class FakeStreamResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                yield b"real-bytes"

        class FakeSession:
            def __init__(self):
                self.calls = []

            def get(self, url, timeout=None, stream=False):
                self.calls.append((url, timeout, stream))
                return FakeStreamResponse()

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            legacy_archiver_module,
            "makerworld_browser_get_text",
        ) as browser_text_mock, patch.object(
            legacy_archiver_module,
            "makerworld_browser_get_json",
        ) as browser_json_mock:
            session = FakeSession()
            dest = Path(temp_dir) / "asset.jpg"
            download_file(session, "https://cdn.example.test/asset.jpg", dest)
            self.assertEqual(dest.read_bytes(), b"real-bytes")

        self.assertEqual(session.calls[0][0], "https://cdn.example.test/asset.jpg")
        browser_text_mock.assert_not_called()
        browser_json_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
