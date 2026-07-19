import unittest
import asyncio
import json
import os
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
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value=archive_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value=missing_3mf), \
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
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value=archive_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value=missing_3mf), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        self.assertEqual(payload["archive_queue"]["queued_count"], 2000)
        self.assertEqual(payload["archive_queue_display"]["raw_queued_count"], 2000)
        self.assertLessEqual(len(payload["archive_queue_display"]["queued"]), 5)

    def test_tasks_light_payload_does_not_iterate_hidden_missing_3mf_items(self):
        class GuardedMissingItems(list):
            def __iter__(self):
                for index, item in enumerate(super().__iter__()):
                    if index >= 5:
                        raise AssertionError("light payload should not iterate hidden missing 3MF items")
                    yield item

        missing_3mf = {
            "items": GuardedMissingItems([
                {
                    "model_id": str(index),
                    "title": f"missing-{index}",
                    "status": "queued",
                    "model_url": f"https://makerworld.com.cn/zh/models/{index}",
                }
                for index in range(2000)
            ]),
            "count": 2000,
        }

        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value={"active": [], "queued": [], "recent_failures": [], "running_count": 0, "queued_count": 0}), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value=missing_3mf), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        self.assertEqual(payload["missing_3mf"]["count"], 2000)
        self.assertTrue(payload["missing_3mf"]["items_truncated"])
        self.assertLessEqual(len(payload["missing_3mf"]["items"]), 5)

    def test_tasks_light_route_does_not_load_legacy_config_missing_3mf_fallback(self):
        async def _run() -> dict:
            with patch.object(tasks_routes.store, "load", side_effect=AssertionError("light tasks must not load app config")), \
                    patch.object(tasks_routes, "build_tasks_light_payload", return_value={"light": True}), \
                    patch.object(tasks_routes, "run_ui_io", side_effect=lambda func: func()):
                return await tasks_routes.get_tasks_light_data()

        payload = asyncio.run(_run())

        self.assertEqual(payload, {"light": True})

    def test_tasks_light_payload_uses_compact_missing_3mf_loader(self):
        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value={"active": [], "queued": [], "recent_failures": [], "running_count": 0, "queued_count": 0}), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf", side_effect=AssertionError("light payload must not normalize full missing 3MF state")), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value={"items": [{"model_id": "1"}], "count": 2000, "items_truncated": True}), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        self.assertEqual(payload["missing_3mf"]["count"], 2000)
        self.assertEqual(payload["missing_3mf"]["items"], [{"model_id": "1"}])
        self.assertTrue(payload["missing_3mf"]["items_truncated"])

    def test_tasks_light_payload_uses_compact_archive_queue_loader(self):
        compact_queue = {
            "active": [{"id": "active-1", "status": "running"}],
            "queued": [{"id": "queued-1", "status": "queued"}],
            "recent_failures": [],
            "running_count": 12,
            "queued_count": 2000,
            "failed_count": 0,
        }
        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue", side_effect=AssertionError("light payload must not load full archive queue")), \
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value=compact_queue, create=True), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value={"items": [], "count": 0}), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        self.assertEqual(payload["archive_queue"]["running_count"], 12)
        self.assertEqual(payload["archive_queue"]["queued_count"], 2000)

    def test_tasks_light_payload_marks_truncated_subscription_group_as_partial(self):
        compact_queue = {
            "active": [],
            "queued": [
                {
                    "id": f"queued-{index}",
                    "status": "queued",
                    "url": f"https://makerworld.com.cn/zh/models/{index}",
                    "meta": {
                        "scan_mode": "subscription:sub-author",
                        "subscription_name": "作者 A | 已发布",
                    },
                }
                for index in range(5)
            ],
            "recent_failures": [],
            "running_count": 0,
            "queued_count": 20,
            "failed_count": 0,
            "queued_truncated": True,
        }
        with patch("app.services.catalog.load_database_json_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_archive_queue_compact", return_value=compact_queue), \
                patch.object(catalog.TaskStateStore, "load_missing_3mf_compact", return_value={"items": [], "count": 0}), \
                patch.object(catalog.TaskStateStore, "load_remote_refresh_state", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_queue", return_value={"active": [], "queued": [], "recent_failures": []}), \
                patch.object(catalog.TaskStateStore, "load_source_refresh_runs", return_value={}), \
                patch.object(catalog.TaskStateStore, "load_organize_tasks", return_value={"items": [], "count": 0}):
            payload = catalog.build_tasks_light_payload()

        grouped = payload["archive_queue_display"]["queued"][0]
        self.assertTrue(grouped["display_partial"])
        self.assertEqual(grouped["child_count"], 5)
        self.assertEqual(grouped["message"], "当前摘要显示 5 个同来源模型，完整队列还有更多任务。")

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
                patch.object(catalog, "query_archive_model_index", return_value=None, create=True), \
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
                patch.object(catalog, "query_archive_model_index", return_value=None, create=True), \
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

    def test_models_light_payload_uses_sql_page_and_cached_facets(self):
        sql_page = {
            "items": [
                {
                    "model_dir": "MW_2",
                    "title": "模型 B",
                    "source": "global",
                    "author": {"name": "作者 B"},
                    "tags": ["tool"],
                    "stats": {"downloads": 8, "likes": 5, "prints": 3},
                    "collect_ts": 20,
                    "publish_ts": 18,
                    "local_flags": {"favorite": True, "printed": False, "deleted": False},
                }
            ],
            "count": 1,
            "filtered_total": 4,
            "page": 2,
            "page_size": 1,
            "has_more": True,
            "revision": 7,
        }
        facets = {
            "total": 12,
            "tags": ["art", "tool"],
            "source_counts": {"all": 12, "cn": 5, "global": 4, "local": 3},
        }

        with patch.object(catalog, "query_archive_model_index", return_value=sql_page, create=True) as query_index, \
                patch.object(catalog, "load_archive_model_facets", return_value=facets, create=True) as load_facets, \
                patch.object(catalog, "database_json_state_signature", return_value=("flags-v1", "flags-hash")), \
                patch.object(catalog, "load_archive_model_index_unchecked", side_effect=AssertionError("SQL page must not load the full index")), \
                patch.object(catalog, "get_decorated_models", side_effect=AssertionError("SQL page must not build decorated models")), \
                patch.object(catalog, "_apply_subscription_flags", side_effect=lambda items: items) as decorate_page:
            payload = catalog.build_models_light_payload(
                q="tool",
                source="global",
                tag="tool",
                sort_key="downloads",
                page=2,
                page_size=1,
            )

        query_index.assert_called_once_with("tool", "global", "tool", "downloads", 2, 1, 0)
        load_facets.assert_called_once_with(7, ("flags-v1", "flags-hash"))
        decorate_page.assert_called_once()
        self.assertEqual(payload["items"][0]["model_dir"], "MW_2")
        self.assertEqual(payload["filtered_total"], 4)
        self.assertEqual(payload["total"], 12)
        self.assertEqual(payload["tags"], ["art", "tool"])
        self.assertEqual(payload["source_counts"]["global"], 4)
        self.assertEqual(payload["filters"], {"q": "tool", "source": "global", "tag": "tool", "sort": "downloads"})
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

    def test_build_runtime_diagnostics_marks_runtime_engine_disabled_and_read_only(self):
        with patch.dict(os.environ, {"MAKERHUB_RUNTIME_ENGINE": "runtime"}), \
                patch.object(runtime_diagnostics, "database_status", return_value={"available": False}):
            payload = runtime_diagnostics.build_runtime_diagnostics()

        self.assertFalse(payload["runtime_engine"]["enabled"])
        self.assertFalse(payload["runtime_engine"]["writable"])
        self.assertIn("冻结", payload["runtime_engine"]["reason"])

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
                patch.object(tasks_routes.crawler.manager, "_refresh_batch_tasks", return_value=False), \
                patch.object(tasks_routes.task_state_store, "load_archive_queue", return_value=repair_payload["queue"]), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(tasks_routes.repair_archive_queue(request))

        require_auth.assert_called_once_with(request)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["summary"]["requeued"], 1)

    def test_repair_archive_queue_route_refreshes_batch_parent_after_repair(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        repair_payload = {
            "summary": {
                "examined": 3,
                "requeued": 0,
                "failed": 0,
                "finalized": 2,
                "skipped": 1,
                "errors": [],
            },
            "queue": {
                "running_count": 1,
                "queued_count": 0,
                "failed_count": 0,
                "active": [
                    {
                        "id": "batch-1",
                        "mode": "author_upload",
                        "status": "waiting_children",
                    }
                ],
                "queued": [],
                "recent_failures": [],
            },
        }
        refreshed_queue = {
            "running_count": 0,
            "queued_count": 0,
            "failed_count": 0,
            "active": [],
            "queued": [],
            "recent_failures": [],
        }

        with patch.object(tasks_routes, "_require_session_auth"), \
                patch.object(tasks_routes.task_state_store, "repair_archive_queue", return_value=repair_payload), \
                patch.object(tasks_routes.crawler.manager, "_refresh_batch_tasks", return_value=True) as refresh_batches, \
                patch.object(tasks_routes.task_state_store, "load_archive_queue", return_value=refreshed_queue), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(tasks_routes.repair_archive_queue(request))

        refresh_batches.assert_called_once_with()
        self.assertEqual(payload["archive_queue"], refreshed_queue)

    def test_verified_missing_3mf_route_queues_only_current_verification_item_in_background(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        health = {
            "platform": "global",
            "three_mf_gate": "verification_required",
            "three_mf_detail": "MakerWorld 需要验证，前往官网任意下载一个模型。",
            "model_url": "https://makerworld.com/zh/models/2193050",
            "model_id": "2193050",
            "instance_id": "profile-1",
        }

        with patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf") as retry_mock, \
                patch.object(tasks_routes, "get_account_health", return_value=health), \
                patch.object(tasks_routes, "mark_account_checking", return_value={"status": "unknown", "three_mf_gate": "unknown"}) as mark_account_checking_mock, \
                patch.object(tasks_routes, "_submit_background_task", return_value=True) as background_mock, \
                patch.object(tasks_routes, "append_business_log") as log_mock:
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        require_auth.assert_called_once_with(request)
        retry_mock.assert_not_called()
        mark_account_checking_mock.assert_called_once_with(
            "global",
            source="manual_verification",
            detail="正在通过指纹浏览器验证 3MF 下载权限。",
        )
        background_mock.assert_called_once_with(
            retry_mock,
            platform="global",
            primary={
                "model_url": "https://makerworld.com/zh/models/2193050",
                "model_id": "2193050",
                "instance_id": "profile-1",
                "source": "global",
                "status": "verification_required",
                "message": "MakerWorld 需要验证，前往官网任意下载一个模型。",
            },
            retry_all=False,
        )
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["accepted_count"], 0)
        self.assertEqual(payload["account_health"]["status"], "unknown")
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
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf", return_value=retry_payload), \
                patch.object(tasks_routes, "get_account_health", return_value={}), \
                patch.object(tasks_routes, "mark_account_checking", return_value={"status": "unknown"}) as mark_account_checking_mock, \
                patch.object(tasks_routes, "_submit_background_task", return_value=True), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        mark_account_checking_mock.assert_called_once_with(
            "global",
            source="manual_verification",
            detail="正在通过指纹浏览器验证 3MF 下载权限。",
        )
        self.assertEqual(payload["account_health"]["status"], "unknown")

    def test_verified_missing_3mf_route_marks_account_ok_before_background_retry(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))
        call_order = []

        def _mark(platform, **_kwargs):
            call_order.append(f"mark:{platform}")
            return {"status": "ok"}

        def _background(_func, **kwargs):
            call_order.append(f"background:{kwargs['platform']}")
            return True

        with patch.object(tasks_routes, "_require_session_auth"), \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf"), \
                patch.object(tasks_routes, "get_account_health", return_value={}), \
                patch.object(tasks_routes, "mark_account_checking", side_effect=_mark), \
                patch.object(tasks_routes, "_submit_background_task", side_effect=_background), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="global"),
                    request,
                )
            )

        self.assertEqual(call_order, ["mark:global", "background:global"])
        self.assertEqual(payload["account_health"]["status"], "ok")

    def test_verified_missing_3mf_route_returns_after_marking_account_ok(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

        with patch.object(tasks_routes, "_require_session_auth"), \
                patch.object(tasks_routes.crawler.manager, "retry_verification_missing_3mf") as retry_mock, \
                patch.object(tasks_routes, "get_account_health", return_value={}), \
                patch.object(tasks_routes, "mark_account_checking", return_value={"status": "unknown", "three_mf_gate": "unknown"}) as mark_account_checking_mock, \
                patch.object(tasks_routes, "_submit_background_task", return_value=True) as background_mock, \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="cn"),
                    request,
                )
            )

        mark_account_checking_mock.assert_called_once()
        retry_mock.assert_not_called()
        background_mock.assert_called_once()
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["queued_count"], 0)
        self.assertEqual(payload["account_health"]["three_mf_gate"], "unknown")

    def test_verified_missing_3mf_route_uses_legacy_retry_when_runtime_env_is_truthy(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

        with patch.dict(os.environ, {"MAKERHUB_RUNTIME_ENGINE": "1"}), \
                patch.object(tasks_routes, "_require_session_auth") as require_auth, \
                patch("app.api.runtime_routes.runtime_engine.submit_run") as submit_runtime_run, \
                patch.object(tasks_routes, "get_account_health", return_value={}), \
                patch.object(tasks_routes, "_submit_background_task", return_value=True) as background_retry, \
                patch.object(tasks_routes, "mark_account_checking", return_value={"status": "unknown"}), \
                patch.object(tasks_routes, "append_business_log"):
            payload = asyncio.run(
                tasks_routes.retry_verified_missing_3mf(
                    tasks_routes.Missing3mfVerificationRetryRequest(platform="cn"),
                    request,
                )
            )

        require_auth.assert_called_once_with(request)
        submit_runtime_run.assert_not_called()
        background_retry.assert_called_once_with(
            tasks_routes.crawler.manager.retry_verification_missing_3mf,
            platform="cn",
            primary=None,
            retry_all=False,
        )
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["account_health"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
