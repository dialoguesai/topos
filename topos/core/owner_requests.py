"""The owner's request path on the node: its own executor, its own count, and the yield point background work consults.

Why. After every restart the node rebuilds each share's search index (`permissions_v2/index_rebuilds.py`), re-embedding
text on the CPU, and derivation batches spawn entity-graph rebuilds every few minutes. On the owner's node on
28 Oct 2026 (1.5.29): three rebuilds of 5.2, 6.8 and 6.2 minutes in the first 20 minutes, 419 embedding calls, one
``_rebuild_once`` write-gate hold of 22 s. During that time ``get_device_info`` was answered from its stale snapshot
28 times in 30 minutes because the real handler took over 5 s, the database probe reported the thread pool
saturated, ``claim_next_job`` waited up to 20 s, and the owner's calibration screen failed: the control plane answered
the app 502 with the node's error, or the gallery drew without its claims card when only that frame timed out.

The doors themselves are fast (``truth_checks.claim_cards(500)``: 36 cards in 8 ms on the live database;
``weekly_stats`` 29 ms; the calibration catalog one small SELECT). The loss was queueing: every owner frame's
database read went through ``asyncio.to_thread``, which is the loop's default pool, the same pool the derivation
batches, the embedding catch-up and the graph rebuild's waiter run on; with its workers busy a read of 8 ms waited
seconds for a worker. Three things here, one module so the request path has one place:

1. **Its own executor.** A frame of an owner request type (``is_owner_request``: the owner's ``signal_*``
   inspection doors, the truth lane's ``truth_*`` doors, the calibration screen's ``calibration_*``, and
   ``get_device_info``) is handled inside ``owner_request``, and every blocking step of its handling that goes
   through ``run_off_loop`` (``core.handlers.common.run_db_read`` / ``run_db_write``, the dispatcher's ``off_loop``
   handlers) runs on ``executor()``: a small pool of its own that index rebuilds,
   embedding jobs and derivation batches never use, so a saturated default pool cannot queue them. Outside an
   owner request ``run_off_loop`` is ``asyncio.to_thread``, exactly as before. The executor's threads resolve their
   own connections (``core.state.get_db_connection`` is thread-local), so the rule of ``run_db_read`` holds. The
   device read's storage snapshot holds no thread at all (``services.local.StorageSnapshot``).
2. **A count.** ``pending()`` is how many owner frames are being handled now; it is raised when one enters and
   lowered when it leaves, however it leaves.
3. **A yield point.** ``pace()`` is what a long background thread calls between its batches: while an owner frame
   is pending it sleeps, in small slices, at most ``PACE_LIMIT_SECONDS`` per call, and returns at once when nothing is
   pending, so a six-minute rebuild stretches around the owner's frames instead of blocking them. It never waits on
   an event-loop thread or while the calling thread holds the write gate: a yield there would hold back what it is
   yielding to. The share-index build consults it between shares and between batches within a share
   (``index_rebuilds.IndexRebuilds._run``, ``refresh_loop``'s restore, ``search_index.SearchIndexService``).

Kill switches, read at each use so a change in the node's environment is live at its next restart:
``TOPOS_OWNER_REQUEST_EXECUTOR=off`` sends owner frames back to the default pool;
``TOPOS_OWNER_REQUEST_PACING=off`` makes ``pace`` a no-op. ``TOPOS_OWNER_REQUEST_WORKERS`` sizes the executor
(default ``DEFAULT_WORKERS``). No table, no door, no setting beyond these.

Logs carry counts and seconds only; nothing here reads a frame's payload.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator, Optional, TypeVar

logger = logging.getLogger("topos.core.owner_requests")

_T = TypeVar("_T")

#: The frame types this path serves. A closed list by prefix and name: the owner's inspection and curation doors,
#: the truth lane's owner doors, the calibration screen, and the device read the shell and the app poll.
OWNER_REQUEST_PREFIXES: tuple[str, ...] = ("signal_", "truth_", "calibration_")
OWNER_REQUEST_TYPES: frozenset[str] = frozenset({"get_device_info"})

EXECUTOR_ENV = "TOPOS_OWNER_REQUEST_EXECUTOR"
PACING_ENV = "TOPOS_OWNER_REQUEST_PACING"
WORKERS_ENV = "TOPOS_OWNER_REQUEST_WORKERS"
#: Enough for the app's screens to ask a few things at once; small enough that the pool itself never becomes the
#: node's background worker. Each thread keeps one SQLite connection of its own.
DEFAULT_WORKERS = 4
#: The most one ``pace`` call waits, so a frame that never ends (a leak, a long owner read) cannot stop a rebuild.
PACE_LIMIT_SECONDS = 5.0
PACE_POLL_SECONDS = 0.01

_OFF = frozenset({"0", "false", "off", "no"})


def _on(name: str) -> bool:
    return os.environ.get(name, "on").strip().lower() not in _OFF


def executor_enabled() -> bool:
    return _on(EXECUTOR_ENV)


def pacing_enabled() -> bool:
    return _on(PACING_ENV)


def is_owner_request(msg_type: Any) -> bool:
    """Whether a frame of this type is handled on the owner's request path."""
    kind = str(msg_type or "").strip().lower()
    return kind in OWNER_REQUEST_TYPES or kind.startswith(OWNER_REQUEST_PREFIXES)


