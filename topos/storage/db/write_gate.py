"""Process-wide SQLite write serialization and busy retry.

SQLite allows only one writer at a time (even in WAL). Overlapping ingest,
SIGNAL_DERIVE, graph refresh, and pipeline bookkeeping previously raced and
raised ``database is locked``. Every path that ``commit()``s or runs
``BEGIN IMMEDIATE`` should hold :func:`with_db_write` (or use
:func:`commit_connection` / :func:`batched_writes`).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, NamedTuple, Optional, TypeVar

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


class _Holder(NamedTuple):
    site: str
    ident: int
    thread: str
    since: float


#: Who currently holds the gate. An RLock names neither its owner nor the call
#: site, so "everything is blocked on the write gate" used to be the end of the
#: diagnosis rather than the start of it: the victim times out and reports
#: itself, while the holder is never named anywhere.
#:
#: Only ever mutated while ``_WRITE_LOCK`` is held, so there is no cross-thread
#: race; ``_holder_lock`` exists so readers (diagnostics on other threads) see a
#: consistent tuple.
_holder: Optional[_Holder] = None
_holder_depth = 0
_holder_lock = threading.Lock()


def _set_holder(site: str) -> None:
    global _holder, _holder_depth
    ident = threading.get_ident()
    with _holder_lock:
        # The gate is reentrant. Keep the OUTERMOST site — that is the one that
        # has actually been holding for the reported duration — and count depth
        # so an inner release does not clear a hold that is still live.
        if _holder is not None and _holder.ident == ident:
            _holder_depth += 1
            return
        _holder = _Holder(site, ident, threading.current_thread().name, time.monotonic())
        _holder_depth = 1


def _clear_holder() -> None:
    global _holder, _holder_depth
    ident = threading.get_ident()
    with _holder_lock:
        if _holder is None or _holder.ident != ident:
            return
        _holder_depth -= 1
        if _holder_depth <= 0:
            _holder = None
            _holder_depth = 0


def describe_gate_holder() -> str:
    """Human-readable description of the current gate holder, for diagnostics."""
    with _holder_lock:
        held = _holder
    if held is None:
        return "not held"
    return (
        f"held by {held.site} on thread {held.thread!r} "
        f"for {time.monotonic() - held.since:.1f}s"
    )


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


#: How far up to look for the caller before giving up. Ten-plus frames of
#: write_gate/contextlib nesting means the real caller is unrecoverable anyway.
_CALLER_SITE_MAX_FRAMES = 12


def _caller_site() -> str:
    """Nearest stack frame outside this module (and contextlib).

    `with_db_write` is a @contextmanager, so contextlib's __enter__ sits
    between us and the real caller; reporting "contextlib.py:135" would make
    a warning unactionable.

    Walks frames instead of building a traceback: ``extract_stack`` materializes
    a StackSummary and consults linecache for source text nothing here reads,
    which costs ~13us per call against ~0.5us for the walk, for identical
    output. That gap did not matter while this only ran on gate acquisition;
    the transaction watchdog calls it on every BEGIN.
    """
    frame = sys._getframe(1)
    for _ in range(_CALLER_SITE_MAX_FRAMES):
        if frame is None:
            break
        name = frame.f_code.co_filename.rsplit("/", 1)[-1]
        if name not in ("write_gate.py", "contextlib.py"):
            return f"{name}:{frame.f_lineno} in {frame.f_code.co_name}"
        frame = frame.f_back
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

    Blind spot: this only fires at commit time. A caller that writes and NEVER
    commits holds the same lock indefinitely and is invisible here — that is
    what the transaction watchdog at the bottom of this module covers.
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
        _set_holder(_caller_site())
        try:
            yield
        finally:
            _clear_holder()
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


@contextmanager
def bounded_busy_timeout(conn, timeout_ms: int) -> Iterator[None]:
    """Temporarily shorten this connection's SQLite ``busy_timeout``.

    Guards against a lock-order inversion. Taking the gate and THEN issuing a
    write that blocks on SQLite pins the process-wide gate for the whole
    busy_timeout — 30s by default — while every other writer queues behind a
    holder that is itself only waiting. Observed in CI as
    ``ensure_migrations_applied: waited=0.0s held=30.0s``, which stalled an app
    startup on another thread into its 30s lifespan timeout.

    Shortening the timeout inside the gated section converts that into a prompt
    ``database is locked`` the caller can release the gate on and retry, so the
    stall is bounded by ``timeout_ms`` instead of by busy_timeout.

    Restores the previous value on exit. Never raises for a connection that
    cannot report or set the pragma.
    """
    previous: Optional[int] = None
    try:
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        if row is not None:
            previous = int(row[0])
        conn.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    except Exception:  # noqa: BLE001 — diagnostics must never break a write
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout={previous}")
            except Exception:  # noqa: BLE001
                pass


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
    _set_holder(_caller_site())
    try:
        # A derivation that started during the final acquire slice would now
        # queue behind this whole section — the exact convoy this exists to
        # prevent. Re-check with the gate held.
        if should_defer():
            raise WriteGateDeferred("higher-priority writer started while waiting")
        yield
    finally:
        _clear_holder()
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


# ---------------------------------------------------------------------------
# Open-transaction watchdog
#
# ``_warn_ungated_transaction`` can only speak at commit time, so a caller that
# writes and never commits says nothing at all — while still holding SQLite's
# RESERVED write lock for as long as its connection stays in-transaction. Every
# other connection's first write then waits out the full busy_timeout and dies
# with "database is locked". ``StatisticsJob._should_promote`` did exactly this
# (an ungated, never-committed ``UPDATE stat_state``) and wedged the whole
# signal lane on repeat ingests; finding it took a throwaway Connection
# subclass and a polling thread. This is that instrument, kept.
# ---------------------------------------------------------------------------

#: How long a connection may sit in-transaction before it is reported. Late
#: enough that ordinary gated batches never trip it, early enough to name the
#: culprit before the first victim exhausts its 30s busy_timeout.
_STUCK_TXN_WARN_S = 5.0

#: Watchdog wake interval; worst-case detection latency is this plus the
#: threshold. Each tick touches only plain Python data — never SQLite, never a
#: connection object — so it costs one dict copy.
_STUCK_TXN_SCAN_S = 2.0

#: Set to 1/true/yes/on to enable. Default off, because the cost is measurable
#: on the Python we ship (3.10, measured 2026-08-09, best of 7 interleaved
#: rounds): +1.6us per statement and +6.0us per transaction — more than double
#: a cheap statement, and an ingest runs millions of them. 83% of that is
#: sqlite3's trace-callback trampoline itself, irreducible short of not hooking
#: at all; 3.12 is roughly half the cost. Off, the whole mechanism is one
#: boolean test per connection open. A diagnostic that taxes the hot path
#: becomes its own outage — see ``_LOOP_WARN_INTERVAL_S`` for the last one that
#: did. Companion ``TOPOS_DB_TXN_WATCHDOG_SECONDS`` overrides the threshold for
#: a field investigation.
_TXN_WATCHDOG_ENV = "TOPOS_DB_TXN_WATCHDOG"
_TXN_WATCHDOG_SECONDS_ENV = "TOPOS_DB_TXN_WATCHDOG_SECONDS"


class _OpenTxn(NamedTuple):
    """What a connection's currently-open transaction was opened by, and when."""

    opened_at: float
    site: str
    thread_ident: int
    thread_name: str
    #: Did the opening thread hold the write gate when the transaction began?
    #: Same test ``commit_connection`` applies — a gated writer's RESERVED lock
    #: is exactly the serialization the gate exists to provide.
    gated: bool


