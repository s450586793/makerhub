import unittest
from unittest.mock import patch

from app.services.runtime_engine import engine


class _FakeAdapter:
    def discover(self, context):
        return [{"model_id": "1"}, {"model_id": "2"}, {"model_id": "3"}]

    def plan(self, candidates, limits):
        return [
            {"items": candidates[:2], "offset": 0, "limit": 2},
            {"items": candidates[2:], "offset": 2, "limit": 2},
        ]


class RuntimeEngineSkeletonTest(unittest.TestCase):
    def setUp(self):
        self.runs = []
        self.batches = []
        self.snapshots = {}
        self.batch_items = []
        self.store_patches = [
            patch.object(engine.store, "upsert_run", side_effect=lambda run, **kwargs: self.runs.append(run) or run),
            patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: self.batches.append(batch) or batch),
            patch.object(engine.store, "save_batch_items", side_effect=lambda batch_id, items: self.batch_items.append((batch_id, items))),
            patch.object(engine.store, "load_runtime_state", side_effect=self._runtime_state),
            patch.object(engine.store, "save_snapshot", side_effect=lambda name, snapshot: self.snapshots.__setitem__(name, snapshot) or self.snapshots),
        ]
        for item in self.store_patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.store_patches):
            item.stop()

    def _runtime_state(self):
        return {
            "runs": {"items": list(self.runs)},
            "batches": {"items": list(self.batches)},
            "failures": {"items": []},
            "snapshots": {},
        }

    def test_submit_run_discovers_and_plans_bounded_batches(self):
        runtime = engine.RuntimeEngine(adapters={"archive": _FakeAdapter()}, batch_size=2)

        result = runtime.submit_run("archive", {"source_url": "https://makerworld.com/zh/models/1"})

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(self.batches), 2)
        self.assertEqual(self.batches[0]["total"], 2)
        self.assertEqual(self.batches[1]["total"], 1)
        self.assertEqual([len(items) for _batch_id, items in self.batch_items], [2, 1])
        self.assertIn("tasks", self.snapshots)

    def test_repair_regenerates_snapshots_without_adapter(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)

        with patch.object(engine.store, "load_runtime_state", return_value={
            "runs": {"items": [{"run_id": "run-1", "type": "archive", "status": "running"}]},
            "batches": {"items": [{"batch_id": "batch-1", "run_id": "run-1", "status": "queued"}]},
            "failures": {"items": []},
            "snapshots": {},
        }):
            result = runtime.repair()

        self.assertTrue(result["success"])
        self.assertIn("tasks", self.snapshots)

    def test_set_run_status_updates_existing_run(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)

        with patch.object(engine.store, "load_runs", return_value={"items": [{"run_id": "run-1", "type": "archive", "status": "running"}]}):
            result = runtime.set_run_status("run-1", "paused")

        self.assertEqual(result["status"], "paused")

    def test_retry_failures_submits_missing_3mf_retry_context(self):
        runtime = engine.RuntimeEngine(adapters={"missing_3mf_retry": _FakeAdapter()}, batch_size=2)

        with patch.object(
            engine.store,
            "load_failures",
            return_value={"items": [{"failure_id": "failure-1", "type": "missing_3mf_retry", "status": "missing_3mf"}]},
        ):
            result = runtime.retry_failures({"failure_ids": ["failure-1"]})

        self.assertEqual(result["type"], "missing_3mf_retry")


if __name__ == "__main__":
    unittest.main()
