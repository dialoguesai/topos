"""When may an older statement stop superseding a newer belief?

Belief revision in ``FactStore`` lets any comparable-confidence value replace
the active one. Re-extracting old messages after new ones therefore brings an
old value back ("resurrection"). This module decides the one case in which that
can be refused: the challenger's evidence definitely happened before the
incumbent's latest supporting evidence.

It is deliberately narrow, because every wrong refusal keeps a stale belief:

- Only points a caller with provenance authority vouches for are ordered. The
  store has no such authority, so a store built without an ``EvidenceTrust``
  never refuses anything and behaves exactly as before. The legacy writers'
  ``unverified_producer`` and ``ingestion_clock_substitute`` times, and a
  ``native_source_clock`` label nobody validated, are never trusted.
- Evidence is read when the decision is made, from the rows the facts'
  current ``source_refs`` name, never from the record frozen at insert. A ref
  counts only while its row still exists, is not deleted or excluded, and the
  ref names that row's table and source, plus its dataset for human messages.
  AI-chat rows have no dataset column; the trust must prove their enrollment
  and parent owner/source binding instead.
- Only owner statements are ordered, and a ref counts only when its row is the
  owner's own (``is_from_self`` 1, or an authored user prompt with validated
  native provenance). A refresh merges refs from any speaker into
  a fact, so the fact's label alone does not show whose message supports it.
- A time is "cannot tell" when it could end after the moment its row is known
  to have existed: the trust's own ceiling for rows it vouches for (for the
  snapshot lane, when the owner attested the snapshot), else the row's
  explicit ``ingested_at``. With neither, it is "cannot tell". So is anything
  unknown. This catches a fabricated future time and a device clock running
  ahead; a clock running behind is not detectable here.
- The challenger must be trusted in every ref (its latest possible time is what
  is compared). The incumbent needs one trusted ref: the latest trusted ref is
  a lower bound on its latest support, so "definitely before" stays sound.
- Equal or overlapping times are "overlapping", which never refuses.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Protocol

from ..temporal.points import TimePoint, order, parse_point, span, unknown
from ..temporal.records import EventTime

LEAF_TABLE = "conversation_messages"
_ORDERED_TABLES = (LEAF_TABLE, "ai_chat_messages")
OWNER = "owner"
_EVENT_COLUMN = {"conversation_messages": "event_at", "ai_chat_messages": "event_at", "journal_entries": "entry_at"}
# SQLite's datetime('now') forms, UTC by definition, with optional fractional seconds.
_SQLITE_UTC = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?)")


class EvidenceTrust(Protocol):
    """Vouches for a canonical row's native event time, or declines.

    Implemented only by callers that can prove where a row came from (the
    owner-attested ingest lanes). ``trusted_event_point`` returns an
    explicit-basis instant with ``native_source_clock`` provenance, or ``None``.
    ``existed_by`` returns an explicit-UTC instant by which the vouched row
    certainly existed, or ``None`` to fall back to the row's ``ingested_at``.
    """

    def trusted_event_point(self, conn: sqlite3.Connection, row: Dict[str, Any]) -> Optional[TimePoint]: ...

    def existed_by(self, conn: sqlite3.Connection, row: Dict[str, Any]) -> Optional[TimePoint]: ...


def _deleted(row: Dict[str, Any]) -> bool:
    return any(row.get(field) not in (None, 0, False, "") for field in ("valid_to", "deleted_at", "is_deleted", "deleted"))


def _excluded_ids(conn: sqlite3.Connection) -> set:
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='record'").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _explicit_instant(point: Optional[TimePoint]) -> Optional[int]:
    if point is None or not point.known or point.precision != "instant" or point.basis not in ("utc", "fixed_offset"):
        return None
    return span(point).lo


def _ceiling(conn, trust: "EvidenceTrust", row: Dict[str, Any]) -> Optional[int]:
    """The latest instant a row's event can have happened by, or ``None`` when nothing says."""
    existed_by = getattr(trust, "existed_by", None)
    if existed_by is not None:
        vouched = _explicit_instant(existed_by(conn, row))
        if vouched is not None:
            return vouched
    raw = row.get("ingested_at")
    if type(raw) is str:
        match = _SQLITE_UTC.fullmatch(raw)
        if match:
            raw = f"{match.group(1)}T{match.group(2)}Z"
    # A naive or unparseable reading is not a ceiling: its span could end after the event.
    return _explicit_instant(parse_point(raw, provenance="producer_clock"))


def _last(point: TimePoint) -> int:
    covered = span(point)
    return covered.hi if covered.hi_inclusive else covered.hi - 1


def _owner_row(row: Dict[str, Any]) -> bool:
    if row.get("_table") == "ai_chat_messages":
        # These labels only permit asking the trust. They never replace the
        # durable proof of the row and its parent account identity.
        return row.get("sender_type") in ("human", "user") and row.get("actor_role") == "authored"
    flag = row.get("is_from_self")
    return flag is True or (type(flag) is int and flag == 1)