# -- the count -------------------------------------------------------------------------------------------------

_in_request: contextvars.ContextVar[bool] = contextvars.ContextVar("topos_owner_request", default=False)
_lock = threading.Lock()
_pending = 0
_handled = 0
#: ``pace``'s own numbers: calls that waited, seconds waited in all, the longest wait. Diagnostics and measurement.
_yields = {"count": 0, "seconds": 0.0, "longest": 0.0}


def in_owner_request() -> bool:
    """True inside ``owner_request``'s handling, in every task and worker that inherits its context."""
    return _in_request.get() is True


def pending() -> int:
    """The owner frames being handled now."""
    return _pending


def handled() -> int:
    """Owner frames handled since the process started."""
    return _handled


@contextlib.contextmanager
def owner_request(msg_type: Any) -> Iterator[bool]:
    """Mark the handling of one frame: counted pending from entry to exit, however it exits, and every blocking step
    inside that goes through ``run_off_loop`` takes the owner executor. Yields whether the frame is one of this
    path's (a frame of any other type is handled as before, and this marks nothing)."""
    global _pending, _handled
    if not is_owner_request(msg_type):
        yield False
        return
    token = _in_request.set(True)
    with _lock:
        _pending += 1
        _handled += 1
    try:
        yield True
    finally:
        with _lock:
            _pending = max(0, _pending - 1)
        _in_request.reset(token)


# -- the executor ----------------------------------------------------------------------------------------------

_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _workers() -> int:
    raw = os.environ.get(WORKERS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_WORKERS
    except ValueError:
        return DEFAULT_WORKERS


def executor() -> ThreadPoolExecutor:
    """The owner request path's pool, made on first use. Its threads are named ``owner-request_<n>``."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="owner-request")
            logger.info("owner request executor: %d workers", _executor._max_workers)
        return _executor


async def run_off_loop(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """``asyncio.to_thread`` for a request's blocking work, on the owner executor inside an owner request.

    The context (the principal, the owner-request mark) goes with the call, as ``to_thread`` carries it.
    """
    if not (_in_request.get() is True and executor_enabled()):
        return await asyncio.to_thread(fn, *args, **kwargs)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    return await loop.run_in_executor(executor(), functools.partial(context.run, fn, *args, **kwargs))


# -- the yield point -------------------------------------------------------------------------------------------

def _on_a_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _holds_the_write_gate() -> bool:
    try:
        from topos.storage.db.write_gate import db_write_lock

        return bool(db_write_lock()._is_owned())
    except Exception:  # noqa: BLE001 -- a thread that cannot say does not wait
        return True


def pace(*, limit: float = PACE_LIMIT_SECONDS, poll: float = PACE_POLL_SECONDS, clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> float:
    """A background thread's yield point: wait while an owner frame is pending, at most ``limit`` seconds.

    Returns at once (0.0) when nothing is pending, with the switch off, on an event-loop thread, or while this
    thread holds the write gate. Returns the seconds waited.
    """
    if _pending <= 0 or not pacing_enabled():
        return 0.0
    if _on_a_loop() or _holds_the_write_gate():
        return 0.0
    started = clock()
    waited = 0.0
    while _pending > 0 and waited < limit:
        sleep(poll)
        waited = clock() - started
    with _lock:
        _yields["count"] += 1
        _yields["seconds"] += waited
        _yields["longest"] = max(_yields["longest"], waited)
    return waited


def yields() -> dict:
    """``pace``'s numbers so far: ``count`` (calls that waited), ``seconds`` (in all), ``longest`` (one wait)."""
    with _lock:
        return dict(_yields)


def _reset_for_tests() -> None:
    global _pending, _handled
    with _lock:
        _pending = 0
        _handled = 0
        _yields.update(count=0, seconds=0.0, longest=0.0)
