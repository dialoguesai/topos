"""G5 — THE BENCH: roles from the owner's record, candidates by demonstrated work.

The quality history is the test plan: the first role builder produced "into / accomplished /
things" (journal furniture), so the tests pin what separates a role from a writing habit.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation.social_bench import (
    MIN_EVIDENCE,
    MIN_RECURRENCE_WEEKS,
    MIN_WORK_RECORDS,
    build_bench_slate,
    build_role_corpus,
    build_role_shapes,
    build_role_shapes_from_clusters,
    find_candidates,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "b.db"))
    c.executescript("""
      CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT,
        source_record_id TEXT);
      CREATE TABLE user_goals (goal_id TEXT PRIMARY KEY, record_id TEXT, goal_text TEXT);
      CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, event_at TEXT);
      CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
        member_count INTEGER, label_terms_json TEXT);
      CREATE TABLE topic_cluster_members (member_id TEXT PRIMARY KEY, cluster_id TEXT,
        record_id TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_key TEXT,
        payload_json TEXT, ontology_id TEXT, valid_to TEXT);
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        a_person_id TEXT, b_person_id TEXT, warmth_band TEXT);
    """)
    yield c
    c.close()


def _journal(conn, eid, day, text):
    conn.execute("INSERT INTO journal_entries VALUES (?,?,?,NULL)",
                 (eid, f"2026-{day}T09:00:00", text))


def test_goals_are_dated_through_their_record_never_created_at(conn):
    """The extraction-time trap, pinned: an undatable goal is DROPPED."""
    _journal(conn, "j1", "05-01", "reviewed the retrieval eval")
    conn.execute("INSERT INTO user_goals VALUES ('g1', 'j1', 'ship the eval harness')")
    conn.execute("INSERT INTO user_goals VALUES ('g_orphan', 'nowhere', 'undatable goal')")
    conn.commit()
    corpus = build_role_corpus(conn)
    texts = {r["text"] for r in corpus}
    assert "ship the eval harness" in texts
    assert "undatable goal" not in texts, "no event time, no corpus row — never created_at"


def test_duplicate_ingests_count_once(conn):
    """The grow connector delivers the same journal through two sources."""
    _journal(conn, "j1", "05-01", "same text")
    _journal(conn, "j2", "05-01", "same text")
    conn.commit()
    assert len(build_role_corpus(conn)) == 1


def test_cluster_roles_need_recurrence_not_just_size(conn):
    """A big cluster from one busy week is a task. Recurrence is the role signal."""
    conn.execute("INSERT INTO topic_clusters VALUES ('tc1','merge / branch','work',30,'[\"merge\",\"branch\"]')")
    conn.execute("INSERT INTO topic_clusters VALUES ('tc2','relay / engine','work',30,'[\"relay\",\"engine\"]')")
    for i in range(6):  # tc1: six entries across six ISO weeks
        _journal(conn, f"a{i}", f"0{(i % 6) + 1}-0{i + 1}", "merge work")
        conn.execute("INSERT INTO topic_cluster_members VALUES (?,?,?)", (f"m{i}", "tc1", f"a{i}"))
    for i in range(6):  # tc2: six entries in ONE week
        _journal(conn, f"b{i}", "05-0" + str(i + 1), "relay work")
    for i in range(6):
        conn.execute("INSERT INTO topic_cluster_members VALUES (?,?,?)", (f"n{i}", "tc2", f"b{i}"))
    conn.commit()
    shapes = build_role_shapes_from_clusters(conn)
    ids = [s["role_shape_id"] for s in shapes]
    assert "role:tc1" in ids
    assert "role:tc2" not in ids, "one busy week is not a role"


def test_journal_furniture_cannot_become_a_role(conn):
    """The regression that motivated the cluster-first design: template vocabulary recurs
    weekly by definition, and the term fallback must not rank it."""
    for i in range(8):
        _journal(conn, f"j{i}", f"0{(i % 6) + 1}-1{i % 3}",
                 "today I accomplished things and got into the way of first next items")
    conn.commit()
    shapes = build_role_shapes(conn)
    for s in shapes:
        for bad in ("accomplished", "into", "things", "first", "next", "way"):
            assert bad not in s["label_terms"], f"furniture term {bad!r} became a role"


def test_the_slate_states_what_is_missing(conn):
    conn.execute("INSERT INTO topic_clusters VALUES ('tc1','merge / branch','work',30,'[\"merge\",\"branch\"]')")
    for i in range(6):
        _journal(conn, f"a{i}", f"0{(i % 6) + 1}-0{i + 1}", "merge work")
        conn.execute("INSERT INTO topic_cluster_members VALUES (?,?,?)", (f"m{i}", "tc1", f"a{i}"))
    conn.commit()
    slate = build_bench_slate(conn)
    assert slate["roles"], "a role exists"
    assert slate["roles_without_candidates"] == [slate["roles"][0]["label"]], \
        "no capability facts -> the gap IS the answer, as a field"
    assert "blocking_signal" in slate["coverage"], "blocking is stated unavailable, not faked"
    assert slate["roles"][0]["blocking_score"] is None


def test_candidates_come_from_demonstrated_skill_and_order_by_warmth(conn):
    role = {"label_terms": ["merge", "branch", "release"]}
    for eid, name, skill, band in (
            ("ent_a", "Priya", "release branch management", "steady"),
            ("ent_b", "Marcus", "branch merge tooling", "warm"),
            ("ent_c", "Dana", "gardening", "warm")):
        conn.execute("INSERT INTO entities VALUES (?,?)", (eid, name))
        conn.execute("INSERT INTO signal_objects VALUES (?,?,?,?,NULL)",
                     (f"o_{eid}", f"fact:{eid}:net.demonstrated_skill",
                      json.dumps({"predicate": "net.demonstrated_skill",
                                  "value_struct": {"person": name, "skill": skill,
                                                   "basis": "shipped_artifact"}}),
                      "net.capability"))
        conn.execute("INSERT INTO messenger_dyad_stats VALUES ('ds','self',?,NULL,?,?)",
                     (eid, eid, band))
    conn.commit()
    cands = find_candidates(conn, role)
    assert [c["name"] for c in cands] == ["Marcus", "Priya"], \
        "skill overlap required (no Dana), then warmth order"
    assert cands[0]["warmth_band"] == "warm"


def test_floors_are_what_the_docstrings_say():
    assert MIN_RECURRENCE_WEEKS == 3 and MIN_EVIDENCE == 5


# --------------------------------------------------------------------------- 2026-08-27

def _goal(conn, gid, eid, day, text):
    """A goal dated through a journal record, which is the only dating this accepts."""
    conn.execute("INSERT OR IGNORE INTO journal_entries VALUES (?,?,?,NULL)",
                 (eid, f"2026-{day}T09:00:00", "carrier"))
    conn.execute("INSERT INTO user_goals VALUES (?,?,?)", (gid, eid, text))


def _day(i):
    """Spread across months and days so distinct ISO weeks accumulate."""
    return f"0{(i % 6) + 1}-{(i % 27) + 1:02d}"


def test_a_role_needs_mass_not_only_rarity(conn):
    """The live defect: `weeks x idf` ranked `place` (23 records, 14 weeks) at 63.8 above
    `topos` (248 records, 16 weeks) at 34.8.

    The corpus spans a handful of weeks, so the week count saturates and idf -- which
    rewards RARITY -- decides the order. Among terms that recur every week, the rarest won.
    Filtering the furniture of a writing habit is the df band's job, and doing it twice
    inverted the answer.
    """
    # `platform` in 14% of records, `sundial` in 5%, over the same span, in DISJOINT
    # records with separate vocabulary — otherwise they co-occur and land in one shape.
    platform_at = {i for i in range(200) if i % 7 == 0}
    sundial_at = {i for i in range(200) if i % 20 == 1} - platform_at
    for i in range(200):
        text = "assorted%d notes" % (i % 37)
        if i in platform_at:
            text += " platform deployment migration"
        if i in sundial_at:
            text += " sundial calibration"
        _goal(conn, f"g{i}", f"e{i}", _day(i), text)
    conn.commit()
    shapes = build_role_shapes(conn, top_n=40)

    def rank(term):
        for i, sh in enumerate(shapes):
            if term in sh["label_terms"]:
                return i
        return None

    assert rank("platform") is not None, "the theme with a body of work behind it is a role"
    assert rank("sundial") is None or rank("platform") < rank("sundial"), \
        "recurrence x mass: a rare term must not outrank the work it recurs beside"
    # And the arithmetic itself, so the ordering above cannot pass for the wrong reason.
    def shape_for(term):
        return next((sh for sh in shapes if term in sh["label_terms"]), None)

    heavy, rare = shape_for("platform"), shape_for("sundial")
    if heavy and rare and heavy is not rare:
        assert heavy["score"] > rare["score"]
        assert heavy["evidence_count"] > rare["evidence_count"]


def test_the_diary_does_not_supply_the_roles_when_a_work_record_exists(conn):
    """84% of the live corpus was goals and every role came from the other 16%: `little`,
    `something`, `lot`, `too` and `him` are 100% journal and zero goals.

    Both substrates are owner-authored, but they are not one corpus -- goals state work and
    entries narrate a life. Pooled, the document frequencies that decide what is
    distinctive are computed across two languages at once, and a word can look
    rare-and-recurring merely by belonging to the smaller one.
    """
    for i in range(200):
        text = "assorted%d notes" % (i % 37)
        if i % 7 == 0:
            text += " ingestion pipeline"
        _goal(conn, f"g{i}", f"e{i}", _day(i), text)
    for i in range(40):
        _journal(conn, f"j{i}", _day(i),
                 "sundial walked" if i % 4 == 0 else "quiet%d evening" % i)
    conn.commit()
    terms = {t for s in build_role_shapes(conn, top_n=30) for t in s["label_terms"]}
    assert "ingestion" in terms or "pipeline" in terms, "the work record names the roles"
    for narrative in ("sundial", "walked"):
        assert narrative not in terms, f"{narrative!r} is diary vocabulary, not a role"


def test_a_thin_work_record_falls_back_and_says_so(conn):
    """A node with almost no goals must still get an answer, and must not claim the answer
    came from a work record it does not have."""
    for i in range(60):
        text = "assorted%d entry" % (i % 21)
        if i % 6 == 0:
            text += " deployment runbook"
        _journal(conn, f"j{i}", _day(i), text)
    conn.commit()
    assert len(build_role_corpus(conn, substrate="work")) < MIN_WORK_RECORDS
    assert build_role_shapes(conn), "a thin work record still produces roles"
    basis = build_bench_slate(conn)["coverage"]["role_basis"]
    assert "too thin" in basis, f"the fallback has to be visible, got {basis!r}"


def test_evidence_count_is_a_count_not_the_sample_cap(conn):
    """`evidence_count` was `len(refs)`, and refs stops at 40 -- so every role with a real
    body of work reported exactly 40, which is both wrong and identical across roles."""
    for i in range(500):
        text = "assorted%d notes" % (i % 61)
        if i % 8 == 0:
            text += " harbour scheduling"
        _goal(conn, f"g{i}", f"e{i}", _day(i), text)
    conn.commit()
    shapes = [s for s in build_role_shapes(conn, top_n=30)
              if "harbour" in s["label_terms"]]
    assert shapes, "the term recurs and has mass"
    assert shapes[0]["evidence_count"] > 40, \
        f"reported {shapes[0]['evidence_count']} — the sample cap leaked into the count"
    assert shapes[0]["evidence_sampled"] <= 40, "the walkable sample stays bounded"


def test_the_slate_names_the_substrate_it_actually_used(conn):
    for i in range(200):
        text = "assorted%d notes" % (i % 37)
        if i % 7 == 0:
            text += " harbour scheduling"
        _goal(conn, f"g{i}", f"e{i}", _day(i), text)
    conn.commit()
    coverage = build_bench_slate(conn)["coverage"]
    assert "user_goals" in coverage["role_substrate"]
    assert "journal_entries" not in coverage["role_substrate"], \
        "naming a substrate it did not read is how a report starts lying about itself"
    assert "constant" in coverage["self_performed_signal"], \
        "self_performed_share is 1.0 by construction and must not read as a measurement"
