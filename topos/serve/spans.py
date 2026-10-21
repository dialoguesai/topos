"""One content-free row per query serve span.

A request span is the wall time of ``handle_query``. A database span is retrieval. An LLM span is
query inference. Time to first token is stored only when a caller observed a first token. This
module never copies ``duration_ms`` into ``ttfb_ms``. A refusal names the field and does not repeat
the value.

The row is written off the app event loop. ``finish_request`` and ``flush_stages`` run on the loop
(the query handler's ``finally``), and a span write takes the SQLite write gate, a blocking OS lock.
On 21 Oct 2026 the automatic message review held that gate 60 to 109 s per row, 111 times before
noon, and every query waited for it here, on the loop: every coroutine froze with it ("app loop did
not respond within 5.0s", MCP timeouts, the owner's routine reading "database is locked"). The
gate's own warning named this module twice (``store_span``, ``ensure_table``). So a caller on the
loop hands the validated row to a queue and returns at once; one worker thread takes the gate on
its own connection. A caller already off the loop writes inline, as before. A row that cannot be
queued or written is dropped: a span is a measurement of the query, never a condition of it.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
import time
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Optional

from ..storage.db.migrations.serve_spans_v1 import create_serve_spans
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger(__name__)

TABLE = "serve_spans"
KINDS = frozenset({"request", "db", "llm"})
NAMES = {"request": "query", "db": "retrieval", "llm": "query_inference"}
OUTCOMES = frozenset({"ok", "timeout", "error"})
MAX_MS = 24 * 3600 * 1000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_parent: ContextVar[Optional[str]] = ContextVar("serve_parent", default=None)
_pending: ContextVar[Optional[tuple]] = ContextVar("serve_pending", default=None)


class SpanRefused(ValueError):
    """A span that is not the closed shape. ``field`` is from this module's vocabulary."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


