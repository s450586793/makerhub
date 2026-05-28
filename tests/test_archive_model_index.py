import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api import config as config_api
from app.services import archive_model_index, archive_profile_backfill, catalog
from tests.test_helpers import InMemoryDatabaseState


def _unwrap_jsonb(value):
    return getattr(value, "obj", value)


class FakeCursor:
    def __init__(self, rowcount=0, rows=None, row=None):
        self.rowcount = rowcount
        self._rows = rows or []
        self._row = row

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self):
        self.rows = {}
        self.metadata = {}

    def execute(self, sql, params=None):
        normalized = " ".join(str(sql).split()).upper()
        if normalized.startswith("INSERT INTO ARCHIVE_MODEL_INDEX"):
            payload = params or {}
            self.rows[payload["model_dir"]] = {
                "short_key": str(payload.get("short_key") or ""),
                "model": dict(_unwrap_jsonb(payload["model_json"])),
                "meta_mtime_ns": int(payload.get("meta_mtime_ns") or 0),
                "meta_size": int(payload.get("meta_size") or 0),
            }
            return FakeCursor(rowcount=1)
        if normalized.startswith("SELECT SHORT_KEY FROM ARCHIVE_MODEL_INDEX WHERE MODEL_DIR"):
            key = params[0] if params else ""
            row = self.rows.get(key)
            return FakeCursor(row={"short_key": row["short_key"]} if row else None)
        if normalized.startswith("SELECT SHORT_KEY FROM ARCHIVE_MODEL_INDEX WHERE SHORT_KEY LIKE"):
            prefix = str(params[0] if params else "").rstrip("%")
            rows = [
                {"short_key": row["short_key"]}
                for row in self.rows.values()
                if str(row.get("short_key") or "").startswith(prefix)
            ]
            return FakeCursor(rows=rows)
        if normalized.startswith("SELECT MODEL_DIR FROM ARCHIVE_MODEL_INDEX WHERE SHORT_KEY"):
            short_key = params[0] if params else ""
            for model_dir, row in self.rows.items():
                if row.get("short_key") == short_key:
                    return FakeCursor(row={"model_dir": model_dir})
            return FakeCursor(row=None)
        if normalized.startswith("SELECT MODEL_DIR, MODEL_JSON"):
            rows = []
            for key in sorted(self.rows):
                rows.append(
                    {
                        "model_dir": key,
                        "model_json": self.rows[key]["model"],
                        "meta_mtime_ns": self.rows[key]["meta_mtime_ns"],
                        "meta_size": self.rows[key]["meta_size"],
                    }
                )
            return FakeCursor(rows=rows)
        if normalized.startswith("SELECT MODEL_JSON FROM ARCHIVE_MODEL_INDEX"):
            return FakeCursor(rows=[{"model_json": self.rows[key]["model"]} for key in sorted(self.rows)])
        if normalized.startswith("DELETE FROM ARCHIVE_MODEL_INDEX"):
            values = params[0] if params else []
            count = 0
            for value in values:
                if value in self.rows:
                    self.rows.pop(value)
                    count += 1
            return FakeCursor(rowcount=count)
        if normalized.startswith("TRUNCATE TABLE ARCHIVE_MODEL_INDEX"):
            self.rows.clear()
            return FakeCursor()
        if normalized.startswith("SELECT COUNT(*) AS COUNT"):
            return FakeCursor(row={"count": len(self.rows)})
        if normalized.startswith("SELECT VALUE FROM MAKERHUB_METADATA"):
            key = params[0] if params else ""
            value = self.metadata.get(key)
            return FakeCursor(row={"value": value} if value is not None else None)
        if normalized.startswith("INSERT INTO MAKERHUB_METADATA"):
            key, value = params
            self.metadata[key] = _unwrap_jsonb(value)
            return FakeCursor(rowcount=1)
        return FakeCursor()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class FakeDatabaseContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


