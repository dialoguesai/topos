"""The per-grant index: what it holds, how it is built, and how it is destroyed.

Design review conditions pinned here:
1. The index is a scrub surface: a black hole, a tombstone, an owner-only mark, a
   row deletion or scrub, a revoke or an expiry deletes the files eagerly, and a
   missing index refuses.
2. No raw content, sender field or internal row id is readable from an index file;
   mode 0600 in a 0700 directory.
4. The tokenizer is Unicode-aware.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, owner
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.permissions_v2 import search_index
from topos.permissions_v2.search_index import index_path, tokenize


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=5, counts={name: 2 for name in mc.KINDS})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path)
    node.rebuild()
    return node


def index_file(node):
    return index_path(node.index.root, node.search_raw["binding"]["grant_id"])


def answers(node):
    output, refused = node.search_request("roadmap deploy review", k=5)
    return output, refused


# -- what the file holds (condition 2) ---------------------------------------------

def test_index_holds_only_ranking_material_and_is_private(node):
    path = index_file(node)
    raw = path.read_bytes()
    assert raw
    for unit in node.corpus.units:
        assert unit.message_id.encode() not in raw
        assert unit.text.encode() not in raw
        assert unit.fact_id.encode() not in raw
        if unit.canary and not unit.p2a_release:
            assert unit.canary.encode() not in raw  # nothing outside P, not even a term
    for forbidden in (b"imessage:", b"sender", b"owner-1", b"dataset-1", b"content"):
        assert forbidden not in raw
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(node.index.root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(node.index.keys.path).st_mode) == 0o600
    assert not path.with_name(path.name + "-wal").exists()
    with sqlite3.connect(path) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"meta", "members", "vectors"}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(members)")}
        assert columns == {"opaque_id", "event_at_us", "doc_len", "terms_json", "sealed"}


def test_members_are_exactly_the_releasable_permitted_units(node):
    with sqlite3.connect(index_file(node)) as conn:
        count = conn.execute("SELECT count(*) FROM members").fetchone()[0]
    # P minus what search can never release: NSFW-flagged, undated, older than the window.
    # A future-dated record stays: the rolling window will reach it.
    expected = sum(unit.p2a_release and unit.kind not in {"nsfw_flagged", "event_missing", "event_old"}
                   for unit in node.corpus.units)
    assert count == expected


def test_sealed_fields_do_not_open_without_the_grant_key(node):
    with sqlite3.connect(index_file(node)) as conn:
        opaque, sealed = conn.execute("SELECT opaque_id, sealed FROM members LIMIT 1").fetchone()
    key = node.index.keys.get(node.search_raw["binding"]["grant_id"], create=False)
    assert search_index.unseal(key, opaque, sealed)["facts"]
    with pytest.raises(Exception):
        search_index.unseal(bytes(32), opaque, sealed)
    with pytest.raises(Exception):
        search_index.unseal(key, "r." + "0" * 64, sealed)  # bound to its own member


# -- the scrub surface (condition 1) -----------------------------------------------

def assert_gone_and_refusing(node):
    assert not index_file(node).exists()
    assert not list(node.index.root.glob("grant-*.db"))
    output, refused = answers(node)
    assert output is None and refused == "permission_denied"


def test_black_hole_through_the_owner_store_deletes_every_index_at_once(node):
    assert answers(node)[1] is None
    with sqlite3.connect(node.corpus.path) as conn:
        BlackholeStore(conn).blackhole_entity(entity_ref="Isolde")
    assert_gone_and_refusing(node)


@pytest.mark.parametrize("write", [
    "INSERT INTO entity_blackholes(blackhole_id, normalized_name, canonical_name) VALUES('bh-raw','isolde','Isolde')",
    "INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','imessage:1')",
    "INSERT INTO intelligence_exclusions(exclusion_id, artifact_type, artifact_key) VALUES('x-raw','record','imessage:1')",
], ids=["black_hole", "owner_only", "tombstone"])
def test_any_protection_write_is_swept_before_the_next_answer(node, write):
    assert answers(node)[1] is None
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(write)
    assert node.index.sweep(now=mc.NOW) >= 1
    assert_gone_and_refusing(node)


def test_the_request_path_sweeps_by_itself(node):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO entity_blackholes(blackhole_id, normalized_name, canonical_name) VALUES('b2','x','X')")
    assert index_file(node).exists()
    assert answers(node) == (None, "permission_denied")
    assert not index_file(node).exists()


@pytest.mark.parametrize("change", [
    "DELETE FROM conversation_messages WHERE message_id=?",                                   # deletion / scrub
    "UPDATE conversation_messages SET content=content || ' edited' WHERE message_id=?",       # edit after build
    "UPDATE conversation_messages SET content_nsfw=1 WHERE message_id=?",                     # re-flagged
    "UPDATE signal_objects SET valid_to='2027-01-01T00:00:00Z' WHERE object_id IN "
    "(SELECT object_id FROM signal_objects WHERE source_refs_json LIKE '%' || ? || '%')",      # witness superseded
], ids=["deleted", "edited", "nsfw_flagged", "fact_superseded"])
def test_a_member_row_deleted_or_scrubbed_drops_the_index(node, change):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(change, (member.message_id,))
    node.index.sweep(now=mc.NOW)
    assert_gone_and_refusing(node)


def test_source_scrub_hook_purges(node):
    assert search_index.purge_for_database(node.corpus.path) == 1
    assert_gone_and_refusing(node)
    assert search_index.purge_for_database("") == 0
    assert search_index.purge_for_database(node.corpus.path.parent / "missing" / "db") == 0


def test_revoke_forgets_index_and_rotates_the_key(node):
    grant = node.search_raw["binding"]["grant_id"]
    first = answers(node)[0]["records"][0]["record_id"]
    old_key = node.index.keys.get(grant, create=False)
    with owner():
        node.ledger.revoke(grant, expected_epoch=node.epoch(), command_id="revoke-search")
    node.index.sweep(now=mc.NOW)
    assert not index_file(node).exists() and node.index.keys.get(grant, create=False) is None
    node.activate(node.search_raw, generation=3)
    node.rebuild()
    assert node.index.keys.get(grant, create=False) != old_key
    ids = {record["record_id"] for record in answers(node)[0]["records"]}
    assert first not in ids


def test_expiry_forgets(node):
    node.now[0] = mc.NOW + 8 * 86_400
    node.index.sweep(now=node.now[0])
    assert not index_file(node).exists()


def test_policy_change_without_rebuild_refuses_then_rebuild_answers(node):
    raw = dict(node.search_raw, policy_version_id="policy-search-2")
    node.activate(raw, generation=2)
    assert answers(node)[1] == "permission_denied"
    node.rebuild()
    assert answers(node)[1] is None


def test_over_cap_refuses_instead_of_truncating(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=9, counts={"clean_positive_C": 6})
    node = Node(corpus, tmp_path, search_raw=mc.search_policy(max_permitted=5))
    assert node.rebuild() == {"grant-search": "over_cap"}
    assert answers(node) == (None, "permission_denied")
    node2 = Node(corpus, tmp_path / "exact", search_raw=mc.search_policy(max_permitted=6))
    assert node2.rebuild() == {"grant-search": "ready"}


def test_rebuild_is_owner_only(node):
    from tests.permissions_v2.message_search_harness import recipient
    with recipient():
        with pytest.raises(Exception, match="owner_authority_required"):
            node.index.rebuild("grant-search", now=mc.NOW)


def test_stale_authority_builds_nothing(node):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','x')")
    with owner():
        assert node.index.rebuild("grant-search", now=mc.NOW)["state"] == "stale"
    assert not index_file(node).exists()


# -- tokenizer (condition 4) --------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Déploiement du Backlog", ["déploiement", "du", "backlog"]),
    ("ＲＯＡＤＭＡＰ review", ["roadmap", "review"]),         # NFKC folds full-width
    ("Straße STRASSE", ["strasse", "strasse"]),             # casefold
    ("会议 纪要 a I", ["会议", "纪要"]),
    ("Встреча по бюджету", ["встреча", "по", "бюджету"]),
])
def test_tokenizer_is_unicode_aware(text, expected):
    assert tokenize(text) == expected


def test_non_ascii_content_is_searchable(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=21, counts={"clean_positive_C": 3})
    target = corpus.units[0]
    with sqlite3.connect(corpus.path) as conn:
        conn.execute("UPDATE conversation_messages SET content='Déploiement: réunion budgétaire' WHERE message_id=?",
                     (target.message_id,))
    # The edit stales that unit's review; the owner reviews it again.
    from topos.permissions_v2.evidence import ReviewedClassification
    with owner():
        snapshot = corpus.resolver.inspect_for_review(target.fact_id)
        corpus.reviews.record_review(resolver=corpus.resolver, review_id="rereview", expected_snapshot=snapshot,
            classifications=[ReviewedClassification(evidence=version, domains=["work"], sensitivity="none",
                subject_entity_ids=["self"], authorship="owner_authored", speech="direct_self_statement",
                independent_copies="none_known") for version in snapshot.artifacts + snapshot.leaves],
            reviewed_at=mc.NOW - 10)
    node = Node(corpus, tmp_path)
    node.rebuild()
    output, refused = node.search_request("RÉUNION", k=3)
    assert refused is None
    assert output["records"] and output["records"][0]["content"] == "Déploiement: réunion budgétaire"


def test_a_later_sibling_fact_or_copy_is_dropped_by_the_owner_sweep_not_the_request(node):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    with sqlite3.connect(node.corpus.path) as conn:
        from topos.features.facts.store import FactStore
        FactStore(conn).assert_fact(subject_entity_id=mc.OWNER_ENTITY, predicate="lives_in", object_value="a later place",
            disclosure="owner_only", source_refs=[{"table": "conversation_messages", "dataset_id": mc.DATASET,
            "source_id": member.source_id, "record_id": member.message_id}], asserted_by="owner")
    # The request path does not scan lineage (its cost would grow with the node) ...
    assert index_file(node).exists()
    output, refused = answers(node)
    assert refused is None and member.text not in json.dumps(output)       # ... and the re-check still refuses it
    # ... the owner-side / daemon sweep does, and drops the index.
    node.index.sweep(now=mc.NOW)
    assert not index_file(node).exists()


def test_an_operational_column_change_does_not_refuse_search(node):
    member = next(unit for unit in node.corpus.units if unit.search_release)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("UPDATE conversation_messages SET content_disclosure='x', created_at='2030-01-01' WHERE message_id=?",
                     (member.message_id,))
    assert answers(node)[1] is None
    node.index.sweep(now=mc.NOW)
    assert index_file(node).exists()
