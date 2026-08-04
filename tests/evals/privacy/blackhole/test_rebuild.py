"""M4: the rebuild that makes a black hole real for prose artifacts.

Read-time filtering handles anything with an entity id. Briefs, digests,
dossiers and stat group keys carry the name as text, and no predicate can
un-write those — they have to be withdrawn. These tests check that they are,
that the D4 notification lifecycle closes, and that the owner keeps everything.
"""

from __future__ import annotations

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.blackhole_guard import guard_for
from topos.features.lifecycle.blackhole_rebuild import (
    PROSE_OBJECT_TYPES,
    rebuild_for_blackhole,
    run_pending_rebuilds,
)
from tests.evals.privacy.blackhole.corpus import BH_CANONICAL, BH_ID, build_blackhole_corpus
from tests.evals.privacy.blackhole.surfaces import read_all, serialize

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


@pytest.fixture()
def pending(tmp_path):
    """A corpus whose black holes are flagged but not yet rebuilt."""
    c = build_blackhole_corpus(str(tmp_path / "rebuild.db"), rebuild_complete=False)
    yield c
    c.conn.close()


def _brief_bodies(conn):
    return [r[0] for r in conn.execute("SELECT markdown_body FROM signal_dimension_briefs")]


# ------------------------------------------------------------- the rebuild


def test_rebuild_withdraws_prose_that_names_the_entity(pending):
    before = " ".join(_brief_bodies(pending.conn))
    assert BH_CANONICAL in before, "fixture must start with the name present"

    report = rebuild_for_blackhole(pending.conn, BH_ID)

    after = " ".join(_brief_bodies(pending.conn))
    assert BH_CANONICAL not in after
    assert report.briefs_invalidated >= 1


def test_rebuild_leaves_unrelated_prose_alone(pending):
    """Surgical: the control entity's brief must survive untouched."""
    rebuild_for_blackhole(pending.conn, BH_ID)

    bodies = [b for b in _brief_bodies(pending.conn) if b]
    assert any(pending.control_tokens["brief"] in b for b in bodies)


def test_rebuild_closes_prose_signal_objects(pending):
    report = rebuild_for_blackhole(pending.conn, BH_ID)

    assert report.objects_closed >= 1
    live = pending.conn.execute(
        f"""
        SELECT payload_json FROM signal_objects
        WHERE valid_to IS NULL
          AND object_type IN ({','.join('?' for _ in PROSE_OBJECT_TYPES)})
        """,
        PROSE_OBJECT_TYPES,
    ).fetchall()
    assert BH_CANONICAL not in " ".join(str(r[0]) for r in live)


def test_rebuild_leaves_id_joinable_facts_intact(pending):
    """Facts are keyed by entity id, so read-time filtering already covers them —
    and withdrawing them would delete something the owner should keep seeing.
    The rebuild is only for artifacts no predicate can reach."""
    rebuild_for_blackhole(pending.conn, BH_ID)

    facts = pending.conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
    ).fetchall()
    assert any(BH_CANONICAL in str(r[0]) for r in facts)
    # ...and the read path still keeps them from a grantee.
    guard = guard_for(pending.conn, mcp_source="rpt")
    from tests.evals.privacy.blackhole.surfaces import SURFACES

    assert BH_CANONICAL not in serialize(SURFACES["facts"](pending.conn, guard))


def test_rebuild_removes_stat_insights_grouped_by_the_entity(pending):
    report = rebuild_for_blackhole(pending.conn, BH_ID)

    assert report.stat_insights_removed >= 1
    rows = pending.conn.execute("SELECT payload_json FROM signal_facts").fetchall()
    assert BH_CANONICAL not in " ".join(str(r[0]) for r in rows)


