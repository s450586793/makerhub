import unittest
from unittest.mock import patch

from app.services import process_jobs


def _write_result_file_only(_queue, payload):
    process_jobs._write_job_result_file(payload, "result", {"ok": True, "value": 42})


def _write_error_file_only(_queue, payload):
    process_jobs._write_job_result_file(payload, "error", {"message": "boom"})


class ProcessJobsTest(unittest.TestCase):
    def test_run_process_job_reads_result_file_when_queue_event_is_missing(self):
        result = process_jobs._run_process_job(_write_result_file_only, {})

        self.assertEqual(result, {"ok": True, "value": 42})

    def test_run_process_job_reads_error_file_when_queue_event_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            process_jobs._run_process_job(_write_error_file_only, {})

    def test_run_discover_batch_urls_job_passes_proxy_config_to_env(self):
        calls = []
        original_use_subprocess = process_jobs._use_subprocess
        process_jobs._use_subprocess = lambda: False

        def fake_discover(url, cookie):
            calls.append((url, cookie))
            return {"items": []}

        proxy_config = {
            "enabled": True,
            "http_proxy": "http://proxy.local:7890",
            "https_proxy": "http://proxy.local:7891",
            "no_proxy": "",
        }

        try:
            with patch.object(process_jobs, "discover_batch_model_urls", side_effect=fake_discover), \
                    patch.object(process_jobs, "temporary_proxy_env") as proxy_env:
                proxy_env.return_value.__enter__.return_value = None
                proxy_env.return_value.__exit__.return_value = False

                result = process_jobs.run_discover_batch_urls_job(
                    "https://makerworld.com.cn/zh/@ace/upload",
                    "token=ok",
                    proxy_config=proxy_config,
                )
        finally:
            process_jobs._use_subprocess = original_use_subprocess

        self.assertEqual(result, {"items": []})
        self.assertEqual(calls, [("https://makerworld.com.cn/zh/@ace/upload", "token=ok")])
        proxy_env.assert_called_once_with(proxy_config, "https://makerworld.com.cn/zh/@ace/upload")


if __name__ == "__main__":
    unittest.main()
