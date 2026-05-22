import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services import archive_model_index, archive_profile_backfill, catalog


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
                "model": dict(payload["model_json"]),
                "meta_mtime_ns": int(payload.get("meta_mtime_ns") or 0),
                "meta_size": int(payload.get("meta_size") or 0),
            }
            return FakeCursor(rowcount=1)
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
            self.metadata[key] = value
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


def _write_meta(root: Path, model_dir: str, title: str, model_id: str = "1"):
    target = root / model_dir
    target.mkdir(parents=True)
    (target / "meta.json").write_text(
        json.dumps(
            {
                "id": model_id,
                "title": title,
                "source": "local",
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

            with patch.object(archive_profile_backfill, "PROFILE_BACKFILL_STATUS_PATH", archive_root / "status.json"), \
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


if __name__ == "__main__":
    unittest.main()
