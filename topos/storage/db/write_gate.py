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


def _warn_loop_acquisition() -> None:
    """Report a gate acquisition on the event-loop thread, at most periodically.

    Not raised: failing a write to punish a bad call site is worse than the
    stall it warns about.
    """
    # Skip this module AND contextlib: `with_db_write` is a @contextmanager, so
    # contextlib's __enter__ sits between us and the real caller. Reporting
    # "contextlib.py:135" would make the warning unactionable.
    stack = traceback.extract_stack(limit=12)[:-2]
    site = "unknown"
    for frame in reversed(stack):
        name = frame.filename.rsplit("/", 1)[-1]
        if name in ("write_gate.py", "contextlib.py"):
            continue
        site = f"{name}:{frame.lineno} in {frame.name}"
        break

    now = time.monotonic()
    with _loop_warn_lock:
        last = _loop_warn_seen.get(site)
        if last is not None and (now - last) < _LOOP_WARN_INTERVAL_S:
            return
        first_time = last is None
        _loop_warn_seen[site] = now

    logger.warning(
        "[WRITE_GATE] acquired on the event-loop thread from %s — this blocks "
        "every coroutine including the control-plane keepalive; move this DB "
        "work into asyncio.to_thread%s",
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

    with _WRITE_LOCK:
        sqlite_retry_busy(_commit)


@contextmanager
def batched_writes(conn: sqlite3.Connection) -> Iterator[None]:
    """Hold the write gate for a batch of mutations; single commit at the end."""
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
