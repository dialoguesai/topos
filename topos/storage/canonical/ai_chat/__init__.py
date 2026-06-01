"""Canonical AI chat layer - unified data models for AI chat sources.

Migrated from engine/canonical/ (commit 7b709af).
Maps source-specific staging data (e.g. ChatGPT) into ai_chat_messages / ai_chat_conversations.
"""

from .model import CanonicalAIChatModel, CanonicalAIChatMessage, CanonicalAIChatConversation
from .mapper import CanonicalMapper, get_mapper, ChatGPTToAIChatMapper, StoreMessageToAIChatMapper
from .tables import CanonicalTablesManager
from .canonicalizer import Canonicalizer

__all__ = [
    "CanonicalAIChatModel",
    "CanonicalAIChatMessage",
    "CanonicalAIChatConversation",
    "CanonicalMapper",
    "get_mapper",
    "ChatGPTToAIChatMapper",
    "StoreMessageToAIChatMapper",
    "CanonicalTablesManager",
    "Canonicalizer",
]
