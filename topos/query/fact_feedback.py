"""``fact_feedback``: the owner's reaction and rank on a pack fact, recorded through an app (DISCLOSURE_RULES DR-72).

An app shows the owner a pack fact (``pack_facts``) and lets them put two separate things on it: a reaction (one of
:data:`REACTIONS`, one per fact) and a rank (a whole number from -3 to 3). This module is the node's whole part of
that: it stores the two values and gives them back with the fact. It never edits the fact, its confidence, its text or
its validity; it applies no ranking of its own; and it reads no text.

What it is not: the owner's verdict on a fact (``features/facts/verdicts.py``: confirm, reject, edit, which change
what the node believes and are owner-confirmed doors) or the owner's revision of one (``derivation/surfaces.py``).
Feedback says how the owner feels about a true fact, and changes nothing the node believes.

Who may write (``core/handlers/query.py``): an enrolled app on the owner socket whose grant names the fact's pack for
feedback (``mcp_clients.AppGrant.feedback``, given with ``--feedback``, never by ``--reads``). Every other caller is
refused before the payload is read.

Which fact: only one the app could read through ``pack_facts`` at that moment. :func:`record` asks that read for the
one id, with the app's own guard and the packs it may both read and record on, and writes only when the read returns
the fact. So a fact outside its packs, a fact of a pack the owner switched off, a special-class fact, a fact
Off-limits withholds (or anything at all while an Off-limits rebuild is pending), a superseded fact and an id that
never existed all get the one refusal, :data:`NOT_AVAILABLE`, and an app cannot use the write to learn whether an id
exists. The read does the same work for an id whether it came by this door or by ``pack_facts`` with ``fact_ids``, so
the write adds no timing signal the read did not already have.

Storage: one small table, ``fact_feedback``, keyed by the fact's id: the reaction, the rank, when it last changed and
which app changed it. One row per fact, so the values are the owner's and not an app's: every app that can read the
fact sees them (``pack_facts`` returns ``reaction`` and ``rank``, never the app's name), and an app with the feedback
grant for the pack may replace what another recorded. Created on the first write, as ``truth_labels`` is, so a node
where no app ever records feedback has no such table and no migration runs; a read of a database without it finds no
feedback and creates nothing. The table is in ``data_explorer_tables.OWNER_ONLY_TABLES``: the legacy table tools
serve it to the owner's own app only.

When a pack re-run supersedes a fact, the old fact is closed and the new one has a new id. The feedback row stays
with the old id: it is not carried to the new fact and not deleted. The old fact is no longer returned by
``pack_facts``, so its feedback is no longer shown and can no longer be changed; the new fact starts with none.

When a fact is erased (a source scrub or the owner's "remove this record" deletes the fact row itself, not merely
closes it), its feedback row goes with it: the same orphan sweep that ends both flows
(``features/lifecycle/derived_scrub.sweep_orphans``) calls :func:`forget_erased`, so no reaction, rank, time or app
name is kept under the id of a fact the node no longer has. A closed or superseded fact still has its row in
``signal_objects``, so the paragraph above is unchanged by this. A write that lost a race with an erasure is refused
inside the gate (the fact row is looked for again there), so it cannot leave a row behind the sweep.

Writes are committed under the write gate, off the event loop. Logs carry the app's name, the fact's id and which of
the two values changed; never a value of the fact.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.query.fact_feedback")

VERSION = "fact_feedback/v1"
TABLE = "fact_feedback"
REACTIONS = ("love", "like", "sad", "down")
RANK_MIN, RANK_MAX = -3, 3
MAX_FACT_ID_CHARS = 64
#: The one refusal code for a payload this write does not take; never the input.
INVALID = "invalid_fact_feedback_request"
#: The one refusal for a fact this app cannot record on, whatever the reason (see the module docstring).
NOT_AVAILABLE = "fact_not_available"
_PAYLOAD_KEYS = frozenset({"fact_id", "reaction", "rank"})
#: How many ids one ``IN (...)`` carries when the read looks feedback up for a page of facts.
_CHUNK = 500


class FactFeedbackRequestError(ValueError):
    """A payload this write refuses; ``str(exc)`` is always :data:`INVALID`."""


class FactNotAvailable(LookupError):
    """The fact is not one this app can record on; ``str(exc)`` is always :data:`NOT_AVAILABLE`."""


def parse_request(payload: Any) -> Dict[str, Any]:
    """``{"fact_id": id, "reaction"?: "love"|"like"|"sad"|"down"|null, "rank"?: -3..3|null}`` -> the request, or
    :class:`FactFeedbackRequestError`.

    At least one of ``reaction`` and ``rank`` must be there. A field that is absent is left as it was; ``reaction:
    null`` takes the reaction back; ``rank: 0`` (or null) takes the rank back. The write is the value, not a toggle:
    sending the same reaction twice leaves it set, so a retried request cannot undo itself. The request keeps only
    the fields that were sent."""
    if not isinstance(payload, dict) or not set(payload) <= _PAYLOAD_KEYS:
        raise FactFeedbackRequestError(INVALID)
    fact_id = payload.get("fact_id")
    if not isinstance(fact_id, str) or not 0 < len(fact_id) <= MAX_FACT_ID_CHARS:
        raise FactFeedbackRequestError(INVALID)
    if "reaction" not in payload and "rank" not in payload:
        raise FactFeedbackRequestError(INVALID)
    request: Dict[str, Any] = {"fact_id": fact_id}
    if "reaction" in payload:
        reaction = payload["reaction"]
        if reaction is not None and (not isinstance(reaction, str) or reaction not in REACTIONS):
            raise FactFeedbackRequestError(INVALID)
        request["reaction"] = reaction
    if "rank" in payload:
        rank = payload["rank"]
        if rank is not None and (type(rank) is not int or not RANK_MIN <= rank <= RANK_MAX):
            raise FactFeedbackRequestError(INVALID)
        request["rank"] = rank or None      # 0 takes the rank back
    return request


def _present(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure_table(conn: sqlite3.Connection) -> None:
    """The table, created by the first write that stores a value. The caller holds the write gate."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            fact_id TEXT PRIMARY KEY,
            reaction TEXT CHECK (reaction IS NULL OR reaction IN ('love', 'like', 'sad', 'down')),
            rank INTEGER CHECK (rank IS NULL OR (rank BETWEEN -3 AND 3 AND rank != 0)),
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )
        """
    )


def for_facts(conn: sqlite3.Connection, fact_ids: Iterable[str]) -> Dict[str, Tuple[Optional[str], Optional[int]]]:
    """fact id -> (reaction, rank) for the ids that have either. A read: it creates nothing, and a database without
    the table has no feedback."""
    ids = [str(i) for i in dict.fromkeys(fact_ids) if i]
    if not ids or not _present(conn):
        return {}
    out: Dict[str, Tuple[Optional[str], Optional[int]]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        for fact_id, reaction, rank in conn.execute(
                f"SELECT fact_id, reaction, rank FROM {TABLE} WHERE fact_id IN ({','.join('?' for _ in chunk)})",
                chunk).fetchall():
            out[str(fact_id)] = (reaction if reaction in REACTIONS else None,
                                 rank if type(rank) is int and RANK_MIN <= rank <= RANK_MAX and rank else None)
    return out


def forget_erased(conn: sqlite3.Connection) -> int:
    """Delete the feedback of facts that no longer exist; -> how many rows went. For the erasure flows' orphan sweep
    (``derived_scrub.sweep_orphans``): the caller holds the write gate and commits.

    "No longer exists" is no ``fact`` row in ``signal_objects`` at all. A closed or superseded fact still has one, so
    its feedback stays (see the module docstring). A database without the table has nothing to forget and gets no
    table."""
    if not _present(conn):
        return 0
    cursor = conn.execute(
        f"DELETE FROM {TABLE} WHERE fact_id NOT IN (SELECT object_id FROM signal_objects WHERE object_type='fact')")
    return int(cursor.rowcount or 0)


def _fact_row_exists(conn: sqlite3.Connection, fact_id: str) -> bool:
    return conn.execute("SELECT 1 FROM signal_objects WHERE object_id=? AND object_type='fact'",
                        (fact_id,)).fetchone() is not None


def _readable(conn: sqlite3.Connection, fact_id: str, *, guard: Any, packs: Tuple[str, ...]) -> bool:
    """Whether ``pack_facts`` returns this fact to this app now, asked of the read itself so the two can never drift."""
    from . import pack_facts

    if not packs:
        return False
    request = {"packs": None, "fact_ids": [fact_id], "since": None, "limit": 1, "include_excerpts": False}
    result = pack_facts.read(conn, request, agent=True, guard=guard, granted=packs)
    return any(fact.get("id") == fact_id for fact in result["facts"])


def record(conn: sqlite3.Connection, request: Dict[str, Any], *, app: str, guard: Any, granted: Tuple[str, ...],
           feedback: Tuple[str, ...], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Record one parsed request for ``app``; -> ``{"version", "fact_id", "reaction", "rank", "updated_at"}`` as stored
    after it (``updated_at`` None when nothing is left on the fact).

    ``granted`` is what the app reads and ``feedback`` what it may record on: the fact must be in a pack on both, and
    readable under ``guard``, or :class:`FactNotAvailable`. The check runs before the gate is taken (it reads source
    records for Off-limits and must not hold up other writers); the write itself is one row."""
    if not app or guard is None:
        raise ValueError("a feedback write needs the app's name and its Off-limits guard")
    fact_id = request["fact_id"]
    if not _readable(conn, fact_id, guard=guard, packs=tuple(p for p in granted if p in feedback)):
        raise FactNotAvailable(NOT_AVAILABLE)
    at = (now or datetime.now(timezone.utc)).isoformat()
    with with_db_write():
        # The readability check ran before the gate. An erasure (derived_scrub) that deleted the fact and swept its
        # feedback in between must not be followed by a row written under the erased id: same refusal, nothing written.
        if not _fact_row_exists(conn, fact_id):
            raise FactNotAvailable(NOT_AVAILABLE)
        row = conn.execute(f"SELECT reaction, rank, updated_at FROM {TABLE} WHERE fact_id=?",
                           (fact_id,)).fetchone() if _present(conn) else None
        was_reaction, was_rank, was_at = row if row else (None, None, None)
        reaction = request["reaction"] if "reaction" in request else was_reaction
        rank = request["rank"] if "rank" in request else was_rank
        changed = [name for name, new, old in (("reaction", reaction, was_reaction), ("rank", rank, was_rank))
                   if new != old]
        if not changed:
            at = was_at                                   # the same values again: nothing written, the row's own time
        elif reaction is None and rank is None:
            conn.execute(f"DELETE FROM {TABLE} WHERE fact_id=?", (fact_id,))
            at = None
        else:
            ensure_table(conn)
            conn.execute(
                f"INSERT INTO {TABLE} (fact_id, reaction, rank, updated_at, updated_by) VALUES (?, ?, ?, ?, ?) "
                f"ON CONFLICT(fact_id) DO UPDATE SET reaction=excluded.reaction, rank=excluded.rank, "
                f"updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (fact_id, reaction, rank, at, app))
        commit_connection(conn)
    logger.info("fact_feedback app=%s fact=%s changed=%s", app, fact_id, ",".join(changed) or "nothing")
    return {"version": VERSION, "fact_id": fact_id, "reaction": reaction, "rank": rank, "updated_at": at}
