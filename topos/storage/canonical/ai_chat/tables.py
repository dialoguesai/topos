"""Canonical tables manager - manages canonical database tables.

Migrated from engine/canonical/tables.py (commit 7b709af).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List

from .model import CanonicalAIChatMessage, CanonicalAIChatConversation

logger = logging.getLogger("topos.storage.canonical.ai_chat.tables")


class CanonicalTablesManager:
    """Manages canonical tables for unified data models."""

    def __init__(self, conn: sqlite3.Connection):
        """Initialize with database connection."""
        self.conn = conn
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure canonical tables exist. Creates them if they don't exist."""
        try:
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
            self.conn.commit()
            logger.debug("Ensured canonical tables exist")
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to ensure canonical tables: %s", e)

    def write_conversations_batch(
        self,
        conversations: List[CanonicalAIChatConversation],
        batch_size: int = 1000,
    ) -> int:
        """Write multiple conversations to canonical table in batches."""
        if not conversations:
            return 0
        written = 0
        try:
            for i in range(0, len(conversations), batch_size):
                batch = conversations[i:i + batch_size]
                values = [
                    (
                        conv.conversation_id,
                        conv.owner_user_id,
                        conv.title,
                        conv.source,
                        conv.created_at,
                        conv.updated_at,
                    )
                    for conv in batch
                ]
                self.conn.executemany("""
                    INSERT OR REPLACE INTO ai_chat_conversations (
                        conversation_id, owner_user_id, title, source_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, values)
                self.conn.commit()
                written += len(batch)
                logger.debug("Wrote batch of %d conversations (total: %d)", len(batch), written)
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to write conversations batch: %s", e)
            raise
        return written

    def write_messages_batch(
        self,
        messages: List[CanonicalAIChatMessage],
        batch_size: int = 1000,
    ) -> int:
        """Write multiple messages to canonical table in batches."""
        if not messages:
            return 0
        import json
        written = 0
        try:
            for i in range(0, len(messages), batch_size):
                batch = messages[i:i + batch_size]
                values = [
                    (
                        msg.message_id,
                        msg.conversation_id,
                        msg.sender_type,
                        msg.sender_id,
                        msg.ts,
                        msg.content,
                        msg.content_rendered,
                        json.dumps(msg.metadata_json) if msg.metadata_json else None,
                        msg.seq,
                        msg.source_id,
                    )
                    for msg in batch
                ]
                self.conn.executemany("""
                    INSERT OR REPLACE INTO ai_chat_messages (
                        message_id, conversation_id, sender_type, sender_id, event_at,
                        content, content_rendered, metadata_json, sequence, source_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, values)
                self.conn.commit()
                written += len(batch)
                logger.debug("Wrote batch of %d messages (total: %d)", len(batch), written)
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to write messages batch: %s", e)
            raise
        return written

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
            for seq, (message_id, _) in enumerate(messages):
                self.conn.execute("""
                    UPDATE ai_chat_messages
                    SET sequence = ?
                    WHERE message_id = ?
                """, (seq, message_id))
            self.conn.commit()
            logger.debug("Updated sequences for conversation %s (%d messages)", conversation_id, len(messages))
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to update message sequences: %s", e)
            raise
