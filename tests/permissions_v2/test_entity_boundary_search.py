"""Signed searches must release useful records while keeping protected holes."""
import json
import sqlite3

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from topos.permissions_v2.search_index import index_path
from topos.storage.canonical.conversations_tables import (ensure_contacts_table,
    ensure_contact_identifiers_table, ensure_conversations_table, ensure_conversation_participants_table)


@pytest.fixture
def node(tmp_path):
    corpus = mc.build(tmp_path / "corpus", seed=781, counts={"unreviewed":15,"forwarded":1,"correspondent":1,"opted_out":1})
    with sqlite3.connect(corpus.path) as conn:
        for create in (ensure_contacts_table, ensure_contact_identifiers_table,
                       ensure_conversations_table, ensure_conversation_participants_table):
            create(conn)
        for index, unit in enumerate(corpus.units):
            conversation = "thread-" + str(index)
            conn.execute("UPDATE conversation_messages SET conversation_id=? WHERE message_id=?", (conversation,unit.message_id))
            conn.execute("INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)", (conversation,mc.DATASET,unit.source_id))
        conn.execute("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name) VALUES('protected-contact',?,'address_book','Mara Example')", (mc.DATASET,))
        conn.execute("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) VALUES('protected-contact',?,'address_book','mara@example.org','email')", (mc.DATASET,))
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,contact_id) VALUES('protected-entity','person','Mara Example','mara example','protected-contact')")
        conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) VALUES('bh','protected-entity','Mara Example','mara example','complete')")
        conn.execute("UPDATE conversation_messages SET content='Mara Example private_protected_canary' WHERE message_id=?", (corpus.units[0].message_id,))
        conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) VALUES('thread-1',?,?,'protected-contact')", (mc.DATASET,mc.SOURCE))
        # Same ordinary thread as a protected mention, but no dependency on it.
        mc.insert_message(conn,message_id="unrelated-neighbor",source_id=mc.SOURCE,content="Mara Example private_neighbor_canary",event_at=corpus.units[2].event_at,conversation_id="thread-2")
    # Recreate the negative reviews on the current closure, so stale consent
    # cannot make the authorship/forwarding checks pass vacuously.
    for index, unit in enumerate(corpus.units):
        if unit.kind in ("forwarded", "correspondent"):
            kind = mc.KINDS[unit.kind]
            mc._review(corpus.resolver,corpus.reviews,unit.fact_id,domains=kind.domains,sensitivity=kind.sensitivity,review_id=f"d8-review-{index}")
    policy = mc.search_policy()
    policy["search"]["max_k"] = 10
    node = Node(corpus,tmp_path,model=None,search_raw=policy)
    assert node.rebuild()[policy["binding"]["grant_id"]] == "ready"
    return node


def search(node, query="roadmap deploy launch review team", *, k=10):
    return node.search_request(query,k=k)


def test_signed_search_has_nonempty_useful_yield_and_protected_holes(node):
    output, refused = search(node)
    assert refused is None and 0 < len(output["records"]) <= 10
    allowed = {unit.text for unit in node.corpus.units[2:] if unit.kind == "unreviewed"}
    assert all(record["content"] in allowed for record in output["records"])
    serialized = json.dumps(output)
    assert "private_protected_canary" not in serialized and "private_neighbor_canary" not in serialized
    assert all(unit.canary not in serialized for unit in node.corpus.units if unit.canary)
    with sqlite3.connect(index_path(node.index.root,node.search_raw["binding"]["grant_id"])) as conn:
        assert conn.execute("SELECT member_count FROM meta").fetchone()[0] == 13
        bags = " ".join(row[0] for row in conn.execute("SELECT terms_json FROM members"))
        assert "mara" not in bags and "private" not in bags


def test_search_above_ten_is_refused(node):
    output, refused = search(node,k=11)
    assert output is None and refused is not None


@pytest.mark.parametrize("change", ["alias", "contact", "parent", "participant", "mention"])
def test_new_protection_dependencies_invalidate_ranking_before_search(node, change):
    unit = node.corpus.units[2]
    with sqlite3.connect(node.corpus.path) as conn:
        if change == "alias":
            conn.execute("UPDATE entities SET aliases_json=? WHERE entity_id='protected-entity'", (json.dumps([unit.text.split()[0]]),))
        elif change == "contact":
            conn.execute("INSERT INTO contact_identifiers(contact_id,dataset_id,source_id,identifier,identifier_type) VALUES('protected-contact',?,'address_book','self','service')", (mc.DATASET,))
        elif change == "parent":
            conn.execute("ALTER TABLE conversations ADD COLUMN title TEXT")
            conn.execute("UPDATE conversations SET title='Mara Example' WHERE conversation_id='thread-2'")
        elif change == "participant":
            conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) VALUES('thread-2',?,?,'protected-contact')", (mc.DATASET,mc.SOURCE))
        else:
            conn.execute("INSERT INTO entity_mentions(mention_id,entity_id,record_id,source_id,canonical_table) VALUES('new','protected-entity',?,?,'conversation_messages')", (unit.message_id,mc.SOURCE))
    output, refused = search(node)
    assert output is None and refused is not None


