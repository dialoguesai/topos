"""Bounded per-process lifetime for budgeted HTTP; not provider/GPU admission.

A native getaddrinfo cannot be killed. Keep its permit until the actual future
finishes, even after the HTTP caller returns. No queued resolver submissions and
no per-request executor shutdown. Individual clients retain isolated credentials.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
import contextvars
import os
import socket
import threading
import time

from .overload import ComputeUnavailable


class BudgetTransport:
    def __init__(self, *, requests=8, resolvers=4):
        if (type(requests) is not int or not 1 <= requests <= 64
                or type(resolvers) is not int or not 1 <= resolvers <= 16):
            raise ValueError("Invalid budget transport limits")
        self._pid = os.getpid()
        self._request_limit, self._resolver_limit = requests, resolvers
        self._lock = threading.RLock()
        self._closed = False
        self._calls = {}  # concurrent result -> (budget, asyncio task or None)
        self._dns = set()  # native futures, not cancellable async waiters
        self._resolver = ThreadPoolExecutor(max_workers=resolvers, thread_name_prefix="budget-dns")
        # Own a stdlib loop regardless of uvicorn/uvloop policy. A C loop
        # need not allow overriding getaddrinfo or use the same resolver path.
        self._loop = asyncio.SelectorEventLoop()
        # HTTPX/AnyIO calls this public loop method. It must not use the
        # unbounded default-executor queue or release slots on waiter cancel.
        self._loop.getaddrinfo = self._resolve
        self._thread = threading.Thread(target=self._serve, name="budget-http", daemon=True)
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _resolve(self, host, port, *, family=0, type=0, proto=0, flags=0):
        from .request_budget import check_budget
        check_budget()
        with self._lock:
            if self._closed:
                raise ComputeUnavailable("compute_unavailable")
            if len(self._dns) >= self._resolver_limit:
                raise ComputeUnavailable("compute_busy")
            future = self._resolver.submit(socket.getaddrinfo, host, port, family, type, proto, flags)
            self._dns.add(future)
            future.add_done_callback(self._resolved)
        wrapped = asyncio.wrap_future(future)
        # Detached DNS exceptions must still be observed, without retaining
        # request bodies/credentials in resolver diagnostics.
        wrapped.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        result = await asyncio.shield(wrapped)
        check_budget()  # A late DNS result must not start a new connection.
        return result

    def _resolved(self, future):
        with self._lock:
            self._dns.discard(future)

    async def _invoke(self, budget, call):
        from .request_budget import await_budgeted
        budget.check()
        return await await_budgeted(call(), budget)

    def _start(self, result, budget, call):
        with self._lock:
            if self._closed:
                self._calls.pop(result, None)
                result.set_exception(ComputeUnavailable("compute_unavailable"))
                self._stop_if_drained()
                return
            task = self._loop.create_task(self._invoke(budget, call))
            self._calls[result] = (budget, task)
            # Task completion, including cancellation before its first bytecode,
            # owns release. A cancelled concurrent future is NOT task drain.
            task.add_done_callback(lambda task: self._finished(result, task))

    def _finished(self, result, task):
        try:
            value = task.result()
        except asyncio.CancelledError:
            result.set_exception(ComputeUnavailable("compute_unavailable"))
        except BaseException as error:
            result.set_exception(error)
        else:
            result.set_result(value)
        finally:
            with self._lock:
                self._calls.pop(result, None)
                self._stop_if_drained()

    def _stop_if_drained(self):
        # Called on the loop thread. Native DNS may still run: its future owns
        # its slot independently and may complete after this loop closes.
        if self._closed and not self._calls:
            self._loop.stop()

    def execute(self, budget, call):
        """Sync worker entry. No waiting queue; caller can stop before cleanup."""
        budget.check()
        if os.getpid() != self._pid:
            raise ComputeUnavailable("compute_unavailable")
        with self._lock:
            if self._closed or not self._thread.is_alive():
                raise ComputeUnavailable("compute_unavailable")
            if len(self._calls) >= self._request_limit:
                raise ComputeUnavailable("compute_busy")
            result = Future()
            self._calls[result] = (budget, None)
            context = contextvars.copy_context()
            self._loop.call_soon_threadsafe(context.run, self._start, result, budget, call)
        while True:
            try:
                value = result.result(timeout=min(.025, budget.remaining()))
            except FutureTimeout:
                if result.done():
                    budget.check()
                    raise
                continue
            except BaseException:
                budget.check()
                raise
            budget.check()
            return value

    def snapshot(self):
        with self._lock:
            return {"boundary": "budgeted_http_resources_per_process", "closed": self._closed,
                    "requests_active": len(self._calls), "request_limit": self._request_limit,
                    "resolvers_active": len(self._dns), "resolver_limit": self._resolver_limit,
                    "transport_thread_alive": self._thread.is_alive(), "provider_drain_known": False}

    def _cancel_all(self):
        with self._lock:
            for budget, task in self._calls.values():
                budget.cancel()
                if task is not None:
                    task.cancel()
            self._stop_if_drained()

    def close(self, timeout=1.0):
        """Refuse new work; bounded observation of HTTP cleanup, never kill DNS.

        Remains closed permanently. A stalled native resolver remains accounted
        for, and can delay interpreter exit (ThreadPoolExecutor's exit join).
        No replacement runtime is automatically created to bypass those limits.
        """
        deadline = time.monotonic() + max(0, timeout)
        with self._lock:
            if not self._closed:
                self._closed = True
                for budget, _ in self._calls.values():
                    budget.cancel()
                self._loop.call_soon_threadsafe(self._cancel_all)
                self._resolver.shutdown(wait=False, cancel_futures=True)
        if threading.current_thread() is not self._thread:
            self._thread.join(max(0, deadline - time.monotonic()))
        return self.snapshot()


_runtime = None
_runtime_closed = False
_runtime_lock = threading.Lock()


def get_transport():
    global _runtime
    # No automatic post-fork resurrection of inherited threads/locks.
    if _runtime is not None and _runtime._pid != os.getpid():
        raise ComputeUnavailable("compute_unavailable")
    with _runtime_lock:
        if _runtime_closed:
            raise ComputeUnavailable("compute_unavailable")
        if _runtime is None:
            _runtime = BudgetTransport()
        return _runtime


def close_transport(timeout=1.0):
    """Explicit process-lifecycle hook; no startup or provider management."""
    global _runtime_closed
    if _runtime is not None and _runtime._pid != os.getpid():
        raise ComputeUnavailable("compute_unavailable")
    with _runtime_lock:
        _runtime_closed = True
        runtime = _runtime
    return runtime.close(timeout) if runtime is not None else None
