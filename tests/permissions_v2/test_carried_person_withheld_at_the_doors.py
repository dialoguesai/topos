"""Third fix round, ruling P.1: a carried person is withheld at once at the real share doors, while the entry waits.

The reviewer's own door probe (`test_s5n_r2_withheld_at_once.py`, re-check point 2), brought into the tree with its
thirteen cases unchanged, and the two ordinary-name cases the re-check said the tests lack: "al" as a username and "Ed"
as a learned alias of the linked entity. For each case: released before the upgrade step (the control), refused the
moment after it, and empty after a fresh assessment, with the entry still carried and waiting; the answer door says
`no_answer` and the model is never handed the message.

protects: an entry the upgrade carried is invisible to every reader that serves the owner himself
(`blackhole.WAITING_COLUMN`). The share doors must not be among those readers: the boundary every share builds reads
every entry. Let the boundary skip what waits and every case here releases the message again.
The real search door and the real answer door, on the knowledge-search harness, with the entry made by the real
upgrade step and never cleaned up. Every person and address is invented; the short ordinary names are the ones this
round is about.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished
from tests.permissions_v2.test_carried_entry_withholds_while_pending import _assess_again, _recording, _states
from tests.permissions_v2.test_imessage_reconciliation import sample, snapshot
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import add_fact, add_goal_graph, node_for
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
                                                           ensure_conversation_participants_table,
                                                           ensure_conversations_table)

pytestmark = pytest.mark.public

EXCLUDE = json.dumps({"name_visibility": "normal", "row_visibility": "exclude_from_grants"})
PLAIN = "I am working on Synthetic message at work."
ADDRESS = "q.vellaby@fernmail.example"

CASES = {
    # label: (message text, fact value or None, contact display, handle, in the thread?, what the search asks)
    # The last two are this round's: (…, usernames, aliases of a linked entity) follow in ORDINARY below.
    "named in the message, not in the thread": (
        "I am working on the launch with Quorra Vellaby at work.", "the launch with Quorra Vellaby",
        "Quorra Vellaby", None, False, "launch"),
    "first name alone in the message, not in the thread": (
        "I am working on the launch with Quorra at work.", "the launch with Quorra", "Quorra Vellaby", None, False,
        "launch"),
    "handle in the message, contact carried by handle only": (
        f"I am working on the launch, write to {ADDRESS} at work.", "the launch", None, ADDRESS, False, "launch"),
    "contact carried by handle only, in the thread": (PLAIN, "Synthetic message", None, ADDRESS, True,
                                                      "Synthetic message"),
    "contact carried by its id only, in the thread": (PLAIN, "Synthetic message", None, None, True,
                                                      "Synthetic message"),
    "named contact in the thread": (PLAIN, "Synthetic message", "Quorra Vellaby", None, True, "Synthetic message"),
    "al as a username, written in the message": (
        "I am working on the launch with Al at work.", "the launch with Al", "Quorra Vellaby", None, False, "launch"),
    "Ed as a learned alias, written in the message": (
        "I am working on the launch with Ed at work.", "the launch with Ed", "Quorra Vellaby", None, False, "launch"),
}
#: What the two ordinary-name cases add to the contact: its usernames, or the aliases of an entity linked to it.
ORDINARY = {"al as a username, written in the message": {"usernames": ["al"]},
            "Ed as a learned alias, written in the message": {"aliases": ["Ed"]}}


@pytest.fixture
def legacy(ingest_fixture, request):  # noqa: F811 -- the tree's fixture, with the message text of the case
    from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
    from topos.permissions_v2.protection_clock import resync_identity_coverage
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up

    service, conn, path = ingest_fixture
    content = request.param
    path.chmod(0o600)
    path.write_bytes(snapshot(count=1, mutate=lambda db: db.execute("UPDATE message SET text=?", (content,))))
    path.chmod(0o400)
    row, _ = sample()
    row["content"] = content
    row["dataset_id"] = "native-dataset"
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                 [row.get(c) for c in columns])
    apply_wiki_entities_v1_up(conn)
    add_entity(conn, "owner-entity")
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
    conn.commit()
    clock = conn.execute("SELECT clock_id,generation FROM permissions_v2_protection_state").fetchone()
    resync_identity_coverage(service.resolver.path, owner_id="owner-1", expected_clock_id=clock[0],
                             expected_generation=clock[1])
    do_attest(conn, "owner-entity")
    conn.commit()
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id="canary", reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id="native-dataset",
                                    snapshot_sha256=desc["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
                                    reader_contract=ATTRIBUTED_CONTRACT)
    return service, conn, path, enrollment["enrollment_id"]


def excluded_contact(conn, *, display, handle, in_thread, usernames=None, aliases=None):
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    conn.execute(TOMBSTONES_SQL)
    conversation, source, dataset = conn.execute(
        "SELECT conversation_id, source_id, dataset_id FROM conversation_messages").fetchone()
    conn.execute("INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)",
                 (conversation, dataset, source))
    conn.execute("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name,sharing_policy_json) "
                 "VALUES('native-dataset:contact:7471fce8530d7bd0',?,'address_book',?,?)", (dataset, display, EXCLUDE))
    if handle:
        conn.execute("INSERT INTO contact_identifiers(dataset_id,source_id,identifier,identifier_type,contact_id) "
                     "VALUES(?,'address_book',?,'email','native-dataset:contact:7471fce8530d7bd0')", (dataset, handle))
    if in_thread:
        conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                     "VALUES(?,?,?,'native-dataset:contact:7471fce8530d7bd0')", (conversation, dataset, source))
    if usernames:
        conn.execute("UPDATE contacts SET known_usernames_json=? WHERE contact_id='native-dataset:contact:7471fce8530d7bd0'",
                     (json.dumps(usernames),))
    if aliases:
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                     "identifiers_json, contact_id, mention_count, metadata_json) VALUES ('linked-entity','person',?,?,?,"
                     "'[]','native-dataset:contact:7471fce8530d7bd0',3,'{}')",
                     (display, display.lower(), json.dumps(aliases)))
    conn.commit()


def still_waiting(conn):
    """The entry the step made is carried and waiting: nobody has acted on it, and no clean-up ran."""
    from topos.features.lifecycle.blackhole import BlackholeStore

    return [(entry["carried_waiting"], entry["rebuild_state"]) for entry in BlackholeStore(conn).list()] == [(True, "pending")]


def released(node, asks):
    output, refused = node.search_request(asks, k=10)
    if refused is not None:
        return "refused", []
    return "answered", [(record["kind"], record.get("content")) for record in output["records"]]


@pytest.mark.parametrize("legacy,label", [(CASES[label][0], label) for label in CASES], indirect=["legacy"],
                         ids=list(CASES))
def test_search_door(legacy, label, tmp_path, monkeypatch):
    text, fact, display, handle, in_thread, asks = CASES[label]
    conn = legacy[1]
    add_fact(legacy, value=fact)
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    excluded_contact(conn, display=display, handle=handle, in_thread=in_thread, **ORDINARY.get(label, {}))
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    before = released(node, asks)
    report = carry_contact_excludes(conn)
    conn.commit()
    at_once = released(node, asks)                      # the moment after the step: the old index must not be served
    _assess_again(node, identity)                       # what the node's catch-up pass does
    after = released(node, asks)
    assert before[0] == "answered" and any(kind == "message" for kind, _ in before[1]), "control: released before"
    assert report["carried"] == 1 and _states(conn) == ["pending"] and still_waiting(conn)
    for state, records in (at_once, after):
        assert text not in json.dumps(records) and not records, (label, state, records)


@pytest.mark.parametrize("legacy,label", [(CASES[label][0], label) for label in CASES], indirect=["legacy"],
                         ids=list(CASES))
def test_answer_door(legacy, label, tmp_path, monkeypatch):
    text, _fact, display, handle, in_thread, asks = CASES[label]
    conn = legacy[1]
    node, identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    excluded_contact(conn, display=display, handle=handle, in_thread=in_thread, **ORDINARY.get(label, {}))
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service, prompts = _recording(node)
    try:
        before = _finished(node, service, _ask(node, service, asks))["outcome"]
        seen_before = any(text in prompt for prompt in prompts)
        prompts.clear()
        carry_contact_excludes(conn)
        conn.commit()
        first = _finished(node, service, _ask(node, service, asks))["outcome"]
        _assess_again(node, identity)
        second = _finished(node, service, _ask(node, service, "What is the owner working on?"))["outcome"]
        # the SAME question as before the step: for a message that is released, this one is answered after a fresh
        # assessment and the other is not (the reviewer's own correction), so only this one tests the boundary
        third = _finished(node, service, _ask(node, service, asks))["outcome"]
        seen_after = any(text in prompt for prompt in prompts)
    finally:
        service.close()
    assert before == "answered" and seen_before, "control"
    assert first == "no_answer" and second == "no_answer" and third == "no_answer" and not seen_after
    assert still_waiting(conn)


GOAL = "My goal is to finish the compiler with Quorra Vellaby at work by Friday."


@pytest.mark.parametrize("legacy", [GOAL], indirect=True)
def test_goal_and_relationship_that_name_the_person(legacy, tmp_path, monkeypatch):
    conn = legacy[1]
    add_goal_graph(legacy)
    named = "finish the compiler with Quorra Vellaby at work by Friday"
    conn.execute("UPDATE user_goals SET goal_text=?", (named,))
    conn.execute("UPDATE entities SET canonical_name=?, normalized_name=? WHERE entity_id='goal-node'", (named, named))
    conn.commit()
    node, identity = node_for(legacy, tmp_path, monkeypatch, labels={"domains": ["work", "plans"]})
    excluded_contact(conn, display="Quorra Vellaby", handle=None, in_thread=False)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    before = released(node, "compiler Friday")
    carry_contact_excludes(conn)
    conn.commit()
    at_once = released(node, "compiler Friday")
    _assess_again(node, identity)
    after = released(node, "compiler Friday")
    assert {kind for kind, _ in before[1]} >= {"goal"}, "control: the goal was released before"
    assert not at_once[1] and not after[1] and still_waiting(conn)
    assert conn.execute("SELECT COUNT(*) FROM user_goals").fetchone()[0] == 1          # and nothing was deleted

