"""The p2c-v3 twin corpora behind `scripts/permissions_v2/p2c_direct_timing_twins.py` are twins.

A timing comparison means something only if the corpora it compares hold the same permitted set
and give the same answers, differ in nothing but hidden facts, and actually route every re-check
through `_floors`. These tests pin all of that on small corpora.
"""
from __future__ import annotations

import json
import sqlite3
import stat

import pytest

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


def test_v3_index_is_private_and_carries_no_plaintext_message(tmp_path):
    node = dst.build(tmp_path / "v3-index", members=5, hidden_facts=0, seed=11)
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    disk = path.read_bytes()
    with sqlite3.connect(node.corpus.path) as conn:
        bodies = [row[0] for row in conn.execute("SELECT content FROM conversation_messages")]
    assert len(bodies) == 5
    assert all(body.encode() not in disk for body in bodies)


@pytest.mark.parametrize("change", ["alias", "participant", "mention"])
def test_v3_protected_dependency_invalidates_ranking_then_withholds_matches(tmp_path, change):
    node = dst.build(tmp_path / "v3-boundary", members=12, hidden_facts=0, seed=9, protected=True)
    before, refused = node.search_request("roadmap", k=5)
    assert refused is None and before["records"]
    protected = before["records"][0]["content"]
    with sqlite3.connect(node.corpus.path) as conn:
        if change == "alias":
            conn.execute("UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'")
        else:
            message_id, conversation_id = conn.execute(
                "SELECT message_id, conversation_id FROM conversation_messages WHERE content=?", (protected,)).fetchone()
            if change == "participant":
                conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                             "VALUES(?,?,?,'protected-contact')", (conversation_id, dst.DATASET, "imessage"))
            else:
                conn.execute("INSERT INTO entity_mentions(mention_id,entity_id,record_id,source_id,canonical_table) "
                             "VALUES('protected-mention','protected-entity',?,'imessage','conversation_messages')",
                             (message_id,))
    stale, refused = node.search_request("roadmap", k=5)
    assert stale is None and refused is not None
    node.rebuild()
    after, refused = node.search_request("roadmap", k=5)
    assert refused is None
    if change == "alias":
        assert all("roadmap" not in item["content"].casefold() for item in after["records"])
    else:
        assert protected not in {item["content"] for item in after["records"]}


# --- the owner's NSFW decision, on the door that ships ----------------------------------------------------------
# `content_nsfw` withholds a row from every share. The locator door's cases drive a test-only adapter now
# (`test_source_release_withholds_nsfw.py`), and the search door's lived on the retired p2c-v1 profile
# (`test_fuzz_discovery.py` D4, D5). These are the two outcomes on a p2c-v3 grant: flagged after indexing, and
# through the next rebuild.

def _a_member_a_search_returns(node, members, seed):
    """(query, record content, message id) for one member a search really returns: nothing is assumed searchable."""
    bodies = dst.texts(members, seed)
    with sqlite3.connect(node.corpus.path) as conn:
        ids = dict(conn.execute("SELECT content, message_id FROM conversation_messages").fetchall())
    for body in bodies:
        query = body.split()[-1]                      # each member's own `item<N>` word
        output, refused = node.search_request(query, k=10)
        assert refused is None, refused
        contents = [record["content"] for record in output["records"]]
        if body in contents:
            return query, body, ids[body]
    raise AssertionError("no search returned a member: there is nothing to flag and nothing proved")


def _flag_nsfw(node, message_id):
    with sqlite3.connect(node.corpus.path) as conn:
        if "content_nsfw" not in [row[1] for row in conn.execute("PRAGMA table_info(conversation_messages)")]:
            conn.execute("ALTER TABLE conversation_messages ADD COLUMN content_nsfw INTEGER")
        conn.execute("UPDATE conversation_messages SET content_nsfw=1 WHERE message_id=?", (message_id,))
        conn.commit()


def test_v3_a_message_flagged_nsfw_after_indexing_never_comes_back(tmp_path):
    """The window the index cannot cover: flagged after the build, still a member until the next one.

    What holds here is wider than the NSFW rule: the flag is part of the row's reviewed surface, so the row no
    longer matches what was proven and assessed, and the door's re-check drops it. The content rule itself is
    pinned in `test_direct_message_evidence.py`, on a row that carried the flag when it was proven.
    """
    node = dst.build(tmp_path / "v3-nsfw-door", members=6, hidden_facts=0, seed=23)
    query, content, message_id = _a_member_a_search_returns(node, 6, 23)
    _flag_nsfw(node, message_id)
    after, refused = node.search_request(query, k=10)
    # Either is safe and the door may choose: answer without the record, or refuse the request whole because
    # its membership no longer matches what the index holds. The record coming back is what must never happen.
    returned = [] if refused is not None else [record["content"] for record in after["records"]]
    assert content not in returned


def test_v3_a_message_flagged_nsfw_after_its_proof_stays_out_through_a_rebuild(tmp_path):
    """A rebuild does not bring it back, and the owner's side cannot assess it again: the flagged row is no
    longer the row that was proven. (A row flagged BEFORE its proof gets past that and meets the content rule
    itself: `test_direct_message_evidence.py::test_a_message_flagged_nsfw_when_it_was_proven_...`.)"""
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2.automatic_message_review import prepare
    from topos.permissions_v2.canonical import PolicyError
    node = dst.build(tmp_path / "v3-nsfw-index", members=6, hidden_facts=0, seed=29)
    query, content, message_id = _a_member_a_search_returns(node, 6, 29)
    grant = node.search_raw["binding"]["grant_id"]
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity("conversation_messages", message_id, "imessage", dst.DATASET)
    with owner():
        prepare(resolver, reviews, identity)             # the control: unflagged, the message can be assessed
    _flag_nsfw(node, message_id)
    with owner(), pytest.raises(PolicyError, match="native_owner_provenance_unavailable"):
        prepare(resolver, reviews, identity)
    assert node.rebuild() == {grant: "ready"}
    after, refused = node.search_request(query, k=10)
    assert refused is None, refused
    assert content not in [record["content"] for record in after["records"]]
    # Only that message left: the others are still members and still answer.
    others = [body for body in dst.texts(6, 29) if body != content]
    found = set()
    for body in others:
        output, refused = node.search_request(body.split()[-1], k=10)
        assert refused is None, refused
        found.update(record["content"] for record in output["records"])
    assert content not in found and found & set(others)
