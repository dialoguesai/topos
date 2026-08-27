"""SGU-1 — the two transports serve identical payloads, proven rather than intended.

The app reaches the engine through CP routes -> websocket messages -> handlers; local tools
hit the HTTP routes. Both wrap `analytics/relationship_reads`, and this file is what keeps
that true: each read runs through BOTH transports on one fixture and the payloads must be
equal. The adversarial retest's worst finds lived BETWEEN tested parts — this is the seam.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    c.execute("""CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, dataset_id TEXT,
        source_id TEXT, display_name TEXT, is_self INTEGER DEFAULT 0,
        known_usernames_json TEXT, created_at TEXT, updated_at TEXT)""")
    c.execute("""CREATE TABLE contact_identifiers (dataset_id TEXT, source_id TEXT,
        identifier TEXT, identifier_type TEXT, contact_id TEXT,
        created_at TEXT, updated_at TEXT)""")
    c.execute("INSERT INTO contacts VALUES ('ct_1', ?, 'address_book', 'Tango Uniform',"
              " 0, NULL, 't', 't')", (DS,))
    c.execute("INSERT INTO contact_identifiers VALUES (?, 'address_book', '+15125551234',"
              " 'phone', 'ct_1', 't', 't')", (DS,))
    n = 0
    for i in range(9):
        for is_self in (0, 1):
            n += 1
            c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                      ("c1", f"m{n}", DS, None if is_self else "+15125551234",
                       (T0 + timedelta(days=i, minutes=is_self * 4)).isoformat(),
                       is_self, "imessage", None))
    c.commit()
    from topos.analytics.messenger_communities import _compute_directed_lane
    _compute_directed_lane(c, DS, None)

    # both transports must resolve THIS connection
    import topos.api.messenger_analytics as api
    import topos.core.handlers as hub
    monkeypatch.setattr(api, "get_db_connection", lambda: c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


def _ws(msg_type, payload):
    from topos.core.handlers.messenger_analytics import handle_relationship_reads

    # asyncio.get_event_loop() is deprecated and returns a loop that ANOTHER test may have
    # closed: running this file after tests/sources failed every case here with "no current
    # event loop", which reads as a contract break and is only test hygiene. Own the loop.
    res = asyncio.run(
        handle_relationship_reads({"id": "t1", "type": msg_type, "payload": payload}))
    assert res["status"] == "ok", res
    out = dict(res["payload"])
    out.pop("status", None)
    return out


def test_relationships_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_relationships

    http = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    ws = _ws("messenger_relationships", {"dataset_id": DS})
    assert ws == http


def test_signals_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_relationship_signals

    http = get_relationship_signals(dataset_id=DS, signal="all")
    ws = _ws("messenger_relationship_signals", {"dataset_id": DS})
    assert ws == http


def test_directed_edges_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_directed_edges

    http = get_directed_edges(dataset_id=DS, peer_key="+15125551234", edge_kind="dm", limit=200)
    ws = _ws("messenger_directed_edges", {"dataset_id": DS, "peer_key": "+15125551234"})
    assert ws == http


def test_bench_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_bench

    http = get_bench()
    ws = _ws("messenger_bench", {})
    assert ws == http


def test_luck_surface_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_luck_surface

    http = get_luck_surface(dataset_id=DS)
    ws = _ws("messenger_luck_surface", {"dataset_id": DS})
    assert ws == http


def test_person_graph_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_person_graph

    http = get_person_graph(dataset_id=DS, include_automated=False)
    ws = _ws("messenger_person_graph", {"dataset_id": DS})
    assert ws == http


def test_naming_queue_identical_over_both_transports(conn):
    from topos.api.messenger_analytics import get_naming_queue

    http = get_naming_queue(dataset_id=DS, limit=25)
    ws = _ws("messenger_naming_queue", {"dataset_id": DS})
    assert ws == http


def test_the_protocol_names_are_pinned():
    """The CP proxy routes and the client will address these exact strings; renaming one
    silently strands the other side."""
    from topos.core.handlers.registry import HANDLERS

    for name in ("messenger_relationships", "messenger_relationship_signals",
                 "messenger_directed_edges", "messenger_bench",
                 "messenger_luck_surface", "messenger_person_graph",
                 "messenger_naming_queue"):
        assert name in HANDLERS, f"protocol name {name!r} unregistered"


def test_a_read_error_answers_rather_than_hanging(conn, monkeypatch):
    """The relay is a single-worker CP's lifeline — an exception must become an error
    REPLY, because an unanswered request is a stuck spinner for every tenant."""
    import topos.analytics.relationship_reads as reads

    def boom(*a, **k):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(reads, "read_relationship_signals", boom)
    from topos.core.handlers.messenger_analytics import handle_relationship_reads

    res = asyncio.run(
        handle_relationship_reads({"id": "t9", "type": "messenger_relationship_signals",
                                   "payload": {"dataset_id": DS}}))
    assert res["status"] == "error"
    assert "kernel exploded" in res["error"]
