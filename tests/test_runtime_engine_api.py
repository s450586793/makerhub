import unittest
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api import runtime_routes
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


class RuntimeEngineRouteTest(unittest.TestCase):
    def _request(self):
        return SimpleNamespace(state=SimpleNamespace(auth_identity={"kind": "session", "username": "admin"}))

    def test_get_runtime_requires_session_and_reads_snapshot_without_repair(self):
        snapshots = {"tasks": {"runs": []}}
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.store, "load_snapshots", return_value=snapshots) as load_snapshots, \
                patch.object(runtime_routes.runtime_engine, "repair") as repair, \
                patch.object(runtime_routes.runtime_engine, "refresh_snapshots") as refresh_snapshots:
            payload = asyncio.run(runtime_routes.get_runtime(self._request()))

        require_auth.assert_called_once()
        load_snapshots.assert_called_once_with()
        repair.assert_not_called()
        refresh_snapshots.assert_not_called()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["writable"])
        self.assertEqual(payload["snapshots"], snapshots)
        self.assertTrue(payload["disabled_reason"])

    def test_runtime_run_list_is_read_only(self):
        runs = [{"run_id": "run-1", "status": "planned"}]
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.store, "load_runs", return_value={"items": runs}) as load_runs, \
                patch.object(runtime_routes.runtime_engine, "repair") as repair, \
                patch.object(runtime_routes.runtime_engine, "refresh_snapshots") as refresh_snapshots:
            payload = asyncio.run(runtime_routes.get_runtime_runs(self._request()))

        require_auth.assert_called_once()
        load_runs.assert_called_once_with()
        repair.assert_not_called()
        refresh_snapshots.assert_not_called()
        self.assertEqual(payload["runs"], runs)
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["writable"])

    def test_run_detail_and_failure_pages_are_session_only(self):
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes, "store") as store_mock:
            store_mock.load_runs.return_value = {"items": [{"run_id": "run-1", "status": "running"}]}
            store_mock.load_batches.return_value = {"items": [{"batch_id": "batch-1", "run_id": "run-1"}]}
            store_mock.load_failures.return_value = {"items": [{"failure_id": "failure-1", "run_id": "run-1"}]}

            detail = asyncio.run(runtime_routes.get_runtime_run("run-1", self._request()))
            failures = asyncio.run(runtime_routes.get_runtime_run_failures("run-1", self._request(), page=1, page_size=20))

        self.assertEqual(require_auth.call_count, 2)
        self.assertEqual(detail["run"]["run_id"], "run-1")
        self.assertEqual(failures["items"][0]["failure_id"], "failure-1")
        self.assertFalse(detail["enabled"])
        self.assertFalse(detail["writable"])
        self.assertFalse(failures["enabled"])
        self.assertFalse(failures["writable"])

    def test_all_runtime_writes_return_503_for_every_old_truthy_environment_value(self):
        calls = (
            lambda: runtime_routes.submit_runtime_run(
                {"type": "archive", "source_url": "https://makerworld.com/zh/models/1"},
                self._request(),
            ),
            lambda: runtime_routes.pause_runtime_run("run-1", self._request()),
            lambda: runtime_routes.resume_runtime_run("run-1", self._request()),
            lambda: runtime_routes.cancel_runtime_run("run-1", self._request()),
            lambda: runtime_routes.retry_runtime_failures({"failure_ids": ["failure-1"]}, self._request()),
            lambda: runtime_routes.repair_runtime(self._request()),
        )
        with patch.object(runtime_routes.config_api, "_require_session_auth") as require_auth, \
                patch.object(runtime_routes.runtime_engine, "submit_run") as submit, \
                patch.object(runtime_routes.runtime_engine, "set_run_status") as set_status, \
                patch.object(runtime_routes.runtime_engine, "retry_failures") as retry, \
                patch.object(runtime_routes.runtime_engine, "repair") as repair, \
                patch.object(runtime_routes, "store") as runtime_store:
            for value in ("1", "true", "v2", "runtime"):
                with patch.dict(os.environ, {"MAKERHUB_RUNTIME_ENGINE": value}), self.subTest(value=value):
                    for call in calls:
                        with self.subTest(endpoint=call):
                            with self.assertRaises(HTTPException) as caught:
                                asyncio.run(call())
                            self.assertEqual(caught.exception.status_code, 503)
                            self.assertIn("已冻结", str(caught.exception.detail))

        self.assertEqual(require_auth.call_count, 24)
        submit.assert_not_called()
        set_status.assert_not_called()
        retry.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(runtime_store.method_calls, [])


class RuntimeEngineExecutionTest(unittest.TestCase):
    def test_execute_next_batch_runs_items_and_records_summary(self):
        class Adapter:
            def execute_item(self, item, context):
                if item["model_id"] == "bad":
                    raise RuntimeError("failed item")
                return {"success": True, "model_id": item["model_id"]}

            def commit_success(self, result, context):
                return None

            def classify_failure(self, error):
                return {"status": "failed", "message": str(error), "retryable": True}

        batches = [{"batch_id": "batch-1", "run_id": "run-1", "type": "archive", "status": "queued", "total": 2}]
        saved_batches = []
        saved_runs = []
        failures = []

        runtime = engine.RuntimeEngine(adapters={"archive": Adapter()}, batch_size=2)

        with patch.object(engine.store, "load_batches", return_value={"items": batches}), \
                patch.object(engine.store, "load_batch_items", return_value=[{"model_id": "ok"}, {"model_id": "bad"}]), \
                patch.object(
                    engine.store,
                    "load_runs",
                    return_value={"items": [{"run_id": "run-1", "type": "archive", "status": "running", "total": 2}]},
                ), \
                patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: saved_batches.append(batch) or batch), \
                patch.object(engine.store, "upsert_run", side_effect=lambda run, **kwargs: saved_runs.append(run) or run), \
                patch.object(engine.store, "append_failure", side_effect=lambda failure, **kwargs: failures.append(failure) or failure), \
                patch.object(engine.store, "delete_batch_items", return_value=True), \
                patch.object(runtime, "refresh_snapshots", return_value={}):
            result = runtime.execute_next_batch()

        self.assertTrue(result["executed"])
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(failures[0]["status"], "failed")
        self.assertEqual(saved_batches[-1]["status"], "completed")

    def test_repair_requeues_interrupted_batches_and_updates_run_totals(self):
        runtime = engine.RuntimeEngine(adapters={}, batch_size=2)
        saved_batches = []
        saved_runs = []

        with patch.object(engine.store, "load_runtime_state", return_value={
            "runs": {"items": [{"run_id": "run-1", "type": "archive", "status": "running", "total": 2}]},
            "batches": {"items": [{"batch_id": "batch-1", "run_id": "run-1", "type": "archive", "status": "interrupted", "completed": 1, "failed": 1, "total": 2}]},
            "failures": {"items": [{"failure_id": "failure-1", "run_id": "run-1", "batch_id": "batch-1"}]},
            "snapshots": {},
        }), \
                patch.object(engine.store, "upsert_batch", side_effect=lambda batch, **kwargs: saved_batches.append(batch) or batch), \
                patch.object(engine.store, "upsert_run", side_effect=lambda run, **kwargs: saved_runs.append(run) or run), \
                patch.object(engine.store, "save_snapshot", return_value={}):
            result = runtime.repair()

        self.assertTrue(result["success"])
        self.assertEqual(saved_batches[0]["status"], "queued")
        self.assertEqual(saved_runs[-1]["completed"], 1)
        self.assertEqual(saved_runs[-1]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
