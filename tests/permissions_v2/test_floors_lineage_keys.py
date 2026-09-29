"""`_floors` asks the migration-78 keys for the facts naming a message, and walks what the table walk walked.

`_floors` runs on every direct-message qualification: once per ranked candidate in the release
re-check (under the node write gate), once per reviewed message in an index build, and in the
automatic assessment and the review preview. It read every fact row on the node and parsed each
one's references to find the few that name the message. The keys give a superset of those facts
by index (migration 78's invariant, which the sibling floor already relies on), and
`_names_a_leaf` still decides each one. These tests pin that the facts walked are the same facts
in the same order, with and without the keys, over the reference shapes the migration's own fuzz
draws; that the read uses the keys; and that the first refusal, and so its reason, is unchanged.
"""
from __future__ import annotations

import json
import random

import pytest

from tests.permissions_v2.test_bk3_lineage_keys import LEAVES, db, random_payload, random_refs, write_fact  # noqa: F401
from tests.permissions_v2.test_direct_message_evidence import qualify, setup
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.message_evidence import facts_naming
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk

TABLES = ("conversation_messages", "ai_chat_messages")


def walked_by_the_table(conn, leaves):
    """The pre-change walk: every fact in table order, decided by the unchanged predicate."""
    return [tuple(row) for row in conn.execute(
        "SELECT object_id,payload_json,source_refs_json FROM signal_objects WHERE object_type='fact' ORDER BY rowid")
        if EvidenceResolver._names_a_leaf(row[2], leaves)]


def fuzz(conn, seed, count=900):
    rng = random.Random(seed)
    for number in range(count):
        write_fact(conn, f"fact-{number}", refs=random_refs(rng), payload=random_payload(rng))
    conn.commit()
    return rng


@pytest.mark.parametrize("seed", range(6))
def test_the_facts_walked_are_the_table_walks_in_its_order(db, seed):
    fuzz(db, seed)
    assert lk.installed(db)
    lk.complete_pending(db, limit=None if seed % 2 else 5)  # a partly completed opaque backlog too
    db.commit()
    walked = 0
    for leaf in LEAVES:
        for table in TABLES:
            expected = walked_by_the_table(db, {leaf: {table}})
            assert [tuple(fact) for fact in facts_naming(db, {leaf: {table}})] == expected, (leaf, table)
            walked += len(expected)
    assert walked, "the fuzz must reach facts that name a leaf"


def test_the_read_asks_the_keys_and_scans_no_facts(db):
    fuzz(db, 3, count=200)
    executed = []
    db.set_trace_callback(executed.append)
    list(facts_naming(db, {LEAVES[0]: {"conversation_messages"}}))
    db.set_trace_callback(None)
    [statement] = [line for line in executed if "signal_objects" in line]
    steps = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + statement)]
    assert not any(step.startswith("SCAN signal_objects") for step in steps), steps
    assert any("permissions_v2_fact_ref_keys" in step for step in steps), steps


def test_without_the_keys_it_walks_the_table_as_before(db):
    fuzz(db, 4, count=300)
    db.execute("DROP TRIGGER fact_lineage_keys_au")
    assert not lk.installed(db)
    for leaf in LEAVES:
        assert [tuple(fact) for fact in facts_naming(db, {leaf: {"conversation_messages"}})] == \
            walked_by_the_table(db, {leaf: {"conversation_messages"}})


def naming_fact(conn, object_id, value):
    conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                 "source_refs_json, valid_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                 (object_id, "profile", "fact", "key:" + object_id,
                  json.dumps({"subject_entity_id": "owner-entity", "predicate": "works_on", "object_value": value,
                              "disclosure": "scoped", "asserted_by": "owner"}),
                  json.dumps([{"table": "conversation_messages", "record_id": "imessage:1", "source_id": "imessage",
                               "dataset_id": "native-dataset"}]), "t", "t", "t"))


@pytest.mark.parametrize("keys", [True, False], ids=["lineage_keys", "table_walk"])
@pytest.mark.parametrize("first, reason", [("excluded", "intelligence_excluded"), ("protected", "owner_only")])
def test_the_first_refusing_fact_in_rowid_order_names_the_reason(legacy, keys, first, reason):
    """Two facts name the message and refuse for different reasons: the earlier row decides, both ways."""
    resolver, reviews, identity = setup(legacy)
    conn = legacy[1]
    if keys:
        lk.apply_permissions_fact_lineage_keys_v1_up(conn)
    assert qualify(resolver, reviews, identity)
    for name in (first, *({"excluded", "protected"} - {first})):
        naming_fact(conn, "fact-" + name, name)
    conn.execute("INSERT INTO intelligence_exclusions(exclusion_id, artifact_type, artifact_key) "
                 "VALUES ('x-fact', 'fact', 'owner-entity:works_on:excluded')")
    conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES ('signal_objects', 'fact-protected')")
    conn.commit()
    assert lk.installed(conn) is keys
    with pytest.raises(PolicyError, match=reason):
        qualify(resolver, reviews, identity)
