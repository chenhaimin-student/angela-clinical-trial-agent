from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .models import AdmissionResult


Notify = Callable[[str], Awaitable[None]]
QueueObserver = Callable[[int], Awaitable[None]]


@dataclass
class _Waiter:
    request_id: str
    session_key: str
    future: asyncio.Future[None]
    enqueued_at: float


class RequestLease:
    def __init__(self, scheduler: "RequestScheduler", session_key: str):
        self._scheduler = scheduler
        self._session_key = session_key
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._scheduler.release(self._session_key)


class RequestScheduler:
    """A process-local bounded FIFO shared by all gateway channels."""

    def __init__(self, max_active: int, queue_capacity: int, queue_timeout_seconds: float, duplicate_limit: int = 1):
        if max_active < 1 or queue_capacity < 0 or queue_timeout_seconds <= 0:
            raise ValueError("invalid scheduler limits")
        self.max_active = max_active
        self.queue_capacity = queue_capacity
        self.queue_timeout_seconds = queue_timeout_seconds
        self.duplicate_limit = duplicate_limit
        self._active = 0
        self._active_sessions: Counter[str] = Counter()
        self._waiters: deque[_Waiter] = deque()
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def queue_depth(self) -> int:
        return sum(not waiter.future.done() for waiter in self._waiters)

    async def acquire(
        self,
        request_id: str,
        session_key: str,
        notify: Notify | None = None,
        on_queued: QueueObserver | None = None,
    ) -> AdmissionResult:
        loop = asyncio.get_running_loop()
        queued = False
        waiter: _Waiter | None = None
        position = 0
        async with self._lock:
            pending_same = self._active_sessions[session_key] + sum(
                1 for item in self._waiters if item.session_key == session_key and not item.future.done()
            )
            if pending_same >= self.duplicate_limit:
                return AdmissionResult(rejection_text="您已有一个请求正在处理，请等待完成后再提交新的请求。")
            if self._active < self.max_active and not self._waiters:
                self._active += 1
                self._active_sessions[session_key] += 1
                return AdmissionResult(lease=RequestLease(self, session_key))
            live_depth = sum(not item.future.done() for item in self._waiters)
            if live_depth >= self.queue_capacity:
                return AdmissionResult(rejection_text="当前请求较多，等待队列已满，请稍后重新发送。")
            future: asyncio.Future[None] = loop.create_future()
            waiter = _Waiter(request_id, session_key, future, time.monotonic())
            self._waiters.append(waiter)
            position = live_depth + 1
            queued = True

        if queued and notify is not None:
            await notify(f"当前请求较多，已进入处理队列，前方约有 {position} 个请求。")
        if queued and on_queued is not None:
            await on_queued(position)
        assert waiter is not None
        try:
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout=self.queue_timeout_seconds)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            async with self._lock:
                if not waiter.future.done():
                    waiter.future.cancel()
                self._discard_done_locked()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return AdmissionResult(rejection_text="等待处理超时，本次请求未开始执行，请稍后重新发送。", queue_position=position)
        wait_seconds = time.monotonic() - waiter.enqueued_at
        if notify is not None and wait_seconds >= 0.5:
            await notify("已轮到您的请求，正在开始处理。")
        return AdmissionResult(
            lease=RequestLease(self, session_key),
            queue_position=position,
            queue_wait_seconds=wait_seconds,
        )

    async def release(self, session_key: str) -> None:
        async with self._lock:
            if self._active_sessions[session_key] > 0:
                self._active_sessions[session_key] -= 1
                if self._active_sessions[session_key] <= 0:
                    self._active_sessions.pop(session_key, None)
            self._active = max(0, self._active - 1)
            self._discard_done_locked()
            while self._waiters and self._active < self.max_active:
                waiter = self._waiters.popleft()
                if waiter.future.done():
                    continue
                self._active += 1
                self._active_sessions[waiter.session_key] += 1
                waiter.future.set_result(None)

    def _discard_done_locked(self) -> None:
        self._waiters = deque(item for item in self._waiters if not item.future.done())