#: ``id(conn)`` -> open transaction. Values are plain data on purpose: a
#: sqlite3.Connection supports neither weakref nor attribute assignment, and a
#: strong reference here would keep connections alive past the point where
#: ``core.state`` deliberately drops them (see its "Deliberately NOT closed"
#: comment) — the watchdog must never change when a connection actually closes.
#: Keys are unregistered explicitly on close; a key that outlives its
#: connection is either overwritten when the address is reused or pruned by the
#: dead-thread rule in the scan.
_open_txns: dict[int, _OpenTxn] = {}

#: ids of connections currently carrying our trace hook, so unregister only
#: ever calls into a connection we actually instrumented. With the watchdog off
#: this keeps ``unregister_connection`` from touching the sqlite3 object at all
#: — ``core.state`` unregisters handles that other threads may be mid-execute
#: on, which is the shape that SIGBUSed there on 2026-08-08.
_traced_conns: set[int] = set()
_txn_registry_lock = threading.Lock()

_watchdog_lifecycle_lock = threading.Lock()
_watchdog_thread: Optional[threading.Thread] = None
_watchdog_stop = threading.Event()
_watchdog_enabled = False
_watchdog_threshold_s = _STUCK_TXN_WARN_S
_watchdog_interval_s = _STUCK_TXN_SCAN_S


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def txn_watchdog_enabled() -> bool:
    """True when connections registered from now on will be watched."""
    return _watchdog_enabled


