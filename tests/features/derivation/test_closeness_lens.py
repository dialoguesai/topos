"""The `rel.closeness_tier` lens — dispatch, stats, and the guards on who counts.

`relationships.social` has declared this lens since the catalog was written, and
`synthesize_closeness` was implemented — but nothing dispatched `Pack.lenses`, so
the predicate stayed empty while facts_direct asked for it on every closeness
question.
"""

import sqlite3

import pytest

from topos.features.derivation.comms_stats import (comms_stats,
                                                   looks_like_a_person_name,
                                                   normalise_handle)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "l.db")
    conn.executescript("""
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT,
        entity_type TEXT, is_self INTEGER DEFAULT 0, contact_id TEXT);
      CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, sender_id TEXT,
        is_from_self INTEGER DEFAULT 0, event_at TEXT, conversation_id TEXT, source_id TEXT);
      CREATE TABLE conversation_participants (conversation_id TEXT, contact_id TEXT);
      CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT, dst_entity_id TEXT,
        edge_type TEXT, weight REAL, valid_to TEXT);
      CREATE TABLE entity_blackholes (blackhole_id TEXT, normalized_name TEXT);
    """)
    conn.execute("INSERT INTO entities VALUES ('e-owner','self','person',1,NULL)")
    for eid, w in (("e1", 40.0), ("e2", 90.0)):
        conn.execute("INSERT INTO entity_edges VALUES (?, 'e-owner', ?, 'communicates_with', ?, NULL)",
                     (f"edge-{eid}", eid, w))
    # one mutual 1:1 partner, one group-only partner
    for cid, name, ident, eid in [("c1", "Mutual Friend", "+15125550001", "e1"),
                                  ("c2", "Group Only", "+15125550002", "e2")]:
        conn.execute("INSERT INTO contacts VALUES (?,?)", (cid, name))
        conn.execute("INSERT INTO contact_identifiers VALUES (?,?,'phone')", (cid, ident))
        conn.execute("INSERT INTO entities VALUES (?,?,'person',0,?)", (eid, name, cid))
    conn.execute("INSERT INTO conversation_participants VALUES ('conv1','c1')")
    for c in ("c2", "cX", "cY"):
        conn.execute("INSERT INTO conversation_participants VALUES ('conv2',?)", (c,))

    n = 0
    def msg(sender, is_self, conv, at="2026-08-20T09:00:00+00:00"):
        nonlocal n
        n += 1
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?, 'imessage')",
                     (f"m{n}", sender, is_self, at, conv))
    for _ in range(6):
        msg("+15125550001", 0, "conv1")          # inbound, 1:1
        msg("owner-handle", 1, "conv1")          # outbound, 1:1
    for _ in range(10):
        msg("+15125550002", 0, "conv2")          # inbound, group
    # far outside any 90d window — must not count
    msg("+15125550001", 0, "conv1", at="2020-01-01T09:00:00+00:00")
    conn.commit()
    return conn


def test_handles_normalise_across_both_sides_of_the_join():
    assert normalise_handle("+1 (512) 740-0415") == normalise_handle("+15127400415")
    assert normalise_handle("Camille@Example.com") == "camille@example.com"


def test_an_identifier_is_never_a_person_name():
    assert looks_like_a_person_name("Mike November")
    assert not looks_like_a_person_name("+17184834576")   # unnamed contact carries its handle
    assert not looks_like_a_person_name("self")           # the owner's own entity
    assert not looks_like_a_person_name("unknown:0")      # has letters, still an id
    assert not looks_like_a_person_name("apple.com@forgotten.name")


def test_both_directions_are_counted_and_balance_is_derived(db):
    st = comms_stats(db, window_days=90, now=_aug26())
    mutual = st["e1"]
    assert mutual["inbound"] == 6 and mutual["outbound"] == 6
    assert mutual["initiation_balance"] == 0.5          # an even exchange
    assert mutual["one_to_one_share"] == 1.0


def test_a_one_sided_correspondent_is_visible_as_one(db):
    st = comms_stats(db, window_days=90, now=_aug26())
    group = st["e2"]
    assert group["inbound"] == 10 and group["outbound"] == 0
    assert group["initiation_balance"] == 0.0          # talking AT the owner
    assert group["one_to_one_share"] == 0.0            # never in a 1:1 thread


def test_the_evidence_window_excludes_older_traffic(db):
    wide = comms_stats(db, window_days=None, now=_aug26())
    narrow = comms_stats(db, window_days=90, now=_aug26())
    assert wide["e1"]["inbound"] == 7                   # the 2020 message counts
    assert narrow["e1"]["inbound"] == 6                 # ...and is outside 90d


def test_the_window_anchors_to_the_data_not_the_wall_clock(db):
    """A node that has not synced for a fortnight must not report everyone quiet."""
    st = comms_stats(db, window_days=90)                # no `now` supplied
    assert st["e1"]["inbound"] == 6


def _aug26():
    from datetime import datetime, timezone
    return datetime(2026, 8, 26, tzinfo=timezone.utc)


def test_the_dispatcher_runs_what_is_implemented_and_reports_the_rest(db):
    """55 lenses are declared across the catalog and three are implemented, so
    'declared, not implemented' has to be a normal outcome, not an error."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.derivation.synthesize import run_pack_lenses

    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    kinds = {(l.kind, tuple(l.predicates)) for l in pack.lenses}
    assert ("graph_labeling", ("rel.closeness_tier",)) in kinds

    calls = []

    class StubWriter:
        def assert_pack_fact(self, **kw):
            calls.append(kw)
            return {"outcome": "written"}

    report = run_pack_lenses(db, StubWriter(), pack, owner="e-owner")
    ran = {r["predicate"] for r in report["ran"]}
    assert "rel.closeness_tier" in ran, report["skipped"]
    # the owner is never a member of their own circle
    people = {c["value"]["person"] for c in calls}
    assert people == {"Mutual Friend", "Group Only"}
    # the mutual 1:1 partner outranks the group-only one despite lower volume
    tiers = {c["value"]["person"]: c["value"]["tier"] for c in calls}
    order = ["inner_circle", "close", "regular", "peripheral"]
    assert order.index(tiers["Mutual Friend"]) <= order.index(tiers["Group Only"])
    # the lens's own floor is honoured rather than hardcoded
    assert next(r for r in report["ran"] if r["predicate"] == "rel.closeness_tier")["window_days"] == 90
    assert any(s.get("reason") == "declared, not implemented" for s in report["skipped"])


def test_a_lens_that_raises_does_not_sink_the_pack(db):
    from topos.features.derivation import synthesize as S
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    original = S._LENS_IMPLS["rel.closeness_tier"]

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    S._LENS_IMPLS["rel.closeness_tier"] = (boom, True)
    try:
        report = S.run_pack_lenses(db, object(), pack, owner="e-owner")
    finally:
        S._LENS_IMPLS["rel.closeness_tier"] = original
    assert report["facts_written"] == 0
    assert any("synthetic failure" in str(s.get("reason", "")) for s in report["skipped"])
