"""The `rel.closeness_tier` lens — dispatch, and reading the L1 rail.

`relationships.social` has declared this lens since the catalog was written and
`synthesize_closeness` was implemented, but nothing dispatched `synthesis[]`, so
the predicate stayed empty while facts_direct asked for it on every closeness
question.

The lens READS `messenger_dyad_stats` rather than deriving interaction itself:
the 2026-08-25 owner decision made the rail the analytical view and closeness_tier
the durable fact view, sharing evidence rather than each computing it.
"""

import sqlite3

import pytest

from topos.features.derivation.person_bridge import (handle_to_entity,
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
      CREATE TABLE entity_blackholes (blackhole_id TEXT, normalized_name TEXT);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER, a_to_b INTEGER,
        b_to_a INTEGER, balance REAL, reciprocal_periods INTEGER,
        longest_reciprocal_streak_months INTEGER, recent_gap_days REAL, tie_state TEXT);
    """)
    conn.execute("INSERT INTO entities VALUES ('e-owner','self','person',1,NULL)")
    people = [
        # entity, name, handles                       (a person may hold several)
        ("e1", "Two Phones", ["+15125550001", "+15125550011"]),
        ("e2", "One Sided", ["+15125550002"]),
        ("e3", "Gone Quiet", ["+15125550003"]),
        ("e4", "A Newsletter", ["+15125550004"]),
    ]
    for eid, name, handles in people:
        cid = f"c-{eid}"
        conn.execute("INSERT INTO contacts VALUES (?,?)", (cid, name))
        conn.execute("INSERT INTO entities VALUES (?,?,'person',0,?)", (eid, name, cid))
        for h in handles:
            conn.execute("INSERT INTO contact_identifiers VALUES (?,?,'phone')", (cid, h))

    def dyad(handle, total, balance, streak, gap, tie, peer="human"):
        conn.execute(
            "INSERT INTO messenger_dyad_stats VALUES ('ds',?, 'self', 1, ?, ?, 0, 0, ?, 3, ?, ?, ?)",
            (handle, peer, total, balance, streak, gap, tie))

    dyad("+15125550001", 100, 0.02, 6, 2.0, "active")     # split across two handles
    dyad("+15125550011", 40, 0.05, 4, 5.0, "active")
    dyad("+15125550002", 80, 0.9, 1, 3.0, "one_sided")    # volume, no reciprocity
    dyad("+15125550003", 90, 0.05, 6, 400.0, "dormant")   # was close, long silent
    dyad("+15125550004", 500, -1.0, 0, 1.0, "broadcast_only")
    conn.commit()
    return conn


def _run(db, **kw):
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.derivation.synthesize import run_pack_lenses

    calls = []

    class StubWriter:
        def assert_pack_fact(self, **k):
            calls.append(k)
            return {"outcome": "written"}

    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    report = run_pack_lenses(db, StubWriter(), pack, owner="e-owner", **kw)
    return report, calls


def test_handles_normalise_across_both_sides_of_the_join():
    assert normalise_handle("+1 (555) 555-0105") == normalise_handle("+15555550105")
    assert normalise_handle("Camille@Example.com") == "camille@example.com"


def test_an_identifier_is_never_a_person_name():
    assert looks_like_a_person_name("Mike November")
    assert not looks_like_a_person_name("+15555550106")   # unnamed contact carries its handle
    assert not looks_like_a_person_name("self")           # the owner's own entity
    assert not looks_like_a_person_name("unknown:0")      # has letters, still an id
    assert not looks_like_a_person_name("shop.example@forgotten.invalid")


def test_the_bridge_maps_every_handle_a_person_holds(db):
    by_handle = handle_to_entity(db)
    assert by_handle[normalise_handle("+15125550001")] == "e1"
    assert by_handle[normalise_handle("+15125550011")] == "e1"


def test_one_person_with_several_handles_is_one_fact_with_combined_traffic(db):
    _, calls = _run(db)
    people = [c["value"]["person"] for c in calls]
    assert people.count("Two Phones") == 1              # was listed twice, split 105/31
    note = next(c["source_refs"][0]["note"] for c in calls
                if c["value"]["person"] == "Two Phones")
    assert "140 msgs" in note                           # 100 + 40, not either alone


def test_a_broadcast_channel_is_not_a_relationship(db):
    _, calls = _run(db)
    assert "A Newsletter" not in [c["value"]["person"] for c in calls]


def test_reciprocity_and_liveness_outrank_raw_volume(db):
    _, calls = _run(db)
    order = [c["value"]["person"] for c in calls]
    # 140 mutual and current beats 80 one-sided and 90 long-dormant
    assert order[0] == "Two Phones"
    assert order.index("Two Phones") < order.index("One Sided")
    assert order.index("Two Phones") < order.index("Gone Quiet")


def test_the_owner_is_never_a_member_of_their_own_circle(db):
    _, calls = _run(db)
    assert "self" not in [c["value"]["person"] for c in calls]


def test_a_blackholed_person_stays_erased(db):
    db.execute("INSERT INTO entity_blackholes VALUES ('b1','two phones')")
    db.commit()
    _, calls = _run(db)
    assert "Two Phones" not in [c["value"]["person"] for c in calls]


def test_the_lens_abstains_when_the_rail_has_not_run(tmp_path):
    conn = sqlite3.connect(tmp_path / "empty.db")
    conn.executescript("""
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT,
        entity_type TEXT, is_self INTEGER DEFAULT 0, contact_id TEXT);
    """)
    report, calls = _run(conn)
    assert calls == [] and report["facts_written"] == 0


def test_the_dispatcher_runs_what_is_implemented_and_reports_the_rest(db):
    """55 lenses are declared across the catalog and three are implemented, so
    'declared, not implemented' has to be a normal outcome, not an error."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.derivation.synthesize import _declared_lenses

    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    # Read through the accessor, not `pack.lenses`: the parsed Lens objects are a
    # peer's uncommitted work, and a dispatcher that only runs against one session's
    # working tree is not a dispatcher.
    kinds = {(l.kind, tuple(l.predicates)) for l in _declared_lenses(pack)}
    assert ("graph_labeling", ("rel.closeness_tier",)) in kinds

    report, _ = _run(db)
    assert "rel.closeness_tier" in {r["predicate"] for r in report["ran"]}
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


