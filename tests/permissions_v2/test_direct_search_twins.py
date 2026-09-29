"""The p2c-v3 twin corpora behind `scripts/permissions_v2/p2c_direct_timing_twins.py` are twins.

A timing comparison means something only if the corpora it compares hold the same permitted set
and give the same answers, differ in nothing but hidden facts, and actually route every re-check
through `_floors`. These tests pin all of that on small corpora.
"""
from __future__ import annotations

import json
import sqlite3

from tests.permissions_v2 import direct_search_twins as dst
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.search_index import index_path


def answers(node, queries):
    found = []
    for query in queries:
        output, refused = node.search_request(query, k=10)
        assert refused is None, refused
        found.append(json.dumps(output, sort_keys=True))
    return found


def test_twins_share_every_answer_and_differ_only_in_hidden_facts(tmp_path):
    from tests.permissions_v2 import message_search_corpus as mc
    clock = mc.NOW
    base = dst.build(tmp_path / "h0", members=5, hidden_facts=0, seed=7)
    assert mc.NOW == clock, "the builder must not leave its clock for later tests"
    hidden = dst.build(tmp_path / "h300", members=5, hidden_facts=300, seed=7)
    queries = dst.queries(5, 7, 6)
    released = answers(base, queries)
    assert released == answers(hidden, queries)
    assert sum(len(json.loads(output)["records"]) for output in released) > 0
    for node in (base, hidden):
        with sqlite3.connect(index_path(node.index.root, node.search_raw["binding"]["grant_id"])) as index:
            assert index.execute("SELECT member_count FROM meta").fetchone()[0] == 5
    counts = []
    for node in (base, hidden):
        with sqlite3.connect(node.corpus.path) as conn:
            counts.append(conn.execute("SELECT count(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0])
            members = {row[0] for row in conn.execute("SELECT message_id FROM conversation_messages")}
            for refs, in conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id LIKE 'hidden-fact-%'"):
                assert not EvidenceResolver._names_a_leaf(refs, {member: {"conversation_messages"} for member in members})
    assert counts == [5, 305]  # one sibling per member, then the hidden facts


def test_every_member_has_a_naming_fact_for_floors_to_walk(tmp_path):
    node = dst.build(tmp_path / "corpus", members=4, hidden_facts=10, seed=2)
    with sqlite3.connect(node.corpus.path) as conn:
        members = [row[0] for row in conn.execute("SELECT message_id FROM conversation_messages")]
        facts = conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_type='fact'").fetchall()
    for member in members:
        assert sum(EvidenceResolver._names_a_leaf(refs, {member: {"conversation_messages"}}) for refs, in facts) == 1
