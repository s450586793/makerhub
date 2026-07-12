import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.security import hash_api_token
from app.core import database
from app.core import database_json_state
from app.core.store import ConfigConflictError, JsonStore
from app.core.database import JsonStateConflict
from app.schemas.models import ApiTokenRecord, AppConfig, CookiePair
from app.services import (
    archive_model_index_rebuild,
    archive_repair,
    archive_worker,
    auth,
    catalog,
    local_preview_worker,
    self_update,
    source_health,
    three_mf_quota,
)
from app.services import business_logs
from app.services import state_events
from app.services import subscriptions
from app.services.task_state import TaskStateStore


class JsonStateDatabaseRoutingTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.patches = [
            patch("app.core.store.load_database_json_state", side_effect=lambda key, default: dict(self.state.get(key) or default)),
            patch("app.core.store.save_database_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.task_state.load_database_json_state", side_effect=lambda key, default: dict(self.state.get(key) or default)),
            patch("app.services.task_state.save_database_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.subscriptions.load_database_json_state", side_effect=lambda key, default: dict(self.state.get(key) or default)),
            patch("app.services.subscriptions.save_database_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.source_library.load_database_json_state", side_effect=lambda key, default: dict(self.state.get(key) or default)),
            patch("app.services.source_library.save_database_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.source_library.database_json_state_signature", side_effect=lambda key, default=None: ("", str(self.state.get(key) or default or {}))),
            patch("app.api.config.load_database_json_state", side_effect=lambda key, default: dict(self.state.get(key) or default)),
            patch("app.api.config.save_database_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.business_logs.database_configured", return_value=True),
            patch("app.services.business_logs.database_driver_available", return_value=True),
            patch("app.core.database_json_state.database_configured", return_value=True),
            patch("app.core.database_json_state.database_driver_available", return_value=True),
            patch("app.core.database_json_state.load_json_state", side_effect=lambda key: self.state.get(key)),
            patch("app.core.database_json_state.save_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_json_store_persists_cookie_and_token_hash_without_plaintext(self):
        raw_token = "mht_database_token"
        config = AppConfig()
        config.cookies = [CookiePair(platform="cn", cookie="token=makerworld")]
        config.api_tokens = [
            ApiTokenRecord(
                id="token-1",
                name="iPhone",
                token_prefix=raw_token[:12],
                token_hash=hash_api_token(raw_token),
                token_value=raw_token,
                permissions=["mobile_import"],
                created_at="2026-05-22T10:00:00+08:00",
            )
        ]
        store = JsonStore()
        store.save(config)
        loaded = store.load()

        self.assertEqual(self.state["app_config"]["cookies"][0]["cookie"], "token=makerworld")
        self.assertNotIn("token_value", self.state["app_config"]["api_tokens"][0])
        self.assertEqual(self.state["app_config"]["api_tokens"][0]["token_hash"], hash_api_token(raw_token))
        self.assertEqual(loaded.cookies[0].cookie, "token=makerworld")
        self.assertFalse(loaded.api_tokens[0].token_value)
        self.assertEqual(loaded.api_tokens[0].token_hash, hash_api_token(raw_token))

    def test_json_store_does_not_backfill_cookie_from_legacy_file_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            legacy_config = AppConfig()
            legacy_config.cookies = [
                CookiePair(
                    platform="cn",
                    cookie="token=legacy",
                    display_name="艾斯",
                    account_id="2024907479",
                ),
                CookiePair(platform="global"),
            ]
            config_path.write_text(legacy_config.model_dump_json(), encoding="utf-8")
            self.state["app_config"] = AppConfig().model_dump()

            with patch("app.core.store.CONFIG_PATH", config_path):
                store = JsonStore(config_path)
                loaded = store.load()

        self.assertEqual(loaded.cookies[0].cookie, "")
        self.assertEqual(loaded.cookies[0].display_name, "")
        self.assertEqual(self.state["app_config"]["cookies"][0]["cookie"], "")

    def test_json_store_keeps_database_cookies_as_runtime_source_of_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            legacy_config = AppConfig()
            legacy_config.cookies = [
                CookiePair(platform="cn", cookie="token=legacy-cn"),
                CookiePair(platform="global", cookie="token=legacy-global"),
            ]
            config_path.write_text(legacy_config.model_dump_json(), encoding="utf-8")
            config = AppConfig()
            config.cookies = [
                CookiePair(platform="cn", cookie="token=db-cn"),
                CookiePair(platform="global"),
            ]
            self.state["app_config"] = config.model_dump()

            with patch("app.core.store.CONFIG_PATH", config_path):
                store = JsonStore(config_path)
                loaded = store.load()

        cookies = {item.platform: item.cookie for item in loaded.cookies}
        self.assertEqual(cookies["cn"], "token=db-cn")
        self.assertEqual(cookies["global"], "")
        db_cookies = {item["platform"]: item["cookie"] for item in self.state["app_config"]["cookies"]}
        self.assertEqual(db_cookies["cn"], "token=db-cn")
        self.assertEqual(db_cookies["global"], "")

    def test_task_state_stores_subscription_model_lists_in_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            subscriptions_path = Path(tmp) / "subscriptions_state.json"
            payload = {
                "items": [
                    {
                        "id": "sub-1",
                        "current_items": [
                            {
                                "model_id": "MW_1",
                                "url": "https://makerworld.com.cn/model/MW_1",
                                "task_key": "model:MW_1",
                            }
                        ],
                        "tracked_items": [
                            {
                                "model_id": "MW_2",
                                "url": "https://makerworld.com.cn/model/MW_2",
                                "task_key": "model:MW_2",
                            }
                        ],
                    }
                ]
            }
            with patch("app.services.task_state.SUBSCRIPTIONS_STATE_PATH", subscriptions_path), \
                    patch.dict("app.services.task_state._JSON_STATE_KEYS", {subscriptions_path.resolve(): "subscriptions_state"}, clear=False):
                store = TaskStateStore()
                saved = store.save_subscriptions_state(payload)
                loaded = store.load_subscriptions_state()

            self.assertEqual(saved["count"], 1)
            self.assertEqual(self.state["subscriptions_state"]["items"][0]["current_items"][0]["model_id"], "MW_1")
            self.assertEqual(loaded["items"][0]["tracked_items"][0]["url"], "https://makerworld.com.cn/model/MW_2")

    def test_cookie_source_inventory_stores_discovered_source_lists_in_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_path = Path(tmp) / "cookie_source_inventory.json"
            with patch("app.services.subscriptions.COOKIE_SOURCE_INVENTORY_PATH", inventory_path):
                subscriptions._patch_cookie_source_inventory_state(
                    "global",
                    account={"uid": "2073587493", "handle": "s450586793"},
                    followed_authors=[{"title": "Author", "url": "https://makerworld.com/zh/@author/upload"}],
                    followed_collections=[{"title": "关注收藏夹", "url": "https://makerworld.com/zh/collections/1"}],
                    imported_sources=[
                        {
                            "subscription_id": "sub-1",
                            "url": "https://makerworld.com/zh/@s450586793/collections/models",
                            "mode": "collection_models",
                            "source_kind": "default_favorites",
                        }
                    ],
                    source_urls=["https://makerworld.com/zh/@s450586793/collections/models"],
                    last_status="success",
                )
                loaded = subscriptions._read_cookie_source_inventory_state()

            self.assertEqual(self.state["cookie_source_inventory"]["platforms"]["global"]["account"]["uid"], "2073587493")
            self.assertEqual(loaded["platforms"]["global"]["followed_authors"][0]["title"], "Author")
            self.assertEqual(loaded["platforms"]["global"]["imported_sources"][0]["subscription_id"], "sub-1")

    def test_business_logs_store_structured_entries_in_database(self):
        calls = []

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                return self

            def fetchall(self):
                return []

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("app.services.business_logs.initialize_database", return_value=True), \
                patch("app.services.business_logs.database_connection", return_value=FakeContext()):
            ok = business_logs.append_database_log_entry(
                "business.log",
                {
                    "time": "2026-05-22T10:00:00+08:00",
                    "level": "info",
                    "category": "settings",
                    "event": "saved",
                    "message": "ok",
                    "token": "***",
                },
                raw='{"event":"saved"}',
            )

        self.assertTrue(ok)
        insert_call = calls[-1]
        self.assertIn("INSERT INTO makerhub_logs", insert_call[0])
        self.assertEqual(insert_call[1]["file_name"], "business.log")
        self.assertEqual(insert_call[1]["event"], "saved")
        self.assertTrue(insert_call[1]["raw_hash"])

    def test_long_job_and_update_statuses_route_through_database_state(self):
        archive_repair.write_archive_repair_status(
            {"running": True, "run_id": "repair-1", "pid": 0, "progress": {"done": 2}}
        )
        repair_status = archive_repair.read_archive_repair_status()
        archive_model_index_rebuild.write_archive_model_index_rebuild_status(
            {"phase": "database_index_rebuild", "auto": True}
        )
        rebuild_status = archive_model_index_rebuild.read_archive_model_index_rebuild_status()
        self_update._write_update_state({"phase": "installing", "target_version": "9.9.9"})
        update_status = self_update._read_update_state()

        self.assertEqual(self.state["archive_repair_status"]["run_id"], "repair-1")
        self.assertEqual(repair_status["progress"]["done"], 2)
        self.assertEqual(self.state["archive_model_index_rebuild_status"]["phase"], "database_index_rebuild")
        self.assertTrue(rebuild_status["auto"])
        self.assertFalse(rebuild_status["force"])
        self.assertEqual(self.state["system_update"]["target_version"], "9.9.9")
        self.assertEqual(update_status["phase"], "installing")

    def test_auth_sessions_route_through_database_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = auth.AuthManager(store=JsonStore(), sessions_path=Path(tmp) / "ignored_sessions.json")
            session = manager.create_session("admin")

        self.assertEqual(self.state["auth_sessions"]["items"][0]["id"], session["id"])

    def test_three_mf_quota_and_limit_guard_route_through_database_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "three_mf_daily_quota.lock"
            quota_result = three_mf_quota.reserve_three_mf_download_slot(
                source="cn",
                limit=2,
                model_id="MW_1",
                lock_path=lock_path,
            )
            archive_worker._write_three_mf_limit_guard(
                {
                    "active": True,
                    "limited_until": "2099-01-02T00:00:00+08:00",
                    "message": "limit",
                    "model_url": "https://makerworld.com.cn/zh/models/1",
                }
            )
            health_guard = source_health._read_limit_guard()
            source_health._write_limit_guard({"active": False, "limited_until": "", "message": ""})

        self.assertTrue(quota_result["allowed"])
        self.assertEqual(self.state["three_mf_daily_quota"]["items"]["cn"]["used"], 1)
        self.assertTrue(health_guard["active"])
        self.assertFalse(self.state["three_mf_limit_guard"]["active"])

    def test_archive_and_preview_markers_route_through_database_state(self):
        token = catalog._write_archive_snapshot_marker("unit_test")
        loaded_token = catalog._read_archive_snapshot_marker()
        local_preview_worker.mark_local_preview_queue_updated("unit_test")
        preview_version = local_preview_worker.local_preview_queue_marker_mtime()

        self.assertEqual(self.state["archive_snapshot_marker"]["token"], token)
        self.assertEqual(loaded_token, token)
        self.assertEqual(self.state["local_preview_queue_marker"]["reason"], "unit_test")
        self.assertGreater(preview_version, 0)


class DatabaseStatusTest(unittest.TestCase):
    def test_database_status_reports_unconfigured_without_file_fallback(self):
        with patch.object(database, "database_url", return_value=""):
            status = database.database_status()

        self.assertFalse(status["configured"])
        self.assertFalse(status["available"])
        self.assertEqual(status["expected_schema_version"], database.DATABASE_SCHEMA_VERSION)


class JsonStoreAtomicUpdateTest(unittest.TestCase):
    def test_sparse_legacy_config_can_save_unrelated_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                '{"user":{"username":"admin","display_name":"旧配置"}}',
                encoding="utf-8",
            )
            store = JsonStore(path)
            config = store.load()

            config.proxy.enabled = True
            saved = store.save(config)

            self.assertEqual(saved.user.display_name, "旧配置")
            self.assertTrue(saved.proxy.enabled)

    def test_stale_saves_preserve_changes_to_unrelated_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            user_update = store.load()
            proxy_update = store.load()

            user_update.user.display_name = "并发用户"
            store.save(user_update)
            proxy_update.proxy.enabled = True
            proxy_update.proxy.http_proxy = "http://127.0.0.1:7890"
            store.save(proxy_update)

            saved = store.load()
            self.assertEqual(saved.user.display_name, "并发用户")
            self.assertTrue(saved.proxy.enabled)
            self.assertEqual(saved.proxy.http_proxy, "http://127.0.0.1:7890")

    def test_stale_save_detects_same_field_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            first = store.load()
            second = store.load()

            first.user.display_name = "第一个值"
            store.save(first)
            second.user.display_name = "第二个值"

            with self.assertRaises(ConfigConflictError):
                store.save(second)

            self.assertEqual(store.load().user.display_name, "第一个值")

    def test_update_mutates_latest_config_and_returns_saved_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")

            def mutate(config):
                config.proxy.enabled = True

            saved = store.update(mutate)

            self.assertTrue(saved.proxy.enabled)
            self.assertTrue(store.load().proxy.enabled)

    def test_update_validation_failure_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp) / "config.json")
            before = store.path.read_text(encoding="utf-8")

            def mutate(_config):
                return {"user": {"theme_preference": "invalid"}}

            with self.assertRaises(ValueError):
                store.update(mutate)

            self.assertEqual(store.path.read_text(encoding="utf-8"), before)

    def test_database_update_forwards_expected_revision_before_mutator(self):
        state = AppConfig().model_dump()
        revision = 7
        mutator_calls = []

        def fake_update(_key, _default, mutator, *, expected_revision=None):
            if expected_revision != revision:
                raise JsonStateConflict("stale")
            updated = mutator(dict(state))
            return updated, revision + 1

        with patch("app.core.store.database_configured", return_value=True), \
                patch("app.core.store.update_database_json_state", side_effect=fake_update):
            store = JsonStore()
            with self.assertRaises(JsonStateConflict):
                store.update(
                    lambda config: mutator_calls.append(True) or config,
                    expected_revision=revision - 1,
                )
            saved = store.update(
                lambda config: mutator_calls.append(True) or config,
                expected_revision=revision,
            )

        self.assertIsInstance(saved, AppConfig)
        self.assertEqual(mutator_calls, [True])

    def test_sparse_database_payload_uses_same_merge_rules_as_file(self):
        state = {"app_config": {"user": {"username": "admin", "display_name": "旧数据库配置"}}}
        with patch("app.core.store.load_database_json_state", side_effect=lambda key, default: dict(state.get(key) or default)), \
                patch("app.core.store.save_database_json_state", side_effect=lambda key, value: state.__setitem__(key, value) or value), \
                patch("app.core.store.database_configured", return_value=False):
            store = JsonStore()
            config = store.load()
            config.proxy.enabled = True
            saved = store.save(config)

        self.assertEqual(saved.user.display_name, "旧数据库配置")
        self.assertTrue(saved.proxy.enabled)


