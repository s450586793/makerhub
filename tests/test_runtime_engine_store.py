from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services import state_contracts
from app.services.runtime_engine import store


class RuntimeEngineStoreTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.load_patch = patch.object(
            store,
            "load_database_json_state",
            side_effect=lambda key, default: self.state.get(key, default),
        )
        self.save_patch = patch.object(
            store,
            "save_database_json_state",
            side_effect=lambda key, value: self.state.__setitem__(key, value) or value,
        )
        self.event_calls = []
        self.event_patch = patch.object(
            store,
            "append_state_event",
            side_effect=lambda event_type, scope, payload: self.event_calls.append((event_type, scope, payload)),
        )
        self.load_patch.start()
        self.save_patch.start()
        self.event_patch.start()

    def tearDown(self):
        self.event_patch.stop()
        self.save_patch.stop()
        self.load_patch.stop()

    def test_load_defaults_are_bounded_and_normalized(self):
        payload = store.load_runtime_state()

        self.assertEqual(payload["runs"]["items"], [])
        self.assertEqual(payload["batches"]["items"], [])
        self.assertEqual(payload["failures"]["items"], [])
        self.assertIn("dashboard", payload["snapshots"])

    def test_upsert_run_saves_normalized_run_and_publishes_event(self):
        run = store.upsert_run(
            {
                "run_id": "run-1",
                "type": "archive",
                "status": "running",
                "total": "3",
                "message": "Running",
            },
            event_type="runtime.run.started",
        )

        self.assertEqual(run["total"], 3)
        saved = self.state[state_contracts.RUNTIME_RUNS_STATE_KEY]
        self.assertEqual(saved["items"][0]["run_id"], "run-1")
        self.assertEqual(self.event_calls[-1][0], "runtime.run.started")
        self.assertEqual(self.event_calls[-1][1], "runtime")

    def test_upsert_batch_replaces_existing_batch(self):
        store.upsert_batch({"batch_id": "batch-1", "run_id": "run-1", "status": "queued"})
        store.upsert_batch({"batch_id": "batch-1", "run_id": "run-1", "status": "running", "completed": 2})

        saved = self.state[state_contracts.RUNTIME_BATCHES_STATE_KEY]["items"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["status"], "running")
        self.assertEqual(saved[0]["completed"], 2)

    def test_append_failure_keeps_failure_durable_and_retryable(self):
        failure = store.append_failure(
            {
                "failure_id": "failure-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "type": "archive",
                "status": "missing_3mf",
                "retryable": True,
            }
        )

        self.assertTrue(failure["retryable"])
        saved = self.state[state_contracts.RUNTIME_FAILURES_STATE_KEY]["items"]
        self.assertEqual(saved[0]["failure_id"], "failure-1")

    def test_save_snapshot_updates_named_snapshot_only(self):
        store.save_snapshot("dashboard", {"active_runs": [{"run_id": "run-1"}]})
        store.save_snapshot("tasks", {"runs": []})

        snapshots = self.state[state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY]
        self.assertEqual(snapshots["dashboard"]["active_runs"][0]["run_id"], "run-1")
        self.assertEqual(snapshots["tasks"]["runs"], [])

    def test_batch_item_temp_file_round_trips_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(store, "RUNTIME_BATCH_ITEM_DIR", Path(tmpdir)):
                store.save_batch_items("batch-1", [{"model_id": "1"}, {"model_id": "2"}])

                self.assertEqual(store.load_batch_items("batch-1"), [{"model_id": "1"}, {"model_id": "2"}])
                self.assertTrue(store.delete_batch_items("batch-1"))
                self.assertEqual(store.load_batch_items("batch-1"), [])


if __name__ == "__main__":
    unittest.main()
