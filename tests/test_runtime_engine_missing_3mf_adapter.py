import unittest
from types import SimpleNamespace

from app.services.runtime_engine.missing_3mf_adapter import Missing3mfRuntimeAdapter


class Missing3mfRuntimeAdapterTest(unittest.TestCase):
    def test_discover_filters_retryable_platform_items(self):
        task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {"model_id": "1", "source": "global", "status": "queued"},
                    {"model_id": "2", "source": "cn", "status": "verification_required"},
                    {"model_id": "3", "source": "global", "status": "missing"},
                ]
            }
        )
        adapter = Missing3mfRuntimeAdapter(task_store=task_store)

        candidates = adapter.discover({"platform": "global"})

        self.assertEqual([item["model_id"] for item in candidates], ["1", "3"])

    def test_plan_splits_candidates(self):
        adapter = Missing3mfRuntimeAdapter(task_store=SimpleNamespace(load_missing_3mf=lambda: {"items": []}))
        batches = adapter.plan([{"model_id": str(index)} for index in range(3)], {"batch_size": 2})

        self.assertEqual([len(batch["items"]) for batch in batches], [2, 1])

    def test_discover_uses_single_retry_context_as_candidate(self):
        adapter = Missing3mfRuntimeAdapter(task_store=SimpleNamespace(load_missing_3mf=lambda: {"items": []}))

        candidates = adapter.discover({
            "model_id": "9",
            "model_url": "https://makerworld.com/zh/models/9",
            "source": "global",
            "instance_id": "profile-9",
        })

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["model_id"], "9")
        self.assertEqual(candidates[0]["instance_id"], "profile-9")

    def test_discover_filters_requested_status_when_present(self):
        task_store = SimpleNamespace(
            load_missing_3mf=lambda: {
                "items": [
                    {"model_id": "1", "source": "global", "status": "queued"},
                    {"model_id": "2", "source": "global", "status": "verification_required"},
                    {"model_id": "3", "source": "global", "status": "cloudflare"},
                ]
            }
        )
        adapter = Missing3mfRuntimeAdapter(task_store=task_store)

        candidates = adapter.discover({"platform": "global", "status": "verification_required"})

        self.assertEqual([item["model_id"] for item in candidates], ["2"])


if __name__ == "__main__":
    unittest.main()
