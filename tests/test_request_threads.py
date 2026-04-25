import asyncio
import threading
import unittest

from app.services import request_threads


class RequestThreadsTest(unittest.TestCase):
    def test_ui_executor_is_separate_from_heavy_web_and_task_executors(self):
        self.assertIsNot(request_threads.UI_IO_EXECUTOR, request_threads.WEB_IO_EXECUTOR)
        self.assertIsNot(request_threads.UI_IO_EXECUTOR, request_threads.TASK_API_EXECUTOR)

    def test_run_ui_io_uses_ui_thread_pool(self):
        thread_name = asyncio.run(request_threads.run_ui_io(lambda: threading.current_thread().name))

        self.assertTrue(thread_name.startswith("makerhub-ui-io"))

    def test_run_web_io_uses_web_thread_pool(self):
        thread_name = asyncio.run(request_threads.run_web_io(lambda: threading.current_thread().name))

        self.assertTrue(thread_name.startswith("makerhub-web-io"))


if __name__ == "__main__":
    unittest.main()