def _names_row(ref: Dict[str, Any], row: Dict[str, Any]) -> bool:
    if ref.get("source_id") in (None, "") or str(ref["source_id"]) != str(row.get("source_id")):
        return False
    if row["_table"] == "ai_chat_messages":
        return ref.get("dataset_id") is None  # the AI leaf contract is datasetless
    return ref.get("dataset_id") not in (None, "") and str(ref["dataset_id"]) == str(row.get("dataset_id"))


def _points(conn, trust: EvidenceTrust, refs: Iterable[Dict[str, Any]]) -> List[Optional[TimePoint]]:
    """One entry per ref: the trusted point, or ``None`` when it cannot tell."""
    refs = [ref for ref in refs or []]
    rows: Dict[tuple, Dict[str, Any]] = {}
    for table in _ORDERED_TABLES:
        ids = sorted({str(ref.get("record_id")) for ref in refs
                      if type(ref) is dict and ref.get("table") == table and ref.get("record_id")})
        if not ids:
            continue
        try:
            cursor = conn.execute(
                f"SELECT * FROM {table} WHERE message_id IN ({','.join('?' for _ in ids)})", ids)
            names = [column[0] for column in cursor.description]
            for values in cursor.fetchall():
                row = {**dict(zip(names, values)), "_table": table}
                rows[(table, str(row["message_id"]))] = row
        except sqlite3.Error:
            continue
    excluded = _excluded_ids(conn) if rows else set()
    out: List[Optional[TimePoint]] = []
    for ref in refs:
        row = (rows.get((ref.get("table"), str(ref.get("record_id"))))
               if type(ref) is dict and ref.get("table") in _ORDERED_TABLES else None)
        if (row is None or _deleted(row) or str(row.get("message_id")) in excluded or not _owner_row(row)
                or not _names_row(ref, row)):
            out.append(None)
            continue
        try:
            point = trust.trusted_event_point(conn, row)
            ceiling = _ceiling(conn, trust, row) if point is not None else None
        except Exception:  # noqa: BLE001 — a failing validator is "cannot tell", never a refusal
            point, ceiling = None, None
        if (point is None or not point.known or point.provenance != "native_source_clock"
                or point.basis not in ("utc", "fixed_offset") or point.text != row.get("event_at")
                or ceiling is None or _last(point) > ceiling):
            out.append(None)
            continue
        out.append(point)
    return out


def older_than_incumbent(
    conn: sqlite3.Connection,
    trust: Optional[EvidenceTrust],
    *,
    challenger_refs: List[Dict[str, Any]],
    challenger_asserted_by: str,
    incumbent: Dict[str, Any],
) -> bool:
    """True only when an owner challenger's evidence is definitely before the incumbent's latest support."""
    if trust is None or not challenger_refs:
        return False
    if str(challenger_asserted_by or OWNER) != OWNER or str((incumbent.get("payload") or {}).get("asserted_by") or OWNER) != OWNER:
        return False
    challenger = _points(conn, trust, challenger_refs)
    if not challenger or any(point is None for point in challenger):
        return False
    supporting = [point for point in _points(conn, trust, incumbent.get("source_refs") or []) if point is not None]
    if not supporting:
        return False
    latest_challenger = max(challenger, key=_last)
    latest_support = max(supporting, key=lambda point: span(point).lo)
    return order(latest_challenger, latest_support) == "before"


def _stored_event_time(conn: Optional[sqlite3.Connection], row: Dict[str, Any]):
    if conn is None or not row.get("message_id"):
        return None
    try:
        stored = conn.execute(
            f"SELECT event_time_json, dataset_id, source_id FROM {LEAF_TABLE} WHERE message_id=?", (str(row["message_id"]),)
        ).fetchone()
    except sqlite3.Error:
        return None
    # A row that does not name its dataset and source cannot show the stored
    # record is its own: message ids such as imessage:<ROWID> carry neither.
    if stored is None or any(row.get(key) in (None, "") or str(row[key]) != str(value)
                             for key, value in (("dataset_id", stored[1]), ("source_id", stored[2]))):
        return None
    return stored[0]


def recorded_evidence(conn: Optional[sqlite3.Connection], table: str, row: Dict[str, Any]) -> TimePoint:
    """The evidence time a shared extractor records for a fact's source row.

    It keeps a stored ingestion-clock mark when that record still describes this
    row's time, and otherwise records the time as ``unverified_producer``. It
    never records ``native_source_clock``: nothing on this path can validate one,
    so an unvalidated label is downgraded. A lane that proves its rows passes its
    own point instead. The record is descriptive only; supersession reads evidence
    again at decision time and never trusts this.
    """
    column = _EVENT_COLUMN.get(table)
    if column is None:
        return unknown()
    text = row.get(column)
    point = parse_point(text, provenance="unverified_producer")
    if table == LEAF_TABLE:
        raw = row["event_time_json"] if "event_time_json" in row else _stored_event_time(conn, row)
        try:
            stored = EventTime.from_json(raw).event if raw is not None else None
        except (TypeError, ValueError):
            stored = None
        if stored is not None and stored.provenance == "ingestion_clock_substitute" and stored.text == point.text and point.known:
            return stored
    return point
