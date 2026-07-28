"""Regression: unstamped ai_chat rows with canonical sender_type='human' must
reach the ai_chat extractor (PLAN_TRUTHFULNESS_PLUGIN.md §3.4).

The batch table-inference in extract_facts_from_batch accepted only
('user','assistant'), while live ChatGPT ingestion writes the owner as 'human'
(chatgpt_parser) — so an unstamped owner row minted no self-facts through the
rules floor. The fix mirrors llm_extract's inference but fails CLOSED for
messenger-shaped rows: 'human' routes to ai_chat only when the conversation
marker KEYS (is_from_self / sender_id) are absent entirely.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.facts.extract import extract_facts_from_batch
from topos.features.facts.store import FactStore


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    connection = sqlite3.connect(str(tmp_path / "facts.sqlite"))
    apply_all_migrations(connection)
    yield connection
    connection.close()


def _owner_facts(connection: sqlite3.Connection):
    rows = connection.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact'"
    ).fetchall()
    return [r[0] for r in rows]


def _lives_in_facts(connection):
    return [p for p in _owner_facts(connection) if "lives_in" in p]


def test_unstamped_human_ai_chat_row_extracts_owner_fact(conn):
    row = {
        "message_id": "regress-1",
        "sender_type": "human",  # canonical owner value for ai_chat_messages
        "content": "I live in Lisbon these days",
        "source_id": "chatgpt_ingestion",
        "event_at": "2026-07-01T10:00:00Z",
        # no _table / canonical_table stamp, no conversation marker keys
    }
    written = extract_facts_from_batch(conn, [row])
    assert written >= 1
    assert _lives_in_facts(conn), "owner self-fact missing for 'human' ai_chat row"


def test_messenger_shaped_human_row_fails_closed(conn):
    # Messenger writes sender_type='human' for OTHER people; the conversation
    # marker keys are present (even when null-ish) → must NOT route to the
    # ai_chat extractor and must extract nothing as the owner.
    row = {
        "message_id": "regress-2",
        "sender_type": "human",
        "is_from_self": 0,
        "sender_id": "contact-77",
        "content": "I live in Marseille these days",
        "source_id": "demo_messenger_file",
        "event_at": "2026-07-01T10:00:00Z",
    }
    extract_facts_from_batch(conn, [row])
    assert not _lives_in_facts(conn), "another person's claim minted an owner fact"


def test_assistant_row_still_extracts_nothing(conn):
    row = {
        "message_id": "regress-3",
        "sender_type": "assistant",
        "content": "I live in a data center",
        "source_id": "chatgpt_ingestion",
        "event_at": "2026-07-01T10:00:00Z",
    }
    extract_facts_from_batch(conn, [row])
    assert not _lives_in_facts(conn)
