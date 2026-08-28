"""A place name is not a mood, and a cluster of place names is not a trend.

A fan-out child's entire document is a place name. Until the table stamp was
corrected those children were filed into ``wellbeing``, so the clusterer grouped
them and named the group as a trend. On the owner's node 2026-08-27, **11 of 186
clusters were majority-child and 4 entirely so** — an apartment name and a lake
were being presented back to the owner as "Wellbeing Trend".

The stamp fix stops new ones forming and cannot unmake these. They are DELETED
rather than relabelled because the grouping itself is the artifact: there is no
honest label for "these records share a place name that was mistaken for a
mood". The records are untouched and cluster normally on the next real pass.

The threshold is a majority on purpose. A real cluster that happens to catch one
or two stubs is still a real cluster, and deleting it would cost the owner
something true to remove something false.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.topic_clustering import retract_fanout_stub_clusters


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "tc.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _cluster(conn, cid, label, member_ids):
    conn.execute(
        "INSERT INTO topic_clusters (cluster_id, label, member_count) VALUES (?,?,?)",
        (cid, label, len(member_ids)),
    )
    for i, rid in enumerate(member_ids):
        conn.execute(
            "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id, source_id)"
            " VALUES (?,?,?,?)",
            (f"{cid}-{i}", cid, rid, "grow_journal"),
        )
    conn.commit()


def _clusters(conn):
    return {r[0] for r in conn.execute("SELECT cluster_id FROM topic_clusters")}


def test_an_all_child_cluster_is_retracted(conn):
    """A live example: 16 members, every one of them a place stub."""
    _cluster(conn, "c-stub", "Wellbeing Trend (a lake)", [f"tl-{i}-loc" for i in range(6)])

    out = retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert out["clusters_retracted"] == 1
    assert _clusters(conn) == set()


def test_a_real_cluster_survives(conn):
    _cluster(conn, "c-real", "job applications", [f"tl-{i}" for i in range(6)])

    out = retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert out["clusters_retracted"] == 0
    assert _clusters(conn) == {"c-real"}


def test_a_real_cluster_that_caught_one_stub_survives(conn):
    """The threshold is a majority; a minority of stubs does not condemn it."""
    _cluster(conn, "c-mixed", "weekend plans",
             ["tl-1", "tl-2", "tl-3", "tl-4", "tl-5-loc"])

    retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert _clusters(conn) == {"c-mixed"}


def test_an_exact_half_is_retracted(conn):
    """Half a cluster being stubs is enough — the label describes both halves."""
    _cluster(conn, "c-half", "somewhere", ["tl-1", "tl-2", "tl-3-loc", "tl-4-loc"])

    retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert _clusters(conn) == set()


def test_members_go_with_the_cluster(conn):
    _cluster(conn, "c-stub", "stub", ["tl-1-loc", "tl-2-loc"])

    retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    left = conn.execute("SELECT COUNT(*) FROM topic_cluster_members").fetchone()[0]
    assert left == 0


def test_the_records_themselves_are_untouched(conn):
    """A cluster is a grouping, not the data. Retracting it removes a claim."""
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, source_id)"
        " VALUES ('tl-1-loc','Somewhere','grow_journal')"
    )
    conn.commit()
    _cluster(conn, "c-stub", "stub", ["tl-1-loc", "tl-2-loc"])

    retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM location_events").fetchone()[0] == 1


def test_the_retraction_dry_runs(conn):
    _cluster(conn, "c-stub", "stub", ["tl-1-loc", "tl-2-loc"])

    out = retract_fanout_stub_clusters(conn)
    conn.commit()

    assert out["clusters_retracted"] == 1
    assert _clusters(conn) == {"c-stub"}


def test_the_retraction_is_idempotent(conn):
    _cluster(conn, "c-stub", "stub", ["tl-1-loc", "tl-2-loc"])

    retract_fanout_stub_clusters(conn, dry_run=False)
    conn.commit()

    assert retract_fanout_stub_clusters(conn)["clusters_retracted"] == 0
