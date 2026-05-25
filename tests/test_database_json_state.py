import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.security import hash_api_token
from app.core import database
from app.core.store import JsonStore
from app.schemas.models import ApiTokenRecord, AppConfig, CookiePair
from app.services import (
    archive_profile_backfill,
    archive_repair,
    archive_worker,
    auth,
    catalog,
    database_migration,
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
            patch("app.services.database_migration.database_configured", return_value=True),
            patch("app.services.database_migration.database_driver_available", return_value=True),
            patch("app.services.database_migration.load_json_state", side_effect=lambda key: self.state.get(key)),
            patch("app.services.database_migration.save_json_state", side_effect=lambda key, value: self.state.__setitem__(key, value) or value),
            patch("app.services.database_migration.append_business_log"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_json_store_migrates_config_with_cookie_and_tokens(self):
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
        self.assertEqual(self.state["app_config"]["api_tokens"][0]["token_value"], raw_token)
        self.assertEqual(loaded.cookies[0].cookie, "token=makerworld")
        self.assertEqual(loaded.api_tokens[0].token_value, raw_token)

    def test_json_store_backfills_cookie_from_legacy_file_when_database_has_empty_defaults(self):
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

        self.assertEqual(loaded.cookies[0].cookie, "token=legacy")
        self.assertEqual(loaded.cookies[0].display_name, "艾斯")
        self.assertEqual(self.state["app_config"]["cookies"][0]["cookie"], "token=legacy")

    def test_json_store_backfills_only_missing_cookie_platforms_from_legacy_file(self):
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
        self.assertEqual(cookies["global"], "token=legacy-global")
        db_cookies = {item["platform"]: item["cookie"] for item in self.state["app_config"]["cookies"]}
        self.assertEqual(db_cookies["cn"], "token=db-cn")
        self.assertEqual(db_cookies["global"], "token=legacy-global")

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
        archive_profile_backfill.write_profile_backfill_status(
            {"phase": "database_migration", "auto_database_migration": True}
        )
        backfill_status = archive_profile_backfill.read_profile_backfill_status()
        self_update._write_update_state({"phase": "installing", "target_version": "9.9.9"})
        update_status = self_update._read_update_state()

        self.assertEqual(self.state["archive_repair_status"]["run_id"], "repair-1")
        self.assertEqual(repair_status["progress"]["done"], 2)
        self.assertEqual(self.state["archive_profile_backfill_status"]["phase"], "database_migration")
        self.assertTrue(backfill_status["auto_database_migration"])
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

    def test_database_migration_preserves_legacy_text_marker_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_marker_path = Path(tmp) / "archive_snapshot.marker"
            preview_marker_path = Path(tmp) / "local_preview_queue.marker"
            secret_path = Path(tmp) / "bambu_studio_download_secret"
            archive_marker_path.write_text("legacy-archive-token", encoding="utf-8")
            preview_marker_path.write_text("legacy-preview-token", encoding="utf-8")
            with patch.object(
                database_migration,
                "JSON_STATE_FILE_MIGRATIONS",
                (
                    ("archive_snapshot_marker", archive_marker_path, {}),
                    ("local_preview_queue_marker", preview_marker_path, {}),
                ),
            ), patch.object(database_migration, "BAMBU_STUDIO_SECRET_PATH", secret_path):
                result = database_migration.migrate_json_files_to_database(force=True)

        self.assertEqual(result["updated"], 3)
        self.assertEqual(self.state["archive_snapshot_marker"]["token"], "legacy-archive-token")
        self.assertEqual(self.state["local_preview_queue_marker"]["token"], "legacy-preview-token")

    def test_database_migration_reads_legacy_config_path_under_parent_config_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_root = Path(tmp) / "config"
            new_config_dir = config_root / "config"
            legacy_config_path = config_root / "config.json"
            new_config_path = new_config_dir / "config.json"
            legacy_config_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_config_path.write_text(
                AppConfig(cookies=[CookiePair(platform="cn", cookie="token=legacy-parent")]).model_dump_json(),
                encoding="utf-8",
            )
            with patch.object(database_migration, "CONFIG_PATH", new_config_path), \
                    patch.object(database_migration, "CONFIG_DIR", new_config_dir), \
                    patch.object(database_migration, "LEGACY_CONFIG_PATH", legacy_config_path), \
                    patch.object(
                        database_migration,
                        "JSON_STATE_FILE_MIGRATIONS",
                        (("app_config", new_config_path, {}),),
                    ):
                result = database_migration.migrate_json_files_to_database(force=True)

        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.state["app_config"]["cookies"][0]["cookie"], "token=legacy-parent")
        self.assertEqual(result["items"][0]["path"], legacy_config_path.as_posix())

    def test_database_migration_backfills_empty_model_flags_from_legacy_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "config" / "state"
            legacy_state_root = Path(tmp) / "state"
            model_flags_path = state_root / "model_flags.json"
            legacy_flags_path = legacy_state_root / "model_flags.json"
            legacy_flags_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_flags_path.write_text(
                '{"favorites": [], "printed": [], "deleted": ["remote/model-1"]}',
                encoding="utf-8",
            )
            self.state["model_flags"] = {"favorites": [], "printed": [], "deleted": []}

            with patch.object(database_migration, "MODEL_FLAGS_PATH", model_flags_path), \
                    patch.object(database_migration, "LEGACY_STATE_DIR", legacy_state_root), \
                    patch.object(
                        database_migration,
                        "JSON_STATE_FILE_MIGRATIONS",
                        (("model_flags", model_flags_path, {"favorites": [], "printed": [], "deleted": []}),),
                    ):
                result = database_migration.migrate_json_files_to_database(force=False)

        self.assertEqual(self.state["model_flags"]["deleted"], ["remote/model-1"])
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["items"][0]["status"], "backfilled")
        self.assertEqual(result["items"][0]["path"], legacy_flags_path.as_posix())

    def test_database_migration_keeps_existing_model_flags_when_database_has_user_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "config" / "state"
            legacy_state_root = Path(tmp) / "state"
            model_flags_path = state_root / "model_flags.json"
            legacy_flags_path = legacy_state_root / "model_flags.json"
            legacy_flags_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_flags_path.write_text(
                '{"favorites": [], "printed": [], "deleted": ["legacy/model"]}',
                encoding="utf-8",
            )
            self.state["model_flags"] = {"favorites": [], "printed": [], "deleted": ["db/model"]}

            with patch.object(database_migration, "MODEL_FLAGS_PATH", model_flags_path), \
                    patch.object(database_migration, "LEGACY_STATE_DIR", legacy_state_root), \
                    patch.object(
                        database_migration,
                        "JSON_STATE_FILE_MIGRATIONS",
                        (("model_flags", model_flags_path, {"favorites": [], "printed": [], "deleted": []}),),
                    ):
                result = database_migration.migrate_json_files_to_database(force=False)

        self.assertEqual(self.state["model_flags"]["deleted"], ["db/model"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["items"][0]["status"], "exists")


class DatabaseStatusTest(unittest.TestCase):
    def test_database_status_reports_unconfigured_without_file_fallback(self):
        with patch.object(database, "database_url", return_value=""):
            status = database.database_status()

        self.assertFalse(status["configured"])
        self.assertFalse(status["available"])
        self.assertEqual(status["expected_schema_version"], database.DATABASE_SCHEMA_VERSION)


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
