import unittest
from unittest.mock import patch

from app.services.runtime_engine import migration


class RuntimeEngineMigrationTest(unittest.TestCase):
    def test_preview_counts_unfinished_legacy_work(self):
        legacy = {
            "archive_queue": {
                "active": [{"id": "active-1", "url": "https://makerworld.com/zh/models/1", "status": "running"}],
                "queued": [{"id": "queued-1", "url": "https://makerworld.com/zh/models/2", "status": "queued"}],
                "recent_failures": [{"id": "failed-1", "url": "https://makerworld.com/zh/models/3", "status": "failed"}],
            },
            "missing_3mf": {
                "items": [{"model_id": "4", "model_url": "https://makerworld.com/zh/models/4", "status": "missing"}]
            },
            "remote_refresh_state": {"status": "running", "last_batch_total": 12},
            "source_refresh_runs": {"active_run": {"run_id": "src-1", "status": "running"}},
            "subscriptions_state": {"items": [{"id": "sub-1", "status": "running"}]},
        }

        preview = migration.preview_migration(legacy)

        self.assertEqual(preview["archive_queued"], 1)
        self.assertEqual(preview["archive_active"], 1)
        self.assertEqual(preview["legacy_failures"], 1)
        self.assertEqual(preview["missing_3mf"], 1)
        self.assertTrue(preview["source_refresh_active"])
        self.assertEqual(preview["subscription_active"], 1)

    def test_apply_migration_is_idempotent_by_digest(self):
        legacy = {
            "archive_queue": {"active": [], "queued": [{"id": "queued-1", "url": "https://makerworld.com/zh/models/2"}]},
            "missing_3mf": {"items": []},
        }
        saved_markers = {}
        submitted = []

        with patch.object(migration, "load_migration_state", side_effect=lambda: dict(saved_markers)), \
                patch.object(migration, "save_migration_state", side_effect=lambda value: saved_markers.update(value) or value), \
                patch.object(migration, "_submit_archive_migration_run", side_effect=lambda item: submitted.append(item)):
            first = migration.apply_migration(legacy)
            second = migration.apply_migration(legacy)

        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertEqual(len(submitted), 1)


if __name__ == "__main__":
    unittest.main()