def test_rebuild_removes_the_entity_context_vector(pending):
    """Centroids feed affinity production — withdraw them with the blackhole."""
    before = pending.conn.execute(
        "SELECT COUNT(*) FROM entity_context_vectors WHERE entity_id=?",
        (BH_ID,),
    ).fetchone()[0]
    assert before == 1

    report = rebuild_for_blackhole(pending.conn, BH_ID)

    assert report.context_vectors_removed == 1
    after = pending.conn.execute(
        "SELECT COUNT(*) FROM entity_context_vectors WHERE entity_id=?",
        (BH_ID,),
    ).fetchone()[0]
    assert after == 0
    # Affinity edges stay (id-joinable; owner keeps them, guard hides them).
    affinity = pending.conn.execute(
        "SELECT COUNT(*) FROM entity_edges "
        "WHERE edge_type='semantic_affinity' AND valid_to IS NULL "
        "AND (src_entity_id=? OR dst_entity_id=?)",
        (BH_ID, BH_ID),
    ).fetchone()[0]
    assert affinity >= 1


def test_withdrawal_not_redaction(pending):
    """D3 — a body with a name-shaped hole still says someone was there."""
    rebuild_for_blackhole(pending.conn, BH_ID)

    bodies = _brief_bodies(pending.conn)
    # The naming brief is emptied outright, not left as "A quiet week with [redacted]".
    assert "" in bodies
    assert not any("redact" in (b or "").lower() for b in bodies)


# ------------------------------------------------- D4 notification lifecycle


def test_rebuild_resolves_the_notification_and_lifts_the_withholding(pending):
    store = BlackholeStore(pending.conn)
    assert {n["kind"] for n in store.notifications(state="open")} == {"rebuild_needed"}
    guard = guard_for(pending.conn, mcp_source="rpt")
    assert guard.withhold_pending_rebuild() is True

    rebuild_for_blackhole(pending.conn, BH_ID)
    rebuild_for_blackhole(pending.conn, pending.quiet_entity_id)

    open_kinds = {n["kind"] for n in store.notifications(state="open")}
    assert "rebuild_needed" not in open_kinds
    assert "rebuild_complete" in open_kinds
    # A fresh guard: the withholding is lifted only once nothing is pending.
    assert guard_for(pending.conn, mcp_source="rpt").withhold_pending_rebuild() is False


def test_run_pending_rebuilds_processes_everything_outstanding(pending):
    reports = run_pending_rebuilds(pending.conn)

    assert len(reports) == 2  # both protected entities in the corpus
    assert all(r["status"] == "complete" for r in reports)
    assert BlackholeStore(pending.conn).has_pending_rebuild() is False


def test_rebuild_is_idempotent(pending):
    first = rebuild_for_blackhole(pending.conn, BH_ID)
    second = rebuild_for_blackhole(pending.conn, BH_ID)

    assert first.details["status"] == "complete"
    assert second.details["status"] == "complete"
    assert second.briefs_invalidated == 0  # nothing left to withdraw


def test_rebuild_of_an_unprotected_entity_is_a_noop(pending):
    report = rebuild_for_blackhole(pending.conn, "ent-not-protected")
    assert report.details["status"] == "not_blackholed"


# --------------------------------------------------------- owner fidelity


def test_owner_still_sees_everything_after_the_rebuild(pending):
    """The rebuild withdraws artifacts from *serving*; the owner's view of the
    entity and its records is untouched."""
    run_pending_rebuilds(pending.conn)

    owner = guard_for(pending.conn, mcp_source="topos_home_chat")
    blob = serialize(read_all(pending.conn, owner))

    assert BH_CANONICAL in blob
    assert pending.tokens["mention_surface"] in blob
    assert pending.tokens["raw_canonical"] in blob


def test_no_leak_survives_the_rebuild(pending):
    """The whole battery's invariant, re-checked in the post-rebuild state."""
    run_pending_rebuilds(pending.conn)

    for source in ("rpt", "claude_desktop", "routine_executor", None):
        guard = guard_for(pending.conn, mcp_source=source)
        blob = serialize(read_all(pending.conn, guard)).lower()
        leaked = [t for t in pending.all_bh_tokens if t.lower() in blob]
        assert leaked == [], f"{source}: {leaked}"
