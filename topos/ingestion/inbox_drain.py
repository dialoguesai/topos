"""Async drain queue for Usage Inbox app_ingest deliveries."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

_DEFAULT_MAX_CONCURRENT = 10


class InboxDrain:
    """Limits concurrent app_ingest processing for inbox flush workloads."""

    def __init__(self, *, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))

    async def run(self, coro_factory: Callable[[], Awaitable[T]]) -> T:
        async with self._sem:
            return await coro_factory()


_inbox_drain = InboxDrain(max_concurrent=_DEFAULT_MAX_CONCURRENT)


async def run_inbox_app_ingest(coro_factory: Callable[[], Awaitable[T]]) -> T:
    return await _inbox_drain.run(coro_factory)
