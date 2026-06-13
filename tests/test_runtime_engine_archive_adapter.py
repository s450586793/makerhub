import unittest
from unittest.mock import patch

from app.services.runtime_engine.archive_adapter import ArchiveRuntimeAdapter


class ArchiveRuntimeAdapterTest(unittest.TestCase):
    def test_discover_single_model_returns_one_candidate(self):
        adapter = ArchiveRuntimeAdapter()

        candidates = adapter.discover({"source_url": "https://makerworld.com/zh/models/123"})

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["model_url"], "https://makerworld.com/zh/models/123")

    def test_plan_splits_candidates_by_batch_size(self):
        adapter = ArchiveRuntimeAdapter()
        candidates = [{"model_url": f"https://makerworld.com/zh/models/{index}"} for index in range(5)]

        batches = adapter.plan(candidates, {"batch_size": 2})

        self.assertEqual([len(batch["items"]) for batch in batches], [2, 2, 1])

    def test_execute_item_calls_existing_archive_submit_boundary(self):
        adapter = ArchiveRuntimeAdapter()

        with patch.object(adapter.manager, "submit", return_value={"accepted": True, "task_id": "task-1"}) as submit:
            result = adapter.execute_item({"model_url": "https://makerworld.com/zh/models/123"}, {"run_id": "run-1"})

        submit.assert_called_once_with("https://makerworld.com/zh/models/123")
        self.assertTrue(result["accepted"])

    def test_classify_failure_sanitizes_message(self):
        adapter = ArchiveRuntimeAdapter()

        failure = adapter.classify_failure(RuntimeError("<html>secret</html>"))

        self.assertEqual(failure["status"], "failed")
        self.assertNotIn("<html>", failure["message"])


if __name__ == "__main__":
    unittest.main()
