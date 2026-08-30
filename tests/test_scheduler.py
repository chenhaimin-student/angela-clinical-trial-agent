from __future__ import annotations

import asyncio
import unittest

from angela_orchestration.scheduler import RequestScheduler


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_queue_and_release(self) -> None:
        scheduler = RequestScheduler(max_active=1, queue_capacity=2, queue_timeout_seconds=2)
        first = await scheduler.acquire("one", "session-one")
        waiting = asyncio.create_task(scheduler.acquire("two", "session-two"))
        await asyncio.sleep(0)
        self.assertEqual(scheduler.active_count, 1)
        self.assertEqual(scheduler.queue_depth, 1)
        await first.lease.release()
        second = await waiting
        self.assertIsNotNone(second.lease)
        self.assertEqual(second.queue_position, 1)
        await second.lease.release()
        self.assertEqual(scheduler.active_count, 0)

    async def test_duplicate_and_full_queue_are_rejected(self) -> None:
        scheduler = RequestScheduler(max_active=1, queue_capacity=1, queue_timeout_seconds=2)
        first = await scheduler.acquire("one", "same")
        duplicate = await scheduler.acquire("two", "same")
        self.assertIn("正在处理", duplicate.rejection_text)
        waiting = asyncio.create_task(scheduler.acquire("three", "other"))
        await asyncio.sleep(0)
        full = await scheduler.acquire("four", "third")
        self.assertIn("队列已满", full.rejection_text)
        await first.lease.release()
        admitted = await waiting
        await admitted.lease.release()

    async def test_cancelled_waiter_does_not_leak_capacity(self) -> None:
        scheduler = RequestScheduler(max_active=1, queue_capacity=2, queue_timeout_seconds=2)
        first = await scheduler.acquire("one", "session-one")
        waiting = asyncio.create_task(scheduler.acquire("two", "session-two"))
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(scheduler.queue_depth, 0)
        await first.lease.release()
        self.assertEqual(scheduler.active_count, 0)


if __name__ == "__main__":
    unittest.main()
