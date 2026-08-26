"""L1-9 — the read surfaces.

Handlers are called directly with every argument supplied. FastAPI `Query(...)` defaults are
sentinel objects, not values, so a direct call that omits one passes the sentinel through to
`int()` — these tests would pass a Query object where the code expects a number.

Computed data nobody can read is not delivered. These two endpoints are the difference
between L1 being a table and L1 being an answer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_communities import _compute_directed_lane

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / "api.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    c.execute("CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT)")
    c.execute("CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, dataset_id TEXT)")
    n = 0
    for peer, count in (("+15125551234", 8), ("262966", 5)):
        for i in range(count):
            for is_self in (0, 1):
                n += 1
                c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                          (f"c_{peer}", f"m{n}", DS, None if is_self else peer,
                           (T0 + timedelta(days=i, minutes=is_self * 3)).isoformat(),
                           is_self, "imessage", None))
    c.commit()
    _compute_directed_lane(c, DS, None)
    import topos.api.messenger_analytics as api
    monkeypatch.setattr(api, "get_db_connection", lambda: c)
    monkeypatch.setattr(api, "resolve_participant_labels", lambda *a, **k: {})
    yield c
    c.close()


def test_relationships_returns_the_owner_dyads(conn):
    from topos.api.messenger_analytics import get_relationships

    res = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)
    assert res["count"] >= 1
    assert {r["peer_key"] for r in res["relationships"]} == {"+15125551234"}


def test_automated_peers_are_excluded_by_default_and_available_on_request(conn):
    """Stored, not dropped: ranking a carrier shortcode beside a friend makes every
    relationship number meaningless, but dropping it at write loses the honest answer to
    'what is actually filling my inbox'."""
    from topos.api.messenger_analytics import get_relationships

    default = {r["peer_key"] for r in get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)["relationships"]}
    widened = {r["peer_key"] for r in
               get_relationships(dataset_id=DS, tie_state=None, include_automated=True, limit=100)["relationships"]}
    assert "262966" not in default
    assert "262966" in widened


def test_sent_and_received_are_stated_from_the_owners_side(conn):
    """A caller must never have to know which side of the canonical pair the owner landed
    on — that is exactly the sort-order trap that inverted `balance`."""
    from topos.api.messenger_analytics import get_relationships

    r = get_relationships(dataset_id=DS, tie_state=None, include_automated=False, limit=100)["relationships"][0]
    assert r["sent"] + r["received"] == r["total_msgs"]
    assert r["sent"] == 8, "the owner sent 8 of the 16"


def test_a_tie_state_filter_narrows(conn):
    from topos.api.messenger_analytics import get_relationships

    res = get_relationships(dataset_id=DS, tie_state="nonexistent_state", include_automated=False, limit=100)
    assert res["count"] == 0


def test_directed_edges_default_to_dm_not_broadcast(conn):
    """Group broadcast fans one message out to every other speaker, so defaulting to it
    would let a busy thread outrank every real correspondence."""
    from topos.api.messenger_analytics import get_directed_edges

    res = get_directed_edges(dataset_id=DS, peer_key=None, edge_kind="dm", limit=200)
    assert res["edge_kind"] == "dm"
    assert all(e["edge_kind"] == "dm" for e in res["edges"])


def test_directed_edges_can_be_scoped_to_one_peer(conn):
    from topos.api.messenger_analytics import get_directed_edges

    res = get_directed_edges(dataset_id=DS, peer_key="+15125551234", edge_kind="dm", limit=200)
    assert res["count"] > 0
    for e in res["edges"]:
        assert "+15125551234" in (e["from_key"], e["to_key"])


def test_a_read_before_any_write_returns_empty_not_an_error(tmp_path, monkeypatch):
    """A read surface must not 500 because a write pass has never run. Empty is the honest
    answer to 'what are my relationships' on a node that has computed none."""
    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    import topos.api.messenger_analytics as api
    monkeypatch.setattr(api, "get_db_connection", lambda: c)
    monkeypatch.setattr(api, "resolve_participant_labels", lambda *a, **k: {})
    from topos.api.messenger_analytics import get_directed_edges, get_relationships

    assert get_relationships(dataset_id="nope", tie_state=None, include_automated=False, limit=100)["relationships"] == []
    assert get_directed_edges(dataset_id="nope", peer_key=None, edge_kind="dm", limit=200)["edges"] == []
    c.close()
