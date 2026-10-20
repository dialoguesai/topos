"""One content-free row per query serve span.

A request span is the wall time of ``handle_query``. A database span is retrieval. An LLM span is
query inference. Time to first token is stored only when a caller observed a first token. This
module never copies ``duration_ms`` into ``ttfb_ms``. A refusal names the field and does not repeat
the value.
"""
from __future__ import annotations

import logging
import re
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


def _store_quietly(conn, **fields) -> None:
    try:
        if conn is None:
            return
        store_span(conn, **fields)
    except Exception:
        logger.debug("serve span dropped", exc_info=True)


def start_request() -> tuple:
    """Begin the outer query span. The token closes it in :func:`finish_request`."""
    token = _parent.set(str(uuid.uuid4()))
    return time.perf_counter(), token


def finish_request(started: float, outcome: str, token: Token, conn=None) -> None:
    """Write the request span and clear the parent. Never raises into the query handler."""
    parent = _parent.get()
    duration_ms = max(0, int(round((time.perf_counter() - started) * 1000)))
    try:
        _parent.reset(token)
    except ValueError:
        _parent.set(None)
    if parent is None:
        return
    closed = outcome if outcome in OUTCOMES else "error"
    try:
        connection = conn if conn is not None else _connection()
    except Exception:
        logger.debug("serve span dropped", exc_info=True)
        return
    _store_quietly(
        connection,
        kind="request", name="query", duration_ms=duration_ms, outcome=closed, parent_span_id=None,
        span_id=parent,
    )
    # The request id was the parent. Children are written before this returns to the handler,
    # while the id is still current. A late flush after reset finds no parent and writes nothing.


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
    try:
        connection = conn if conn is not None else _connection()
    except Exception:
        logger.debug("serve span dropped", exc_info=True)
        return
    if retrieval_ms is not None:
        _store_quietly(
            connection,
            kind="db", name="retrieval", duration_ms=max(0, int(round(retrieval_ms))),
            outcome="ok", parent_span_id=parent,
        )
    if inference_ms is not None:
        _store_quietly(
            connection,
            kind="llm", name="query_inference", duration_ms=max(0, int(round(inference_ms))),
            outcome=llm_outcome, ttfb_ms=None, parent_span_id=parent,
        )