class DatabaseAtomicJsonStateTest(unittest.TestCase):
    def test_database_object_update_rejects_invalid_payload_before_sql_update(self):
        calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "SELECT value, revision" in sql:
                    return FakeResult({"value": {"count": 1}, "revision": 2})
                if "UPDATE makerhub_json_state" in sql:
                    raise AssertionError("invalid payload must fail before UPDATE")
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(database_json_state, "require_database_json_state", return_value=None), \
                patch.object(database, "initialize_database", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            with self.assertRaises(ValueError):
                database_json_state.update_database_json_state(
                    "counter",
                    {"count": 0},
                    lambda _value: ["invalid"],
                )

        self.assertFalse(any("UPDATE makerhub_json_state" in sql for sql, _params in calls))

    def test_update_json_state_locks_row_and_increments_revision_once(self):
        calls = []
        state = {"value": {"count": 1}, "revision": 4}
        mutator_calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "SELECT value, revision" in sql:
                    return FakeResult(dict(state))
                if "UPDATE makerhub_json_state" in sql:
                    state["value"] = params[0].obj if hasattr(params[0], "obj") else params[0]
                    state["revision"] += 1
                    return FakeResult({"revision": state["revision"]})
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        def mutate(value):
            mutator_calls.append(True)
            value["count"] += 1
            return value

        with patch.object(database, "initialize_database", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            updated, revision = database.update_json_state("counter", {"count": 0}, mutate, expected_revision=4)

        self.assertEqual(updated, {"count": 2})
        self.assertEqual(revision, 5)
        self.assertEqual(len(mutator_calls), 1)
        self.assertTrue(any("FOR UPDATE" in sql for sql, _params in calls))

    def test_update_json_state_rejects_stale_revision_before_mutator(self):
        mutator_calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                if "SELECT value, revision" in sql:
                    return FakeResult({"value": {"count": 1}, "revision": 5})
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(database, "initialize_database", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            with self.assertRaises(database.JsonStateConflict):
                database.update_json_state(
                    "counter",
                    {"count": 0},
                    lambda value: mutator_calls.append(True) or value,
                    expected_revision=4,
                )

        self.assertEqual(mutator_calls, [])


class DatabaseInitializationGuardTest(unittest.TestCase):
    def test_initialization_guard_is_scoped_to_pid_and_database_url(self):
        schema_connections = []
        current_url = "postgresql://example/one"

        class FakeContext:
            def __enter__(self):
                schema_connections.append(current_url)
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql, params=None):
                return self

        database._reset_database_initialization_for_tests()
        with patch.object(database, "database_configured", return_value=True), \
                patch.object(database, "database_url", side_effect=lambda: current_url), \
                patch.object(database.os, "getpid", return_value=123), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            self.assertTrue(database.initialize_database())
            self.assertTrue(database.initialize_database())
            current_url = "postgresql://example/two"
            self.assertTrue(database.initialize_database())

        self.assertEqual(
            schema_connections,
            ["postgresql://example/one", "postgresql://example/two"],
        )

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
                if "RETURNING id" in sql:
                    return FakeResult(
                        {
                            "id": 1,
                            "type": "state.changed",
                            "scope": "archive_queue",
                            "payload": {"queued_count": 1},
                            "created_at": "now",
                        }
                    )
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
            database.append_state_event("state.changed", "archive_queue", {"queued_count": 1})

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

    def test_json_state_array_summary_reads_count_and_limited_items(self):
        calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row or {}

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "jsonb_array_length" in sql and "generate_series" in sql:
                    return FakeResult({"count": 2000, "items": [{"model_id": "1"}]})
                if "jsonb_array_elements" in sql:
                    raise AssertionError("compact array summary must not expand the full JSON array")
                if "SELECT value FROM makerhub_json_state" in sql:
                    raise AssertionError("compact array summary must not load full JSON value")
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        database._reset_database_initialization_for_tests()
        with patch.object(database, "database_configured", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            summary = database.load_json_state_array_summary("missing_3mf", "items", limit=5)

        self.assertEqual(summary, {"items": [{"model_id": "1"}], "count": 2000})
        self.assertTrue(any(params == ("items", 5, "missing_3mf") for _sql, params in calls))

    def test_load_json_states_reads_multiple_keys_in_one_query(self):
        calls = []

        class FakeResult:
            def fetchall(self):
                return [
                    {"key": "runtime_snapshots", "value": {"dashboard": {"active": 1}}},
                    {"key": "account_health", "value": {"cn": {"status": "ok"}}},
                ]

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(database, "initialize_database", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            payload = database.load_json_states(
                ["runtime_snapshots", "account_health", "missing_key", "runtime_snapshots"]
            )

        self.assertEqual(payload["runtime_snapshots"]["dashboard"]["active"], 1)
        self.assertEqual(payload["account_health"]["cn"]["status"], "ok")
        self.assertIsNone(payload["missing_key"])
        self.assertEqual(len(calls), 1)
        self.assertIn("WHERE key = ANY", calls[0][0])
        self.assertEqual(calls[0][1], (["runtime_snapshots", "account_health", "missing_key"],))

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


class StateEventsTest(unittest.TestCase):
    def test_normalize_state_event_converts_created_at_and_payload(self):
        row = {
            "id": 7,
            "type": "archive.completed",
            "scope": "archive_queue",
            "payload": {"count": 1, "nested": {"value": object()}},
            "created_at": "2026-05-26T10:00:00+08:00",
        }

        event = state_events.normalize_state_event(row)

        self.assertEqual(event["id"], 7)
        self.assertEqual(event["type"], "archive.completed")
        self.assertEqual(event["scope"], "archive_queue")
        self.assertEqual(event["created_at"], "2026-05-26T10:00:00+08:00")
        self.assertIsInstance(event["payload"]["nested"]["value"], str)

    def test_append_state_event_inserts_event_and_notifies(self):
        calls = []

        class FakeResult:
            def __init__(self, row=None):
                self.row = row or {}

            def fetchone(self):
                return self.row

        class FakeConnection:
            def execute(self, sql, params=None):
                calls.append((sql, params))
                if "RETURNING id" in sql:
                    return FakeResult(
                        {
                            "id": 12,
                            "type": "state.changed",
                            "scope": "archive_queue",
                            "payload": {"queued_count": 1},
                            "created_at": "now",
                        }
                    )
                return FakeResult()

        class FakeContext:
            def __enter__(self):
                return FakeConnection()

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch.object(database, "initialize_database", return_value=True), \
                patch.object(database, "database_connection", return_value=FakeContext()):
            event = database.append_state_event("state.changed", "archive_queue", {"queued_count": 1})

        self.assertEqual(event["id"], 12)
        self.assertIn("INSERT INTO makerhub_state_events", calls[0][0])
        self.assertIn("pg_notify", calls[1][0])
        self.assertEqual(calls[1][1][0], database.DATABASE_STATE_EVENT_CHANNEL)


if __name__ == "__main__":
    unittest.main()
