import unittest

from app.core import settings
from app.services.archive_worker import ArchiveTaskManager


class LegacyHistoryMaintenanceRemovedTest(unittest.TestCase):
    def test_legacy_history_maintenance_service_file_is_removed(self):
        legacy_module_name = "_".join(["archive", "profile", "backfill"])
        self.assertFalse((settings.ROOT_DIR / "app" / "services" / f"{legacy_module_name}.py").exists())

    def test_archive_worker_no_longer_exposes_history_maintenance_submitter(self):
        submitter_name = "_".join(["submit", "profile", "metadata", "backfill"])
        self.assertFalse(hasattr(ArchiveTaskManager, submitter_name))

    def test_sources_do_not_reference_legacy_history_maintenance_flow(self):
        forbidden_tokens = (
            "_".join(["archive", "profile", "backfill"]),
            "_".join(["profile", "backfill"]),
            "_".join(["PROFILE", "BACKFILL"]),
            "_".join(["profile", "metadata", "only"]),
            "_".join(["profile", "metadata", "backfill"]),
            "_".join(["submit", "profile", "metadata", "backfill"]),
            "-".join(["source", "backfill"]),
            "_".join(["archive", "profile", "backfill", "status"]),
        )
        roots = [
            settings.ROOT_DIR / "app",
            settings.ROOT_DIR / "frontend" / "src",
        ]
        offenders: list[str] = []
        for root in roots:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in {".py", ".js", ".vue", ".css"}:
                    continue
                text = path.read_text(encoding="utf-8")
                for token in forbidden_tokens:
                    if token in text:
                        offenders.append(f"{path.relative_to(settings.ROOT_DIR)}:{token}")

        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
