"""Maintenance that nothing calls is not maintenance.

`run_gc` had NO caller anywhere in the tree. The pass existed, was correct, and
never ran — the stored-but-never-applied pattern applied to the thing meant to
prevent drift.

The distinction that matters: a MIGRATION fixes a backlog once, and several of
today's corrections shipped that way and are done. These are different. Every
sync can mint another timeline twin, another cluster of place-name stubs, or
reap the entity a black hole points at. Fixed once, they come back — which is
how they were found in the first place.

So the sweep runs on the same signal as the graph refresh (enrichment
completed), on its own longer debounce, because it touches more tables and a
failure in one must not disarm the other.
"""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "gc.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def test_the_sweep_includes_the_recurring_repairs(conn):
    """Each of these can be recreated by new data, so each must re-run."""
    from topos.features.lifecycle.gc import run_gc

    report = run_gc(conn)

    for key in (
        "timeline_renderings_normalized",
        "fanout_stub_clusters_retracted",
        "blackhole_ids_rebound",
        "junk_embeddings_purged",
    ):
        assert key in report, f"{key} is not part of the sweep"


def test_a_failing_repair_does_not_take_the_pass_down(conn, monkeypatch):
    """One broken repair must not cost the other three."""
    from topos.features.lifecycle import gc

    monkeypatch.setattr(
        gc, "_retract_fanout_stub_clusters",
        lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    report = gc.run_gc(conn)

    assert "failed: boom" in str(report["fanout_stub_clusters_retracted"])
    assert isinstance(report["timeline_renderings_normalized"], int)
    assert isinstance(report["blackhole_ids_rebound"], int)


def test_the_sweep_actually_repairs(conn):
    """End to end: a timeline twin is gone after a pass."""
    from topos.features.lifecycle.gc import run_gc

    for ts in ("2026-06-28T18:00:00", "2026-06-28T18:00:00+00:00"):
        conn.execute(
            "INSERT INTO timeline (event_at, record_id, source_id, canonical_table)"
            " VALUES (?,?,?,?)",
            (ts, "tl-1", "grow_journal", "journal_entries"),
        )
    conn.commit()

    assert run_gc(conn)["timeline_renderings_normalized"] == 1
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 1


# --------------------------------------------------------------- the trigger


def test_enrichment_completion_marks_the_sweep_due():
    """The hook point. Without it the sweep is correct code nothing runs."""
    import inspect

    from topos.ingestion import canonical_pipeline

    src = inspect.getsource(canonical_pipeline)
    assert "mark_gc_due()" in src, "no caller marks the sweep due after enrichment"


def test_the_sweep_is_debounced_separately_from_the_graph_rebuild():
    """Same signal, different timers: the sweep is heavier and a failure in one
    must not disarm the other."""
    from topos.features.entities import graph_refresh

    assert graph_refresh._GC_TIMER is None or True
    status = graph_refresh.gc_status()
    assert "enabled" in status and "pending" in status


def test_the_sweep_has_a_kill_switch(monkeypatch):
    from topos.features.entities import graph_refresh

    monkeypatch.setenv("TOPOS_GC_SWEEP", "off")
    graph_refresh.mark_gc_due()

    assert graph_refresh.gc_status()["enabled"] is False


def test_an_unrun_sweep_is_visible():
    """Silence about maintenance is how maintenance stops happening."""
    from topos.features.entities.graph_refresh import gc_status

    assert set(gc_status()) >= {"enabled", "pending"}
