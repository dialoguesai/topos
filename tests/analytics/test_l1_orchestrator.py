"""L1-7/10 — the directed lane runs inside the pass that already runs.

No new trigger, and no second rebuild lifecycle. Two of the three existing messenger triggers
call `compute_and_persist_messenger_analytics` synchronously, and prod CP is a single uvicorn
worker where any added synchronous work starves every tenant. Converging the messenger lane
onto `graph_materialization_state` is real work with its own failure modes; doing it as a side
effect of L1 would put an unreviewed refactor on a single-worker service's critical path.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_communities import (
    _compute_directed_lane,
    compute_and_persist_messenger_analytics,
)
from topos.analytics.messenger_directed import (
    MESSENGER_DIRECTED_EDGES_TABLE,
    MESSENGER_DYAD_STATS_TABLE,
)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "ds"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "o.db"))
    # the undirected lane requires it; the directed lane must work under it too, which is
    # part of what this fixture proves
    c.row_factory = sqlite3.Row
    # the undirected lane reads `content` and conversation_participants; the directed lane
    # reads neither, but the orchestrator runs both, so the fixture carries the union
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT, content TEXT, sender_type TEXT, message_type TEXT)""")
    c.execute("CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, is_self INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE conversation_participants (
        conversation_id TEXT, contact_id TEXT, dataset_id TEXT, role TEXT,
        PRIMARY KEY (conversation_id, contact_id))""")
    c.execute("""CREATE TABLE conversations (
        conversation_id TEXT PRIMARY KEY, dataset_id TEXT, source_id TEXT, title TEXT)""")
    c.execute("""CREATE TABLE contact_identifiers (
        contact_id TEXT, identifier TEXT, identifier_type TEXT, dataset_id TEXT)""")
    c.execute("INSERT INTO conversations VALUES ('c1', ?, 'imessage', 'dm')", (DS,))
    c.executemany("INSERT INTO conversation_participants VALUES (?,?,?,?)",
                  [("c1", "peer", DS, "member"), ("c1", "me", DS, "self")])
    for i in range(6):
        c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("c1", f"p{i}", DS, "peer", (T0 + timedelta(hours=i * 9)).isoformat(),
                   0, "imessage", None, "hi", "contact", "text"))
        c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  ("c1", f"o{i}", DS, None, (T0 + timedelta(hours=i * 9, minutes=4)).isoformat(),
                   1, "imessage", None, "hey", "self", "text"))
    c.commit()
    yield c
    c.close()


def test_the_directed_lane_populates_both_tables(conn):
    out = _compute_directed_lane(conn, DS, None)
    assert out["directed_edges_written"] > 0
    assert out["dyads_written"] == 1
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE}").fetchone()[0] > 0
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DYAD_STATS_TABLE}").fetchone()[0] == 1


@pytest.fixture()
def no_undirected(monkeypatch):
    """Stub the UNDIRECTED extractor.

    These two tests assert L1's wiring — that the directed lane runs inside the existing pass
    and that its failure is contained. Standing up the undirected lane's full input schema
    (conversations, participants, contact_identifiers, a dozen columns of
    conversation_messages) would test a subsystem L1 does not change, and would break
    whenever that subsystem's inputs move. Its own tests cover it.
    """
    import topos.analytics.messenger_communities as mc

    monkeypatch.setattr(mc, "extract_messenger_graph",
                        lambda **kw: {"periods": [], "dataset_id": kw.get("dataset_id")})


def test_running_the_full_orchestrator_reports_the_directed_totals(conn, no_undirected):
    res = compute_and_persist_messenger_analytics(dataset_id=DS, conn=conn)
    assert res["totals"]["directed_edges_written"] > 0
    assert res["totals"]["dyads_written"] == 1
    # the undirected counters are still reported, at zero
    assert "edges_written" in res["totals"]


def test_a_directed_failure_does_not_lose_the_undirected_results(conn, no_undirected, monkeypatch):
    """The lane is additive. A partial answer beats no answer, and a graph feature that can
    take down messenger analytics is worse than one that is simply missing."""
    import topos.analytics.messenger_communities as mc

    def boom(*a, **k):
        raise RuntimeError("directed lane exploded")

    monkeypatch.setattr(mc, "_compute_directed_lane", boom)
    res = compute_and_persist_messenger_analytics(dataset_id=DS, conn=conn)
    assert "error" in res["totals"]
    assert res["totals"]["directed_edges_written"] == 0
    assert "edges_written" in res["totals"], "the undirected pass must still have completed"


def test_the_lane_is_idempotent_across_runs(conn):
    _compute_directed_lane(conn, DS, None)
    first = conn.execute(f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE}").fetchone()[0]
    _compute_directed_lane(conn, DS, None)
    assert conn.execute(
        f"SELECT COUNT(*) FROM {MESSENGER_DIRECTED_EDGES_TABLE}").fetchone()[0] == first


def test_a_single_connector_filter_narrows_the_edge_lane(conn):
    conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("c2", "s1", DS, "other", T0.isoformat(), 0, "signal", None, "yo", "contact", "text"))
    conn.commit()
    _compute_directed_lane(conn, DS, ["imessage"])
    connectors = {r[0] for r in conn.execute(
        f"SELECT DISTINCT connector FROM {MESSENGER_DIRECTED_EDGES_TABLE}")}
    assert connectors == {"imessage"}


def test_no_new_trigger_was_introduced():
    """L1-10 as an assertion: the directed lane must be reachable only through the pass that
    already existed, so it cannot add a synchronous caller to a single-worker service."""
    import inspect

    import topos.analytics.messenger_directed as md

    src = inspect.getsource(md)
    for bad in ("@router", "APIRouter", "add_api_route", "schedule", "BackgroundTasks"):
        assert bad not in src, f"messenger_directed must not register {bad}"
