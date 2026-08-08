"""Process-wide SQLite write serialization and busy retry.

SQLite allows only one writer at a time (even in WAL). Overlapping ingest,
SIGNAL_DERIVE, graph refresh, and pipeline bookkeeping previously raced and
raised ``database is locked``. Every path that ``commit()``s or runs
``BEGIN IMMEDIATE`` should hold :func:`with_db_write` (or use
:func:`commit_connection` / :func:`batched_writes`).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_WRITE_LOCK = threading.RLock()
_defer_commit: ContextVar[bool] = ContextVar("sqlite_defer_commit", default=False)

_BUSY_MAX_ATTEMPTS = 5
_BUSY_BASE_DELAY_S = 0.05
_BUSY_MAX_DELAY_S = 0.5


def db_write_lock() -> threading.RLock:
    """Return the process-wide SQLite write lock (reentrant)."""
    return _WRITE_LOCK


#: Threshold above which holding the gate is reported. A rebuild that holds it
#: for 77s (observed 2026-07-30) starves every other writer, and if the event
#: loop is one of them the control-plane websocket misses its keepalive and the
#: node looks offline.
_SLOW_HOLD_WARN_S = 5.0


def _on_event_loop() -> bool:
    """True when called from a thread running an asyncio event loop.

    ``_WRITE_LOCK`` is a blocking OS lock, so taking it here stalls EVERY
    coroutine on this loop — including the control-plane keepalive. Such a call
    is a bug: DB work belongs in ``asyncio.to_thread``.
    """
    try:
        import asyncio

        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
    except Exception:  # noqa: BLE001 — diagnostics must never break a write
        return False


#: One warning per call site, then silence for this long. A poll loop hitting
#: the gate four times a second turned this diagnostic into its own problem:
#: thousands of WARNING lines with a stack trace each, drowning the log it was
#: meant to make readable. Per-site so a second offender is never masked by a
#: noisy first one.
_LOOP_WARN_INTERVAL_S = 300.0
_loop_warn_seen: dict[str, float] = {}
_loop_warn_lock = threading.Lock()


def _caller_site() -> str:
    """Nearest stack frame outside this module (and contextlib).

    `with_db_write` is a @contextmanager, so contextlib's __enter__ sits
    between us and the real caller; reporting "contextlib.py:135" would make
    a warning unactionable.
    """
    stack = traceback.extract_stack(limit=12)[:-2]
    for frame in reversed(stack):
        name = frame.filename.rsplit("/", 1)[-1]
        if name in ("write_gate.py", "contextlib.py"):
            continue
        return f"{name}:{frame.lineno} in {frame.name}"
    return "unknown"


def _rate_limited(key: str) -> Optional[bool]:
    """True on a site's first report, False on a due repeat, None to suppress."""
    now = time.monotonic()
    with _loop_warn_lock:
        last = _loop_warn_seen.get(key)
        if last is not None and (now - last) < _LOOP_WARN_INTERVAL_S:
            return None
        _loop_warn_seen[key] = now
        return last is None


def _warn_loop_acquisition() -> None:
    """Report a gate acquisition on the event-loop thread, at most periodically.

    Not raised: failing a write to punish a bad call site is worse than the
    stall it warns about.
    """
    site = _caller_site()
    first_time = _rate_limited(f"loop:{site}")
    if first_time is None:
        return

    logger.warning(
        "[WRITE_GATE] acquired on the event-loop thread from %s — this blocks "
        "every coroutine including the control-plane keepalive; move this DB "
        "work into asyncio.to_thread%s",
        site,
        "" if first_time else f" (repeats suppressed for {int(_LOOP_WARN_INTERVAL_S)}s)",
        stack_info=first_time,
    )


def _warn_ungated_transaction() -> None:
    """Report a commit whose writes ran OUTSIDE the gate, at most periodically.

    In WAL the first write statement takes SQLite's process-wide write lock at
    execute time. A caller that executes ungated and only enters the gate here
    holds that lock while queuing — and whoever holds the gate now blocks on
    SQLite until busy_timeout. That lock-order inversion stretched every
    rebuild write into a 30s busy wait on 2026-08-07; the fix at the call site
    is with_db_write around the writes AND the commit.
    """
    site = _caller_site()
    first_time = _rate_limited(f"ungated:{site}")
    if first_time is None:
        return

    logger.warning(
        "[WRITE_GATE] commit from %s arrived with an open write transaction "
        "but without holding the gate — its statements took SQLite's write "
        "lock ungated, which can deadlock-until-busy_timeout against the "
        "gate holder; wrap the writes AND the commit in with_db_write%s",
        site,
        "" if first_time else f" (repeats suppressed for {int(_LOOP_WARN_INTERVAL_S)}s)",
        stack_info=first_time,
    )


def reset_loop_warning_state() -> None:
    """Forget which call sites have warned. For tests."""
    with _loop_warn_lock:
        _loop_warn_seen.clear()


@contextmanager
def with_db_write() -> Iterator[None]:
    """Serialize a write-critical section across threads."""
    if _on_event_loop():
        _warn_loop_acquisition()
    waited_at = time.monotonic()
    with _WRITE_LOCK:
        waited = time.monotonic() - waited_at
        held_at = time.monotonic()
        try:
            yield
        finally:
            held = time.monotonic() - held_at
            if held >= _SLOW_HOLD_WARN_S or waited >= _SLOW_HOLD_WARN_S:
                # Name the section: an anonymous "held=109.2s" (2026-08-08)
                # forced a log-archaeology session to find the phase.
                logger.warning(
                    "[WRITE_GATE] slow section at %s: waited=%.1fs held=%.1fs — "
                    "other writers were blocked for this long",
                    _caller_site(),
                    waited,
                    held,
                )


