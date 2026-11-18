from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, patch

from app import main


class AccountVerificationLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_shared_sweeper_cleans_expired_sessions_and_can_be_cancelled(self):
        sleeping = asyncio.Event()
        never = asyncio.Event()
        ticks = []

        async def tick(delay):
            ticks.append(delay)
            if len(ticks) > 1:
                sleeping.set()
                await never.wait()

        async def run_io(callback):
            callback()

        manager = Mock()
        with patch.object(main.asyncio, "sleep", side_effect=tick), \
                patch.object(main, "run_web_io", side_effect=run_io) as dispatch, \
                patch.object(main, "manual_verification_manager", manager):
            task = asyncio.create_task(main._expire_manual_verifications())
            try:
                await asyncio.wait_for(sleeping.wait(), timeout=1)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(ticks, [30, 30])
            dispatch.assert_awaited_once_with(manager.prune_expired)
            manager.prune_expired.assert_called_once_with()

    async def test_application_shutdown_cancels_sweeper_and_closes_verification_sessions(self):
        waiting = asyncio.Event()
        task = asyncio.create_task(waiting.wait())
        previous = getattr(main.app.state, "verification_cleanup_task", None)
        main.app.state.verification_cleanup_task = task
        manager = Mock()
        try:
            with patch.object(main, "manual_verification_manager", manager), \
                    patch.object(main.local_organizer, "stop"), \
                    patch.object(main, "shutdown_business_log_writer"), \
                    patch.object(main, "shutdown_request_threads"), \
                    patch.object(main, "close_database_pool"):
                await main.shutdown_thread_pools()
            self.assertTrue(task.cancelled())
            manager.shutdown.assert_called_once_with()
        finally:
            if not task.done():
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            if previous is None:
                del main.app.state.verification_cleanup_task
            else:
                main.app.state.verification_cleanup_task = previous


if __name__ == "__main__":
    unittest.main()
