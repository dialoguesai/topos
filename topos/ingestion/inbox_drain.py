"""Async drain queue for Usage Inbox app_ingest deliveries."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")

_DEFAULT_MAX_CONCURRENT = 10
_MAX_WRITE_ID_LOCK_HOLD_SECONDS = 120.0

logger = logging.getLogger("topos.ingestion.inbox_drain")


class InboxDrain:
    """Limits concurrent app_ingest processing for inbox flush workloads."""

    def __init__(self, *, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
        self._sem = asyncio.Semaphore(max(1, int(max_concurrent)))
        self._write_id_locks: Dict[str, asyncio.Lock] = {}
        self._write_id_lock_started: Dict[str, float] = {}

    def _lock_for_write_id(self, write_id: str) -> asyncio.Lock:
        wid = str(write_id or "").strip()
        lock = self._write_id_locks.get(wid)
        if lock is None:
            lock = asyncio.Lock()
            self._write_id_locks[wid] = lock
        return lock

    async def run(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        *,
        write_id: Optional[str] = None,
    ) -> T:
        """Run inbox ingest with global concurrency cap and optional per-write_id lock."""
        wid = str(write_id or "").strip() or None

        async def _guarded() -> T:
            async with self._sem:
                if not wid:
                    return await coro_factory()
                lock = self._lock_for_write_id(wid)
                async with lock:
                    started = time.monotonic()
                    self._write_id_lock_started[wid] = started
                    try:
                        return await asyncio.wait_for(
                            coro_factory(),
                            timeout=_MAX_WRITE_ID_LOCK_HOLD_SECONDS,
                        )
                    finally:
                        self._write_id_lock_started.pop(wid, None)
                        if time.monotonic() - started > _MAX_WRITE_ID_LOCK_HOLD_SECONDS * 0.9:
                            logger.warning(
                                "usage_inbox write_id lock held long write_id=%s seconds=%.1f",
                                wid[:8],
                                time.monotonic() - started,
                            )

        return await _guarded()


_inbox_drain = InboxDrain(max_concurrent=_DEFAULT_MAX_CONCURRENT)


async def run_inbox_app_ingest(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    write_id: Optional[str] = None,
) -> T:
    """Serialize concurrent deliveries for the same write_id; cap global concurrency at 10."""
    return await _inbox_drain.run(coro_factory, write_id=write_id)
