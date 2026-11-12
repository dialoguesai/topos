"""The node's side of voluntary diagnostics sharing: the report, the preview and the consent clock.

Nothing here sends anything. This build has no collector, no network transport and no recipient policy: the one
instance the node runs (``sharing``, at the bottom) is built with neither, so ``available`` is false, both send
doors answer ``collection_unavailable``, and the only thing that works is the owner's local preview of the report.
``tests/diagnostics/test_sharing.py`` pins that (no sender on the node's instance, no network import in this
package, nothing in ``topos/`` that hands it one).

What a report may hold is decided in one place, ``report_bytes``: a fixed set of keys, numbers within bounds and
words from closed lists. Nothing is copied through from a request, an exception, a span or the database.

All async controls belong to the owner socket's event loop. Measurements may be recorded from any thread. A
transport, when one exists, MUST honor cancellation and MUST NOT retry, persist, enrich, redirect or buffer reports
on its own. See ``docs/DIAGNOSTICS_SHARING.md`` for what has to be true before one is wired in, and DISCLOSURE_RULES
DR-73 for the ruling.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Awaitable, Callable


class SharingError(Exception):
    pass


@dataclass(frozen=True)
class RecipientPolicy:
    """What the recipient promises about a report (how long it is kept, who may read it, what it is linked to), in
    words the owner is shown before a send. Supplied in code together with the sender by the change that adds a real
    recipient, never read from the environment or a setting. A send names the ``revision`` the owner was shown, so
    consent to one policy is not consent to the next."""
    revision: str
    retention: str
    access: str
    linkage: str

    def __post_init__(self):
        if not all(type(v) is str and v.strip() for v in vars(self).values()):
            raise ValueError("Complete recipient policy required")


def report_bytes(raw: dict) -> bytes:
    """Construct a new schema; never forward attributes, arbitrary keys or text."""
    out = {"schema_version": 1, "measurements": {}, "system": {}}
    for key, ceiling in {"http_requests": 10**9, "http_server_errors": 10**9,
                         "http_duration_ms": 10**12}.items():
        value = raw.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= ceiling:
            out["measurements"][key] = round(value, 3)
    for key, allowed in {"os_family": {"Darwin", "Linux", "Windows"},
                         "architecture": {"arm64", "aarch64", "x86_64", "AMD64"}}.items():
        value = raw.get(key)
        out["system"][key] = value if type(value) is str and value in allowed else "other"
    cpu = raw.get("cpu_count")
    if type(cpu) is int and 1 <= cpu <= 4096:
        out["system"]["cpu_count"] = cpu
    return json.dumps(out, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


#: What the owner is told the switch is. The description names every field of the report and nothing else, so it
#: has to change with ``report_bytes`` (the test file pins the report's keys and says so).
LABEL = "Share node diagnostics with Dialogues"
DESCRIPTION = ("Help troubleshoot Topos by sharing three totals since this node last started (HTTP requests, HTTP "
               "server errors, time spent on HTTP requests) and three facts about this computer (operating system "
               "family, CPU architecture, CPU count). Your messages, documents, prompts and answers are not "
               "included. Off unless you turn it on, and off again after a restart.")


class Sharing:
    def __init__(self, *, sender: Callable[[bytes], Awaitable[None]] | None = None,
                 policy: RecipientPolicy | None = None, clock=time.monotonic, interval=30):
        self._sender, self.policy = sender, policy
        self._clock, self._interval = clock, interval
        self._lock = threading.Lock()
        self._counts = Counter(http_requests=0, http_server_errors=0, http_duration_ms=0)
        self._controls = asyncio.Lock()
        self._preview_timer: asyncio.TimerHandle | None = None
        self._preview: tuple[str, bytes, float] | None = None
        self._deadline = 0.0
        self._session: asyncio.Task | None = None
        self._upload: asyncio.Task | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._generation = 0

    @property
    def available(self):
        return self._sender is not None and self.policy is not None

    def record_http(self, seconds: float, server_error: bool):
        with self._lock:
            self._counts["http_requests"] = min(10**9, self._counts["http_requests"] + 1)
            self._counts["http_server_errors"] = min(10**9, self._counts["http_server_errors"] + int(server_error))
            if math.isfinite(seconds) and seconds >= 0:
                self._counts["http_duration_ms"] = min(10**12, self._counts["http_duration_ms"] + seconds * 1000)

    def _snapshot(self):
        with self._lock:
            raw = dict(self._counts)
        raw.update(os_family=platform.system(), architecture=platform.machine(), cpu_count=os.cpu_count())
        return report_bytes(raw)

    def status(self):
        self._expire()
        return {"available": self.available, "sharing": self._deadline > self._clock(),
                "remaining_seconds": max(0, int(self._deadline - self._clock())),
                "policy": vars(self.policy) if self.policy else None,
                "label": LABEL, "description": DESCRIPTION}

    def preview(self):
        self._expire()
        body = self._snapshot()
        # Hash binds the exact UTF-8 upload, not a mutable subsequent snapshot.
        digest = hashlib.sha256(body).hexdigest()
        self._preview = (digest, body, self._clock() + 300)
        if self._preview_timer:
            self._preview_timer.cancel()
        self._preview_timer = asyncio.get_running_loop().call_later(300, self._clear_preview)
        return {"sha256": digest, "body": body.decode(), "expires_in_seconds": 300,
                "policy": vars(self.policy) if self.policy else None}

    def _clear_preview(self):
        self._preview = None
        if self._preview_timer:
            self._preview_timer.cancel()
            self._preview_timer = None

    def _ready(self, revision):
        if not self.available:
            raise SharingError("collection_unavailable")
        if revision != self.policy.revision:
            raise SharingError("policy_changed")

    def _expire(self):
        if self._deadline and self._clock() >= self._deadline:
            self._stop()
        if self._preview and self._clock() >= self._preview[2]:
            self._clear_preview()

    def _stop(self):
        self._generation += 1
        self._deadline = 0
        self._clear_preview()
        if self._timer:
            self._timer.cancel()
            self._timer = None
        for task in (self._session, self._upload):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        self._session = self._upload = None

    async def _revoke(self):
        tasks = [t for t in (self._session, self._upload) if t]
        self._stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.status()

    async def revoke(self):
        async with self._controls:
            return await self._revoke()

    async def _send(self, body, generation):
        self._expire()
        if generation != self._generation:
            raise SharingError("sharing_stopped")
        if self._upload and not self._upload.done():
            raise SharingError("upload_in_progress")
        self._upload = asyncio.create_task(self._sender(body))
        try:
            await asyncio.wait_for(self._upload, timeout=10)
        except asyncio.CancelledError:
            raise SharingError("sharing_stopped") from None
        except Exception:
            raise SharingError("upload_failed") from None

    async def send_preview(self, digest, revision):
        self._ready(revision)
        self._expire()
        if not self._preview or digest != self._preview[0]:
            raise SharingError("preview_missing_or_expired")
        body = self._preview[1]
        self._clear_preview()  # Single use; no retries or pending queue.
        await self._send(body, self._generation)
        return {"sent": True, "sha256": digest}

    async def start_hour(self, revision):
        async with self._controls:
            self._ready(revision)
            await self._revoke()
            self._deadline = self._clock() + 3600
            generation = self._generation
            self._timer = asyncio.get_running_loop().call_later(3600, self._stop)
            self._session = asyncio.create_task(self._run(generation))
            return self.status()

    async def _run(self, generation):
        try:
            while generation == self._generation:
                await asyncio.sleep(self._interval)
                self._expire()
                if generation != self._generation:
                    return
                await self._send(self._snapshot(), generation)
        except (SharingError, asyncio.CancelledError):
            # Fail closed: no retry queue, and no silent resumption after failure.
            if generation == self._generation:
                self._stop()


#: The node's one instance. Deliberately no sender and no policy: no recipient exists yet, so this build cannot
#: send a report, and it shows the owner no promise about retention or readers that nothing enforces. A recipient is
#: added by passing both here in code, after the requirements in docs/DIAGNOSTICS_SHARING.md are met; no environment
#: variable, setting or request can supply either.
sharing = Sharing()


class DiagnosticsMeasurements:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start, failed = time.monotonic(), False

        async def measured_send(message):
            nonlocal failed
            if message["type"] == "http.response.start":
                failed = message["status"] >= 500
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        except Exception:
            failed = True
            raise
        finally:
            sharing.record_http(time.monotonic() - start, failed)
