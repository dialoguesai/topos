"""Edges must carry EVIDENCE time, not the time the derivation ran.

Two live defects, same shape. `rel.closeness_tier` anchored every fact to the
synthesis clock, so all 26 ranked people landed inside one second and the graph
showed them all as active that instant -- people last spoken to in May rendered
in the most recent few days. `participates_in` passed no time at all, leaving
181 edges unplaceable on any timeline.

Both assert on the DISTRIBUTION across an edge type, not on one edge: a single
edge dated "now" looks perfectly correct on its own. Zero variance across every
row of a type is the signal.
"""

from __future__ import annotations

import sqlite3

import pytest

from datetime import datetime, timezone

from topos.features.entities.edges import update_edge


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations
    c = sqlite3.connect(str(tmp_path / "e.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _distinct_days(conn, edge_type: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(DISTINCT substr(last_event_at,1,10)) FROM entity_edges"
        " WHERE edge_type=? AND last_event_at IS NOT NULL", (edge_type,)
    ).fetchone()[0])


@pytest.fixture()
def rail(tmp_path):
    """A dyad rail carrying `last_ts`, as live nodes do."""
    c = sqlite3.connect(str(tmp_path / "r.db"))
    c.executescript("""
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT,
        entity_type TEXT, is_self INTEGER DEFAULT 0, contact_id TEXT);
      CREATE TABLE entity_blackholes (blackhole_id TEXT, normalized_name TEXT);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER, a_to_b INTEGER,
        b_to_a INTEGER, balance REAL, reciprocal_periods INTEGER,
        longest_reciprocal_streak_months INTEGER, recent_gap_days REAL, tie_state TEXT,
        last_ts TEXT);
    """)
    c.execute("INSERT INTO entities VALUES ('e-owner','self','person',1,NULL)")
    # Two people whose last contact is MONTHS apart. Anchored to the run clock
    # they collapse onto one instant; anchored to evidence they stay apart.
    for eid, name, handle, gap, last_ts in [
        ("e1", "Mike November", "+15125550001", 2.0, "2026-08-25T10:00:00Z"),
        ("e2", "Sierra Quebec", "+15125550002", 95.0, "2026-05-22T09:00:00Z"),
    ]:
        cid = f"c-{eid}"
        c.execute("INSERT INTO contacts VALUES (?,?)", (cid, name))
        c.execute("INSERT INTO entities VALUES (?,?,'person',0,?)", (eid, name, cid))
        c.execute("INSERT INTO contact_identifiers VALUES (?,?,'phone')", (cid, handle))
        c.execute(
            "INSERT INTO messenger_dyad_stats VALUES "
            "('ds',?,'self',1,'human',60,0,0,0.05,3,5,?, 'active', ?)",
            (handle, gap, last_ts))
    c.commit()
    yield c
    c.close()


def _closeness_calls(db):
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.derivation.synthesize import run_pack_lenses

    calls = []

    class StubWriter:
        def assert_pack_fact(self, **k):
            calls.append(k)
            return {"outcome": "written"}

    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    run_pack_lenses(db, StubWriter(), pack, owner="e-owner")
    return calls


def test_each_tier_anchors_to_that_dyads_last_contact(rail):
    calls = _closeness_calls(rail)
    dates = {c["value"]["person"]: c.get("event_date") for c in calls}
    assert dates["Mike November"] == "2026-08-25T10:00:00Z"
    assert dates["Sierra Quebec"] == "2026-05-22T09:00:00Z"


def test_tiers_do_not_all_collapse_onto_one_instant(rail):
    """The live symptom: every ranked person appearing active the same second."""
    calls = _closeness_calls(rail)
    anchors = {str(c.get("event_date") or "")[:10] for c in calls}
    assert len(anchors) > 1, f"all tiers share one anchor: {anchors}"


def test_a_rail_without_last_ts_still_anchors_in_the_past(rail):
    """Older rails lack the column. The lens must degrade, never vanish.

    Returning [] here would delete the whole closeness lens silently, which is
    what naming the column unconditionally did.
    """
    rail.execute("ALTER TABLE messenger_dyad_stats RENAME COLUMN last_ts TO last_ts_old")
    rail.commit()
    calls = _closeness_calls(rail)
    assert calls, "the lens must still produce facts without `last_ts`"
    # 95 days ago is not today: the gap still places this person in the past.
    quiet = next(c for c in calls if c["value"]["person"] == "Sierra Quebec")
    assert quiet.get("event_date"), "a gap-derived date should still anchor the fact"
    assert quiet["event_date"][:10] < datetime.now(timezone.utc).date().isoformat()


def test_an_edge_type_dated_entirely_within_one_second_is_a_defect(conn):
    """The distribution check itself, on synthetic edges.

    26 rows sharing a timestamp to the second is not a coincidence -- it is a
    run clock. This is the assertion a live-data guard would make.
    """
    from topos.features.entities.resolver import EntityResolver
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    people = [r._create_entity(f"Person {i}", "person") for i in range(8)]
    conn.commit()
    # Every edge stamped at the same instant -- the bug's shape.
    for p in people:
        update_edge(conn, src_entity_id=owner, dst_entity_id=p,
                    edge_type="rel.closeness_tier", event_at="2026-08-26T22:40:40Z")
    conn.commit()
    assert _distinct_days(conn, "rel.closeness_tier") == 1

    # Dated from each dyad's real last contact, the spread returns.
    for i, p in enumerate(people):
        conn.execute(
            "UPDATE entity_edges SET last_event_at=? WHERE dst_entity_id=?"
            " AND edge_type='rel.closeness_tier'",
            (f"2026-0{(i % 5) + 4}-1{i % 9}T12:00:00Z", p),
        )
    conn.commit()
    assert _distinct_days(conn, "rel.closeness_tier") > 1, (
        "a closeness edge per person should spread across their real last contacts"
    )