def _write_meta(root: Path, model_dir: str, title: str, model_id: str = "1", source: str = "local"):
    target = root / model_dir
    target.mkdir(parents=True)
    (target / "meta.json").write_text(
        json.dumps(
            {
                "id": model_id,
                "title": title,
                "source": source,
                "url": f"https://makerworld.com.cn/zh/models/{model_id}" if source == "cn" else "",
                "collectDate": "2026-05-22 12:00",
                "author": {"name": "Ace"},
                "instances": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class ArchiveModelIndexTest(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.patches = [
            patch.object(archive_model_index, "_SCHEMA_READY", False),
            patch.object(archive_model_index, "_DB_RETRY_AFTER", 0.0),
            patch.object(archive_model_index, "database_configured", return_value=True),
            patch.object(archive_model_index, "database_driver_available", return_value=True),
            patch.object(archive_model_index, "initialize_database", return_value=True),
            patch.object(archive_model_index, "database_connection", side_effect=lambda: FakeDatabaseContext(self.connection)),
            patch.object(archive_model_index, "append_business_log"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_database_index_is_used_after_bootstrap_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_Demo", "Demo")
            meta_path = archive_root / "LOCAL_Demo" / "meta.json"
            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                model = catalog._normalize_model(meta_path, include_detail=False)
                self.assertTrue(model)
                archive_model_index.upsert_archive_model_index("LOCAL_Demo", model=model, meta_path=meta_path)
                archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=1)
                loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded[0]["model_dir"], "LOCAL_Demo")
            self.assertEqual(loaded[0]["title"], "Demo")

    def test_upsert_assigns_short_detail_paths_for_remote_and_local_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "CN_Demo", "CN Demo", model_id="1067877", source="cn")
            _write_meta(archive_root, "GLOBAL_Demo", "Global Demo", model_id="1067877", source="global")
            _write_meta(archive_root, "LOCAL_Demo", "Local Demo", model_id="", source="local")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                for model_dir in ["CN_Demo", "GLOBAL_Demo", "LOCAL_Demo"]:
                    meta_path = archive_root / model_dir / "meta.json"
                    model = catalog._normalize_model(meta_path, include_detail=False)
                    self.assertTrue(model)
                    self.assertTrue(archive_model_index.upsert_archive_model_index(model_dir, model=model, meta_path=meta_path))
                archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=3)
                loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

            self.assertIsNotNone(loaded)
            paths = {item["model_dir"]: item["detail_path"] for item in loaded}
            self.assertEqual(paths["CN_Demo"], "/models/mwcn1067877")
            self.assertEqual(paths["GLOBAL_Demo"], "/models/mwg1067877")
            self.assertRegex(paths["LOCAL_Demo"], r"^/models/local\d+$")

    def test_local_short_keys_are_stable_and_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_One", "Local One", model_id="", source="local")
            _write_meta(archive_root, "LOCAL_Two", "Local Two", model_id="", source="local")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                for model_dir in ["LOCAL_One", "LOCAL_Two"]:
                    meta_path = archive_root / model_dir / "meta.json"
                    model = catalog._normalize_model(meta_path, include_detail=False)
                    self.assertTrue(model)
                    self.assertTrue(archive_model_index.upsert_archive_model_index(model_dir, model=model, meta_path=meta_path))
                archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=2)
                first_loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

                first_model = catalog._normalize_model(archive_root / "LOCAL_One" / "meta.json", include_detail=False)
                self.assertTrue(archive_model_index.upsert_archive_model_index("LOCAL_One", model=first_model, meta_path=archive_root / "LOCAL_One" / "meta.json"))
                second_loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

            self.assertIsNotNone(first_loaded)
            self.assertIsNotNone(second_loaded)
            first_paths = {item["model_dir"]: item["detail_path"] for item in first_loaded}
            second_paths = {item["model_dir"]: item["detail_path"] for item in second_loaded}
            self.assertEqual(first_paths["LOCAL_One"], second_paths["LOCAL_One"])
            self.assertEqual(first_paths["LOCAL_One"], "/models/local100001")
            self.assertEqual(first_paths["LOCAL_Two"], "/models/local100002")

    def test_local_short_key_is_assigned_by_index_not_meta_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_2026_Demo", "Local Numbered Name", model_id="", source="local")
            meta_path = archive_root / "LOCAL_2026_Demo" / "meta.json"

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                model = catalog._normalize_model(meta_path, include_detail=False)
                self.assertTrue(model)
                self.assertEqual(model.get("detail_path"), "")

                self.assertTrue(archive_model_index.upsert_archive_model_index("LOCAL_2026_Demo", model=model, meta_path=meta_path))

            self.assertEqual(model.get("detail_path"), "/models/local100001")

    def test_resolve_model_dir_from_short_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "CN_Demo", "CN Demo", model_id="1067877", source="cn")
            _write_meta(archive_root, "LOCAL_Demo", "Local Demo", model_id="", source="local")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                for model_dir in ["CN_Demo", "LOCAL_Demo"]:
                    meta_path = archive_root / model_dir / "meta.json"
                    model = catalog._normalize_model(meta_path, include_detail=False)
                    self.assertTrue(model)
                    self.assertTrue(archive_model_index.upsert_archive_model_index(model_dir, model=model, meta_path=meta_path))

                self.assertEqual(archive_model_index.resolve_model_dir_from_short_key("mwcn1067877"), "CN_Demo")
                self.assertEqual(archive_model_index.resolve_model_dir_from_short_key("local100001"), "LOCAL_Demo")
                self.assertEqual(archive_model_index.resolve_model_dir_from_short_key("CN_Demo"), "")

    def test_duplicate_remote_model_id_does_not_reuse_short_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "CN_One", "CN One", model_id="1067877", source="cn")
            _write_meta(archive_root, "CN_Two", "CN Two", model_id="1067877", source="cn")

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), patch.object(archive_model_index, "ARCHIVE_DIR", archive_root):
                for model_dir in ["CN_One", "CN_Two"]:
                    meta_path = archive_root / model_dir / "meta.json"
                    model = catalog._normalize_model(meta_path, include_detail=False)
                    self.assertTrue(model)
                    self.assertTrue(archive_model_index.upsert_archive_model_index(model_dir, model=model, meta_path=meta_path))
                archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=2)
                loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

            self.assertIsNotNone(loaded)
            paths = {item["model_dir"]: item.get("detail_path", "") for item in loaded}
            self.assertEqual(paths["CN_One"], "/models/mwcn1067877")
            self.assertEqual(paths["CN_Two"], "")

    def test_model_detail_api_resolves_short_key_to_internal_model_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "CN_Demo", "CN Demo", model_id="1067877", source="cn")
            meta_path = archive_root / "CN_Demo" / "meta.json"

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), \
                patch.object(archive_model_index, "ARCHIVE_DIR", archive_root), \
                patch.object(config_api, "get_model_detail", catalog.get_model_detail), \
                InMemoryDatabaseState():
                model = catalog._normalize_model(meta_path, include_detail=False)
                self.assertTrue(model)
                self.assertTrue(archive_model_index.upsert_archive_model_index("CN_Demo", model=model, meta_path=meta_path))

                detail = asyncio.run(config_api.get_model_detail_data("mwcn1067877"))

            self.assertEqual(detail["model_dir"], "CN_Demo")
            self.assertEqual(detail["detail_path"], "/models/mwcn1067877")

    def test_stale_database_index_returns_snapshot_and_queues_worker_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_Demo", "Demo")
            meta_path = archive_root / "LOCAL_Demo" / "meta.json"
            state = {}

            def load_state(_key, default):
                return dict(state or default)

            def save_state(_key, payload):
                state.clear()
                state.update(payload)
                return payload

            with patch.object(catalog, "ARCHIVE_DIR", archive_root), \
                patch.object(archive_model_index, "ARCHIVE_DIR", archive_root), \
                patch.object(archive_model_index, "load_database_json_state", side_effect=load_state), \
                patch.object(archive_model_index, "save_database_json_state", side_effect=save_state), \
                patch.object(archive_model_index, "publish_state_event"):
                model = catalog._normalize_model(meta_path, include_detail=False)
                self.assertTrue(model)
                archive_model_index.upsert_archive_model_index("LOCAL_Demo", model=model, meta_path=meta_path)
                archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=1)
                meta_path.write_text(
                    json.dumps(
                        {
                            "id": "1",
                            "title": "Demo changed",
                            "source": "local",
                            "collectDate": "2026-05-22 12:00",
                            "author": {"name": "Ace"},
                            "instances": [],
                            "staleMarker": "force-signature-change",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                loaded = archive_model_index.load_archive_model_index(archive_root=archive_root)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded[0]["model_dir"], "LOCAL_Demo")
            self.assertEqual(loaded[0]["title"], "Demo")
            self.assertTrue(state["running"])
            self.assertEqual(state["phase"], "database_migration")
            self.assertTrue(state["database_rebuild_requested"])
            self.assertTrue(state["force_database_rebuild"])
            self.assertTrue(state["database_only"])
            database_index = state["last_result"]["database_index"]
            self.assertEqual(database_index["requested_by"], "archive_model_index_stale_rows")
            self.assertEqual(database_index["stale_count"], 1)
            self.assertEqual(database_index["stale_model_dirs"], ["LOCAL_Demo"])

    def test_rebuild_skips_when_marker_exists_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_Demo", "Demo")
            archive_model_index.mark_archive_model_index_bootstrapped(archive_root=archive_root, processed_count=1)

            with patch.object(archive_profile_backfill, "write_profile_backfill_status"), \
                patch.object(archive_profile_backfill, "invalidate_archive_snapshot"), \
                patch.object(archive_profile_backfill, "append_business_log"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root), \
                patch.object(archive_profile_backfill, "ARCHIVE_DIR", archive_root):
                result = archive_profile_backfill.rebuild_archive_model_database_index(
                    archive_root=archive_root,
                    force=False,
                )

            self.assertTrue(result["skipped"])
            self.assertEqual(result["reason"], "数据库索引已迁移完成。")

    def test_queue_profile_backfill_rebuilds_database_before_profile_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_Demo", "Demo")
            manager = SimpleNamespace(submit_profile_metadata_backfill=lambda *_args, **_kwargs: {"accepted": True})

            status_state = {}

            def load_status(_key, default):
                return dict(status_state or default)

            def save_status(_key, payload):
                status_state.clear()
                status_state.update(payload)
                return payload

            with patch.object(archive_profile_backfill, "load_database_json_state", side_effect=load_status), \
                patch.object(archive_profile_backfill, "save_database_json_state", side_effect=save_status), \
                patch.object(archive_profile_backfill, "append_business_log"), \
                patch.object(archive_profile_backfill, "invalidate_archive_snapshot"), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = archive_profile_backfill.queue_profile_backfill(
                    manager,
                    archive_root=archive_root,
                    rebuild_database=True,
                    force_database_rebuild=True,
                )

            database_result = result["last_result"]["database_index"]
            self.assertEqual(database_result["processed"], 1)
            self.assertEqual(database_result["updated"], 1)
            self.assertTrue(archive_model_index.archive_model_index_is_bootstrapped(archive_root=archive_root))

    def test_queue_profile_backfill_database_only_skips_profile_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive_root = Path(tmp).resolve()
            _write_meta(archive_root, "LOCAL_Demo", "Demo")
            manager = SimpleNamespace(submit_profile_metadata_backfill=lambda *_args, **_kwargs: {"accepted": True})

            status_state = {}

            def load_status(_key, default):
                return dict(status_state or default)

            def save_status(_key, payload):
                status_state.clear()
                status_state.update(payload)
                return payload

            with patch.object(archive_profile_backfill, "load_database_json_state", side_effect=load_status), \
                patch.object(archive_profile_backfill, "save_database_json_state", side_effect=save_status), \
                patch.object(archive_profile_backfill, "append_business_log"), \
                patch.object(archive_profile_backfill, "invalidate_archive_snapshot"), \
                patch.object(archive_profile_backfill, "discover_profile_backfill_candidates", side_effect=AssertionError("profile scan should not run")), \
                patch.object(catalog, "ARCHIVE_DIR", archive_root):
                result = archive_profile_backfill.queue_profile_backfill(
                    manager,
                    archive_root=archive_root,
                    rebuild_database=True,
                    force_database_rebuild=True,
                    database_only=True,
                )

            self.assertEqual(result["phase"], "completed")
            self.assertEqual(result["message"], "数据库索引重建完成。")
            self.assertFalse(status_state["running"])
            self.assertFalse(status_state["database_only"])
            self.assertEqual(status_state["last_result"]["scanned_candidates"], 0)
            database_result = status_state["last_result"]["database_index"]
            self.assertEqual(database_result["processed"], 1)
            self.assertEqual(database_result["updated"], 1)


if __name__ == "__main__":
    unittest.main()
