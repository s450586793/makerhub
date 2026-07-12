import unittest
import time
from unittest.mock import patch

from app.services import process_jobs, resource_limiter


def _write_result_file_only(_queue, payload):
    process_jobs._write_job_result_file(payload, "result", {"ok": True, "value": 42})


def _write_error_file_only(_queue, payload):
    process_jobs._write_job_result_file(payload, "error", {"message": "boom"})


def _emit_final_progress_without_result(queue, _payload):
    queue.put({"type": "progress", "payload": {"percent": 100, "message": "归档完成"}})
    time.sleep(1)


def _emit_final_progress_then_result(queue, payload):
    queue.put({"type": "progress", "payload": {"percent": 100, "message": "归档完成"}})
    time.sleep(0.05)
    process_jobs._emit_finished(queue, payload, "result", {"ok": True})


def _report_spawn_resource_limits(queue, payload):
    process_jobs._emit_finished(
        queue,
        payload,
        "result",
        {
            "payload": payload.get("__resource_limits"),
            "configured": {
                "makerworld_page_api": resource_limiter.RESOURCE_LIMITS["makerworld_page_api"],
                "comment_assets": resource_limiter.RESOURCE_LIMITS["comment_assets"],
                "three_mf_download": resource_limiter.RESOURCE_LIMITS["three_mf_download"],
                "disk_io": resource_limiter.RESOURCE_LIMITS["disk_io"],
            },
        },
    )


class ProcessJobsTest(unittest.TestCase):
    def test_spawn_entry_applies_limits_without_republishing_stale_capacity(self):
        calls = []

        def target(_queue, _payload):
            calls.append("target")

        with patch.object(process_jobs, "configure_resource_limits") as configure:
            process_jobs._run_spawned_job_entry(
                target,
                None,
                {"__resource_limits": {"disk_io_limit": 1}},
            )

        configure.assert_called_once_with(
            {"disk_io_limit": 1},
            publish_global=False,
        )
        self.assertEqual(calls, ["target"])

    def test_run_process_job_passes_and_applies_current_resource_limits(self):
        original_limits = dict(resource_limiter.RESOURCE_LIMITS)
        expected_payload = {
            "makerworld_request_limit": 8,
            "comment_asset_download_limit": 16,
            "three_mf_download_limit": 4,
            "disk_io_limit": 4,
        }
        try:
            resource_limiter.configure_resource_limits(expected_payload)
            result = process_jobs._run_process_job(_report_spawn_resource_limits, {})
        finally:
            resource_limiter.configure_resource_limits(
                {
                    "makerworld_request_limit": original_limits["makerworld_page_api"],
                    "comment_asset_download_limit": original_limits["comment_assets"],
                    "three_mf_download_limit": original_limits["three_mf_download"],
                    "disk_io_limit": original_limits["disk_io"],
                }
            )

        self.assertEqual(result["payload"], expected_payload)
        self.assertEqual(
            result["configured"],
            {
                "makerworld_page_api": 8,
                "comment_assets": 16,
                "three_mf_download": 4,
                "disk_io": 4,
            },
        )

    def test_run_process_job_reads_result_file_when_queue_event_is_missing(self):
        result = process_jobs._run_process_job(_write_result_file_only, {})

        self.assertEqual(result, {"ok": True, "value": 42})

    def test_run_process_job_reads_error_file_when_queue_event_is_missing(self):
        with self.assertRaisesRegex(RuntimeError, "boom"):
            process_jobs._run_process_job(_write_error_file_only, {})

    def test_run_process_job_times_out_quickly_after_final_progress_without_result(self):
        with self.assertRaisesRegex(RuntimeError, "后台任务已上报完成进度，但没有返回结果"):
            process_jobs._run_process_job(
                _emit_final_progress_without_result,
                {},
                idle_timeout_seconds=30,
                final_progress_timeout_seconds=0.1,
            )

    def test_run_process_job_accepts_result_after_final_progress(self):
        result = process_jobs._run_process_job(
            _emit_final_progress_then_result,
            {},
            final_progress_timeout_seconds=1,
        )

        self.assertEqual(result, {"ok": True})

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

    def test_run_archive_model_job_passes_instance_ids_to_inline_archiver(self):
        calls = []
        original_use_subprocess = process_jobs._use_subprocess
        process_jobs._use_subprocess = lambda: False

        def fake_archive_model(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        try:
            with patch.object(process_jobs, "legacy_archive_model", side_effect=fake_archive_model), \
                    patch.object(process_jobs, "temporary_proxy_env") as proxy_env, \
                    patch.object(process_jobs, "resource_slot") as resource_slot:
                proxy_env.return_value.__enter__.return_value = None
                proxy_env.return_value.__exit__.return_value = False
                resource_slot.return_value.__enter__.return_value = None
                resource_slot.return_value.__exit__.return_value = False

                result = process_jobs.run_archive_model_job(
                    url="https://makerworld.com.cn/zh/models/123",
                    cookie="token=ok",
                    download_dir="/tmp/archive",
                    logs_dir="/tmp/logs",
                    instance_ids=["profile-1", "profile-2"],
                )
        finally:
            process_jobs._use_subprocess = original_use_subprocess

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls[0]["instance_ids"], ["profile-1", "profile-2"])

    def test_run_archive_model_job_passes_comment_collection_flag_to_inline_archiver(self):
        calls = []
        original_use_subprocess = process_jobs._use_subprocess
        process_jobs._use_subprocess = lambda: False

        def fake_archive_model(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        try:
            with patch.object(process_jobs, "legacy_archive_model", side_effect=fake_archive_model), \
                    patch.object(process_jobs, "temporary_proxy_env") as proxy_env, \
                    patch.object(process_jobs, "resource_slot") as resource_slot:
                proxy_env.return_value.__enter__.return_value = None
                proxy_env.return_value.__exit__.return_value = False
                resource_slot.return_value.__enter__.return_value = None
                resource_slot.return_value.__exit__.return_value = False

                result = process_jobs.run_archive_model_job(
                    url="https://makerworld.com.cn/zh/models/123",
                    cookie="token=ok",
                    download_dir="/tmp/archive",
                    logs_dir="/tmp/logs",
                    collect_comments_data=False,
                )
        finally:
            process_jobs._use_subprocess = original_use_subprocess

        self.assertEqual(result, {"ok": True})
        self.assertIs(calls[0]["collect_comments_data"], False)


if __name__ == "__main__":
    unittest.main()
