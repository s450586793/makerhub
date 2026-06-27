import unittest
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.api import system as system_api
from app.api import tasks_routes
from app.schemas.models import AppConfig, SubscriptionRecord
from app.services import catalog
from app.services import runtime_diagnostics


class _FakeConnection:
    def __init__(self, rows_by_marker):
        self.rows_by_marker = rows_by_marker
        self.last_marker = ""

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        if "pg_stat_user_tables" in text:
            self.last_marker = "tables"
        elif "FROM makerhub_state_events" in text and "GROUP BY scope" in text:
            self.last_marker = "events"
        elif "FROM makerhub_logs" in text and "GROUP BY file_name" in text:
            self.last_marker = "logs"
        elif "FROM makerhub_json_state" in text:
            self.last_marker = "states"
        else:
            self.last_marker = "unknown"
        return self

    def fetchall(self):
        return self.rows_by_marker.get(self.last_marker, [])


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class RuntimeDiagnosticsTest(unittest.TestCase):
    def test_dashboard_payload_can_include_runtime_snapshot(self):
        runtime_snapshot = {
            "dashboard": {
                "active_runs": [{"run_id": "run-1", "status": "running"}],
                "active_batches": [],
                "summary": {"active_runs": 1, "active_batches": 0, "failures": 0},
            }
        }

        with patch("app.services.catalog.load_database_json_state", return_value=runtime_snapshot):
            payload = catalog._runtime_dashboard_snapshot()

        self.assertEqual(payload["active_runs"][0]["run_id"], "run-1")

    def test_tasks_payload_can_include_runtime_task_snapshot(self):
        runtime_snapshot = {
            "tasks": {
                "runs": [{"run_id": "run-1", "status": "running"}],
                "batches": [{"batch_id": "batch-1", "status": "queued"}],
                "failures": [],
            }
        }

        with patch("app.services.catalog.load_database_json_state", return_value=runtime_snapshot), \
                patch.object(catalog.TaskStateStore, "load_archive_queue", return_value={"active": [], "queued": [], "recent_failures": [], "running_count": 0, "queued_count": 0}), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf", return_value={"items": [], "count": 0}), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_payload()

        self.assertEqual(payload["runtime"]["runs"][0]["run_id"], "run-1")

    def test_tasks_light_payload_keeps_first_load_small(self):
        archive_queue = {
            "active": [{"id": f"active-{index}", "status": "running"} for index in range(12)],
            "queued": [{"id": f"queued-{index}", "status": "queued"} for index in range(30)],
            "recent_failures": [{"id": f"failed-{index}", "status": "failed"} for index in range(18)],
            "running_count": 12,
            "queued_count": 30,
        }
        missing_3mf = {
            "items": [{"model_id": str(index), "title": f"missing-{index}"} for index in range(16)],
            "count": 16,
        }
        organize_tasks = {
            "items": [{"id": f"organize-{index}", "status": "running"} for index in range(14)],
            "count": 14,
            "detected_total": 14,
            "running_count": 14,
            "queued_count": 0,
        }

        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue", return_value=archive_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf", return_value=missing_3mf), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value=organize_tasks):
            payload = catalog.build_tasks_light_payload()

        self.assertLessEqual(len(payload["archive_queue"]["active"]), 5)
        self.assertLessEqual(len(payload["archive_queue"]["queued"]), 5)
        self.assertLessEqual(len(payload["archive_queue"]["recent_failures"]), 5)
        self.assertLessEqual(len(payload["missing_3mf"]["items"]), 5)
        self.assertLessEqual(len(payload["organize_tasks"]["items"]), 8)
        self.assertEqual(payload["summary"]["running_or_queued"], 42)
        self.assertEqual(payload["summary"]["missing_3mf_count"], 16)
        self.assertEqual(payload["organize_tasks"]["active_count"], 14)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False)), 8000)

    def test_tasks_light_payload_groups_only_visible_archive_items(self):
        class GuardedQueueItems(list):
            def __iter__(self):
                for index, item in enumerate(super().__iter__()):
                    if index >= 5:
                        raise AssertionError("light payload should not iterate hidden archive queue items")
                    yield item

        archive_queue = {
            "active": [],
            "queued": GuardedQueueItems([{"id": f"queued-{index}", "status": "queued"} for index in range(2000)]),
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 2000,
        }
        missing_3mf = {"items": [], "count": 0}

        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue", return_value=archive_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf", return_value=missing_3mf), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        self.assertEqual(payload["archive_queue"]["queued_count"], 2000)
        self.assertEqual(payload["archive_queue_display"]["raw_queued_count"], 2000)
        self.assertLessEqual(len(payload["archive_queue_display"]["queued"]), 5)

    def test_tasks_payload_groups_subscription_children_for_display(self):
        archive_queue = {
            "active": [
                {
                    "id": "model-running-1",
                    "status": "running",
                    "title": "https://makerworld.com.cn/zh/models/1001",
                    "mode": "single_model",
                    "url": "https://makerworld.com.cn/zh/models/1001",
                    "progress": 60,
                    "message": "正在下载",
                    "meta": {
                        "scan_mode": "subscription:sub-author",
                        "subscription_name": "作者 A | 已发布",
                        "batch_summary": {"discovered": 2, "queued": 2, "expected_total": 30},
                    },
                },
                {
                    "id": "model-running-2",
                    "status": "running",
                    "title": "https://makerworld.com.cn/zh/models/1002",
                    "mode": "single_model",
                    "url": "https://makerworld.com.cn/zh/models/1002",
                    "progress": 20,
                    "message": "等待 3MF",
                    "meta": {
                        "scan_mode": "subscription:sub-author",
                        "subscription_name": "作者 A | 已发布",
                        "batch_summary": {"discovered": 2, "queued": 2, "expected_total": 30},
                    },
                },
            ],
            "queued": [
                {
                    "id": "source-author",
                    "status": "queued",
                    "title": "https://makerworld.com.cn/zh/@demo/upload",
                    "mode": "author_upload",
                    "url": "https://makerworld.com.cn/zh/@demo/upload",
                    "meta": {
                        "scan_mode": "subscription:sub-author",
                        "subscription_name": "作者 A | 已发布",
                        "batch_summary": {"discovered": 2, "queued": 2, "expected_total": 30},
                    },
                },
                {
                    "id": "source-collection",
                    "status": "queued",
                    "title": "https://makerworld.com.cn/zh/@demo/collections/models",
                    "mode": "collection_models",
                    "url": "https://makerworld.com.cn/zh/@demo/collections/models",
                    "meta": {
                        "scan_mode": "subscription:sub-collection",
                        "subscription_name": "收藏夹 B",
                        "batch_summary": {"discovered": 3, "queued": 3, "expected_total": 100},
                    },
                },
            ],
            "recent_failures": [],
            "running_count": 2,
            "queued_count": 2,
        }

        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue", return_value=archive_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf", return_value={"items": [], "count": 0}), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_payload()

        self.assertEqual(len(payload["archive_queue"]["active"]), 2)
        display_queue = payload["archive_queue_display"]
        self.assertEqual(display_queue["running_count"], 1)
        self.assertEqual(display_queue["queued_count"], 2)
        self.assertEqual([item["display_kind"] for item in display_queue["active"]], ["subscription_source"])
        self.assertEqual(display_queue["active"][0]["title"], "作者 A | 已发布")
        self.assertEqual(display_queue["active"][0]["child_count"], 2)
        self.assertEqual(display_queue["active"][0]["progress"], 40)
        self.assertEqual(display_queue["active"][0]["message"], "发现 2 个模型，按来源聚合展示。")
        self.assertEqual(display_queue["queued"][0]["title"], "作者 A | 已发布")
        self.assertEqual(display_queue["queued"][0]["child_count"], 2)
        self.assertEqual(display_queue["queued"][0]["message"], "发现 2 个模型，按来源聚合展示。")
        self.assertEqual(display_queue["queued"][1]["title"], "收藏夹 B")
        self.assertEqual(display_queue["queued"][1]["source_mode"], "collection_models")

    def test_dashboard_light_payload_does_not_load_full_subscription_state(self):
        config = AppConfig(
            subscriptions=[
                SubscriptionRecord(
                    id="sub-1",
                    name="作者",
                    url="https://makerworld.com/zh/@demo/upload",
                    mode="author_upload",
                    enabled=True,
                )
            ]
        )

        with patch.object(catalog.TaskStateStore, "load_subscriptions_state", side_effect=AssertionError("full state should not load")), \
                patch.object(catalog, "build_tasks_light_payload", return_value={
                    "archive_queue": {"active": [], "queued": [], "recent_failures": [], "running_count": 0, "queued_count": 0},
                    "missing_3mf": {"items": [], "count": 0},
                    "organize_tasks": {"items": [], "count": 0, "running_count": 0, "queued_count": 0, "detected_total": 0, "active_count": 0},
                    "remote_refresh": {"status": "idle", "running": False},
                    "source_refresh": {"queue": {}, "runs": {}},
                    "summary": {"running_or_queued": 0, "missing_3mf_count": 0, "organize_count": 0},
                }), \
                patch.object(catalog, "archive_model_index_row_count", return_value=12), \
                patch.object(catalog, "build_source_health_cards", return_value=[]):
            payload = catalog.build_dashboard_light_payload(config)

        self.assertEqual(payload["stats"][0]["value"], 12)
        self.assertEqual(payload["automation_overview"]["subscriptions"]["count"], 1)
        self.assertEqual(payload["automation_overview"]["subscriptions"]["enabled_count"], 1)

    def test_models_light_payload_does_not_build_decorated_models(self):
        with patch.object(catalog, "get_decorated_models", side_effect=AssertionError("full model decoration should not run")), \
                patch.object(catalog, "load_archive_model_index_unchecked", return_value=[
                    {
                        "model_dir": "MW_1",
                        "title": "模型 A",
                        "source": "cn",
                        "author": {"name": "作者"},
                        "tags": ["tag"],
                        "stats": {"downloads": 1, "likes": 2, "prints": 3},
                        "cover_url": "/archive/MW_1/cover.webp",
                        "collect_ts": 10,
                        "publish_ts": 8,
                        "local_flags": {},
                    }
                ]), \
                patch.object(catalog.TaskStateStore, "load_model_flags", return_value={"favorites": [], "printed": [], "deleted": []}):
            payload = catalog.build_models_light_payload(page=1, page_size=12)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["model_dir"], "MW_1")
        self.assertTrue(payload["light"])

    def test_models_light_payload_uses_unchecked_index_snapshot(self):
        with patch.object(catalog, "get_decorated_models", side_effect=AssertionError("full model decoration should not run")), \
                patch.object(catalog, "load_archive_model_index", side_effect=AssertionError("stale checked index should not run")), \
                patch.object(catalog, "load_archive_model_index_unchecked", return_value=[
                    {
                        "model_dir": "MW_1",
                        "title": "模型 A",
                        "source": "cn",
                        "author": {"name": "作者"},
                        "tags": ["tag"],
                        "stats": {"downloads": 1, "likes": 2, "prints": 3},
                        "cover_url": "/archive/MW_1/cover.webp",
                        "collect_ts": 10,
                        "publish_ts": 8,
                        "local_flags": {},
                    }
                ]), \
                patch.object(catalog.TaskStateStore, "load_model_flags", return_value={"favorites": [], "printed": [], "deleted": []}):
            payload = catalog.build_models_light_payload(page=1, page_size=12)

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["model_dir"], "MW_1")
        self.assertTrue(payload["light"])

    def test_build_runtime_diagnostics_returns_database_aggregates(self):
        now = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)
        connection = _FakeConnection(
            {
                "tables": [
                    {"relname": "makerhub_logs", "total_size": "575 MB", "rows": 536482, "dead_rows": 2},
                ],
                "events": [
                    {"scope": "archive_queue", "rows": 71707, "newest": now},
                ],
                "logs": [
                    {
                        "file_name": "business.log",
                        "category": "archive",
                        "event": "single_completed",
                        "level": "info",
                        "rows": 9942,
                        "newest": now,
                    }
                ],
                "states": [
                    {"key": "archive_queue", "type": "object", "updated_at": now},
                ],
            }
        )

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": True, "schema_version": 2}), \
                patch.object(runtime_diagnostics, "database_connection", return_value=_ConnectionContext(connection)):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertTrue(payload["database"]["available"])
        self.assertEqual(payload["tables"][0]["name"], "makerhub_logs")
        self.assertEqual(payload["state_events_by_scope"][0]["scope"], "archive_queue")
        self.assertEqual(payload["recent_logs"][0]["event"], "single_completed")
        self.assertEqual(payload["json_states"][0]["key"], "archive_queue")

    def test_build_runtime_diagnostics_degrades_when_database_unavailable(self):
        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False, "error": "missing"}):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertFalse(payload["database"]["available"])
        self.assertEqual(payload["tables"], [])
        self.assertEqual(payload["state_events_by_scope"], [])

    def test_build_runtime_diagnostics_always_includes_account_health_snapshot(self):
        account_health = {
            "cn": {
                "platform": "cn",
                "status": "ok",
                "reason": "current_action_succeeded",
                "source": "archive_download",
                "updated_at": "2026-06-11T10:00:00+08:00",
            },
            "global": {
                "platform": "global",
                "status": "verification_required",
                "reason": "download_probe",
                "source": "diagnostic_probe",
                "updated_at": "2026-06-11T10:01:00+08:00",
            },
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False, "error": "missing"}), \
                patch.object(runtime_diagnostics, "load_account_health", return_value=account_health):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
        self.assertEqual(payload["account_health"]["cn"]["reason"], "current_action_succeeded")
        self.assertEqual(payload["account_health"]["cn"]["source"], "archive_download")
        self.assertEqual(payload["account_health"]["cn"]["updated_at"], "2026-06-11T10:00:00+08:00")
        self.assertEqual(payload["account_health"]["global"]["status"], "verification_required")
        self.assertEqual(payload["account_health"]["global"]["reason"], "download_probe")
        self.assertEqual(payload["account_health"]["global"]["source"], "diagnostic_probe")
        self.assertEqual(payload["account_health"]["global"]["updated_at"], "2026-06-11T10:01:00+08:00")
        self.assertNotIn("missing_3mf", payload["account_health"]["cn"])
        self.assertNotIn("missing_3mf", payload["account_health"]["global"])
        self.assertNotIn("model_url", payload["account_health"]["cn"])
        self.assertNotIn("message", payload["account_health"]["global"])

    def test_build_runtime_diagnostics_includes_archive_queue_summary(self):
        queue = {
            "active": [
                {
                    "id": "task-1",
                    "status": "running",
                    "title": "Running",
                    "lease_expires_at": "2026-06-04T09:00:00+08:00",
                },
                {"id": "batch-1", "status": "waiting_children", "title": "Batch"},
            ],
            "queued": [{"id": "task-2", "status": "queued", "title": "Queued"}],
            "recent_failures": [{"id": "task-3", "status": "failed", "title": "Failed"}],
            "running_count": 2,
            "queued_count": 1,
            "failed_count": 1,
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics.task_state_store, "load_archive_queue", return_value=queue), \
                patch.object(runtime_diagnostics, "is_lease_expired", return_value=True):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["archive_queue"]["running_count"], 2)
        self.assertEqual(payload["archive_queue"]["queued_count"], 1)
        self.assertEqual(payload["archive_queue"]["failed_count"], 1)
        self.assertEqual(payload["archive_queue"]["stale_candidates"][0]["id"], "task-1")

    def test_build_runtime_diagnostics_keeps_archive_failures_separate_from_account_health(self):
        queue = {
            "active": [],
            "queued": [],
            "recent_failures": [
                {
                    "id": "task-old",
                    "status": "failed",
                    "title": "Old failure",
                    "updated_at": "2026-06-01T09:00:00+08:00",
                }
            ],
            "running_count": 0,
            "queued_count": 0,
            "failed_count": 1,
        }
        account_health = {
            "cn": {
                "platform": "cn",
                "status": "ok",
                "reason": "current_action_succeeded",
                "source": "archive_download",
                "detail": "账号状态正常。",
                "updated_at": "2026-06-12T10:20:00+08:00",
            },
            "global": {
                "platform": "global",
                "status": "unknown",
                "reason": "",
                "source": "system",
                "detail": "",
                "updated_at": "",
            },
        }

        with patch.object(runtime_diagnostics, "database_status", return_value={"available": False}), \
                patch.object(runtime_diagnostics.task_state_store, "load_archive_queue", return_value=queue), \
                patch.object(runtime_diagnostics, "load_account_health", return_value=account_health):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertEqual(payload["archive_queue"]["failed_count"], 1)
        self.assertEqual(payload["archive_queue"]["running_count"], 0)
        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
        self.assertEqual(payload["account_health"]["cn"]["detail"], "账号状态正常。")
        self.assertEqual(payload["account_health"]["cn"]["source"], "archive_download")

    def test_system_diagnostics_route_requires_session_and_returns_payload(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session"}))
        with patch.object(system_api.config_api, "_require_session_auth") as require_auth, \
                patch.object(system_api, "build_runtime_diagnostics", return_value={"database": {"available": True}}):
            payload = asyncio.run(system_api.get_system_diagnostics(request))

        require_auth.assert_called_once_with(request)
        self.assertTrue(payload["database"]["available"])

    def test_repair_archive_queue_route_requires_session_and_returns_summary(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        repair_payload = {
            "summary": {
                "examined": 1,
                "requeued": 1,
                "failed": 0,
                "finalized": 0,
                "skipped": 0,
                "errors": [],
            },
            "queue": {
                "running_count": 0,
                "queued_count": 1,
                "failed_count": 0,
                "active": [],
                "queued": [],
                "recent_failures": [],
            },
        }

        with patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch.object(tasks_routes.task_state_store, "repair_archive_queue", return_value=repair_payload), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(tasks_routes.repair_archive_queue(request))

        require_auth.assert_called_once_with(request)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["requeued"], 1)

    def test_verified_missing_3mf_route_retries_same_platform_verification_items(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        retry_payload = {
            "accepted": True,
            "accepted_count": 2,
            "queued_count": 1,
            "failed_count": 0,
            "message": "验证后重试完成。",
        }

        with patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch.object(tasks_routes, "run_task_api", side_effect=lambda func, **kwargs: func(**kwargs)) as run_task_api, \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf", return_value=retry_payload) as retry_mock, \
                patch.object(tasks_routes, "mark_account_ok", return_value={"status": "ok"}) as mark_account_ok_mock, \
                patch.object(tasks_routes, "append_business_log") as log_mock:
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        require_auth.assert_called_once_with(request)
        retry_mock.assert_called_once_with(platform="global")
        mark_account_ok_mock.assert_called_once_with(
            "global",
            source="manual_verification",
            detail="用户已在 MakerWorld 完成验证，已重新启动同平台 3MF 重试。",
        )
        run_task_api.assert_called_once()
        self.assertEqual(payload["accepted_count"], retry_payload["accepted_count"])
        self.assertEqual(payload["account_health"]["status"], "ok")
        log_mock.assert_called_once()

    def test_verified_missing_3mf_route_marks_platform_account_ok_when_user_confirms(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        retry_payload = {
            "accepted": False,
            "accepted_count": 0,
            "queued_count": 0,
            "failed_count": 0,
            "message": "当前没有同平台验证类 3MF 任务。",
        }

        with patch.object(tasks_routes, "_require_session_auth"), \
                patch.object(tasks_routes, "run_task_api", side_effect=lambda func, **kwargs: func(**kwargs)), \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf", return_value=retry_payload), \
                patch.object(tasks_routes, "mark_account_ok", return_value={"status": "ok"}) as mark_account_ok_mock, \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        mark_account_ok_mock.assert_called_once_with(
            "global",
            source="manual_verification",
            detail="用户已在 MakerWorld 完成验证，已重新启动同平台 3MF 重试。",
        )
        self.assertEqual(payload["account_health"]["status"], "ok")

    def test_verified_missing_3mf_route_marks_account_ok_before_retrying_items(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        call_order = []

        def _retry(platform):
            call_order.append(f"retry:{platform}")
            return {
                "accepted": True,
                "accepted_count": 1,
                "queued_count": 0,
                "failed_count": 0,
                "message": "验证后重试完成。",
            }

        def _mark(platform, **_kwargs):
            call_order.append(f"mark:{platform}")
            return {"status": "ok"}

        with patch.object(tasks_routes, "_require_session_auth"), \
                patch.object(tasks_routes, "run_task_api", side_effect=lambda func, **kwargs: func(**kwargs)), \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf", side_effect=_retry), \
                patch.object(tasks_routes, "mark_account_ok", side_effect=_mark), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        self.assertEqual(call_order, ["mark:global", "retry:global"])
        self.assertEqual(payload["account_health"]["status"], "ok")

    def test_verified_missing_3mf_runtime_route_retries_cookie_invalid_items(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        runtime_payload = {
            "run_id": "run-verified",
            "total": 2,
            "message": "已提交运行核心。",
        }

        with patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch.object(tasks_routes, "_runtime_engine_enabled", return_value=True), \
                patch.object(tasks_routes, "run_task_api", side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)), \
                patch.object(tasks_routes, "_submit_runtime_run", return_value=runtime_payload) as submit_runtime_run, \
                patch.object(tasks_routes, "mark_account_ok", return_value={"status": "ok"}), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="cn"),
                    request,
                )
            )

        require_auth.assert_called_once_with(request)
        submit_runtime_run.assert_called_once()
        run_type, context = submit_runtime_run.call_args.args
        self.assertEqual(run_type, "missing_3mf_retry")
        self.assertEqual(context["platform"], "cn")
        self.assertIn("cookie_invalid", context["statuses"])
        self.assertIn("verification_required", context["statuses"])
        self.assertNotIn("status", context)
        self.assertEqual(payload["account_health"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
