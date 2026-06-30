import unittest

from app.services import state_contracts
from app.services.runtime_engine import contracts


class RuntimeEngineContractsTest(unittest.TestCase):
    def test_runtime_state_keys_are_registered(self):
        self.assertEqual(state_contracts.RUNTIME_RUNS_STATE_KEY, "runtime_runs")
        self.assertEqual(state_contracts.RUNTIME_BATCHES_STATE_KEY, "runtime_batches")
        self.assertEqual(state_contracts.RUNTIME_FAILURES_STATE_KEY, "runtime_failures")
        self.assertEqual(state_contracts.RUNTIME_SNAPSHOTS_STATE_KEY, "runtime_snapshots")

    def test_normalize_run_summary_keeps_known_fields_and_defaults(self):
        run = contracts.normalize_run_summary(
            {
                "run_id": "run-1",
                "type": "archive",
                "status": "running",
                "total": "12",
                "completed": "3",
                "failed": 1,
                "extra": "ignored",
            }
        )

        self.assertEqual(run["run_id"], "run-1")
        self.assertEqual(run["type"], "archive")
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["total"], 12)
        self.assertEqual(run["completed"], 3)
        self.assertEqual(run["failed"], 1)
        self.assertEqual(run["skipped"], 0)
        self.assertNotIn("extra", run)

    def test_normalize_batch_summary_rejects_unknown_status_to_queued(self):
        batch = contracts.normalize_batch_summary(
            {
                "batch_id": "batch-1",
                "run_id": "run-1",
                "type": "archive",
                "status": "mystery",
            }
        )

        self.assertEqual(batch["status"], "queued")
        self.assertEqual(batch["completed"], 0)
        self.assertEqual(batch["failed"], 0)

    def test_normalize_failure_keeps_retryable_failure_detail(self):
        failure = contracts.normalize_failure(
            {
                "failure_id": "failure-1",
                "run_id": "run-1",
                "batch_id": "batch-1",
                "type": "archive",
                "platform": "global",
                "model_id": "123",
                "status": "verification_required",
                "message": "Needs verification",
                "retryable": True,
            }
        )

        self.assertEqual(failure["status"], "verification_required")
        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["model_id"], "123")
        self.assertEqual(failure["platform"], "global")

    def test_runtime_event_scopes_are_coarse(self):
        self.assertEqual(
            contracts.RUNTIME_EVENT_SCOPES,
            {
                "runtime.run.started",
                "runtime.batch.progress",
                "runtime.batch.completed",
                "runtime.run.completed",
                "runtime.run.blocked",
                "runtime.failure.created",
                "account_health.changed",
            },
        )


if __name__ == "__main__":
    unittest.main()
