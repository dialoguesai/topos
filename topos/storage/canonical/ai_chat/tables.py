"""Canonical tables manager - manages canonical database tables.

Migrated from engine/canonical/tables.py (commit 7b709af).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Optional

from .model import CanonicalAIChatMessage, CanonicalAIChatConversation
from ...db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.storage.canonical.ai_chat.tables")


class CanonicalTablesManager:
    """Manages canonical tables for unified data models."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize with database connection."""
        self.conn = conn
        self._ensure_tables()
        from ...db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)

    def _ensure_tables(self) -> None:
        """Ensure canonical tables exist. Creates them if they don't exist."""
        try:
            # DDL takes SQLite's write lock at execute time — gate it with the commit.
            with with_db_write():
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS ai_chat_conversations (
                        conversation_id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        title TEXT,
                        source_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_chat_conversations_owner
                    ON ai_chat_conversations(owner_user_id)
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_chat_conversations_source_id
                    ON ai_chat_conversations(source_id)
                """)
                self.conn.execute("""
                    CREATE TABLE IF NOT EXISTS ai_chat_messages (
                        message_id TEXT PRIMARY KEY,
                        conversation_id TEXT NOT NULL,
                        sender_type TEXT NOT NULL,
                        sender_id TEXT,
                        event_at TEXT NOT NULL,
                        content TEXT NOT NULL,
                        content_rendered TEXT,
                        metadata_json TEXT,
                        sequence INTEGER NOT NULL DEFAULT 0,
                        source_id TEXT NOT NULL
                    )
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_conversation
                    ON ai_chat_messages(conversation_id, sequence)
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_event_at
                    ON ai_chat_messages(event_at)
                """)
                self.conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_source
                    ON ai_chat_messages(source_id)
                """)
                commit_connection(self.conn)
            logger.debug("Ensured canonical tables exist")
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to ensure canonical tables: %s", e)

    def write_conversations_batch(
        self,
        conversations: List[CanonicalAIChatConversation],
        batch_size: int = 1000,
        *,
        sync_batch_id: Optional[str] = None,
    ) -> int:
        """Write conversations via CanonicalStore with provenance."""
        if not conversations:
            return 0
        from ..canonical_store import SQLiteCanonicalStore

        store = SQLiteCanonicalStore(self.conn)
        records = [
            {
                "conversation_id": conv.conversation_id,
                "owner_user_id": conv.owner_user_id,
                "title": conv.title,
                "source_id": conv.source,
                "created_at": conv.created_at,
                "updated_at": conv.updated_at,
                "source_record_id": conv.conversation_id,
            }
            for conv in conversations
        ]
        refs = store.upsert_batch("ai_chat_conversations", records, sync_batch_id=sync_batch_id)
        return sum(1 for ref in refs if ref.created)

    def write_messages_batch(
        self,
        messages: List[CanonicalAIChatMessage],
        batch_size: int = 1000,
        *,
        sync_batch_id: Optional[str] = None,
        mapping_source_id: Optional[str] = None,
    ) -> int:
        """Write messages via CanonicalStore in a single transaction."""
        if not messages:
            return 0
        from ..canonical_store import SQLiteCanonicalStore
        from ..mapping_store import MappingRecord, SQLiteMappingStore

        store = SQLiteCanonicalStore(self.conn)
        mapping_store = SQLiteMappingStore(self.conn)

        records = [
            {
                "message_id": msg.message_id,
                "conversation_id": msg.conversation_id,
                "sender_type": msg.sender_type,
                "sender_id": msg.sender_id,
                "ts": msg.ts,
                "content": msg.content,
                "content_rendered": msg.content_rendered,
                "metadata_json": msg.metadata_json,
                "seq": msg.seq,
                "source_id": msg.source_id,
                "source_record_id": getattr(msg, "source_record_id", None) or msg.message_id,
                "content_hash": getattr(msg, "content_hash", None),
            }
            for msg in messages
        ]
        refs = store.upsert_batch("ai_chat_messages", records, sync_batch_id=sync_batch_id)
        if mapping_source_id:
            mapping_store.save_mappings_batch(
                MappingRecord(
                    source_id=mapping_source_id,
                    source_record_id=record["source_record_id"],
                    canonical_id=record["message_id"],
                    canonical_table="ai_chat_messages",
                )
                for record in records
            )
        return sum(1 for ref in refs if ref.created)

    def update_message_sequences(self, conversation_id: str) -> None:
        """Update sequence numbers for messages in a conversation."""
        try:
            cursor = self.conn.execute("""
                SELECT message_id, event_at
                FROM ai_chat_messages
                WHERE conversation_id = ?
                ORDER BY event_at ASC
            """, (conversation_id,))
            messages = cursor.fetchall()
            with with_db_write():
                for seq, (message_id, _) in enumerate(messages):
                    self.conn.execute("""
                        UPDATE ai_chat_messages
                        SET sequence = ?
                        WHERE message_id = ?
                    """, (seq, message_id))
                commit_connection(self.conn)
            logger.debug("Updated sequences for conversation %s (%d messages)", conversation_id, len(messages))
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to update message sequences: %s", e)
            raise
