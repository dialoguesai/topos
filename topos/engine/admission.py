"""Bounded HTTP worker admission, not a global GPU scheduler.

Only active jobs enter the executor. Waiting jobs live in a bounded FIFO with
independent expiry. Caller cancellation cannot release a running worker's slot.
A worker returning (including an upstream timeout) is NOT proof of GPU drain.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .overload import ComputeUnavailable


class AdmissionRejected(ComputeUnavailable):
    def __init__(self, reason: str) -> None:
        if reason not in {"queue_full", "queue_expired", "stopping"}:
            raise ValueError("Unknown admission rejection reason")
        super().__init__("compute_unavailable" if reason == "stopping" else "compute_busy")
        self.reason = reason


@dataclass(eq=False)
class _Job:
    call: Callable[[], Any]
    context: contextvars.Context = field(default_factory=contextvars.copy_context)
    result: Future = field(default_factory=Future)
    state: str = "queued"
    deadline: float = 0.0
    detached: bool = False


class EngineAdmission:
    """One application process's Engine HTTP worker budget; thread/loop safe."""

    def __init__(self, *, active: int = 1, waiting: int = 4, queue_timeout: float = 30.0) -> None:
        if (type(active) is not int or type(waiting) is not int
                or not 1 <= active <= 32 or not 0 <= waiting <= 128
                or not 0 < queue_timeout <= 300):
            raise ValueError("Invalid Engine HTTP admission limits")
        self.active_limit, self.waiting_limit = active, waiting
        self.queue_timeout = queue_timeout
        self._pool = ThreadPoolExecutor(max_workers=active, thread_name_prefix="engine-http")
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._waiting: deque[_Job] = deque()
        self._active = 0
        self._detached = 0
        self._closed = False
        self._counts: Counter = Counter({name: 0 for name in (
            "offered", "admitted", "rejected_stopping", "rejected_full",
            "queue_expired", "stopping", "dispatched", "cancelled_before_worker_start",
            "worker_failed", "worker_returned", "cancelled_queued", "detached_active",
        )})
        # One expiry observer per dispatcher, not one thread per waiting job.
        self._expiry = threading.Thread(target=self._expiry_loop, name="engine-http-expiry", daemon=True)
        self._expiry.start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"boundary": "engine_http_workers_per_process", "global_gpu_bound": False,
                    "backend_drain_known": False, "active_limit": self.active_limit,
                    "waiting_limit": self.waiting_limit, "queue_timeout_seconds": self.queue_timeout,
                    "active": self._active, "waiting": len(self._waiting),
                    "detached_active": self._detached, "stopping": self._closed,
                    "totals": dict(self._counts)}

    async def execute(self, call: Callable[[], Any]) -> Any:
        job = self._submit(call)
        wrapped = asyncio.wrap_future(job.result)
        # A cancellation can race an expiry/worker exception. Observe the
        # detached wrapper too; awaiting it still raises the same exception.
        wrapped.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            self._cancel(job)
            raise

    def _submit(self, call: Callable[[], Any]) -> _Job:
        with self._lock:
            self._counts["offered"] += 1
            if self._closed:
                self._counts["rejected_stopping"] += 1
                raise AdmissionRejected("stopping")
            # Timers may be delayed by the OS. Expired work must not occupy the
            # admission budget or be submitted to the executor in that interval.
            self._expire_waiters_locked()
            if self._active >= self.active_limit and len(self._waiting) >= self.waiting_limit:
                self._counts["rejected_full"] += 1
                raise AdmissionRejected("queue_full")
            job = _Job(call=call, deadline=time.monotonic() + self.queue_timeout)
            self._counts["admitted"] += 1
            if self._active < self.active_limit:
                self._start_locked(job)
            else:
                self._waiting.append(job)
                self._changed.notify_all()
            return job

    def _expiry_loop(self) -> None:
        with self._changed:
            while not self._closed:
                self._expire_waiters_locked()
                delay = max(0.0, self._waiting[0].deadline - time.monotonic()) if self._waiting else None
                self._changed.wait(delay)

    def _expire_waiters_locked(self) -> None:
        now = time.monotonic()
        for job in list(self._waiting):
            if job.deadline <= now:
                self._waiting.remove(job)
                self._reject_waiter_locked(job, "queue_expired")

    def _reject_waiter_locked(self, job: _Job, reason: str) -> None:
        job.state = "finished"
        self._counts[reason] += 1
        job.result.set_exception(AdmissionRejected(reason))

    def _start_locked(self, job: _Job) -> None:
        job.state = "active"
        self._active += 1
        try:
            future = self._pool.submit(self._invoke, job)
        except Exception as exc:
            self._active -= 1
            job.state = "finished"
            self._counts["worker_failed"] += 1
            job.result.set_exception(exc)
            return
        # This may run inline if the worker already finished: use an RLock.
        future.add_done_callback(lambda completed: self._finish(job, completed))

    def _invoke(self, job: _Job) -> Any:
        with self._lock:
            if job.detached:
                self._counts["cancelled_before_worker_start"] += 1
                return None
            self._counts["dispatched"] += 1
        return job.context.run(job.call)

    def _finish(self, job: _Job, completed: Future) -> None:
        with self._lock:
            self._active -= 1
            if job.detached:
                self._detached -= 1
            job.state = "finished"
            error = completed.exception()
            self._counts["worker_failed" if error else "worker_returned"] += 1
            if not job.result.done():
                if error:
                    job.result.set_exception(error)
                else:
                    job.result.set_result(completed.result())
            self._expire_waiters_locked()
            while not self._closed and self._waiting and self._active < self.active_limit:
                self._start_locked(self._waiting.popleft())
            self._changed.notify_all()

    def _cancel(self, job: _Job) -> None:
        with self._lock:
            if job.state == "queued":
                self._waiting.remove(job)
                job.state = "finished"
                self._counts["cancelled_queued"] += 1
                job.result.cancel()
            elif job.state == "active" and not job.detached:
                job.detached = True
                self._detached += 1
                self._counts["detached_active"] += 1
                job.result.cancel()

    def close(self, timeout: float = 5.0) -> int:
        """Stop admissions, reject waiting jobs, wait boundedly for local workers.

        Return still-active workers; neither shutdown nor caller cancellation can
        kill a Python thread or establish termination at a remote provider.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._changed:
            self._closed = True
            self._changed.notify_all()
            while self._waiting:
                self._reject_waiter_locked(self._waiting.popleft(), "stopping")
            while self._active and time.monotonic() < deadline:
                self._changed.wait(max(0.0, deadline - time.monotonic()))
            remaining = self._active
        self._pool.shutdown(wait=False)
        self._expiry.join(max(0.0, deadline - time.monotonic()))
        return remaining
