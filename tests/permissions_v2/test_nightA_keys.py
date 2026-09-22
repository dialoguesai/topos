"""Night review A: the migration-78 candidate keys on the integrated merge tree.

The audited claim is the one the migration states in its own docstring:

    "A row is clean only when SQLite and Python's strict parser (``evidence._json``)
     must read it identically"

and the property that rests on it:

    "the candidate set is a SUPERSET of the rows the old loops would have matched."

A key that is missing, or right for a value the row does not hold, is not a slow
read -- it is a fact that stops being withheld.

  A1  the write matrix: INSERT OR REPLACE (both conflicts), UPSERT, INSERT OR
      IGNORE, UPDATE OR REPLACE and a plain UPDATE of the referenced ids, driven
      through the real triggers on the production schema.                    PASSES
  A2  a row and its keys move together: the triggers run inside the writer's own
      statement, so no rollback can leave a live row unkeyed.                PASSES
  A3  the opaque set under adversarial shapes. Every shape in A3a is either keyed
      exactly or left always-checked -- except a NUL character, where SQLite's
      string functions stop and Python's do not.                             FAILS
  A4  what the read does when the trigger or the key table is gone.          PASSES
      (except A4e, which records what it does NOT detect)

Every database is built by the production migration runner via
``tests/permissions_v2/production_corpus.py``; no DDL is written by hand here.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2 import production_corpus as pc
from tests.permissions_v2.test_bk3_lineage_keys import (keys_of, new_sibling_candidates, old_sibling_matches,
                                                        payload, write_fact)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver, ReviewedClassification, _json
from topos.permissions_v2.identity import ATTESTED_CONTRACT
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk

# SQLite's TEXT functions (length, substr, GLOB, json_valid) stop at the first NUL.
# Python's do not. Everything in A3 turns on that one difference.
NUL = chr(0)

KEY_TABLES = ("permissions_v2_fact_key_rows", "permissions_v2_fact_ref_keys", "permissions_v2_fact_claim_keys",
              "permissions_v2_fact_key_opaque", "permissions_v2_fact_key_completion")


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "canonical.db")
    pc.production_schema(conn)
    yield conn
    conn.close()


def ref_keys(conn, object_id):
    return sorted(row[0] for row in conn.execute(
        "SELECT ref_key FROM permissions_v2_fact_ref_keys WHERE object_id=?", (object_id,)))


def claim_key_of(conn, object_id):
    row = conn.execute("SELECT claim_key FROM permissions_v2_fact_claim_keys WHERE object_id=?",
                       (object_id,)).fetchone()
    return row[0] if row else None


def mint_reviewed_fact(corpus, *, object_value, message_id, content, review_id, predicate="works_on"):
    """A second releasable fact in an existing corpus, reviewed exactly as `production_corpus` does."""
    from topos.features.facts.store import FactStore
    with sqlite3.connect(corpus.path) as conn:
        pc.insert_message(conn, message_id=message_id, content=content, event_at=pc.NOW - 7_200)
        fact = FactStore(conn).assert_fact(subject_entity_id=pc.OWNER_ENTITY, predicate=predicate,
                                           object_value=object_value, disclosure="scoped",
                                           source_refs=[pc._ref(message_id)], asserted_by="owner")
    fact_id = fact["object_id"]
    with pc.owner():
        snapshot = corpus.resolver.inspect_for_review(fact_id)
        corpus.reviews.record_review(
            resolver=corpus.resolver, review_id=review_id, expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                                                    subject_entity_ids=["self"], authorship="owner_authored",
                                                    speech="direct_self_statement", independent_copies="none_known")
                             for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=pc.NOW - 60)
    corpus.messages[fact_id] = message_id
    return fact_id


# --- A1: the write matrix ----------------------------------------------------------------------
# REPLACE conflict resolution deletes the old row without firing the DELETE trigger. The insert
# and update triggers compensate by forgetting the keys of any row that shared the active unique
# tuple and is no longer there. These pin every verb a writer in this tree can reach.


def test_A1_upsert_do_update_rekeys_the_row_that_survives(db):
    """`ON CONFLICT ... DO UPDATE` updates the EXISTING row, so the id in VALUES never exists.

    SQLite fires the UPDATE triggers, not the INSERT ones. The surviving row must carry the
    new references, and the id that was never inserted must carry no keys at all.
    """
    write_fact(db, "up1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="shared")
    db.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json,"
               " source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
               " ON CONFLICT(signal_dimension, object_type, object_key) WHERE valid_to IS NULL"
               " DO UPDATE SET payload_json=excluded.payload_json, source_refs_json=excluded.source_refs_json",
               ("up2", "profile", "fact", "shared", payload(value="beta"),
                json.dumps([{"record_id": "imessage:77"}]), "t", "t", "t"))
    assert db.execute("SELECT object_id FROM signal_objects").fetchall() == [("up1",)]
    assert ref_keys(db, "up1") == ["imessage:77"]
    assert claim_key_of(db, "up1") == lk.claim_key("works_on", "beta")
    assert sum(keys_of(db, "up2").values()) == 0
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == set()
    assert new_sibling_candidates(db, {"imessage:77": {"conversation_messages"}}) == {"up1"}


def test_A1_upsert_that_touches_no_watched_column_changes_no_key(db):
    """`AFTER UPDATE OF` fires on the SET list, not on what changed. A DO UPDATE that sets only
    an unwatched column leaves the keyed columns alone, so leaving the keys alone is correct."""
    write_fact(db, "u1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="shared2")
    db.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json,"
               " source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
               " ON CONFLICT(signal_dimension, object_type, object_key) WHERE valid_to IS NULL"
               " DO UPDATE SET updated_at=excluded.updated_at",
               ("u2", "profile", "fact", "shared2", payload(value="beta"),
                json.dumps([{"record_id": "imessage:77"}]), "t", "t", "t2"))
    refs, updated = db.execute("SELECT source_refs_json, updated_at FROM signal_objects").fetchone()
    assert updated == "t2" and json.loads(refs) == [{"record_id": "imessage:5"}]
    assert ref_keys(db, "u1") == ["imessage:5"]


def test_A1_update_or_replace_forgets_the_replaced_rows_keys(db):
    """`UPDATE OR REPLACE` deletes the conflicting row without firing the DELETE trigger."""
    write_fact(db, "a", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="k-a")
    write_fact(db, "b", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(value="beta"), key="k-b")
    db.execute("UPDATE OR REPLACE signal_objects SET object_key='k-a' WHERE object_id='b'")
    assert db.execute("SELECT object_id FROM signal_objects").fetchall() == [("b",)]
    assert sum(keys_of(db, "a").values()) == 0
    assert ref_keys(db, "b") == ["imessage:77"]
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == set()


def test_A1_a_row_sharing_the_tuple_that_still_lives_keeps_its_keys(db):
    """The unique index is partial (`WHERE valid_to IS NULL`), so a CLOSED row may share the
    tuple with the row a REPLACE removes. Forgetting its keys too would unkey a live row."""
    write_fact(db, "closed", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="same")
    db.execute("UPDATE signal_objects SET valid_to='2026-01-01' WHERE object_id='closed'")
    write_fact(db, "live", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(value="b"), key="same")
    write_fact(db, "new", refs=json.dumps([{"record_id": "imessage:123456"}]), payload=payload(value="c"),
               key="same", verb="INSERT OR REPLACE")
    assert sorted(row[0] for row in db.execute("SELECT object_id FROM signal_objects")) == ["closed", "new"]
    assert ref_keys(db, "closed") == ["imessage:5"]          # still there, still keyed
    assert sum(keys_of(db, "live").values()) == 0            # replaced away, keys gone with it
    assert ref_keys(db, "new") == ["imessage:123456"]
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == {"closed"}


def test_A1_the_real_fact_writer_keeps_the_keys_through_refresh_and_supersession(db):
    """`FactStore` is the in-tree writer. It never uses REPLACE, UPSERT or IGNORE: it closes the
    incumbent with `UPDATE ... SET valid_to`, refreshes with `UPDATE ... SET source_refs_json`,
    and inserts plainly. All three name a watched column, so all three re-key."""
    from topos.features.facts.store import FactStore
    for number, message_id in enumerate(("imessage:5", "imessage:77", "imessage:123456")):
        pc.insert_message(db, message_id=message_id, content=f"line {number}", event_at=pc.NOW + 600 * number)
    store = FactStore(db)
    first = store.assert_fact(subject_entity_id=pc.OWNER_ENTITY, predicate="works_on", object_value="alpha",
                              disclosure="scoped", source_refs=[pc._ref("imessage:5")], asserted_by="owner")
    assert ref_keys(db, first["object_id"]) == ["imessage:5"]
    # refresh: same value, a second citation folded into the SAME row
    store.assert_fact(subject_entity_id=pc.OWNER_ENTITY, predicate="works_on", object_value="alpha",
                      disclosure="scoped", source_refs=[pc._ref("imessage:77")], asserted_by="owner", confidence=0.9)
    assert ref_keys(db, first["object_id"]) == ["imessage:5", "imessage:77"]
    # a second value: `works_on` is multi-valued, so this takes its own object_key and its own row
    second = store.assert_fact(subject_entity_id=pc.OWNER_ENTITY, predicate="works_on", object_value="beta",
                               disclosure="scoped", source_refs=[pc._ref("imessage:123456")], asserted_by="owner",
                               confidence=0.95)["object_id"]
    rows = dict(db.execute("SELECT object_id, object_key FROM signal_objects WHERE object_type='fact'").fetchall())
    assert set(rows) == {first["object_id"], second} and rows[first["object_id"]] != rows[second]
    # and a close, which is the writer's other watched update
    store._close(first["object_id"], valid_to="2026-01-01")
    assert db.execute("SELECT valid_to FROM signal_objects WHERE object_id=?",
                      (first["object_id"],)).fetchone()[0] == "2026-01-01"
    # the sibling floor reads current, closed and deleted rows alike, so both must stay candidates
    assert ref_keys(db, first["object_id"]) == ["imessage:5", "imessage:77"]
    assert ref_keys(db, second) == ["imessage:123456"]
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == {first["object_id"]}
    assert claim_key_of(db, second) == lk.claim_key("works_on", "beta")
    db.commit()
    live = {table: sorted(db.execute(f"SELECT * FROM {table}").fetchall()) for table in KEY_TABLES}
    lk._rebuild(db)
    lk.complete_pending(db)
    assert live == {table: sorted(db.execute(f"SELECT * FROM {table}").fetchall()) for table in KEY_TABLES}


def test_A1_every_write_verb_leaves_the_keys_equal_to_a_rebuild(db):
    """The end-to-end shape of A1: after a run of every verb, what the triggers hold must equal
    what a from-scratch rebuild of the same rows produces."""
    write_fact(db, "v1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="t1")
    write_fact(db, "v1", refs=json.dumps([{"record_id": "imessage:6"}]), payload=payload(value="b"),
               verb="INSERT OR REPLACE")
    write_fact(db, "v2", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(value="c"), key="t2")
    write_fact(db, "v3", refs=json.dumps([{"record_id": "imessage:55"}]), payload=payload(value="d"), key="t2",
               verb="INSERT OR REPLACE")
    write_fact(db, "v4", refs=json.dumps([{"record_id": "other:1"}]), payload=payload(value="e"), key="t2",
               verb="INSERT OR IGNORE")
    db.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id='v1'",
               (json.dumps([{"record_id": "imessage:123456"}]),))
    db.execute("UPDATE OR REPLACE signal_objects SET object_key='t2' WHERE object_id='v1'")
    db.commit()
    live = {table: sorted(db.execute(f"SELECT * FROM {table}").fetchall()) for table in KEY_TABLES}
    lk._rebuild(db)
    lk.complete_pending(db)
    assert live == {table: sorted(db.execute(f"SELECT * FROM {table}").fetchall()) for table in KEY_TABLES}


# --- A2: a row and its keys move together --------------------------------------------------------


def test_A2_a_rollback_restores_the_row_and_its_keys_together(tmp_path):
    """The key writes live in the writer's own statement (a trigger body), so there is no window
    between the row delete and the key delete for a crash to land in. A rollback brings both back."""
    corpus = pc.build(tmp_path, seed=21, positives=2, hidden_facts=6)
    conn = sqlite3.connect(corpus.path, isolation_level=None)
    try:
        fact = corpus.positives[0]
        before = keys_of(conn, fact)
        assert sum(before.values()) > 0
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM signal_objects WHERE object_id=?", (fact,))
        assert sum(keys_of(conn, fact).values()) == 0       # gone inside the transaction, with the row
        conn.execute("ROLLBACK")
        assert conn.execute("SELECT 1 FROM signal_objects WHERE object_id=?", (fact,)).fetchone() is not None
        assert keys_of(conn, fact) == before
    finally:
        conn.close()


def test_A2_the_scrub_paths_never_leave_a_live_fact_unkeyed(tmp_path):
    """`purge_facts_for_source` / `purge_derived_for_records` rewrite or delete fact rows. Whether
    they commit or roll back, no surviving fact may be missing from the key tables."""
    from topos.features.lifecycle.derived_scrub import purge_derived_for_records, purge_facts_for_source
    corpus = pc.build(tmp_path, seed=22, positives=4, hidden_facts=12)
    conn = sqlite3.connect(corpus.path)
    try:
        doomed = corpus.positives[:2]
        purge_facts_for_source(conn, pc.SOURCE, scrubbed_record_ids={corpus.messages[doomed[0]]})
        purge_derived_for_records(conn, [corpus.messages[doomed[1]]])
        conn.rollback()                                      # the crash case: nothing committed
        unkeyed = {row[0] for row in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")} \
            - {row[0] for row in conn.execute("SELECT object_id FROM permissions_v2_fact_key_rows")}
        assert not unkeyed, unkeyed
        purge_facts_for_source(conn, pc.SOURCE, scrubbed_record_ids={corpus.messages[doomed[0]]})
        conn.commit()                                        # and the committed case
        unkeyed = {row[0] for row in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")} \
            - {row[0] for row in conn.execute("SELECT object_id FROM permissions_v2_fact_key_rows")}
        assert not unkeyed, unkeyed
    finally:
        conn.close()


def test_A2_the_python_completion_pass_never_settles_a_row_it_did_not_read(tmp_path):
    """`complete_pending` writes the completion keys and flips the state to 1 in one transaction.
    A row it settles must carry keys that match the value it settled from."""
    corpus = pc.build(tmp_path, seed=23, positives=1, hidden_facts=20)
    conn = sqlite3.connect(corpus.path, isolation_level=None)
    try:
        # `pc.build` ends in a node start, which already settled the backlog. Add a fresh one.
        for number in range(12):
            write_fact(conn, f"pending-{number}", refs=json.dumps([{"record_id": 10 ** 25 + number}]),
                       payload=payload(value=f"Zürich {number}"))
        state = lambda: (conn.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE state<>0").fetchone()[0],
                         conn.execute("SELECT count(*) FROM permissions_v2_fact_key_completion").fetchone()[0])
        before = state()
        assert conn.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE state=0").fetchone()[0] >= 24
        conn.execute("BEGIN IMMEDIATE")
        lk.complete_pending(conn)
        conn.execute("ROLLBACK")
        assert state() == before                             # all or nothing
        conn.execute("BEGIN IMMEDIATE")
        lk.complete_pending(conn)
        conn.execute("COMMIT")
        for object_id, family, refs, pay in conn.execute(
                "SELECT o.object_id, o.family, s.source_refs_json, s.payload_json FROM permissions_v2_fact_key_opaque o "
                "JOIN signal_objects s ON s.object_id=o.object_id WHERE o.state=1"):
            stored = {row[0] for row in conn.execute(
                "SELECT key FROM permissions_v2_fact_key_completion WHERE object_id=? AND family LIKE ?||'%'",
                (object_id, family))}
            if family == "claim":
                assert stored == {lk.exact_claim_key(pay)}
            else:
                exact, substrings = lk.exact_ref_keys(refs)
                assert stored == exact | substrings
    finally:
        conn.close()


# --- A3: the opaque set under adversarial shapes -------------------------------------------------

# Everything the bookkeeping A1 finding named, plus the rest of the malformed-shape space. Each
# entry: (label, source_refs_json). A row is SAFE when it is opaque (Python keys it) or when the
# SQL keys are exactly the keys Python would compute.
REF_SHAPES = (
    ("plain", json.dumps([{"record_id": "imessage:5"}])),
    ("record_id past int64", json.dumps([{"record_id": 10 ** 30}])),
    ("id past int64", json.dumps([{"record_id": "x", "id": 2 ** 63}])),
    ("id 10**30", json.dumps([{"record_id": "x", "id": 10 ** 30}])),
    ("record_id float", json.dumps([{"record_id": 5.0, "id": "imessage:5"}])),
    ("record_id -0", json.dumps([{"record_id": -0}])),
    ("big exponent", '[{"record_id": 1e400}]'),
    ("NaN element", '[{"record_id":"imessage:5"}, NaN]'),
    ("Infinity", '[{"record_id": Infinity}]'),
    ("nested 40 deep", "[" + "[" * 40 + '"imessage:5"' + "]" * 40 + "]"),
    ("nested 2000 deep", "[" + "[" * 2000 + '"imessage:5"' + "]" * 2000 + "]"),
    ("duplicate key, leaf first", '[{"record_id":"imessage:5","record_id":"x"}]'),
    ("escaped key spells a duplicate", '[{"record\\u005fid":"imessage:5","record_id":"x"}]'),
    ("escaped leaf", '[{"record_id":"imessage\\u003a5"}]'),
    ("BOM prefix", "﻿" + json.dumps([{"record_id": "imessage:5"}])),
    ("trailing whitespace", json.dumps([{"record_id": "imessage:5"}]) + "   \n"),
    ("NBSP around the leaf", json.dumps([{"record_id": " imessage:5 "}], ensure_ascii=False)),
    ("vertical tab around the leaf", json.dumps([{"record_id": "\x0bimessage:5\x0b"}])),
    ("Kelvin sign run", json.dumps([{"record_id": "Kimessage:5"}], ensure_ascii=False)),
    ("dotted I run", json.dumps([{"record_id": "İimessage:5"}], ensure_ascii=False)),
    ("full-width digits", json.dumps([{"record_id": "１２"}], ensure_ascii=False)),
    ("array of arrays", "[[]]"),
    ("object not array", json.dumps({"record_id": "imessage:5"})),
    ("not json at all", "not json imessage:5"),
    ("empty array", "[]"),
    # SQLite's json_valid() stops at the first NUL; Python's json.loads() does not.
    ("raw NUL then a leaf id", json.dumps([{"record_id": "other:1"}]) + NUL + "imessage:5"),
    ("raw NUL at the very end", json.dumps([{"record_id": "imessage:5"}]) + NUL),
)

CLAIM_SHAPES = (
    ("plain ascii", "alpha beta"),
    ("Kelvin sign", "K"),
    ("dotted I", "İstanbul"),
    ("eszett", "straße"),
    ("ff ligature", "ﬀ"),
    ("full width", "ＡＢ"),
    ("NBSP", "a b"),
    ("tab", "a\tb"),
    ("double space", "a  b"),
    ("leading space", " a"),
    ("exactly the prefix", "x" * lk.CLAIM_PREFIX),
    ("one past the prefix", "x" * lk.CLAIM_PREFIX + "y"),
    # SQLite's length() and substr() stop at the first NUL; Python's len() and slice do not.
    ("a NUL inside the value", "abc" + NUL + "def"),
)


@pytest.mark.parametrize("label,refs", REF_SHAPES, ids=[label for label, _ in REF_SHAPES])
def test_A3a_a_reference_list_is_keyed_exactly_or_left_always_checked(db, label, refs):
    """No reference list may be SQL-clean while Python reads it differently.

    A row SQL calls clean never reaches the Python completion pass, so whatever the SQL
    triggers stored is the whole of its candidacy for the rest of its life.
    """
    write_fact(db, "f1", refs=refs, payload=payload())
    db.commit()
    opaque = db.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE object_id='f1' AND family='refs'"
                        ).fetchone()[0]
    if opaque:
        lk.complete_pending(db)                      # Python keys it; nothing left to prove
        return
    stored = db.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='f1'").fetchone()[0]
    found = lk.exact_ref_keys(stored)
    assert found is not None, f"{label}: SQL called it clean but Python gave up on it"
    exact, substrings = found
    assert not substrings, (f"{label}: SQL called it clean, but Python's strict parser refuses it and "
                            f"falls back to a substring test -- the keyed set cannot speak for it")
    assert set(ref_keys(db, "f1")) == exact, label


@pytest.mark.parametrize("label,value", CLAIM_SHAPES, ids=[label for label, _ in CLAIM_SHAPES])
def test_A3b_a_claim_is_keyed_exactly_or_left_always_checked(db, label, value):
    """The key a trigger stores must be the key `_claim_candidates` looks the row up by."""
    write_fact(db, "f1", refs="[]", payload=payload(value=value))
    db.commit()
    opaque = db.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE object_id='f1' AND family='claim'"
                        ).fetchone()[0]
    if opaque:
        lk.complete_pending(db)
        return
    stored = db.execute("SELECT payload_json FROM signal_objects WHERE object_id='f1'").fetchone()[0]
    assert claim_key_of(db, "f1") == lk.exact_claim_key(stored), label


def test_A3c_a_nul_in_the_reference_text_hides_a_fact_the_predicate_matches(db):
    """K1's own oracle, on one shape it never generated.

    `json_valid` reads `[...]` + NUL + tail as a valid array and stops, so the trigger keys the
    row from the part before the NUL and never marks it opaque. `_names_a_leaf` -- the predicate
    the design says decides -- cannot parse the same text, falls back to a substring test over
    all of it, and matches. The superset property fails.
    """
    write_fact(db, "hidden", refs=json.dumps([{"record_id": "other:1"}]) + NUL + "imessage:5", payload=payload())
    db.commit()
    lk.complete_pending(db)
    leaves = {"imessage:5": {"conversation_messages"}}
    assert EvidenceResolver._names_a_leaf(
        db.execute("SELECT source_refs_json FROM signal_objects WHERE object_id='hidden'").fetchone()[0],
        leaves) is True
    missed = old_sibling_matches(db, leaves) - new_sibling_candidates(db, leaves)
    assert not missed, missed


def test_A3d_a_nul_in_a_claim_hides_an_independent_copy(tmp_path):
    """The app path. A reviewed, releasable fact whose object value carries a NUL, and a second
    active fact holding the identical claim.

    Before migration 78 the copy check read every active fact, so this refused with
    `independent_copy_lineage`. The trigger stores `<predicate>\\x1f3\\x1fabc` for the copy --
    `length()` and `substr()` stopped at the NUL -- while the read looks it up by
    `<predicate>\\x1f7\\x1fabc\\x00def`, so the copy is not a candidate and the release goes out.
    """
    corpus = pc.build(tmp_path, seed=24, positives=2)
    value = "abc" + NUL + "def"
    fact = mint_reviewed_fact(corpus, object_value=value, message_id="imessage:900001",
                              content="nul claim carrier", review_id="review-nul")
    assert corpus.resolver.qualify(fact, reviews=corpus.reviews, contract=ATTESTED_CONTRACT).verdict == "qualified"

    with sqlite3.connect(corpus.path) as conn:
        original = conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (fact,)).fetchone()[0]
        write_fact(conn, "hidden-copy", refs="[]", payload=original, key="hidden-copy")
        conn.commit()
        assert _json(original, dict)["object_value"] == value
        assert conn.execute("SELECT valid_to FROM signal_objects WHERE object_id='hidden-copy'").fetchone()[0] is None
        # The mechanism, stated the way it must end up: a value SQLite and Python cut
        # differently has to go down the opaque path, where the Python pass keys it exactly.
        # Before the fix this count was 0 -- the GLOBs stopped at the NUL, the row was called
        # clean ASCII and was keyed from 'abc' alone, so the read never found it.
        assert conn.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE object_id='hidden-copy' "
                            "AND family='claim'").fetchone()[0] == 1, "the NUL row was not marked opaque"

    verdict = corpus.resolver.qualify(fact, reviews=corpus.reviews, contract=ATTESTED_CONTRACT)
    assert verdict.reason_code == "independent_copy_lineage", (
        f"an active fact with the identical claim did not withhold the release: {verdict.reason_code}")


def test_A3e_a_nul_stops_a_malformed_fact_refusing_every_release(tmp_path):
    """The migration promises: "So a malformed fact still refuses every release, as it did before."

    `test_K1_a_malformed_fact_still_refuses_every_release` pins that with a duplicate-key payload,
    which goes opaque and stays always-checked. A payload that is a well-formed object followed by
    a raw NUL and junk is the same kind of malformed -- Python's parser raises `evidence_malformed`
    on it -- but `json_valid` calls it clean, so it is keyed under whatever the prefix said and
    drops out of the candidate set.
    """
    corpus = pc.build(tmp_path, seed=25, positives=2)
    with sqlite3.connect(corpus.path) as conn:
        good = json.dumps({"subject_entity_id": "e", "predicate": "unrelated", "object_value": "qqq"})
        write_fact(conn, "fact-malformed", refs="[]", payload=good + NUL + '","trailing garbage')
        conn.commit()
        stored = conn.execute("SELECT payload_json FROM signal_objects WHERE object_id='fact-malformed'").fetchone()[0]
    with pytest.raises(PolicyError, match="evidence_malformed"):
        _json(stored, dict)                                   # Python: malformed, beyond doubt
    verdict = corpus.resolver.qualify(corpus.positives[0], reviews=corpus.reviews, contract=ATTESTED_CONTRACT)
    assert verdict.reason_code == "evidence_malformed", (
        f"a malformed active fact no longer refuses the release: {verdict.reason_code}")


# --- A4: what the read does when the keys cannot be trusted ---------------------------------------


@pytest.mark.parametrize("label,tamper", [
    ("a trigger dropped", ["DROP TRIGGER fact_lineage_keys_ai"]),
    ("a trigger replaced with a no-op", ["DROP TRIGGER fact_lineage_keys_ai",
                                         "CREATE TRIGGER fact_lineage_keys_ai AFTER INSERT ON signal_objects "
                                         "BEGIN SELECT 1; END"]),
    ("a key table dropped", ["DROP TABLE permissions_v2_fact_ref_keys"]),
    ("an index dropped", ["DROP INDEX permissions_v2_fact_ref_keys_key"]),
], ids=["trigger-dropped", "trigger-no-op", "table-dropped", "index-dropped"])
def test_A4_the_read_detects_every_schema_tamper_and_falls_back(db, label, tamper):
    """`installed()` compares the exact SQL of every table, index and trigger, so all four of
    these are caught before a single key table is read, and the read scans instead."""
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    for statement in tamper:
        db.execute(statement)
    assert lk.installed(db) is False, label
    lk.apply_permissions_fact_lineage_keys_v1_up(db)          # the next node start rebuilds
    assert lk.installed(db) is True
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == {"f1"}


def test_A4e_an_emptied_key_table_is_trusted_and_releases_silently(db):
    """The one tamper `installed()` cannot see: the SQL is intact and the rows are gone.

    Nothing in this tree deletes from these tables -- the triggers and `_rebuild` are the only
    writers, and `data_explorer_tables.is_permission_state_table` keeps them off every explorer
    surface -- so this is a tripwire, not a live leak. It records that the trust gate is a
    schema-text check with no row-level cross-check, and that `_rebuild` is skipped whenever
    `installed()` is true however empty the tables are.
    """
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    leaves = {"imessage:5": {"conversation_messages"}}
    assert new_sibling_candidates(db, leaves) == {"f1"}
    for table in KEY_TABLES:
        db.execute(f"DELETE FROM {table}")
    assert lk.installed(db) is True                           # the read still trusts the tables
    assert new_sibling_candidates(db, leaves) == set()        # and finds nothing
    lk.apply_permissions_fact_lineage_keys_v1_up(db)          # a node start does NOT repair it
    assert new_sibling_candidates(db, leaves) == set()
    db.execute("DROP TABLE permissions_v2_fact_ref_keys")     # only a forced rebuild does
    lk.apply_permissions_fact_lineage_keys_v1_up(db)
    assert new_sibling_candidates(db, leaves) == {"f1"}


# --- A5: the couplings the substring scheme rests on ----------------------------------------------


def test_A5_the_substring_keys_cover_every_identifier_the_grammar_allows():
    """`substring_keys` adds a suffix only where the character is ASCII-alphanumeric, and cuts it
    at IDENTIFIER_MAX. Both are exactly right for today's `Identifier` -- and only for it.

    If that grammar ever gained a leading `.`/`_`/`:`/`-`, or a length past IDENTIFIER_MAX, no
    stored key would start with such a leaf and every substring-keyed fact would drop out of the
    sibling floor silently. Nothing else in the tree pins the two together.
    """
    from topos.permissions_v2.contract import Identifier
    grammar = Identifier.__metadata__[0]
    assert grammar.pattern == r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$"
    assert grammar.max_length <= lk.IDENTIFIER_MAX
    from topos.permissions_v2.evidence import EvidenceIdentity
    assert EvidenceIdentity.model_fields["record_id"].metadata == list(Identifier.__metadata__)

    for leaf in ("a", "imessage:5", "0" + "._:@/-" * 30, "z" * grammar.max_length):
        for surround in ("", "xx", "-", "/", ".", "é", "K"):
            text = surround + "pre" + surround + leaf + surround + "post"
            keys = lk.substring_keys(text)
            assert keys is not None and any(key.startswith(leaf) for key in keys), (leaf, surround)


def test_A5_a_lone_surrogate_reference_stores_a_key_that_cannot_be_read_back(db):
    """A `\\ud800` escape decodes, in SQLite, to bytes that are not valid UTF-8.

    The triggers store it happily and every comparison the read makes is bytewise, so nothing
    breaks today. It is recorded because any future code that SELECTs a `ref_key` value -- a
    diagnostic, an export, a repair script -- raises `OperationalError` on such a node.
    """
    write_fact(db, "sur", refs='[{"record_id": "\\ud800imessage:5"}]', payload=payload())
    db.commit()
    assert db.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE object_id='sur'").fetchone()[0] == 0
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == set()
    with pytest.raises(sqlite3.OperationalError, match="Could not decode to UTF-8"):
        db.execute("SELECT ref_key FROM permissions_v2_fact_ref_keys WHERE object_id='sur'").fetchall()


def test_A5_the_message_content_key_avoids_the_bug_the_claim_key_has():
    """`permissions_read_path_indexes_v1` met the same hazard and handled it.

    Its content key is compared by running the PARAMETER through the same SQLite functions as the
    column (evidence.py `_COPY_KEY`), so a NUL truncates both sides identically and the full
    `content=?1` decides. `claim_key_sql` cuts one side in SQLite and the other in Python
    (`lk.claim_key`), which is what A3b and A3d fail on.
    """
    from topos.storage.db.migrations.permissions_read_path_indexes_v1 import CONTENT_KEY
    from topos.permissions_v2 import evidence as ev
    for expression in CONTENT_KEY:
        assert f"{expression}={expression.replace('content', '?1', 1)}" in ev._COPY_KEY
    assert ev._COPY_COUNT.endswith("AND content=?1")
    probe = sqlite3.connect(":memory:")
    probe.execute("CREATE TABLE m(content TEXT)")
    probe.execute("INSERT INTO m VALUES(?)", ("abc" + NUL + "def",))
    assert probe.execute(ev._COPY_COUNT.format(table="m"), ("abc" + NUL + "def",)).fetchone()[0] == 1
    assert probe.execute(ev._COPY_COUNT.format(table="m"), ("abc" + NUL + "zzz",)).fetchone()[0] == 0
    probe.close()
