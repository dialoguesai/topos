"""P1.3 (PLAN_PROVENANCE_SPLIT): dimension-brief inputs carry sender identity.

brief_canonical_loader must SELECT the sender truth columns (Appendix A:
ai_chat sender_type; conversation is_from_self/sender_id) so brief_input_text
can label non-authored speech — briefs consume other people's words only as
labeled context ("[contact] …" / "[assistant] …"), never as the owner's.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.jobs.canonical.brief_fallback import brief_input_text
from topos.features.signal.brief_canonical_loader import (
    load_canonical_messages_for_dimension,
)
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "briefs.db"))
    apply_all_migrations(c)
    ensure_all_tables(c)  # conversations + conversation_messages
    CanonicalTablesManager(c)  # ai_chat tables
    _seed(c)
    yield c
    c.close()


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES ('a1', 'conv1', 'human', 'chatgpt_file_ingestion',
                   'I want to learn mandolin', '2026-06-01T10:00:00+00:00', 0)"""
    )
    conn.execute(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, source_id, content, event_at, sequence)
           VALUES ('a2', 'conv1', 'assistant', 'chatgpt_file_ingestion',
                   'You clearly prefer linen workwear', '2026-06-01T10:00:05+00:00', 1)"""
    )
    conn.execute(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, dataset_id, sender_type, sender_id,
            content, event_at, source_id, is_from_self)
           VALUES ('m1', 'c1', 'd1', 'human', '+31612345678',
                   'kombucha is the only drink worth brewing',
                   '2026-06-01T11:00:00+00:00', 'demo_messenger_file', 0)"""
    )
    conn.execute(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, dataset_id, sender_type, sender_id,
            content, event_at, source_id, is_from_self)
           VALUES ('m2', 'c1', 'd1', 'human', 'self',
                   'I started fiddle tunes on the mandolin',
                   '2026-06-01T11:05:00+00:00', 'demo_messenger_file', 1)"""
    )
    conn.commit()


def _by_id(records):
    return {str(r["message_id"]): r for r in records}


class TestLoaderSenderFields:
    def test_message_rows_carry_sender_truth_columns(self, conn):
        # 'memory' maps both message families.
        records = _by_id(load_canonical_messages_for_dimension(conn, "memory"))
        assert {"a1", "a2", "m1", "m2"} <= set(records)

        assert records["a1"]["sender_type"] == "human"
        assert records["a2"]["sender_type"] == "assistant"
        assert records["m1"]["is_from_self"] == 0
        assert records["m1"]["sender_id"] == "+31612345678"
        assert records["m2"]["sender_id"] == "self"
        assert records["m2"]["is_from_self"] == 1

    def test_canonical_table_still_stamped(self, conn):
        records = _by_id(load_canonical_messages_for_dimension(conn, "memory"))
        assert records["a1"]["canonical_table"] == "ai_chat_messages"
        assert records["m1"]["canonical_table"] == "conversation_messages"

    def test_non_message_tables_unaffected(self, conn):
        conn.execute(
            """INSERT INTO journal_entries
               (entry_id, source_id, content, entry_at, category)
               VALUES ('j1', 'demo_journal_file', 'Long run this morning',
                       '2026-06-01T07:00:00+00:00', 'exercise')"""
        )
        conn.commit()
        records = _by_id(load_canonical_messages_for_dimension(conn, "wellbeing"))
        assert "j1" in records
        assert "sender_type" not in records["j1"]


class TestBriefInputAttribution:
    def test_owner_rows_stay_unprefixed(self, conn):
        records = _by_id(load_canonical_messages_for_dimension(conn, "memory"))
        assert brief_input_text(records["a1"]).startswith("I want to learn mandolin")
        assert brief_input_text(records["m2"]).startswith("I started fiddle tunes")

    def test_assistant_rows_labeled(self, conn):
        records = _by_id(load_canonical_messages_for_dimension(conn, "memory"))
        assert brief_input_text(records["a2"]).startswith("[assistant] ")

    def test_contact_rows_labeled(self, conn):
        records = _by_id(load_canonical_messages_for_dimension(conn, "memory"))
        digest = brief_input_text(records["m1"])
        assert digest.startswith("[contact] ")
        assert "kombucha" in digest

    def test_system_rows_labeled(self):
        digest = brief_input_text(
            {
                "message_id": "a3",
                "canonical_table": "ai_chat_messages",
                "sender_type": "system",
                "content": "You are a helpful assistant",
            }
        )
        assert digest.startswith("[system] ")

    def test_senderless_records_never_prefixed(self):
        # journal/profile/activity records carry no sender keys: unchanged
        # (the "[exercise]" category band is pre-existing, not a speaker label).
        digest = brief_input_text(
            {"entry_id": "j9", "category": "exercise", "content": "Morning swim"}
        )
        for label in ("[contact] ", "[assistant] ", "[system] "):
            assert not digest.startswith(label)
        assert digest == "[exercise] Morning swim"
