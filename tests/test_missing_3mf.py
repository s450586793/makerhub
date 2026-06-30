import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.services.archive_worker as archive_worker_module
import app.services.legacy_archiver as legacy_archiver_module
import app.services.task_state as task_state_module
from app.services.legacy_archiver import (
    _build_instance_api_candidates,
    _missing_3mf_failure_for_skipped_fetch,
    download_file,
    fetch_instance_3mf,
)
from app.services.archive_worker import ArchiveTaskManager
from app.services.remote_refresh import _build_missing_3mf_items
from app.services.task_state import METADATA_ONLY_MISSING_3MF_MESSAGE, _normalize_missing_3mf


class Missing3mfTest(unittest.TestCase):
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

    def test_normalize_filters_metadata_only_placeholders(self):
        payload = {
            "items": [
                {
                    "model_id": "1686848",
                    "title": "仪羽毛球（球头无挖孔）",
                    "status": "missing",
                    "message": METADATA_ONLY_MISSING_3MF_MESSAGE,
                },
                {
                    "model_id": "2",
                    "title": "Real missing profile",
                    "status": "missing",
                    "message": "未获取到 3MF 下载地址。",
                },
            ]
        }

        with patch.object(task_state_module, "load_database_json_state", return_value={}):
            normalized = _normalize_missing_3mf(payload)

        self.assertEqual(len(normalized["items"]), 1)
        self.assertEqual(normalized["items"][0]["model_id"], "2")

    def test_remote_refresh_missing_builder_skips_metadata_only_placeholders(self):
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
                        "title": "信息补全占位配置",
                        "downloadState": "missing",
                        "downloadMessage": METADATA_ONLY_MISSING_3MF_MESSAGE,
                    },
                    {
                        "id": "profile-2",
                        "title": "真实缺失配置",
                        "downloadState": "missing",
                        "downloadMessage": "未获取到 3MF 下载地址。",
                    },
                ],
            }

            items = _build_missing_3mf_items(meta_path, meta, resolved_files={"matches": {}})

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["instance_id"], "profile-2")

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

    def test_manual_missing_retry_clears_stale_limit_guard_and_queues(self):
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
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "cn"}):
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
                guard_state = archive_worker_module._read_three_mf_limit_guard()
        finally:
            archive_worker_module._select_cookie = original_select_cookie

        self.assertTrue(result["accepted"])
        self.assertEqual(len(submitted), 1)
        self.assertTrue(submitted[0]["force"])
        self.assertFalse(guard_state["active"])
        self.assertEqual(updates[-1]["status"], "queued")

    def test_manual_missing_retry_resets_daily_quota_before_queueing(self):
        original_select_cookie = archive_worker_module._select_cookie
        quota_calls = []
        try:
            with patch.object(archive_worker_module, "load_database_json_state", side_effect=lambda _key, default: dict(default)), \
                    patch.object(archive_worker_module, "save_database_json_state", side_effect=lambda _key, payload: payload), \
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", side_effect=lambda **kwargs: quota_calls.append(kwargs) or {"reset": True, "source": "global", "previous": {"used": 100}}):
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
        self.assertEqual(quota_calls, [{"url": "https://makerworld.com/zh/models/2193050"}])
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
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}), \
                    patch.object(archive_worker_module, "get_archive_snapshot", return_value={"archived_keys": []}), \
                    patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                    patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                    patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True}), \
                    patch.object(archive_worker_module, "threading") as threading_mock:
                threading_mock.Thread.side_effect = AssertionError("worker should not start in this test")
                manager = ArchiveTaskManager(background_enabled=False)
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store.update_missing_3mf_status = lambda **_payload: None
                manager._deleted_task_lookup = lambda: {}
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
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}), \
                    patch.object(archive_worker_module, "get_archive_snapshot", return_value={"archived_keys": []}), \
                    patch.object(archive_worker_module, "_read_three_mf_limit_guard", return_value={"active": False}), \
                    patch.object(archive_worker_module, "_is_three_mf_limit_guard_active_for_url", return_value=False), \
                    patch.object(archive_worker_module, "three_mf_gate_for_url", return_value={"open": True}), \
                    patch.object(archive_worker_module, "threading") as threading_mock:
                threading_mock.Thread.side_effect = AssertionError("worker should not start in this test")
                manager = ArchiveTaskManager(background_enabled=False)
                manager.store = SimpleNamespace(load=lambda: SimpleNamespace(cookies=[]))
                manager.task_store.update_missing_3mf_status = lambda **_payload: None
                manager._deleted_task_lookup = lambda: {}
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
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}):
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
                    patch.object(archive_worker_module, "reset_three_mf_daily_quota", return_value={"reset": False, "source": "global"}):
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
                patch.object(archive_worker_module, "append_business_log"), \
                patch.object(archive_worker_module, "_reset_three_mf_daily_quota_for_manual_retry"):
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
                patch.object(archive_worker_module, "append_business_log"), \
                patch.object(archive_worker_module, "_reset_three_mf_daily_quota_for_manual_retry"):
            result = manager.retry_all_missing_3mf()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["accepted_count"], 0)
        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["failed_count"], 0)

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

    def test_run_single_task_marks_account_ok_after_missing_3mf_retry_success(self):
        manager = ArchiveTaskManager(background_enabled=False)
        manager.store = SimpleNamespace(
            load=lambda: SimpleNamespace(cookies=[], proxy=None, three_mf_limits=None)
        )
        active_updates = []
        completed = []
        replaced_missing = []
        removed_failures = []
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
                    },
                ), \
                patch.object(archive_worker_module, "mark_account_ok") as mark_account_ok_mock, \
                patch.object(archive_worker_module, "invalidate_model_detail_cache"), \
                patch.object(archive_worker_module, "upsert_archive_snapshot_model", return_value=True), \
                patch.object(archive_worker_module, "invalidate_archive_snapshot"), \
                patch.object(archive_worker_module, "_log_archive"):
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
        self.assertEqual(completed, [("task-1", {"progress": 100, "message": "归档完成：Demo Model"})])
        self.assertFalse(active_updates)

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

    def test_fetch_instance_3mf_continues_after_auth_required_candidate(self):
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        original_scrapling_fetch_json = legacy_archiver_module.fetch_json_with_scrapling
        original_scrapling_only = legacy_archiver_module.scrapling_only
        try:
            legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
            legacy_archiver_module.fetch_json_with_scrapling = lambda *_args, **_kwargs: (
                None,
                SimpleNamespace(ok=False, status_code=0, text="", error="", engine="disabled"),
            )
            legacy_archiver_module.scrapling_only = lambda *_args, **_kwargs: False
            session = SimpleNamespace(headers={"User-Agent": "test-agent"})
            calls = []

            class FakeResponse:
                def __init__(self, status_code: int, payload: dict):
                    self.status_code = status_code
                    self._payload = payload
                    self.text = __import__("json").dumps(payload, ensure_ascii=False)

                def json(self):
                    return self._payload

            def fake_get(url, timeout=None, headers=None):
                calls.append(url)
                if len(calls) == 1:
                    return FakeResponse(403, {"code": 1, "error": "Please log in to download models."})
                return FakeResponse(200, {"name": "ok.3mf", "url": "https://example.test/ok.3mf"})

            session.get = fake_get

            name, url, used_api_url, failure = fetch_instance_3mf(
                session,
                2864062,
                "token=abc",
                api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                origin="https://makerworld.com.cn",
            )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait
            legacy_archiver_module.fetch_json_with_scrapling = original_scrapling_fetch_json
            legacy_archiver_module.scrapling_only = original_scrapling_only

        self.assertEqual(name, "ok.3mf")
        self.assertEqual(url, "https://example.test/ok.3mf")
        self.assertEqual(failure["state"], "available")
        self.assertEqual(len(calls), 2)
        self.assertEqual(used_api_url, calls[-1])

    def test_fetch_instance_3mf_uses_scrapling_download_payload(self):
        original_wait = legacy_archiver_module._wait_before_three_mf_download
        original_scrapling_fetch_json = legacy_archiver_module.fetch_json_with_scrapling
        try:
            legacy_archiver_module._wait_before_three_mf_download = lambda *_args, **_kwargs: 0
            session = SimpleNamespace(headers={"User-Agent": "test-agent"}, get=lambda *_args, **_kwargs: None)
            calls = []

            def fake_scrapling(url, **kwargs):
                calls.append((url, kwargs))
                return (
                    {"name": "from-scrapling.3mf", "url": "https://example.test/from-scrapling.3mf"},
                    SimpleNamespace(ok=True, status_code=200, text="", error="", engine="scrapling-static"),
                )

            legacy_archiver_module.fetch_json_with_scrapling = fake_scrapling

            name, url, used_api_url, failure = fetch_instance_3mf(
                session,
                2864062,
                "token=abc",
                api_url="https://makerworld.com.cn/api/v1/design-service/instance/2864062/f3mf?type=download&fileType=",
                origin="https://makerworld.com.cn",
            )
        finally:
            legacy_archiver_module._wait_before_three_mf_download = original_wait
            legacy_archiver_module.fetch_json_with_scrapling = original_scrapling_fetch_json

        self.assertEqual(name, "from-scrapling.3mf")
        self.assertEqual(url, "https://example.test/from-scrapling.3mf")
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


if __name__ == "__main__":
    unittest.main()
