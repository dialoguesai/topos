"""Canonical data models - unified schemas for compatible sources.

Migrated from engine/canonical/model.py (commit 7b709af).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class CanonicalAIChatMessage:
    """Canonical AI chat message model.

    This is the unified format for all AI chat conversations regardless of source
    (ChatGPT, Claude, Gemini, etc.).
    """
    message_id: str
    conversation_id: str  # Unified conversation identifier
    sender_type: str  # "human" | "assistant" | "system"
    ts: str  # ISO timestamp
    content: str  # Message content
    source_id: str = ""  # Original source identifier (e.g., "chatgpt")
    sender_id: Optional[str] = None  # Optional sender identifier
    content_rendered: Optional[str] = None  # Optional rendered content
    metadata_json: Optional[Dict[str, Any]] = None  # Source-specific metadata
    seq: int = 0  # Sequence number within conversation

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        import json
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "sender_type": self.sender_type,
            "sender_id": self.sender_id,
            "ts": self.ts,
            "content": self.content,
            "content_rendered": self.content_rendered,
            "metadata_json": json.dumps(self.metadata_json) if self.metadata_json else None,
            "seq": self.seq,
            "source_id": self.source_id,
        }


@dataclass
class CanonicalAIChatConversation:
    """Canonical AI chat conversation model.

    Represents a unified conversation across all AI chat sources.
    """
    conversation_id: str
    owner_user_id: str
    title: Optional[str] = None
    source: str = ""  # Original source (e.g., "chatgpt")
    created_at: str = ""  # ISO timestamp
    updated_at: str = ""  # ISO timestamp

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            "conversation_id": self.conversation_id,
            "owner_user_id": self.owner_user_id,
            "title": self.title,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class CanonicalAIChatModel:
    """Canonical AI chat conversation model.

    This model unifies all AI chat sources (ChatGPT, Claude, Gemini, etc.)
    into a single canonical format.
    """

    @staticmethod
    def get_conversation_table() -> str:
        """Return the canonical conversations table name."""
        return "ai_chat_conversations"

    @staticmethod
    def get_messages_table() -> str:
        """Return the canonical messages table name."""
        return "ai_chat_messages"