def _whole(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_MS:
        raise SpanRefused(field)
    return value


def validate_span(*, kind, name, duration_ms, outcome, ttfb_ms=None, parent_span_id=None) -> dict:
    """The span as it will be stored, or :class:`SpanRefused`."""
    if kind not in KINDS:
        raise SpanRefused("kind")
    if name != NAMES[kind]:
        raise SpanRefused("name")
    if outcome not in OUTCOMES:
        raise SpanRefused("outcome")
    duration = _whole(duration_ms, "duration_ms")
    if kind != "llm":
        if ttfb_ms is not None:
            raise SpanRefused("ttfb_ms")
        ttfb = None
    elif ttfb_ms is None:
        ttfb = None
    else:
        ttfb = _whole(ttfb_ms, "ttfb_ms")
        if ttfb > duration:
            raise SpanRefused("ttfb_ms")
    if kind == "request":
        if parent_span_id is not None:
            raise SpanRefused("parent_span_id")
        parent = None
    else:
        if not isinstance(parent_span_id, str) or _UUID.match(parent_span_id) is None:
            raise SpanRefused("parent_span_id")
        parent = parent_span_id
    return {
        "kind": kind,
        "name": name,
        "duration_ms": duration,
        "outcome": outcome,
        "ttfb_ms": ttfb,
        "parent_span_id": parent,
    }


def _table_exists(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None


def ensure_table(conn) -> None:
    if _table_exists(conn):
        return
    with with_db_write():
        create_serve_spans(conn)
        commit_connection(conn)


def store_span(conn, *, recorded_at: Optional[str] = None, span_id: Optional[str] = None, **fields) -> str:
    """Insert one validated span. Returns its id. Raises :class:`SpanRefused` for a bad shape."""
    span = validate_span(**fields)
    if span_id is None:
        span_id = str(uuid.uuid4())
    elif _UUID.match(span_id) is None:
        raise SpanRefused("span_id")
    stamp = recorded_at or datetime.now(timezone.utc).isoformat()
    ensure_table(conn)
    with with_db_write():
        conn.execute(
            f"""INSERT INTO {TABLE}
                (span_id, parent_span_id, recorded_at, kind, name, duration_ms, ttfb_ms, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                span_id, span["parent_span_id"], stamp, span["kind"], span["name"],
                span["duration_ms"], span["ttfb_ms"], span["outcome"],
            ),
        )
        commit_connection(conn)
    return span_id


def _connection():
    from ..core.state import get_db_connection

    return get_db_connection()


def _on_event_loop() -> bool:
    """True on a thread that is running an asyncio loop: a write here would stall every coroutine."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


#: Rows the loop has handed over and the worker has not written yet. Bounded: a gate held for
#: minutes must not grow this without limit, and a dropped span costs nothing the query needs.
_QUEUE_LIMIT = 512
_queue: "queue.Queue[tuple]" = queue.Queue(maxsize=_QUEUE_LIMIT)
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_outstanding = 0
_idle = threading.Condition()


def _write(conn, fields: dict) -> None:
    """One span on the calling thread. Never raises."""
    try:
        if conn is None:
            return
        store_span(conn, **fields)
    except Exception:
        logger.debug("serve span dropped", exc_info=True)


def _drain_forever() -> None:
    global _outstanding
    while True:
        conn, fields = _queue.get()
        try:
            # The worker's OWN connection, resolved on this thread: ``get_db_connection`` is
            # per-thread on a file-backed node, and the loop thread's handle must never be
            # shared with a worker (one connection, one transaction state, one statement cache).
            _write(conn if conn is not None else _safe_connection(), fields)
        finally:
            with _idle:
                _outstanding -= 1
                if _outstanding == 0:
                    _idle.notify_all()


def _safe_connection():
    try:
        return _connection()
    except Exception:
        logger.debug("serve span dropped", exc_info=True)
        return None


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_drain_forever, name="serve-spans-writer", daemon=True)
        _worker.start()


def _enqueue(conn, fields: dict) -> None:
    """Hand one span to the worker. Never blocks and never raises."""
    global _outstanding
    with _idle:
        _outstanding += 1
    try:
        _queue.put_nowait((conn, fields))
    except queue.Full:
        with _idle:
            _outstanding -= 1
            if _outstanding == 0:
                _idle.notify_all()
        logger.debug("serve span dropped: writer queue full")
        return
    except Exception:
        with _idle:
            _outstanding -= 1
            if _outstanding == 0:
                _idle.notify_all()
        logger.debug("serve span dropped", exc_info=True)
        return
    try:
        _ensure_worker()
    except Exception:
        logger.debug("serve span writer did not start", exc_info=True)


def wait_for_writes(timeout: float = 5.0) -> bool:
    """Block until every queued span is written or dropped. For tests and shutdown; True when idle."""
    deadline = time.monotonic() + max(0.0, timeout)
    with _idle:
        while _outstanding:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _idle.wait(remaining)
    return True


def _store_quietly(conn, **fields) -> None:
    """Write now when this thread is free to block; otherwise queue for the worker.

    ``conn`` is the caller's explicit handle or ``None``. ``None`` is resolved on the thread that
    writes, never here: on the loop that would hand the loop thread's handle to the worker.
    """
    if _on_event_loop():
        _enqueue(conn, fields)
        return
    _write(conn if conn is not None else _safe_connection(), fields)


def start_request() -> tuple:
    """Begin the outer query span. The token closes it in :func:`finish_request`."""
    token = _parent.set(str(uuid.uuid4()))
    return time.perf_counter(), token


def finish_request(started: float, outcome: str, token: Token, conn=None) -> None:
    """Write the request span and clear the parent. Never raises into the query handler, and never
    waits on the write gate from the event loop."""
    parent = _parent.get()
    duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
    try:
        _parent.reset(token)
    except ValueError:
        _parent.set(None)
    if parent is None:
        return
    closed = outcome if outcome in OUTCOMES else "error"
    _store_quietly(
        conn,
        kind="request", name="query", duration_ms=duration_ms, outcome=closed, parent_span_id=None,
        span_id=parent,
    )
    # The request id was the parent. Children are queued before this row, while the id is still
    # current, and the worker writes in that order. A late flush after reset finds no parent and
    # writes nothing.


def publish_stages(retrieval_ms, inference_ms, llm_outcome: str = "ok") -> None:
    """Remember the stage clocks for the open request. No parent means the caller is not a served query."""
    if _parent.get() is None:
        return
    _pending.set((retrieval_ms, inference_ms, llm_outcome if llm_outcome in OUTCOMES else "error"))


def flush_stages(conn=None) -> None:
    """Write the remembered database and LLM spans under the open request. Never raises."""
    pending = _pending.get()
    _pending.set(None)
    parent = _parent.get()
    if not pending or parent is None:
        return
    retrieval_ms, inference_ms, llm_outcome = pending
    if retrieval_ms is not None:
        _store_quietly(
            conn,
            kind="db", name="retrieval", duration_ms=max(0, int(round(retrieval_ms))),
            outcome="ok", parent_span_id=parent,
        )
    if inference_ms is not None:
        _store_quietly(
            conn,
            kind="llm", name="query_inference", duration_ms=max(0, int(round(inference_ms))),
            outcome=llm_outcome, ttfb_ms=None, parent_span_id=parent,
        )
