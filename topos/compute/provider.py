"""Bounded local-provider ownership. Client cancellation is not provider drain."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
from dataclasses import dataclass, field
import json
import math
import uuid

import httpx

from topos.engine.overload import ComputeUnavailable
from .journal import OwnershipJournal, disk

ERROR_FRAME = b'{"error":"Compute is temporarily unavailable.","error_type":"compute_unavailable"}\n'


class Rejected(ComputeUnavailable):
    def __init__(self, reason: str):
        super().__init__("compute_busy" if reason in {"queue_full", "queue_expired"} else "compute_unavailable")
        self.reason = reason


@dataclass(eq=False)
class Operation:
    path: str
    body: bytes
    stream: bool
    deadline: float
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    headers: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    buffer: deque = field(default_factory=deque)
    buffered: int = 0
    state: str = "queued"
    detached: bool = False
    response_done: bool = False
    failed: bool = False
    timer: asyncio.TimerHandle | None = None

    def emit(self, data: bytes, limit: int):
        if self.detached:
            return
        if self.buffered + len(data) > limit:
            # A slow client must not block draining the provider or grow memory.
            self.detach()
            self.failed = True
            return
        self.buffer.append(data)
        self.buffered += len(data)
        self.changed.set()

    def detach(self):
        self.detached = self.response_done = True
        self.buffer.clear()
        self.buffered = 0
        self.changed.set()

    async def chunks(self):
        while True:
            while self.buffer:
                data = self.buffer.popleft()
                self.buffered -= len(data)
                yield data
            if self.response_done:
                if self.failed and self.stream:
                    yield ERROR_FRAME
                return
            self.changed.clear()
            await self.changed.wait()


class ProviderAuthority:
    """One event loop, one provider, one journal. Not a global GPU scheduler."""

    def __init__(self, journal: OwnershipJournal, client: httpx.AsyncClient, *, active=1,
                 waiting=4, queue_seconds=30.0, execution_seconds=300.0,
                 buffer_bytes=262144, frame_bytes=1048576):
        if (type(active) is not int or not 1 <= active <= 32
                or type(waiting) is not int or not 0 <= waiting <= 128
                or not math.isfinite(queue_seconds) or not 0 < queue_seconds <= 300
                or not math.isfinite(execution_seconds) or not 0 < execution_seconds <= 3600
                or type(buffer_bytes) is not int or not 1024 <= buffer_bytes <= 4194304
                or type(frame_bytes) is not int or not 1024 <= frame_bytes <= 4194304):
            raise ValueError("Invalid provider admission limits")
        self.journal, self.client = journal, client
        self.active_limit, self.waiting_limit = active, waiting
        self.queue_seconds, self.execution_seconds = queue_seconds, execution_seconds
        self.buffer_bytes, self.frame_bytes = buffer_bytes, frame_bytes
        self.uncertain = journal.unresolved
        self.running: dict[Operation, asyncio.Task] = {}
        self.waiting: deque[Operation] = deque()
        self.stopping = False
        self.storage_failed = False
        self.timings = {"queue_wait_seconds_total": 0.0, "provider_observation_seconds_total": 0.0}
        self.counts = Counter({key: 0 for key in (
            "offered", "admitted", "rejected_full", "rejected_unavailable", "queue_expired",
            "cancelled_queued", "detached_active", "dispatched", "terminal_observed", "uncertain",
            "aborted_queued", "slow_consumer",
        )})

    def snapshot(self):
        return {"boundary": "configured_provider_authority", "epoch": self.journal.epoch,
                "active": len(self.running), "waiting": len(self.waiting), "uncertain": self.uncertain,
                "detached_active": sum(job.detached for job in self.running),
                "active_limit": self.active_limit, "waiting_limit": self.waiting_limit,
                "stopping": self.stopping, "storage_failed": self.storage_failed,
                "global_gpu_bound": False, "hardware_drain_known": False,
                "timings": dict(self.timings),
                "totals": dict(self.counts)}

    def submit(self, path: str, body: bytes, stream: bool) -> Operation:
        self.counts["offered"] += 1
        if self.stopping or self.storage_failed or self.uncertain:
            self.counts["rejected_unavailable"] += 1
            raise Rejected("unresolved_execution")
        self._expire()
        if len(self.running) >= self.active_limit and len(self.waiting) >= self.waiting_limit:
            self.counts["rejected_full"] += 1
            raise Rejected("queue_full")
        job = Operation(path, body, stream, asyncio.get_running_loop().time() + self.queue_seconds)
        # Detached clients must not leave unobserved exception futures.
        job.headers.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self.counts["admitted"] += 1
        if len(self.running) < self.active_limit:
            self._start(job)
        else:
            self.waiting.append(job)
            job.timer = asyncio.get_running_loop().call_at(job.deadline, self._expire)
        return job

    def _reject(self, job, reason):
        if job.timer:
            job.timer.cancel()
        job.body = b""
        job.state = reason
        if not job.headers.done():
            job.headers.set_exception(Rejected(reason))
        job.detach()

    def _expire(self):
        now = asyncio.get_running_loop().time()
        for job in list(self.waiting):
            if job.deadline <= now:
                self.waiting.remove(job)
                self.counts["queue_expired"] += 1
                self._reject(job, "queue_expired")

    def detach(self, job):
        if job.detached:
            return
        if job in self.waiting:
            self.waiting.remove(job)
            self.counts["cancelled_queued"] += 1
            self._reject(job, "cancelled")
        else:
            if job in self.running:
                self.counts["detached_active"] += 1
            job.detach()

    def _start(self, job):
        if job.timer:
            job.timer.cancel()
        job.state = "executing"
        self.timings["queue_wait_seconds_total"] += max(0.0, asyncio.get_running_loop().time() - (job.deadline - self.queue_seconds))
        self.running[job] = asyncio.create_task(self._execute(job))

    def _pump(self):
        self._expire()
        if self.stopping or self.storage_failed or self.uncertain:
            while self.waiting:
                self.counts["aborted_queued"] += 1
                self._reject(self.waiting.popleft(), "unresolved_execution")
            return
        while self.waiting and len(self.running) < self.active_limit:
            self._start(self.waiting.popleft())

    async def _execute(self, job):
        terminal = False
        observed_at = None
        try:
            # Durable intent precedes the first possible provider request.
            await disk(self.journal.begin, job.id)
            self.counts["dispatched"] += 1
            observed_at = asyncio.get_running_loop().time()
            async with asyncio.timeout(self.execution_seconds):
                terminal = await self._read_provider(job)
        except (Exception, asyncio.CancelledError):
            # No raw provider exceptions, payloads or model names in diagnostics.
            pass
        finally:
            if observed_at is not None:
                self.timings["provider_observation_seconds_total"] += asyncio.get_running_loop().time() - observed_at
            try:
                cleanup = asyncio.create_task(disk(self.journal.finish, job.id, terminal))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Shutdown can race the terminal commit. Complete both the
                    # durable transition and in-memory cleanup before returning.
                    await cleanup
            except Exception:
                self.storage_failed = True
                terminal = False
            if terminal:
                self.counts["terminal_observed"] += 1
                job.state = "terminal"
            else:
                self.uncertain += 1
                self.counts["uncertain"] += 1
                job.state = "uncertain"
                job.failed = True
            if not job.headers.done():
                job.headers.set_exception(Rejected("provider_unavailable"))
            job.body = b""
            job.response_done = True
            job.changed.set()
            self.running.pop(job, None)
            self._pump()

    def _emit(self, job, data, limit):
        detached = job.detached
        job.emit(data, limit)
        if job.detached and not detached:
            self.counts["slow_consumer"] += 1
            self.counts["detached_active"] += 1

    async def _read_provider(self, job) -> bool:
        async with self.client.stream("POST", job.path, content=job.body,
                                      headers={"Content-Type": "application/json"}) as response:
            # An error HTTP status is not proof of provider-side termination.
            if response.status_code != 200:
                return False
            if job.stream:
                job.headers.set_result((200, "application/x-ndjson"))
            pending = bytearray()
            done = False
            # Do not ask httpx to coalesce small token frames into large chunks:
            # that would withhold the first tokens until the buffer fills.
            async for chunk in response.aiter_bytes():
                pending.extend(chunk)
                if job.stream:
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        if not line.strip():
                            continue
                        if len(line) > self.frame_bytes or done:
                            return False
                        value = json.loads(line)
                        if not isinstance(value, dict) or "error" in value:
                            return False
                        done = value.get("done") is True
                        self._emit(job, bytes(line) + b"\n", self.buffer_bytes)
                if len(pending) > self.frame_bytes:
                    return False
            if job.stream:
                if pending.strip():
                    if done:
                        return False
                    value = json.loads(pending)
                    if not isinstance(value, dict) or "error" in value:
                        return False
                    done = value.get("done") is True
                    self._emit(job, bytes(pending) + b"\n", self.buffer_bytes)
                return done
            value = json.loads(pending)
            if not isinstance(value, dict) or value.get("done") is not True or "error" in value:
                return False
            # JSON is returned atomically; never truncate it to the stream buffer.
            job.headers.set_result((200, "application/json"))
            self._emit(job, bytes(pending), self.frame_bytes)
            return True

    async def close(self, grace=5.0):
        self.stopping = True
        self._pump()
        tasks = list(self.running.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await disk(self.journal.close)