def enable_txn_watchdog(
    *,
    threshold_s: Optional[float] = None,
    interval_s: Optional[float] = None,
) -> None:
    """Turn the watchdog on. Only connections registered AFTER this are watched.

    The scanning thread starts lazily on the first :func:`register_connection`,
    so enabling at import time costs nothing until a connection appears.
    """
    global _watchdog_enabled, _watchdog_threshold_s, _watchdog_interval_s
    if threshold_s is not None:
        _watchdog_threshold_s = max(0.0, float(threshold_s))
    if interval_s is not None:
        _watchdog_interval_s = max(0.01, float(interval_s))
    _watchdog_enabled = True


def disable_txn_watchdog() -> None:
    """Turn the watchdog off, stop its thread, and forget every tracked transaction.

    Already-registered connections keep their trace hook until they are
    unregistered; with the registry cleared it only writes to a dict nobody
    reads. Used by tests, and available as a kill switch.
    """
    global _watchdog_enabled, _watchdog_thread
    _watchdog_enabled = False
    with _txn_registry_lock:
        _open_txns.clear()
    with _watchdog_lifecycle_lock:
        # Stop signal and handle swap under the same lock a restart takes, so a
        # concurrent re-enable cannot have its fresh thread killed by this one.
        thread, _watchdog_thread = _watchdog_thread, None
        if thread is not None:
            _watchdog_stop.set()
    if thread is not None:
        thread.join(timeout=5)


def _txn_verb(statement: str) -> str:
    """Leading SQL keyword, upper-cased, from a bounded slice.

    Bounded so the per-statement cost does not scale with a 4KB INSERT: no
    realistic leading whitespace pushes the verb past 32 characters.
    """
    return statement[:32].lstrip()[:9].upper()


def _make_txn_tracer(key: int) -> Callable[[str], None]:
    """A trace callback that tracks ``key``'s transaction state from statements.

    Runs on the connection's own thread for every statement SQLite executes, so
    it sees pysqlite's implicit ``BEGIN`` (and ``COMMIT``/``ROLLBACK`` from
    ``conn.commit()``/``rollback()``) with the real caller still on the stack —
    which is the only place the opening call site can be captured. Deliberately
    closes over the int key alone: a captured connection would be a reference
    cycle through an object that cannot be weakly referenced.

    Everything expensive (``_caller_site``) happens only on the transition into
    a transaction; SELECTs and non-first writes fall out at the first compare.
    """

    def _trace(statement: str) -> None:
        try:
            verb = _txn_verb(statement)
            if verb.startswith("BEGIN"):
                if not _watchdog_enabled:
                    # A hook can outlive `disable_txn_watchdog` on a connection
                    # nobody unregistered; recording here would repopulate a
                    # registry that is supposed to be dead. Checked inside the
                    # BEGIN branch so the per-statement path is untouched.
                    return
                entry = _OpenTxn(
                    opened_at=time.monotonic(),
                    site=_caller_site(),
                    thread_ident=threading.get_ident(),
                    thread_name=threading.current_thread().name,
                    gated=_WRITE_LOCK._is_owned(),
                )
                with _txn_registry_lock:
                    _open_txns[key] = entry
            elif verb.startswith(("COMMIT", "ROLLBACK", "END")):
                with _txn_registry_lock:
                    _open_txns.pop(key, None)
        except Exception:  # noqa: BLE001 — diagnostics must never break a write
            pass

    return _trace


def register_connection(conn: sqlite3.Connection) -> None:
    """Watch ``conn`` for transactions left open outside the write gate.

    No-op unless the watchdog is enabled, so the default production path adds
    one boolean test per connection open. Installs a trace callback — the only
    consumer of ``set_trace_callback`` in this codebase; a caller that wants its
    own tracer on a watched connection would have to chain them.
    """
    if not _watchdog_enabled:
        return
    key = id(conn)
    try:
        conn.set_trace_callback(_make_txn_tracer(key))
    except Exception as exc:  # noqa: BLE001 — a diagnostic never blocks an open
        logger.debug("txn watchdog could not attach to a connection: %s", exc)
        return
    with _txn_registry_lock:
        # A prior connection may have died at this address without
        # unregistering; its record would otherwise be misread as this one's
        # open transaction.
        _open_txns.pop(key, None)
        _traced_conns.add(key)
    _ensure_watchdog_thread()


