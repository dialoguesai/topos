"""Schema probes that tell "not created yet" apart from "connection unusable".

Several hot paths avoid the write gate by probing first: read ``sqlite_master``
or a ``PRAGMA``, and only run the idempotent DDL when the schema really is
missing. That is what took ``_ensure_tables`` from 134 gate acquisitions on the
event-loop thread down to none (see ``write_gate``).

The probes swallowed *every* exception and reported "absent", which quietly
inverted the optimization. On 2026-08-17 a connection's sqlite3 statement cache
was corrupted by cross-thread use and began raising ``KeyError(('<sql>',))`` on
every ``execute``. The probes read that as "table missing", took the write gate
ON THE EVENT LOOP, ran DDL that failed the same way — and the blocking gate
stalled the control-plane keepalive, so the node read as offline while its
database was dead. The DDL never had any chance of succeeding: the connection,
not the schema, was the problem.

So a probe now has three outcomes, not two:

* present  -> ``True``
* absent   -> ``False``          (run the DDL, as before)
* unusable -> :class:`UnusableConnection`

``sqlite3.OperationalError`` stays in the "absent" bucket on purpose. It is what
a locked database raises, and the DDL path takes the gate with busy retry, so
falling through can genuinely succeed. Everything else means this handle cannot
answer questions and the caller should surface that instead of gating.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable


class UnusableConnection(RuntimeError):
    """A schema probe failed for a reason that is not a missing schema.

    Carries the original error as ``__cause__`` — for the statement-cache
    corruption that motivated this, the payload of that error is the SQL of an
    unrelated statement, so the message alone misleads.
    """


def probe_bool(fn: Callable[[], bool], *, what: str) -> bool:
    """Run a present/absent schema probe, classifying its failure.

    Returns ``True``/``False`` for a real answer, ``False`` for
    ``sqlite3.OperationalError`` (locked or busy — the gated DDL path can still
    succeed, so behave as before), and raises :class:`UnusableConnection` for
    anything else.

    ``what`` names the thing being probed and appears in the error, because the
    underlying exception frequently does not describe the real problem.
    """
    try:
        return bool(fn())
    except sqlite3.OperationalError:
        return False
    except Exception as exc:  # noqa: BLE001 — classification is the whole point
        raise UnusableConnection(
            f"schema probe for {what} could not run on this connection: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def describe_unusable(exc: BaseException) -> str:
    """A log line that names the real cause rather than the misleading payload."""
    cause: Any = getattr(exc, "__cause__", None)
    if isinstance(cause, KeyError):
        return (
            f"{exc} — a KeyError whose key is a SQL string means this "
            "connection's sqlite3 statement cache was corrupted by cross-thread "
            "use; the SQL in the key is NOT the failing query, and the handle "
            "stays broken for the life of the process"
        )
    return str(exc)
