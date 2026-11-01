"""A request lifetime shared by worker threads and compute transports.

Closing our HTTP request bounds local work; it is not proof of GPU drain. A
remote hop receives only a remaining duration, never a foreign monotonic clock.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
import time

import httpx

from .overload import ComputeUnavailable

DEFAULT_VERIFY_SECONDS = 60.0
MAX_SECONDS = 120.0
TIMEOUT_HEADER = "X-Topos-Timeout-Ms"
_current: ContextVar = ContextVar("compute_request_budget", default=None)


class RequestStopped(ComputeUnavailable):
    def __init__(self, reason: str):
        super().__init__("compute_unavailable")
        self.reason = reason

    def detail(self):
        return {**super().detail(), "reason": self.reason}

    def envelope(self, request_id):
        return {**super().envelope(request_id), "termination_reason": self.reason}


class RequestBudget:
    def __init__(self, seconds: float):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS:
            raise ValueError("Invalid compute request budget")
        self.deadline = time.monotonic() + seconds
        self.cancelled = threading.Event()
        self.reason = "cancelled"

    def cancel(self, reason="cancelled"):
        if not self.cancelled.is_set():
            self.reason = reason
            self.cancelled.set()

    def remaining(self):
        if self.cancelled.is_set():
            raise RequestStopped(self.reason)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.cancel("deadline_exceeded")
            raise RequestStopped("deadline_exceeded")
        return remaining

    def check(self):
        self.remaining()


def current_budget():
    return _current.get()


def check_budget():
    budget = current_budget()
    if budget is not None:
        budget.check()


@contextmanager
def budget_scope(budget):
    token = _current.set(budget)
    try:
        yield budget
    finally:
        _current.reset(token)


async def await_budgeted(awaitable, budget):
    """Cancel an async operation on expiry or cross-thread cancellation.

If awaitable uses a thread, cancellation only stops its waiter. That worker
must also observe the shared budget; callers must not report GPU drain here.
"""
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            await asyncio.wait((task,), timeout=min(.025, budget.remaining()))
        budget.check()
        return await task
    except asyncio.CancelledError:
        budget.cancel()
        raise
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def request(method, url, *, timeout, **kwargs):
    """Synchronous worker entry, cancellation-aware async socket ownership.

One connection attempt, no retries. Total elapsed time, including a trickling
body, is bounded by the inherited budget (HTTP read timeout alone is not).
"""
    budget = current_budget()
    if budget is None:
        raise RuntimeError("Budgeted transport requires a request budget")
    budget.check()

    async def run():
        async with httpx.AsyncClient(timeout=min(timeout, budget.remaining())) as client:
            try:
                return await await_budgeted(client.request(method, url, **kwargs), budget)
            except httpx.TimeoutException:
                budget.check()
                raise ComputeUnavailable("compute_unavailable") from None
            except httpx.RequestError:
                raise ComputeUnavailable("compute_unavailable") from None

    return asyncio.run(run())