def unregister_connection(conn: sqlite3.Connection) -> None:
    """Stop watching ``conn``. Safe on an unregistered or already-closed handle.

    Must be called wherever a connection is closed OR its last reference is
    dropped: without weakrefs there is nothing else to tell the registry that
    an address went stale.
    """
    key = id(conn)
    with _txn_registry_lock:
        _open_txns.pop(key, None)
        traced = key in _traced_conns
        _traced_conns.discard(key)
    if not traced:
        return
    try:
        conn.set_trace_callback(None)
    except Exception:  # noqa: BLE001 — already closed is the common case
        pass


def _scan_open_transactions(now: Optional[float] = None) -> int:
    """Report transactions open past the threshold outside the gate.

    Returns the number of WARNINGs emitted (rate limiting suppresses repeats).
    ``now`` is injectable so tests need no sleeps.
    """
    now = time.monotonic() if now is None else now
    with _txn_registry_lock:
        snapshot = list(_open_txns.items())
    if not snapshot:
        return 0

    live_idents = {t.ident for t in threading.enumerate()}
    reported = 0
    stale: list[tuple[int, _OpenTxn]] = []
    for key, txn in snapshot:
        age = now - txn.opened_at
        if age < _watchdog_threshold_s:
            continue
        thread_alive = txn.thread_ident in live_idents
        if not thread_alive:
            # Nobody is left to commit or roll this back, and no COMMIT trace
            # will ever clear it — report it once (below), then drop it rather
            # than warn about the same corpse every scan.
            stale.append((key, txn))
        if txn.gated:
            # Held under the gate: writers are serialized as designed. A gated
            # section that runs long is _SLOW_HOLD_WARN_S's story, not this one.
            continue
        if _warn_stuck_transaction(txn, age, thread_alive=thread_alive):
            reported += 1

    if stale:
        with _txn_registry_lock:
            for key, txn in stale:
                # Identity, not just the key: this connection may have opened a
                # fresh transaction since the snapshot, and dropping THAT record
                # would blind the watchdog to a live leak.
                if _open_txns.get(key) is txn:
                    del _open_txns[key]
    return reported


def _warn_stuck_transaction(txn: _OpenTxn, age: float, *, thread_alive: bool) -> bool:
    """Report one stuck transaction, at most periodically per call site.

    No ``stack_info``: the stack here belongs to the watchdog thread and would
    be noise. ``txn.site`` is the frame that actually opened the transaction.
    """
    first_time = _rate_limited(f"stuck-txn:{txn.site}")
    if first_time is None:
        return False

    logger.warning(
        "[WRITE_GATE] transaction opened at %s (thread %s%s) has stayed open "
        "%.1fs without holding the gate — it holds SQLite's RESERVED write "
        "lock, so every other connection's first write blocks for the full "
        "busy_timeout and then fails 'database is locked'; commit or roll back "
        "that transaction, inside with_db_write%s",
        txn.site,
        txn.thread_name,
        "" if thread_alive else ", which has since exited",
        age,
        "" if first_time else f" (repeats suppressed for {int(_LOOP_WARN_INTERVAL_S)}s)",
    )
    return True


def _watchdog_loop() -> None:
    while not _watchdog_stop.wait(_watchdog_interval_s):
        try:
            _scan_open_transactions()
        except Exception as exc:  # noqa: BLE001 — the watchdog outlives its own bugs
            logger.debug("txn watchdog scan failed: %s", exc)


def _ensure_watchdog_thread() -> None:
    global _watchdog_thread
    with _watchdog_lifecycle_lock:
        if _watchdog_thread is not None and _watchdog_thread.is_alive():
            return
        _watchdog_stop.clear()
        _watchdog_thread = threading.Thread(
            target=_watchdog_loop,
            name="db-txn-watchdog",
            daemon=True,
        )
        _watchdog_thread.start()


def _enable_txn_watchdog_from_env() -> None:
    """Apply ``TOPOS_DB_TXN_WATCHDOG`` at import. No thread starts until a
    connection registers, so this stays a cheap no-op when the flag is unset."""
    if not _env_flag(_TXN_WATCHDOG_ENV):
        return
    raw = os.getenv(_TXN_WATCHDOG_SECONDS_ENV)
    threshold: Optional[float] = None
    if raw:
        try:
            threshold = float(raw)
        except ValueError:
            # Never refuse to watch over a malformed tuning knob.
            logger.warning(
                "%s=%r is not a number; using the %.0fs default",
                _TXN_WATCHDOG_SECONDS_ENV,
                raw,
                _STUCK_TXN_WARN_S,
            )
    enable_txn_watchdog(threshold_s=threshold)


_enable_txn_watchdog_from_env()
