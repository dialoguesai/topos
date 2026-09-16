"""Neither fact extractor may turn someone else's words into the owner's statement.

Two paths did, and both are closed here.

- An iMessage reaction ("tapback") is stored as a row authored by whoever
  reacted, but its text quotes the message it reacts to: 'Loved “I work at X”'.
  When the owner reacts to a correspondent's message, the rules pattern read the
  correspondent's words as the owner's. Both extractors now skip a row that
  carries reaction metadata.
- The LLM pass inferred a row's table from ``sender_type`` alone, and ``human``
  is the owner's value in AI chat. A messenger row without a ``_table`` stamp,
  whose ``human`` sender is a correspondent, was read as the owner's AI-chat
  turn. It now uses the rule extractor's condition: ``human`` means AI chat only
  when the conversation-table keys are absent.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.facts.extract import extract_facts_from_batch, extract_message_facts
from topos.features.facts.llm_extract import _infer_table, extract_owner_facts_llm
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "facts.db"))
    apply_all_migrations(db)
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
               " VALUES ('ent-owner', 'person', 'Owner', 'owner', 1)")
    db.commit()
    yield db
    db.close()


def owner_row(content, metadata=None, *, stamped=True):
    row = {"message_id": "imessage:9", "conversation_id": "c1", "sender_type": "human", "sender_id": "self",
           "is_from_self": 1, "content": content, "event_at": "2026-06-01T10:00:00+00:00", "source_id": "imessage"}
    if metadata is not None:
        row["metadata_json"] = json.dumps(metadata)
    if stamped:
        row["_table"] = "conversation_messages"
    return row


def owner_facts(conn):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL")]


REACTIONS = [
    {"associated_message_guid": "p:0/SYNTHETIC-GUID", "associated_message_type": 2000},
    {"associated_message_type": 2001},
    {"associated_message_guid": "p:0/SYNTHETIC-GUID"},
]


@pytest.mark.parametrize("metadata", REACTIONS)
def test_an_owner_reaction_quoting_a_correspondent_yields_no_rule_fact(conn, metadata):
    row = owner_row("Loved “I work at Harbor Synthetic”", metadata)
    assert extract_message_facts(row, conn, table="conversation_messages") == []
    assert extract_facts_from_batch(conn, [row]) == 0
    assert owner_facts(conn) == []


@pytest.mark.parametrize("carrier", ["metadata_json_text", "metadata_dict", "_metadata"])
def test_reaction_metadata_is_found_in_every_shape_a_row_carries_it(conn, carrier):
    metadata = REACTIONS[0]
    row = owner_row("Loved “I work at Harbor Synthetic”")
    row.update({"metadata_json_text": {"metadata_json": json.dumps(metadata)},
                "metadata_dict": {"metadata_json": metadata}, "_metadata": {"_metadata": metadata}}[carrier])
    assert extract_message_facts(row, conn, table="conversation_messages") == []


@pytest.mark.parametrize("metadata", [None, {}, {"associated_message_type": 0}, {"associated_message_guid": ""},
                                      {"thread_originator_guid": "SYNTHETIC"}])
def test_an_ordinary_owner_message_still_yields_its_fact(conn, metadata):
    row = owner_row("I work at Ferrograph Instruments", metadata)
    assert extract_facts_from_batch(conn, [row]) == 1


def stub(prompt, row):
    return [{"predicate": "works_at", "object": "Harbor Synthetic"}]


def test_the_llm_pass_skips_an_owner_reaction(conn, monkeypatch):
    row = owner_row("Loved “I work at Harbor Synthetic, since last spring”", REACTIONS[0])
    assert extract_owner_facts_llm(conn, [row], extractor=stub) == 0
    assert owner_facts(conn) == []


def test_an_unstamped_messenger_row_from_a_correspondent_is_not_the_owners_ai_chat_turn(conn):
    row = {"message_id": "m-them", "conversation_id": "c1", "sender_type": "human", "sender_id": "+15555550123",
           "is_from_self": 0, "content": "I work at Harbor Synthetic and have for years", "event_at": "2026-06-01T10:00:00+00:00"}
    assert _infer_table(row) == "conversation_messages"
    extract_owner_facts_llm(conn, [row], extractor=stub)
    assert [f for f in owner_facts(conn) if f.get("asserted_by") == "owner"] == []


@pytest.mark.parametrize("row,table", [
    ({"sender_type": "human", "content": "x"}, "ai_chat_messages"),
    ({"sender_type": "user", "content": "x"}, "ai_chat_messages"),
    ({"sender_type": "assistant", "content": "x"}, "ai_chat_messages"),
    ({"sender_type": "human", "sender_id": "self", "content": "x"}, "conversation_messages"),
    ({"sender_type": "human", "is_from_self": 1, "content": "x"}, "conversation_messages"),
    # Ambiguous: the conversation key is present but empty. The rule extractor
    # infers no table and skips the row; the LLM pass must not read it as AI chat.
    ({"sender_type": "system", "sender_id": None, "content": "x"}, ""),
])
def test_llm_table_inference_matches_the_rule_extractor(row, table):
    assert _infer_table(row) == table


def test_the_unstamped_owner_messenger_row_still_yields_an_owner_fact(conn):
    row = owner_row("I work at Ferrograph Instruments and love it", stamped=False)
    written = extract_owner_facts_llm(conn, [row], extractor=lambda p, r: [{"predicate": "works_at", "object": "Ferrograph Instruments"}])
    assert written == 1 and owner_facts(conn)[0]["asserted_by"] == "owner"
