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


@contextmanager
def with_db_write() -> Iterator[None]:
    """Serialize a write-critical section across threads."""
    with _WRITE_LOCK:
        yield


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
