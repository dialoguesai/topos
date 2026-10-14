"""NR166B: the share index's member loop, its build and its sweep with the boundary's pass memory, against without it.

On a knowledge share over invented messages with an active Off-limits boundary (`direct_search_twins`):
  - the member loop matches each member's row once (it matched it three times), and releases the same records;
  - a build with the memory and one without seal the same members (the same revisions and contexts);
  - the request's check and the sweep's deep check give the same verdict and the same stale class, for a share
    left as built and after each kind of change below (the classes reached: basis, dependencies,
    classification_context, member_unavailable; `context`, `fingerprint` and `lineage` are behind the member's own
    dependency, which carries the same context, so no single change reaches them first).
`entity_boundary.PASS_MEMORY` off is the code as it was before NR166B. Invented rows only; no model, port or network.
"""
from __future__ import annotations

import collections
import json
import sqlite3

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from topos.permissions_v2 import entity_boundary, search_timing
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.protection_clock import clock_state
from topos.permissions_v2.search_index import SearchIndexService, index_path, unseal

MEMBERS = 8


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return dst.build(tmp_path_factory.mktemp("nr166b") / "share", members=MEMBERS, hidden_facts=0, seed=9,
                     protected=True)


@pytest.fixture
def node(built):
    """The module's share, restored to its built state after each test (the canonical database and the index)."""
    saved = sqlite3.connect(":memory:")
    with sqlite3.connect(built.corpus.path) as source:
        source.backup(saved)
    path = index_path(built.index.root, built.search_raw["binding"]["grant_id"])
    index_bytes = path.read_bytes()
    yield built
    with sqlite3.connect(built.corpus.path) as target:
        saved.backup(target)
    path.write_bytes(index_bytes)


def grant(node):
    return node.search_raw["binding"]["grant_id"]


def full_matches(monkeypatch):
    """(table, record_id) -> how many times the member loop matched that row in full."""
    counts, inside = collections.Counter(), []
    loop, full = SearchIndexService._members_current, EntityBoundary._observe

    def members_current(self, *args, **kwargs):
        inside.append(True)
        try:
            return loop(self, *args, **kwargs)
        finally:
            inside.pop()

    def observe(self, **kwargs):
        if inside:
            counts[(kwargs["table"], kwargs["record_id"])] += 1
        return full(self, **kwargs)
    monkeypatch.setattr(SearchIndexService, "_members_current", members_current)
    monkeypatch.setattr(EntityBoundary, "_observe", observe)
    return counts


def released(node, memory, monkeypatch):
    monkeypatch.setattr(entity_boundary, "PASS_MEMORY", memory)
    found = []
    for query in dst.queries(MEMBERS, 9, 4):
        output, refused = node.search_request(query, k=10)
        found.append(refused if output is None else json.dumps(output["records"], sort_keys=True))
    return found


def test_the_member_loop_matches_each_member_once_and_releases_the_same(node, monkeypatch):
    counts = full_matches(monkeypatch)
    without = released(node, False, monkeypatch)
    assert sum(isinstance(item, str) and item != "[]" for item in without) > 0   # something is released
    per_row_without = dict(counts)
    counts.clear()
    assert released(node, True, monkeypatch) == without
    assert len(counts) == len(per_row_without) >= MEMBERS
    # Without the memory each member's row was matched three times per check; with it, once.
    assert set(per_row_without.values()) == {3 * len(without)}
    assert set(counts.values()) == {len(without)}


def sealed_members(node):
    index = node.index
    path = index_path(index.root, grant(node))
    opened = index._open(path)
    key = index.keys.get(grant(node), create=False)
    return opened["basis"], sorted((opaque, unseal(key, opaque, blob)) for opaque, blob in opened["sealed"])


def test_a_build_seals_the_same_members_with_or_without_the_memory(node, monkeypatch):
    builds = []
    for memory in (False, True):
        monkeypatch.setattr(entity_boundary, "PASS_MEMORY", memory)
        with owner():
            assert node.index.rebuild(grant(node), now=node.now[0])["state"] == "ready"
        builds.append(sealed_members(node))
    assert builds[0] == builds[1] and len(builds[0][1]) == MEMBERS


def verdict(node, *, deep, monkeypatch):
    """`_current` on a fresh read snapshot (the sweep's deep check, or the request's), and the stale classes it gave."""
    stages = []
    monkeypatch.setattr(search_timing, "index_event", lambda kind, path, **fields: stages.append((kind, fields)))
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, grant(node), node.now[0])[0]
    conn = sqlite3.connect(node.corpus.resolver.path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("BEGIN")
        return node.index._current(index_path(node.index.root, grant(node)), grant(node), authority,
                                   clock_state(conn), conn, deep=deep), stages
    finally:
        conn.close()


def a_member(node):
    with sqlite3.connect(node.corpus.path) as conn:
        return conn.execute("SELECT message_id, conversation_id FROM conversation_messages ORDER BY message_id "
                            "LIMIT 1 OFFSET 3").fetchone()


CHANGES = {
    "none": [],
    "content": ["UPDATE conversation_messages SET content=content || ' and more' WHERE message_id=:message"],
    "protected_content": ["UPDATE conversation_messages SET content=content || ' Mara Example' WHERE message_id=:message"],
    "participant": ["INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                    "VALUES(:conversation,'" + dst.DATASET + "','imessage','protected-contact')"],
    "sender": ["UPDATE conversation_messages SET sender_id='someone-else' WHERE message_id=:message"],
    "other_participant": ["INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                          "VALUES(:conversation,'" + dst.DATASET + "','imessage','contact-new')"],
    "mention": ["INSERT INTO entity_mentions(mention_id,entity_id,record_id,source_id,canonical_table) "
                "VALUES('nr166b-mention','protected-entity',:message,'imessage','conversation_messages')"],
    "metadata": ["UPDATE conversation_messages SET metadata_json='{\"note\": \"Mara Example\"}' WHERE message_id=:message"],
    "bookkeeping": ["UPDATE conversations SET updated_at='2030-01-01 00:00:00' WHERE conversation_id=:conversation"],
    "deleted": ["DELETE FROM conversation_messages WHERE message_id=:message"],
    "owner_only": ["INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversations',:conversation)"],
    "alias": ["UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'"],
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_the_check_and_the_sweep_give_the_same_verdict_with_or_without_the_memory(node, monkeypatch, change):
    message, conversation = a_member(node)
    with sqlite3.connect(node.corpus.path) as conn:
        for sql in CHANGES[change]:
            conn.execute(sql, {"message": message, "conversation": conversation})
    verdicts = {}
    for memory in (False, True):
        monkeypatch.setattr(entity_boundary, "PASS_MEMORY", memory)
        verdicts[memory] = [verdict(node, deep=deep, monkeypatch=monkeypatch) for deep in (False, True)]
    assert verdicts[False] == verdicts[True]
    if change in ("none", "bookkeeping"):   # a sync batch's touch moves no revision (BL-155)
        assert verdicts[True] == [(True, []), (True, [])]
    else:
        assert any(current is False for current, _stages in verdicts[True]), change   # each change is seen
