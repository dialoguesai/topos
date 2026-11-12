"""``message_context``: the turns around a message a search returned (DISCLOSURE_RULES DR-65, DR-66).

A search returns matches only, so an app holding a line of a conversation has no way to read the question it answered.
This read takes a search row's ``record_id`` and returns that message and its neighbours in the conversation, in time
order. No time limit: a reply three days later is still the next turn. No ranking, no prompt, no write.

Who may read it (``core/handlers/query.py``): the owner's app, and an owner agent on the socket whose grant names
``messages``. Other people's messages come too: the app runs next to the owner's node on the owner's own approval.

An owner agent gets what its search would show: Off-limits is applied by absence through the request's own guard, a
protected row is left out rather than marked, and nothing counts it. While an Off-limits rebuild is pending nothing is
returned. An id this read can't find or may not show answers the same way, with no turns, so the answer doesn't say which.

``event_at`` is ordered through ``julianday`` because stored values mix formats and offsets, and with ``message_id``
second so two turns with one timestamp neither repeat nor go missing. A row with no readable time has no place in the
order and is left out. A message with no text is not a turn.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

VERSION = "message_context/v1"

DEFAULT_TURNS = 5
MAX_TURNS = 20
MAX_ID_CHARS = 200
#: How many rows to fetch per turn wanted, so a few protected rows do not shorten the window.
FETCH_FACTOR = 4
#: The one refusal code for a payload this read does not take; never the input.
INVALID = "invalid_message_context_request"
_PAYLOAD_KEYS = frozenset({"record_id", "before", "after"})
_COLUMNS = "message_id, conversation_id, sender_id, source_id, content, event_at, is_from_self"


class MessageContextRequestError(ValueError):
    """A payload this read refuses; ``str(exc)`` is always :data:`INVALID`."""


def parse_request(payload: Any) -> Dict[str, Any]:
    """``{"record_id": id, "before"?: 0..20, "after"?: 0..20}`` -> the request, or :class:`MessageContextRequestError`."""
    if not isinstance(payload, dict) or not set(payload) <= _PAYLOAD_KEYS:
        raise MessageContextRequestError(INVALID)
    record_id = payload.get("record_id")
    if not isinstance(record_id, str) or not 0 < len(record_id) <= MAX_ID_CHARS:
        raise MessageContextRequestError(INVALID)
    request: Dict[str, Any] = {"record_id": record_id}
    for side in ("before", "after"):
        turns = payload.get(side, DEFAULT_TURNS)
        if type(turns) is not int or not 0 <= turns <= MAX_TURNS:
            raise MessageContextRequestError(INVALID)
        request[side] = turns
    return request


def _row(values: Any, *, match: bool = False) -> Dict[str, Any]:
    """A turn in a raw search row's shape, plus who wrote it as the table says and whether it is the match."""
    message_id, conversation_id, sender_id, source_id, content, event_at, is_from_self = values
    return {"_table": "conversation_messages", "record_id": message_id, "conversation_id": conversation_id,
            "sender_id": sender_id, "source_id": source_id, "content": content, "event_at": event_at,
            "is_from_self": bool(is_from_self), "is_match": match}


def _side(conn: sqlite3.Connection, match: Any, *, older: bool, turns: int, guard: Any) -> List[Dict[str, Any]]:
    """Up to ``turns`` neighbours on one side of the match, nearest first."""
    if not turns:
        return []
    filtering = guard is not None and guard.active
    compare, order = ("<", "DESC") if older else (">", "ASC")
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM conversation_messages WHERE conversation_id = ? AND content IS NOT NULL AND content != '' "
        f"AND (julianday(event_at), message_id) {compare} (julianday(?), ?) "
        f"ORDER BY julianday(event_at) {order}, message_id {order} LIMIT ?",
        (match[1], match[5], match[0], turns * FETCH_FACTOR if filtering else turns)).fetchall()
    shaped = [_row(r) for r in rows]
    return (guard.filter_canonical_rows(shaped) if filtering else shaped)[:turns]


def read(conn: sqlite3.Connection, request: Dict[str, Any], *, agent: bool, guard: Any = None) -> Dict[str, Any]:
    """The turns for one parsed request. ``agent`` applies Off-limits through ``guard`` (required for an agent); the
    owner's app reads unfiltered."""
    if agent and guard is None:
        raise ValueError("an agent read needs its Off-limits guard")
    result: Dict[str, Any] = {"version": VERSION, "turns": []}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'").fetchone():
        return result  # a node with no messages yet
    active = guard if agent else None
    if active is not None and active.active:
        from ..features.lifecycle.blackhole import BlackholeStore

        if BlackholeStore(conn).has_pending_rebuild(view=active.view):
            return result
    match = conn.execute(f"SELECT {_COLUMNS} FROM conversation_messages WHERE message_id = ?",
                         (request["record_id"],)).fetchone()
    if match is None:
        return result
    own = [_row(match, match=True)]
    if active is not None and active.active and not active.filter_canonical_rows(own):
        return result
    older = _side(conn, match, older=True, turns=request["before"], guard=active)
    newer = _side(conn, match, older=False, turns=request["after"], guard=active)
    result["turns"] = older[::-1] + own + newer
    return result