def test_mid_build_alias_change_is_not_published(node,monkeypatch):
    original = node.index._members
    calls = []
    def build(conn,*args):
        result = original(conn,*args)
        if not calls:
            with sqlite3.connect(node.corpus.path) as writer:
                writer.execute("UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'")
        calls.append(1)
        return result
    monkeypatch.setattr(node.index,"_members",build)
    # Use WAL on this disposable synthetic DB for a writer during its snapshot.
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    node.rebuild()
    assert len(calls) >= 2
    output, refused = search(node,"roadmap")
    assert refused is None and output["records"] == []


@pytest.mark.parametrize("change", ["alias", "participant"])
def test_dependency_change_after_ranking_refuses_the_entire_result(node,monkeypatch,change):
    from topos.permissions_v2 import search_release
    original = search_release.rank
    def rank(*args,**kwargs):
        order = original(*args,**kwargs)
        with sqlite3.connect(node.corpus.path) as conn:
            if change == "alias":
                conn.execute("UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'")
            else:
                conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) VALUES('thread-2',?,?,'protected-contact')", (mc.DATASET,mc.SOURCE))
        return order
    monkeypatch.setattr(search_release,"rank",rank)
    output, refused = search(node)
    assert output is None and refused is not None


def test_canary_detects_a_disabled_name_scan_instead_of_vacuous_empty_success(node,monkeypatch):
    from topos.permissions_v2.entity_boundary import EntityBoundary
    output, refused = search(node,"private_protected_canary")
    assert refused is None and output["records"] == []
    monkeypatch.setattr(EntityBoundary,"_hits",lambda *_: False)
    node.rebuild()
    output, refused = search(node,"private_protected_canary")
    assert refused is None and any("private_protected_canary" in record["content"] for record in output["records"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "alias", "participant"])
async def test_dependency_change_after_checkpoint_is_checked_before_send(node,monkeypatch,change):
    from tests.permissions_v2.test_message_search_refusals import PAYLOAD, Socket, relay_message, signed
    from topos.permissions_v2 import search_transport
    message = relay_message(node,signed(node),PAYLOAD,monkeypatch)
    original = node.search.dispatch
    checkpointed = []
    def dispatch(**kwargs):
        result = original(**kwargs)
        assert result[1]["records"]
        checkpointed.append(True)
        with sqlite3.connect(node.corpus.path) as conn:
            if change == "alias":
                conn.execute("UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'")
            elif change == "participant":
                conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) VALUES('thread-2',?,?,'protected-contact')", (mc.DATASET,mc.SOURCE))
        return result
    monkeypatch.setattr(node.search,"dispatch",dispatch)
    socket = Socket()
    await search_transport.dispatch_message_search(socket,message)
    assert checkpointed == [True]
    assert [json.loads(value)["status"] for value in socket.sent] == (["ok"] if change == "none" else ["error"])


def link_two_safe_leaves(node):
    with sqlite3.connect(node.corpus.path) as conn:
        first, second = node.corpus.units[2:4]
        refs = [json.loads(conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id=?", (unit.fact_id,)).fetchone()[0])[0]
                for unit in (first,second)]
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (json.dumps(refs),first.fact_id))
    node.rebuild()
    assert search(node)[1] is None


def protect_second_support_thread(node):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) VALUES('thread-3',?,?,'protected-contact')", (mc.DATASET,mc.SOURCE))


def test_unranked_support_contributor_invalidates_ranking(node):
    link_two_safe_leaves(node)
    protect_second_support_thread(node)
    # The first leaf's own thread is unchanged, but its witness fact now has a
    # protected contributor. Its old terms must not shape even a different query.
    assert search(node) == (None,"permission_denied")


def test_context_change_during_build_retries_before_publishing(node,monkeypatch):
    link_two_safe_leaves(node)
    original = node.index._members
    calls = []
    def build(conn,*args):
        result = original(conn,*args)
        if not calls:
            protect_second_support_thread(node)
        calls.append(1)
        return result
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    monkeypatch.setattr(node.index,"_members",build)
    node.rebuild()
    assert len(calls) >= 2
    output, refused = search(node)
    assert refused is None and output["records"]
    assert all(record["content"] not in {unit.text for unit in node.corpus.units[2:4]} for record in output["records"])
