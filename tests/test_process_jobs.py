import unittest

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


if __name__ == "__main__":
    unittest.main()