class WriteGateDeferred(Exception):
    """A cooperative writer stepped aside instead of taking the gate."""


#: How long one cooperative acquisition attempt blocks before re-checking
#: ``should_defer``. Short enough that a derive starting mid-wait is noticed
#: promptly; long enough not to spin.
_COOPERATIVE_SLICE_S = 2.0


@contextmanager
def with_db_write_cooperative(
    should_defer: Callable[[], bool],
    *,
    slice_s: float = _COOPERATIVE_SLICE_S,
) -> Iterator[None]:
    """Take the gate like :func:`with_db_write`, but as a LOW-PRIORITY writer.

    For background sections that hold the gate for a long time (the entity-graph
    rebuild held it 77–156s). Such a writer must never win the gate against an
    in-flight derivation batch: on 2026-08-07 that interleaving left the event
    loop blocked behind the rebuild and froze the node until SIGKILL.

    The wait is polled in ``slice_s`` slices; whenever ``should_defer()`` is
    true — before waiting, while waiting, or by the time the gate is finally
    acquired — :class:`WriteGateDeferred` is raised (releasing the gate if
    held) so the caller can reschedule instead of contending.
    """
    if _on_event_loop():
        _warn_loop_acquisition()
    waited_at = time.monotonic()
    while True:
        if should_defer():
            raise WriteGateDeferred("higher-priority writer active")
        if _WRITE_LOCK.acquire(timeout=max(0.05, slice_s)):
            break
    waited = time.monotonic() - waited_at
    held_at = time.monotonic()
    try:
        # A derivation that started during the final acquire slice would now
        # queue behind this whole section — the exact convoy this exists to
        # prevent. Re-check with the gate held.
        if should_defer():
            raise WriteGateDeferred("higher-priority writer started while waiting")
        yield
    finally:
        _WRITE_LOCK.release()
        held = time.monotonic() - held_at
        if held >= _SLOW_HOLD_WARN_S or waited >= _SLOW_HOLD_WARN_S:
            logger.warning(
                "[WRITE_GATE] slow section: waited=%.1fs held=%.1fs — other "
                "writers were blocked for this long",
                waited,
                held,
            )


def is_busy_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def sqlite_retry_busy(
    fn: Callable[[], T],
    *,
    attempts: int = _BUSY_MAX_ATTEMPTS,
) -> T:
    """Retry ``fn`` on SQLITE_BUSY / database-is-locked with exponential backoff."""
    delay = _BUSY_BASE_DELAY_S
    last: Optional[BaseException] = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_busy_error(exc) or i + 1 >= attempts:
                raise
            last = exc
            logger.debug(
                "sqlite busy retry attempt=%d/%d delay=%.3fs: %s",
                i + 1,
                attempts,
                delay,
                exc,
            )
            time.sleep(delay)
            delay = min(delay * 2, _BUSY_MAX_DELAY_S)
    assert last is not None
    raise last


def begin_immediate(conn: sqlite3.Connection) -> None:
    """``BEGIN IMMEDIATE`` with busy retry, clearing any leaked implicit transaction.

    In Python's legacy isolation mode even a 0-row UPDATE opens an implicit
    transaction, so one writer that returns without committing leaves the
    connection in-transaction and every later ``BEGIN`` on it fails with
    "cannot start a transaction within a transaction" (which is how every
    topic_clusters batch died on 2026-08-06). The rollback here contains that
    class of leak to a warning instead of a poisoned connection.
    """
    if getattr(conn, "in_transaction", False):
        logger.warning(
            "begin_immediate: connection carried an open transaction — a writer "
            "returned without commit/rollback; rolling it back"
        )
        conn.rollback()
    sqlite_retry_busy(lambda: conn.execute("BEGIN IMMEDIATE"))


def commit_connection(conn: sqlite3.Connection) -> None:
    """Commit under the write gate + busy retry, or no-op inside :func:`batched_writes`."""
    if _defer_commit.get():
        return

    def _commit() -> None:
        conn.commit()

    # Same diagnostic as with_db_write: this path takes the same blocking lock,
    # and during the 2026-08-07 rebuild stall the caller queuing here was
    # invisible precisely because only with_db_write was instrumented.
    if _on_event_loop():
        _warn_loop_acquisition()
    # Checked BEFORE acquiring: an open write transaction here means the
    # caller's statements took SQLite's write lock outside the gate — the
    # lock-order inversion that turns a queued commit into a busy_timeout
    # standoff with whoever holds the gate.
    try:
        if getattr(conn, "in_transaction", False) and not _WRITE_LOCK._is_owned():
            _warn_ungated_transaction()
    except Exception:  # noqa: BLE001 — diagnostics must never break a write
        pass
    with _WRITE_LOCK:
        sqlite_retry_busy(_commit)


@contextmanager
def batched_writes(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold the write gate for a batch of mutations; single commit at the end."""
    if _on_event_loop():
        _warn_loop_acquisition()
    with _WRITE_LOCK:
        token = _defer_commit.set(True)
        try:
            yield

            def _commit() -> None:
                conn.commit()

            sqlite_retry_busy(_commit)
        except Exception:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            _defer_commit.reset(token)
