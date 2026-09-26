"""Migration 78: candidate keys for the sibling-fact floor (R2) and the claim copy check (R3).

The keys choose candidates; the unchanged Python predicates decide. So the one
property that makes the change exact is a SUPERSET: every fact the old scans
would have matched is a candidate. It is pinned four ways:

  K1  a seeded fuzz over reference and payload shapes a writer can leave, against
      the old scan's own matches
  K2  the triggers keep the keys through every write, REPLACE of both kinds and
      INSERT OR IGNORE included, and a key never outlives its fact (scrub surface)
  K3  a missing or altered trigger makes reads fall back to the scan, and the next
      node start rebuilds every key, rows written meanwhile included
  K4  the read no longer scans signal_objects when the keys are installed

Every database here is built by the production migration runner.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from tests.permissions_v2 import production_corpus as pc
from topos.permissions_v2 import evidence
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.identity import ATTESTED_CONTRACT
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk

LEAVES = ("imessage:5", "imessage:77", "chatgpt:msg-9", "imessage:123456")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "canonical.db"
    conn = sqlite3.connect(path)
    pc.production_schema(conn)
    yield conn
    conn.close()


def write_fact(conn, object_id, *, refs, payload, key=None, dimension="profile", verb="INSERT"):
    conn.execute(f"{verb} INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                 "source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (object_id, dimension, "fact", key or f"key:{object_id}", payload, refs, "t", "t", "t"))


def payload(predicate="works_on", value="alpha", subject="entity-owner", **extra):
    return json.dumps({"subject_entity_id": subject, "predicate": predicate, "object_value": value,
                       "disclosure": "scoped", **extra})


# --- generators ---------------------------------------------------------------------------

SPACES = [chr(code) for code in lk.PY_WHITESPACE]


def random_id(rng):
    return rng.choice(LEAVES + ("imessage:6", "other:1", "imessage:55"))


def random_refs(rng) -> str | bytes | None:
    leaf = random_id(rng)
    shape = rng.randrange(29)
    element = {"table": rng.choice(["conversation_messages", "ai_chat_messages", "signal_objects", "weird", None]),
               "record_id": leaf, "source_id": "imessage"}
    element = {k: v for k, v in element.items() if v is not None}
    if shape == 0:
        return json.dumps([element])
    if shape == 1:
        return json.dumps([element, {"record_id": random_id(rng)}])
    if shape == 2:
        return json.dumps([{"record_id": rng.choice(SPACES) + leaf + rng.choice(SPACES)}])
    if shape == 3:
        return json.dumps([{"id": leaf}])
    if shape == 4:
        return json.dumps([{"record_id": None, "note": leaf}])
    if shape == 5:
        return json.dumps([leaf])
    if shape == 6:
        return "not json " + leaf
    if shape == 7:
        return '[{"record_id":"x","record_id":"' + leaf + '"}]'
    if shape == 8:
        return '[{"record_id":"' + leaf.replace(":", "\\u003a") + '"}]'
    if shape == 9:
        return '[{"record\\u005fid":"' + leaf + '","record_id":"x"}]'
    if shape == 10:
        return json.dumps([{"record_id": 10 ** rng.randint(18, 30)}])
    if shape == 11:
        return json.dumps([{"record_id": True, "note": leaf}])
    if shape == 12:
        return json.dumps([{"record_id": 5.0, "id": leaf}])
    if shape == 13:
        return json.dumps([{"record_id": "x", "id": leaf}])
    if shape == 14:
        return json.dumps([{"record_id": leaf, "nested": {"record_id": "imessage:5"}}])
    if shape == 15:
        return json.dumps([element]).encode()
    if shape == 16:
        return "[" + "[" * 40 + '"' + leaf + '"' + "]" * 40 + "]"
    if shape == 17:
        return '[{"record_id":"' + leaf + '"}, NaN]'
    if shape == 18:
        return "﻿" + json.dumps([element])
    if shape == 19:
        return json.dumps([{"record_id": ""}, {"record_id": leaf}])
    if shape == 20:
        return json.dumps({"record_id": leaf})
    if shape == 21:
        return json.dumps([{"record_id": leaf.upper()}])
    if shape == 22:
        return json.dumps([{"record_id": "  "}, element]).replace(leaf, leaf + "\\/")
    if shape == 23:
        return "garbage xx" + leaf + ".tail:9 " + leaf.replace(":", "\\u003a")
    if shape == 24:
        return json.dumps([{"record_id": None, "note": "pre-" + leaf + "/post"}, element])
    if shape == 25:
        return ("[" + json.dumps({"record_id": leaf}) + ",") * 3
    if shape == 26:
        # Python reads this id exactly; SQLite hands back a REAL past int64.
        return json.dumps([{"record_id": "x", "id": int(leaf.split(":")[1]) if leaf.split(":")[1].isdigit() else 10 ** 20}])
    if shape == 27:
        return json.dumps([{"record_id": "x", "id": 2 ** 63}])
    return json.dumps([{"record_id": int(leaf.split(":")[1]) if leaf.split(":")[1].isdigit() else leaf}])


WORDS = ["Alpha", "alpha", "ALPHA", "zürich", "ZÜRICH", "K", "k", "straße", "STRASSE", "İstanbul", "i̇stanbul",
         "a  b", "a b", " a b ", "a\tb", "a b", "a b", "x" * 40, "x" * 40 + "y", "", "日本"]


def random_payload(rng) -> str | bytes:
    shape = rng.randrange(12)
    if shape == 0:
        return "not json"
    if shape == 1:
        return '{"predicate":"works_on","predicate":"lives_in","object_value":"alpha","subject_entity_id":"e"}'
    if shape == 2:
        return json.dumps({"predicate": "works_on", "object_value": 5, "subject_entity_id": "e"})
    if shape == 3:
        return payload(predicate=rng.choice(WORDS), value=rng.choice(WORDS)).encode()
    if shape == 4:
        return json.dumps({"predicate": rng.choice(WORDS), "object_value": rng.choice(WORDS),
                           "subject_entity_id": "entity-owner"}, ensure_ascii=False)
    return payload(predicate=rng.choice(["works_on", "Works_On", "works on", "lives_in"]), value=rng.choice(WORDS),
                   subject=rng.choice(["entity-owner", "self", "entity-other"]))


# --- K1: superset ---------------------------------------------------------------------------


def old_sibling_matches(conn, leaves: dict) -> set[str]:
    """What the pre-78 scan kept, decided by the unchanged predicate."""
    found = set()
    for object_id, refs in conn.execute("SELECT object_id, source_refs_json FROM signal_objects WHERE object_type='fact'"):
        if EvidenceResolver._names_a_leaf(refs, leaves):
            found.add(object_id)
    return found


def new_sibling_candidates(conn, leaves: dict) -> set[str]:
    sql = lk.SIBLING_CANDIDATES.format(marks=",".join("?" * len(leaves)),
                                       ranges=" OR ".join(["(key>=? AND key<?)"] * len(leaves)))
    return {row[0] for row in conn.execute(sql, lk.sibling_arguments(leaves))}


def normalized(raw):
    value = evidence._json(raw, dict)
    return tuple(" ".join(str(value.get(field) or "").lower().split())
                 for field in ("subject_entity_id", "predicate", "object_value"))


@pytest.mark.parametrize("seed", range(6))
def test_K1_every_fact_the_old_scans_matched_is_a_candidate(db, seed):
    rng = random.Random(seed)
    for number in range(900):
        write_fact(db, f"fact-{number}", refs=random_refs(rng), payload=random_payload(rng))
    db.commit()
    assert lk.installed(db)
    lk.complete_pending(db, limit=None if seed % 2 else 5)  # a partly completed backlog too
    db.commit()
    for leaf_count in (1, 2, 4):
        leaves = {leaf: {"conversation_messages"} for leaf in rng.sample(LEAVES, leaf_count)}
        missed = old_sibling_matches(db, leaves) - new_sibling_candidates(db, leaves)
        assert not missed, missed
    rows = db.execute("SELECT object_id, payload_json FROM signal_objects WHERE object_type='fact'").fetchall()
    claims = {}
    for object_id, raw in rows:
        try:
            claims[object_id] = normalized(raw)
        except PolicyError:
            claims[object_id] = None
    for object_id, claim in list(claims.items())[:120]:
        if claim is None:
            continue
        key = lk.claim_key(claim[1], claim[2])
        candidates = {row[0] for row in db.execute(
            "SELECT object_id FROM permissions_v2_fact_claim_keys WHERE claim_key=? UNION SELECT object_id FROM permissions_v2_fact_key_completion "
            "WHERE family='claim' AND key=? UNION SELECT object_id FROM permissions_v2_fact_key_opaque WHERE family='claim' AND state<>1",
            (key, key))}
        equal_or_malformed = {other for other, value in claims.items()
                              if other != object_id and (value is None or value[1:] == claim[1:])}
        assert equal_or_malformed <= candidates


@pytest.mark.parametrize("value", [99999999999999999999, 2 ** 63, 10 ** 30])
def test_K1_an_id_past_int64_is_keyed_by_python_not_dropped(db, value):
    """`_names_a_leaf` reads `id` with exact integer arithmetic; SQLite gives back a REAL for
    anything past int64, so the SQL key would be silently wrong. Such rows go opaque and the
    Python pass keys them, which is what keeps the candidate set a superset."""
    write_fact(db, "f1", refs=json.dumps([{"record_id": "x", "id": value}]), payload=payload())
    leaves = {str(value): {"conversation_messages"}}
    assert EvidenceResolver._names_a_leaf(json.dumps([{"record_id": "x", "id": value}]), leaves)
    assert new_sibling_candidates(db, leaves) == {"f1"}          # opaque: always a candidate
    lk.complete_pending(db)
    assert new_sibling_candidates(db, leaves) == {"f1"}          # keyed exactly by Python
    assert db.execute("SELECT count(*) FROM permissions_v2_fact_key_completion WHERE key=?",
                      (str(value),)).fetchone()[0] == 1


def test_K1_whitespace_list_is_exactly_what_python_strips():
    assert {chr(code) for code in lk.PY_WHITESPACE} == {chr(code) for code in range(0x110000) if chr(code).isspace()}


def test_K1_clean_ascii_lower_equals_python_normalization():
    for word in ["Works_On", "a b", "A~Z !", "x" * 64]:
        assert word.lower() == " ".join(word.lower().split())
    assert unicodedata.name("K") == "KELVIN SIGN" and "K".lower() == "k"


def test_K1_a_malformed_fact_still_refuses_every_release(tmp_path):
    corpus = pc.build(tmp_path, seed=5, positives=2)
    with sqlite3.connect(corpus.path) as conn:
        write_fact(conn, "fact-malformed", refs="[]", payload='{"predicate":"a","predicate":"b"}')
    verdict = corpus.resolver.qualify(corpus.positives[0], reviews=corpus.reviews, contract=ATTESTED_CONTRACT)
    assert verdict.reason_code == "evidence_malformed"


# --- K2: the triggers keep the keys ------------------------------------------------------------


def keys_of(conn, object_id):
    return {table: conn.execute(f"SELECT count(*) FROM {table} WHERE object_id=?", (object_id,)).fetchone()[0]
            for table in ("permissions_v2_fact_key_rows", "permissions_v2_fact_ref_keys", "permissions_v2_fact_claim_keys", "permissions_v2_fact_key_opaque", "permissions_v2_fact_key_completion")}


def test_K2_writers_never_break_on_malformed_text(db):
    for number, refs in enumerate(["not json", b"[1]", None, "[NaN]", "[" * 2000 + "]" * 2000, "﻿[]"]):
        write_fact(db, f"odd-{number}", refs=refs if refs is not None else "[]", payload="not json")
    db.commit()
    assert db.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE family='refs'").fetchone()[0] >= 5


def test_K2_every_watched_update_rekeys(db):
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    db.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id='f1'", (json.dumps([{"record_id": "imessage:77"}]),))
    assert new_sibling_candidates(db, {"imessage:77": {"conversation_messages"}}) == {"f1"}
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == set()
    db.execute("UPDATE signal_objects SET payload_json=? WHERE object_id='f1'", (payload(value="Zürich"),))
    assert keys_of(db, "f1")["permissions_v2_fact_claim_keys"] == 0 and keys_of(db, "f1")["permissions_v2_fact_key_opaque"] == 1
    db.execute("UPDATE signal_objects SET object_type='note' WHERE object_id='f1'")
    assert sum(keys_of(db, "f1").values()) == 0
    db.execute("UPDATE signal_objects SET object_type='fact' WHERE object_id='f1'")
    db.execute("UPDATE signal_objects SET object_id='f2' WHERE object_id='f1'")
    assert sum(keys_of(db, "f1").values()) == 0 and keys_of(db, "f2")["permissions_v2_fact_key_rows"] == 1
    db.execute("DELETE FROM signal_objects WHERE object_id='f2'")
    assert sum(keys_of(db, "f2").values()) == 0


def test_K2_completion_is_dropped_by_the_next_write(db):
    write_fact(db, "f1", refs=json.dumps([{"record_id": 10 ** 25}]), payload=payload(value="Zürich"))
    lk.complete_pending(db)
    assert keys_of(db, "f1")["permissions_v2_fact_key_completion"] == 2
    db.execute("UPDATE signal_objects SET payload_json=? WHERE object_id='f1'", (payload(value="Genève"),))
    assert db.execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE object_id='f1' AND state=0").fetchone()[0] == 2
    assert keys_of(db, "f1")["permissions_v2_fact_key_completion"] == 0


def test_K2_replace_on_object_id_leaves_one_set_of_keys(db):
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(value="beta"), verb="INSERT OR REPLACE")
    assert db.execute("SELECT ref_key FROM permissions_v2_fact_ref_keys WHERE object_id='f1'").fetchall() == [("imessage:77",)]
    assert keys_of(db, "f1")["permissions_v2_fact_claim_keys"] == 1


def test_K2_replace_through_the_active_unique_tuple_removes_the_old_rows_keys(db):
    write_fact(db, "old", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="same")
    write_fact(db, "new", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(), key="same",
               verb="INSERT OR REPLACE")
    assert db.execute("SELECT count(*) FROM signal_objects WHERE object_id='old'").fetchone()[0] == 0
    assert sum(keys_of(db, "old").values()) == 0
    assert keys_of(db, "new")["permissions_v2_fact_ref_keys"] == 1


def test_K2_insert_or_ignore_keeps_the_existing_rows_keys(db):
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload(), key="same")
    write_fact(db, "f2", refs=json.dumps([{"record_id": "imessage:77"}]), payload=payload(), key="same",
               verb="INSERT OR IGNORE")
    write_fact(db, "f1", refs="[]", payload=payload(), verb="INSERT OR IGNORE")
    assert db.execute("SELECT ref_key FROM permissions_v2_fact_ref_keys WHERE object_id='f1'").fetchall() == [("imessage:5",)]
    assert sum(keys_of(db, "f2").values()) == 0


def test_K2_scrub_and_record_purge_leave_no_key(tmp_path):
    from topos.features.lifecycle.derived_scrub import purge_derived_for_records, purge_facts_for_source
    corpus = pc.build(tmp_path, seed=9, positives=4, hidden_facts=10)
    conn = sqlite3.connect(corpus.path)
    try:
        doomed = corpus.positives[:2]
        purge_facts_for_source(conn, pc.SOURCE, scrubbed_record_ids={corpus.messages[doomed[0]]})
        purge_derived_for_records(conn, [corpus.messages[doomed[1]]])
        conn.commit()
        for object_id in doomed:
            if conn.execute("SELECT 1 FROM signal_objects WHERE object_id=?", (object_id,)).fetchone() is None:
                assert sum(keys_of(conn, object_id).values()) == 0
        live = {row[0] for row in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")}
        for table in ("permissions_v2_fact_key_rows", "permissions_v2_fact_ref_keys", "permissions_v2_fact_claim_keys", "permissions_v2_fact_key_opaque", "permissions_v2_fact_key_completion"):
            assert {row[0] for row in conn.execute(f"SELECT object_id FROM {table}")} <= live
    finally:
        conn.close()


# --- K3: fallback and rebuild ------------------------------------------------------------------


def test_K3_an_altered_trigger_falls_back_and_the_next_start_rebuilds(db):
    write_fact(db, "f1", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    db.execute("DROP TRIGGER fact_lineage_keys_ai")
    write_fact(db, "f2", refs=json.dumps([{"record_id": "imessage:5"}]), payload=payload())
    db.commit()
    assert not lk.installed(db)
    lk.apply_permissions_fact_lineage_keys_v1_up(db)
    assert lk.installed(db)
    assert new_sibling_candidates(db, {"imessage:5": {"conversation_messages"}}) == {"f1", "f2"}


def test_K3_reads_fall_back_while_keys_are_not_trusted(tmp_path, monkeypatch):
    corpus = pc.build(tmp_path, seed=4, positives=2)
    with sqlite3.connect(corpus.path) as conn:
        # Both write triggers gone: the sibling below is keyed nowhere, so only a read that
        # checks the triggers before trusting the tables can still see it.
        conn.execute("DROP TRIGGER fact_lineage_keys_ai")
        conn.execute("DROP TRIGGER fact_lineage_keys_au")
        # A sibling written while the keys are down must still refuse the release.
        write_fact(conn, "sibling", refs=json.dumps([{"record_id": corpus.messages[corpus.positives[0]]}]),
                   payload=payload(value="x", disclosure="unknown"))
        conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id='sibling'",
                     (json.dumps({"subject_entity_id": "e", "predicate": "p", "object_value": "v", "disclosure": "unknown"}),))
    with pytest.raises(PolicyError, match="owner_only"):
        corpus.resolver.with_qualified(corpus.positives[0], reviews=corpus.reviews, contract=ATTESTED_CONTRACT,
                                       discloses_sources=True, callback=lambda *_: None)


def test_K1_a_superseded_fact_with_the_same_claim_does_not_block_a_release(tmp_path):
    """Candidates are narrowed to active facts, as the old scan was: a closed fact with an
    equal claim is not an independent copy, and must not withhold the release."""
    corpus = pc.build(tmp_path, seed=7, positives=2)
    fact = corpus.positives[0]
    with sqlite3.connect(corpus.path) as conn:
        payload_json = conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (fact,)).fetchone()[0]
        write_fact(conn, "closed-copy", refs="[]", payload=payload_json, key="closed-copy")
        conn.execute("UPDATE signal_objects SET valid_to='2026-01-01' WHERE object_id='closed-copy'")
    assert corpus.resolver.qualify(fact, reviews=corpus.reviews, contract=ATTESTED_CONTRACT).verdict == "qualified"
    with sqlite3.connect(corpus.path) as conn:  # the same claim left active IS a copy
        conn.execute("UPDATE signal_objects SET valid_to=NULL WHERE object_id='closed-copy'")
    assert corpus.resolver.qualify(fact, reviews=corpus.reviews,
                                   contract=ATTESTED_CONTRACT).reason_code == "independent_copy_lineage"


def test_K3_the_read_completes_a_bounded_batch(tmp_path, monkeypatch):
    corpus = pc.build(tmp_path, seed=6, positives=1)
    with sqlite3.connect(corpus.path) as conn:
        for number in range(150):
            write_fact(conn, f"de-{number}", refs="[]", payload=payload(value=f"Zürich {number}"))
    count = lambda: sqlite3.connect(corpus.path).execute("SELECT count(*) FROM permissions_v2_fact_key_opaque WHERE state=0").fetchone()[0]
    assert count() == 150
    corpus.resolver.qualify(corpus.positives[0], reviews=corpus.reviews, contract=ATTESTED_CONTRACT)
    assert count() == 150 - evidence.COMPLETION_BATCH


# --- K4: no scan of signal_objects ------------------------------------------------------------------


def test_K4_the_read_path_never_scans_signal_objects(tmp_path, monkeypatch):
    corpus = pc.build(tmp_path, seed=8, positives=2, hidden_facts=30)
    statements = []
    real = sqlite3.connect

    def tracing(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(evidence.sqlite3, "connect", tracing)
    corpus.resolver.with_qualified(corpus.positives[0], reviews=corpus.reviews, contract=ATTESTED_CONTRACT,
                                   discloses_sources=True, callback=lambda *_: None)
    monkeypatch.undo()
    probe = sqlite3.connect(corpus.path)
    scans = []
    for sql in statements:
        if "signal_objects" not in sql or not sql.lstrip().upper().startswith("SELECT"):
            continue
        for row in probe.execute("EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")):
            if re.match(r"SCAN (signal_objects|s)( |$)", row[3]):
                scans.append((sql[:120], row[3]))
    assert not scans, scans


def test_K2_the_key_tables_are_hidden_permission_state():
    # They hold record ids and claim text prefixes; no explorer surface may serve or drop them.
    from topos.data_explorer_tables import is_permission_state_table
    assert all(is_permission_state_table(name) for name in lk.TABLES)
    assert not any(name.startswith("permissions_v2") for name in lk.TRIGGERS)  # clock_state owns that trigger prefix