def test_the_known_item_aliases_cover_how_people_actually_ask():
    """Measured against live chat turns: each of these matched NOTHING, so the
    deterministic lane never fired and a 9B model answered from generic retrieval —
    "Do I have any siblings?" came back as a bare "Yes." with the brother in hand."""
    from topos.query.facts_direct import match_known_item

    def preds(q):
        m = match_known_item(q)
        return m["predicates"] if m else []

    # bare role words carry the owner frame in "I", not in "my <role>"
    assert "rel.relationship" in preds("Do I have any siblings?")
    assert "rel.relationship" in preds("Who are my parents?")
    # person-scoped and comparative, not only owner-scoped list questions
    for q in ("How close am I to Mike November?",
              "Am I closer to Mike November or Alpha Xray?",
              "Who should I reconnect with?",
              "Who have I drifted away from?",
              "Who haven't I talked to in a while?"):
        assert "rel.closeness_tier" in preds(q), q
    # and the neighbours still route where they did
    assert preds("What medications am I taking?") == ["health.medication"]
    assert preds("What's on my calendar?") == []


def test_a_question_spanning_two_predicate_families_gets_both():
    """First-match-wins hid one half. "Is my mom in my inner circle?" matches the
    role alias AND the closeness alias; returning only the first meant the tiers
    never reached the answer, and the live reply was "there's no explicit 'inner
    circle' label in your relationship context" while two people held that label."""
    from topos.query.facts_direct import match_known_item

    both = match_known_item("Is my mom in my inner circle?")
    assert both is not None
    assert set(both["predicates"]) >= {"rel.relationship", "rel.closeness_tier"}

    # a single-family question is unchanged
    assert match_known_item("Who is in my family?")["predicates"] == ["rel.relationship"]

    # widening the match must never widen disclosure: special is OR-ed, so a
    # health class still takes the stricter gate
    assert match_known_item("What medications am I taking?")["special"] is True
