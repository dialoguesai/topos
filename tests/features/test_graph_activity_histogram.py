"""Per-day edge activity — the density strip under the graph time scrubber.

The bars sit directly beneath a window the owner drags, so the only useful
property is that a tall bar means the window will find something there. These
tests pin the three ways that promise can break: counting a belief stamp as an
activity stamp, letting an undated edge invent a day, and letting a protected
entity change a bar's height.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.edges import graph_activity_daily
from topos.features.entities.reads import entity_graph_activity
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from topos.storage.db.migrations import apply_all_migrations


def _entity(c, eid, name, etype="person"):
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)"
        " VALUES (?,?,?,?)",
        (eid, etype, name, name.lower()),
    )


def _edge(c, eid, src, dst, *, last_event_at, valid_from=None, valid_to=None,
          edge_type="co_occurrence", weight=1.0):
    c.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type,"
        " weight, last_event_at, valid_from, valid_to) VALUES (?,?,?,?,?,?,?,?)",
        (eid, src, dst, edge_type, weight, last_event_at, valid_from, valid_to),
    )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "graph_activity.db"))
    apply_all_migrations(c)
    _entity(c, "ent-a", "Ada Rowe")
    _entity(c, "ent-b", "Mudlark Studio", etype="org")
    _entity(c, "ent-secret", "Dana Reyes")
    # Two edges on one day, one on the next.
    _edge(c, "e1", "ent-a", "ent-b", last_event_at="2026-06-01T09:00:00Z")
    _edge(c, "e2", "ent-a", "ent-b", last_event_at="2026-06-01T21:30:00Z",
          edge_type="discusses")
    _edge(c, "e3", "ent-a", "ent-b", last_event_at="2026-06-02T08:00:00Z",
          edge_type="relates_to")
    c.commit()
    return c


def _owner(conn):
    return BlackholeGuard(conn, caller_class=CallerClass.OWNER_UI)


def _stranger(conn):
    return BlackholeGuard(conn, caller_class=CallerClass.UNKNOWN)


def test_counts_group_by_utc_calendar_day_oldest_first(conn):
    out = graph_activity_daily(conn)
    assert out["days"] == [
        {"day": "2026-06-01", "edges": 2},
        {"day": "2026-06-02", "edges": 1},
    ]
    assert out["meta"]["total_edges"] == 3
    assert out["meta"]["max_edges"] == 2
    assert out["meta"]["first_day"] == "2026-06-01"
    assert out["meta"]["last_day"] == "2026-06-02"


def test_an_undated_edge_gets_no_bar_and_is_reported_instead(conn):
    """The reason the bars do not sum to the graph's edge count.

    A structural edge (semantic affinity, part_of) carries no event at all.
    Silently dropping it makes the strip look like data went missing; smearing
    it onto valid_from's day would put a rebuild's clock on the axis.
    """
    _edge(conn, "e-structural", "ent-a", "ent-b", last_event_at=None,
          valid_from="2026-07-04T00:00:00Z", edge_type="semantic_affinity",
          weight=0.6)
    conn.commit()
    out = graph_activity_daily(conn)
    assert [d["day"] for d in out["days"]] == ["2026-06-01", "2026-06-02"]
    assert out["meta"]["undated_edges"] == 1
    assert out["meta"]["total_edges"] == 3


def test_valid_from_is_never_read_as_activity(conn):
    """The regression graph_snapshot documents, one layer down.

    An edge whose only stamp is the recompute's clock must not raise a bar on
    the day of the recompute — that is what put dormant entities inside "the
    last 11 days" on the graph above this strip.
    """
    _edge(conn, "e-rebuilt", "ent-a", "ent-b", last_event_at=None,
          valid_from="2026-09-09T12:00:00Z", edge_type="part_of")
    conn.commit()
    assert "2026-09-09" not in {d["day"] for d in graph_activity_daily(conn)["days"]}


def test_an_ended_edge_still_counts_on_the_day_it_happened(conn):
    """Activity is not validity.

    graph_snapshot's own event-window branch drops `valid_to IS NULL`, because
    a relationship that has since ended still happened on the day it happened.
    A strip that hid those days would under-draw exactly the history the
    scrubber exists to visit.
    """
    _edge(conn, "e-closed", "ent-a", "ent-b", last_event_at="2026-05-20T10:00:00Z",
          valid_from="2026-05-20T10:00:00Z", valid_to="2026-06-01T00:00:00Z",
          edge_type="communicates_with")
    conn.commit()
    days = {d["day"]: d["edges"] for d in graph_activity_daily(conn)["days"]}
    assert days["2026-05-20"] == 1


def test_since_and_until_bound_the_axis(conn):
    out = graph_activity_daily(conn, since="2026-06-02", until="2026-06-03")
    assert [d["day"] for d in out["days"]] == ["2026-06-02"]
    assert out["meta"]["since"] == "2026-06-02"


def test_min_weight_moves_bars_and_edges_together(conn):
    """The graph applies this floor server-side; the strip must apply the same one.

    Bars drawn without it would promise edges a raised floor has already cut.
    """
    _edge(conn, "e-weak", "ent-a", "ent-b", last_event_at="2026-06-03T10:00:00Z",
          edge_type="mentions", weight=0.4)
    conn.commit()
    assert "2026-06-03" in {d["day"] for d in graph_activity_daily(conn)["days"]}
    floored = graph_activity_daily(conn, min_weight=1.0)
    assert "2026-06-03" not in {d["day"] for d in floored["days"]}


def test_semantic_affinity_is_exempt_from_the_floor_as_it_is_in_the_graph(conn):
    """Affinity weights are cosines in [0,1]; the spine floor is calibrated for
    accumulating counts. graph_snapshot exempts them, so this must too."""
    _edge(conn, "e-aff", "ent-a", "ent-b", last_event_at="2026-06-04T10:00:00Z",
          edge_type="semantic_affinity", weight=0.55)
    conn.commit()
    days = {d["day"] for d in graph_activity_daily(conn, min_weight=1.5)["days"]}
    assert "2026-06-04" in days


def test_a_black_holed_entity_does_not_change_a_bar(conn):
    """A count that moves when a person is protected confirms they exist (D5).

    Filtering the returned rows cannot help here — a day bucket carries no
    entity id — so the exclusion has to be in the query that produced the
    number.
    """
    _edge(conn, "e-secret", "ent-a", "ent-secret", last_event_at="2026-06-02T11:00:00Z")
    _edge(conn, "e-secret-dst", "ent-secret", "ent-b", last_event_at="2026-06-02T12:00:00Z")
    conn.commit()
    assert dict(
        (d["day"], d["edges"])
        for d in entity_graph_activity(conn, guard=_owner(conn))["days"]
    )["2026-06-02"] == 3

    BlackholeStore(conn).blackhole_entity(entity_ref="ent-secret")
    conn.commit()
    hidden = entity_graph_activity(conn, guard=_stranger(conn))
    by_day = {d["day"]: d["edges"] for d in hidden["days"]}
    # Both ends are excluded: neither the edge INTO the protected entity nor the
    # one out of it may raise the bar.
    assert by_day["2026-06-02"] == 1
    assert by_day["2026-06-01"] == 2
    # The owner still sees everything.
    owner_by_day = {
        d["day"]: d["edges"] for d in entity_graph_activity(conn, guard=_owner(conn))["days"]
    }
    assert owner_by_day["2026-06-02"] == 3


def test_empty_graph_returns_an_empty_axis_not_an_error(tmp_path):
    c = sqlite3.connect(str(tmp_path / "empty.db"))
    apply_all_migrations(c)
    out = graph_activity_daily(c)
    assert out["days"] == []
    assert out["meta"]["max_edges"] == 0
    assert out["meta"]["first_day"] is None
