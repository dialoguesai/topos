"""Review R1 (node), R-B1: an entry the carry step makes withholds at the share boundary while it waits.

protects: the upgrade step no longer runs the clean-up of derived text, so each entry it makes stays `pending` until
the owner starts that clean-up. That is only safe if a waiting entry already keeps the person out of everything a
share releases. These tests ask the real doors, with the entry made by the real step and never cleaned up:
  - a recipient's search over a share whose one message sits in a thread with the excluded contact gets nothing
    (refused while the index is out of date, empty once it is rebuilt);
  - a recipient's question to that share finishes with no answer, and the model is never handed the message;
  - the same share answered both before the step (the control: the message is released and reaches the model).
Adding any Off-limits entry also puts every earlier assessment out of date (`message_evidence.snapshot_message`,
`protected_scope`), and that alone empties a share for a while. So each test assesses the message again after the
step, as the node's catch-up pass would: what is withheld after that is withheld by the boundary, not by a stale
assessment. Every person and id here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_automatic_message_review import answer
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
                                                           ensure_conversation_participants_table,
                                                           ensure_conversations_table)

EXCLUDE = json.dumps({"name_visibility": "normal", "row_visibility": "exclude_from_grants"})
MESSAGE = "I am working on Synthetic message at work."


def _excluded_contact_in_the_thread(conn: sqlite3.Connection) -> None:
    """The tables a node has, and one contact the owner excluded in the older model, in the message's own thread."""
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    conn.execute(TOMBSTONES_SQL)
    conversation, source, dataset = conn.execute(
        "SELECT conversation_id, source_id, dataset_id FROM conversation_messages").fetchone()
    conn.execute("INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)",
                 (conversation, dataset, source))
    conn.execute("INSERT INTO contacts(contact_id,dataset_id,source_id,display_name,sharing_policy_json) "
                 "VALUES('contact-excluded',?,'address_book','Quorra Vellaby',?)", (dataset, EXCLUDE))
    conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id) "
                 "VALUES(?,?,?,'contact-excluded')", (conversation, dataset, source))
    conn.commit()


def _recording(node, text="The owner is working on a synthetic task [1]."):
    prompts = []

    async def generate(prompt, *, deadline):
        prompts.append(str(prompt))
        return text

    service = _service(node, text)
    service.generate = generate
    return service, prompts


def _assess_again(node, identity) -> str:
    """The catch-up pass's work for the one message: a fresh assessment against the Off-limits list as it is now,
    then the index rebuilt. Says whether the assessment was made or refused; the caller asserts what gets out."""
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        try:
            prepared = prepare(resolver, reviews, identity)
            publish(resolver, reviews, prepared, answer(prepared), now=node.now[0])
            made = "assessed"
        except PolicyError as exc:
            made = "refused:" + exc.code
        node.index.rebuild("grant-search", now=node.now[0])
    return made


def _states(conn):
    return [row[0] for row in conn.execute("SELECT rebuild_state FROM entity_blackholes")]


def test_before_the_step_a_search_share_releases_the_message(legacy, tmp_path, monkeypatch):  # noqa: F811
    """The control for search: with the contact in the thread and no entry, the message is returned."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch)
    _excluded_contact_in_the_thread(legacy[1])
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and [record["content"] for record in output["records"]] == [MESSAGE]


def test_before_the_step_an_answer_share_hands_the_message_to_the_model(legacy, tmp_path, monkeypatch):  # noqa: F811
    """The control for answers (a share that answers and does not list): the message reaches the model."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    _excluded_contact_in_the_thread(legacy[1])
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"
    service, prompts = _recording(node)
    try:
        assert _finished(node, service, _ask(node, service, "Synthetic message"))["outcome"] == "answered"
        assert any("Synthetic message at work" in prompt for prompt in prompts)
    finally:
        service.close()


def test_a_waiting_entry_keeps_the_person_out_of_search(legacy, tmp_path, monkeypatch):  # noqa: F811
    """Rule: the share boundary reads every Off-limits entry, whatever its clean-up state. Make it read only
    entries whose clean-up is complete and the message in the excluded contact's thread is released again."""
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    conn = legacy[1]
    _excluded_contact_in_the_thread(conn)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    assert carry_contact_excludes(conn)["carried"] == 1
    conn.commit()
    assert _states(conn) == ["pending"]                              # made by the step, never cleaned up
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is not None and output is None                    # the index of before the entry is not served
    _assess_again(node, identity)
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is not None or output["records"] == []            # and after a fresh assessment, still nothing
    assert MESSAGE not in json.dumps(output or {})
    assert _states(conn) == ["pending"]


def test_a_waiting_entry_keeps_the_person_out_of_answers(legacy, tmp_path, monkeypatch):  # noqa: F811
    node, identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    conn = legacy[1]
    _excluded_contact_in_the_thread(conn)
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    carry_contact_excludes(conn)
    conn.commit()
    service, prompts = _recording(node)
    try:
        first = _finished(node, service, _ask(node, service, "Synthetic message"))
        _assess_again(node, identity)
        second = _finished(node, service, _ask(node, service, "What is the owner working on?"))
    finally:
        service.close()
    assert first == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert second == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert not any("Synthetic message at work" in prompt for prompt in prompts)   # the model never saw the message
    assert _states(conn) == ["pending"]


def test_the_clean_up_the_owner_starts_changes_nothing_a_share_releases(legacy, tmp_path, monkeypatch):  # noqa: F811
    """Withholding does not wait for the clean-up and does not end with it."""
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    conn = legacy[1]
    _excluded_contact_in_the_thread(conn)
    carry_contact_excludes(conn)
    conn.commit()
    name = conn.execute("SELECT normalized_name FROM entity_blackholes").fetchone()[0]
    rebuild_for_blackhole(conn, name)
    conn.commit()
    assert _states(conn) == ["complete"]
    _assess_again(node, identity)
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is not None or output["records"] == []
