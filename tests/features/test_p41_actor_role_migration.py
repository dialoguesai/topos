"""P4.1 actor_role migration tests (PLAN_PROVENANCE_SPLIT P4).

The migration adds ``actor_role TEXT NULL`` to conversation_messages and
ai_chat_messages (+ partial indexes) and backfills once via
features.provenance.roles.record_role — the single source of truth for role.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.db.migrations import apply_all_migrations, ensure_migrations_applied
from topos.storage.db.migrations.actor_role_v1 import (
    MIGRATION_ID,
    apply_actor_role_v1_up,
)

pytestmark = pytest.mark.public


def _create_message_tables(c: sqlite3.Connection) -> None:
    """Minimal mirrors of the canonical DDL (tables.py creators)."""
    c.execute(
        """CREATE TABLE conversation_messages (
               message_id TEXT NOT NULL PRIMARY KEY,
               conversation_id TEXT NOT NULL,
               dataset_id TEXT NOT NULL DEFAULT 'ds',
               sender_type TEXT,
               sender_id TEXT,
               content TEXT,
               event_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
               source_id TEXT NOT NULL DEFAULT 'demo_messenger_file',
               metadata_json TEXT,
               is_from_self INTEGER DEFAULT 0,
               event_type TEXT,
               message_type TEXT
           )"""
    )
    c.execute(
        """CREATE TABLE ai_chat_messages (
               message_id TEXT PRIMARY KEY,
               conversation_id TEXT NOT NULL,
               sender_type TEXT NOT NULL,
               sender_id TEXT,
               event_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00Z',
               content TEXT NOT NULL DEFAULT '',
               sequence INTEGER NOT NULL DEFAULT 0,
               source_id TEXT NOT NULL DEFAULT 'chatgpt_file_ingestion'
           )"""
    )


def _seed_rows(c: sqlite3.Connection) -> None:
    c.executemany(
        """INSERT INTO conversation_messages
           (message_id, conversation_id, sender_type, sender_id, is_from_self, content)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            ("cm-owner", "conv-1", "human", "self", 1, "mine"),
            ("cm-owner-sid", "conv-1", "contact", "self", 0, "also mine"),
            ("cm-other", "conv-1", "human", "+15550001111", 0, "someone else"),
        ],
    )
    c.executemany(
        """INSERT INTO ai_chat_messages
           (message_id, conversation_id, sender_type, content)
           VALUES (?, ?, ?, ?)""",
        [
            ("ai-human", "chat-1", "human", "owner prompt"),
            ("ai-user", "chat-1", "user", "legacy owner prompt"),
            ("ai-assistant", "chat-1", "assistant", "model reply"),
            ("ai-system", "chat-1", "system", "scaffolding"),
        ],
    )


def _columns(c: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _roles(c: sqlite3.Connection, table: str) -> dict:
    return dict(c.execute(f"SELECT message_id, actor_role FROM {table}").fetchall())


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "p41.db"))
    _create_message_tables(c)
    _seed_rows(c)
    yield c
    c.close()


class TestActorRoleMigration:
    def test_columns_indexes_and_backfill(self, conn) -> None:
        apply_actor_role_v1_up(conn)
        assert "actor_role" in _columns(conn, "conversation_messages")
        assert "actor_role" in _columns(conn, "ai_chat_messages")
        idx = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_conversation_messages_actor_role" in idx
        assert "idx_ai_chat_messages_actor_role" in idx

        # Backfill agrees with record_role: owner rows authored, others
        # observed; ai_chat assistant addressed, system ambient.
        conv = _roles(conn, "conversation_messages")
        assert conv == {
            "cm-owner": "authored",
            "cm-owner-sid": "authored",
            "cm-other": "observed",
        }
        chat = _roles(conn, "ai_chat_messages")
        assert chat == {
            "ai-human": "authored",
            "ai-user": "authored",
            "ai-assistant": "addressed",
            "ai-system": "ambient",
        }

    def test_backfill_matches_record_role_exactly(self, conn) -> None:
        from topos.features.provenance.roles import record_role

        apply_actor_role_v1_up(conn)
        for table in ("conversation_messages", "ai_chat_messages"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            names = [d[0] for d in conn.execute(f"SELECT * FROM {table}").description]
            for row in rows:
                record = dict(zip(names, row))
                expected = record_role(
                    {k: v for k, v in record.items() if k != "actor_role"},
                    table=table,
                )
                assert record["actor_role"] == expected, (table, record["message_id"])

    def test_idempotent_and_ledger_guarded(self, conn) -> None:
        apply_actor_role_v1_up(conn)
        # Rows written AFTER the one-time backfill stay NULL (producers own
        # ongoing stamping); a re-run must not re-backfill or error.
        conn.execute(
            """INSERT INTO conversation_messages
               (message_id, conversation_id, sender_type, sender_id, is_from_self)
               VALUES ('cm-late', 'conv-1', 'human', 'self', 1)"""
        )
        apply_actor_role_v1_up(conn)
        assert _roles(conn, "conversation_messages")["cm-late"] is None
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (MIGRATION_ID,),
        ).fetchone()
        assert row is not None

    def test_missing_tables_are_tolerated(self, tmp_path) -> None:
        c = sqlite3.connect(str(tmp_path / "fresh.db"))
        try:
            apply_actor_role_v1_up(c)  # neither table exists: no-op, no error
            apply_actor_role_v1_up(c)
        finally:
            c.close()

    def test_registered_in_both_runners(self, tmp_path) -> None:
        for runner in (apply_all_migrations, ensure_migrations_applied):
            c = sqlite3.connect(":memory:")
            try:
                _create_message_tables(c)
                _seed_rows(c)
                runner(c)
                assert "actor_role" in _columns(c, "conversation_messages")
                assert "actor_role" in _columns(c, "ai_chat_messages")
                assert _roles(c, "conversation_messages")["cm-owner"] == "authored"
            finally:
                c.close()
