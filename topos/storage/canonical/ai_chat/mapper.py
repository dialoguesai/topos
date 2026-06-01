"""Canonical mapper - maps staging data to canonical models.

Migrated from engine/canonical/mapper.py (commit 7b709af).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from .model import CanonicalAIChatMessage, CanonicalAIChatConversation

logger = logging.getLogger("topos.storage.canonical.ai_chat.mapper")


class CanonicalMapper(ABC):
    """Base class for mapping staging data to canonical models."""

    @abstractmethod
    def map_to_canonical(
        self,
        staging_record: Dict[str, Any],
        source: str
    ) -> List[CanonicalAIChatMessage]:
        """Map a staging record to canonical format.

        Args:
            staging_record: Record from staging table
            source: Source identifier (e.g., "chatgpt")

        Returns:
            List of canonical messages (usually one, but could be multiple)
        """
        pass

    @abstractmethod
    def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
        """Extract conversation ID from staging record.

        Args:
            staging_record: Record from staging table

        Returns:
            Conversation ID (unified across sources)
        """
        pass


class ChatGPTToAIChatMapper(CanonicalMapper):
    """Maps ChatGPT staging records to canonical AI chat model."""

    def map_to_canonical(
        self,
        staging_record: Dict[str, Any],
        source: str = "chatgpt"
    ) -> List[CanonicalAIChatMessage]:
        """Map ChatGPT staging record to canonical AI chat message.

        ChatGPT staging format:
        {
            "message_id": "...",
            "dataset_id": "...",
            "thread_id": "...",
            "ts": "...",
            "sender_type": "human" | "assistant",
            "content": "..."
        }

        Returns:
            List with single canonical message
        """
        conversation_id = self.extract_conversation_id(staging_record)

        # Use source_id from staging_record if available (actual source_id like "chatgpt_file_ingestion"),
        # otherwise fall back to source parameter (mapper ID like "chatgpt")
        actual_source_id = staging_record.get("source_id") or source

        # Preserve _metadata if present (for conversation tree reconstruction)
        metadata = {
            "thread_id": staging_record.get("thread_id"),
            "original_source": source,
        }
        if "_metadata" in staging_record:
            metadata.update(staging_record["_metadata"])

        message = CanonicalAIChatMessage(
            message_id=staging_record.get("message_id", ""),
            conversation_id=conversation_id,
            sender_type=staging_record.get("sender_type", ""),
            sender_id=None,
            ts=staging_record.get("ts", ""),
            content=staging_record.get("content") or "",
            content_rendered=None,
            metadata_json=metadata,
            seq=0,
            source_id=actual_source_id,
        )
        return [message]

    def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
        """Extract conversation ID from ChatGPT staging record."""
        thread_id = staging_record.get("thread_id")
        if thread_id:
            return f"chatgpt:{thread_id}"
        dataset_id = staging_record.get("dataset_id", "")
        return f"chatgpt:{dataset_id}"


class StoreMessageToAIChatMapper(CanonicalMapper):
    """Maps messages from the messages table (store_message) to canonical AI chat model."""

    def map_to_canonical(
        self,
        staging_record: Dict[str, Any],
        source: str = "store_message"
    ) -> List[CanonicalAIChatMessage]:
        """Map store_message record to canonical AI chat message."""
        conversation_id = self.extract_conversation_id(staging_record)
        message = CanonicalAIChatMessage(
            message_id=staging_record.get("message_id", ""),
            conversation_id=conversation_id,
            sender_type=staging_record.get("sender_type", ""),
            sender_id=None,
            ts=staging_record.get("ts", ""),
            content=staging_record.get("content") or "",
            content_rendered=None,
            metadata_json={
                "thread_id": staging_record.get("thread_id"),
                "original_source": source,
            },
            seq=0,
            source_id=source,
        )
        return [message]

    def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
        """Extract conversation ID from store_message record."""
        thread_id = staging_record.get("thread_id")
        if thread_id:
            return f"store_message:{thread_id}"
        dataset_id = staging_record.get("dataset_id", "")
        return f"store_message:{dataset_id}"


_MAPPER_REGISTRY: Dict[str, type[CanonicalMapper]] = {
    "chatgpt": ChatGPTToAIChatMapper,
    "store_message": StoreMessageToAIChatMapper,
}


def register_mapper(source: str, mapper_class: type[CanonicalMapper]) -> None:
    """Register a mapper class for a source."""
    if not issubclass(mapper_class, CanonicalMapper):
        raise TypeError(f"Mapper class must be a subclass of CanonicalMapper, got {mapper_class}")
    _MAPPER_REGISTRY[source] = mapper_class
    logger.info("Registered canonical mapper for source: %s -> %s", source, mapper_class.__name__)


def get_mapper(source: str) -> CanonicalMapper:
    """Get a mapper instance for a source."""
    mapper_class = _MAPPER_REGISTRY.get(source)
    if not mapper_class:
        available = ", ".join(_MAPPER_REGISTRY.keys())
        raise ValueError(
            f"Unknown source for canonical mapping: {source}. Available sources: {available}"
        )
    return mapper_class()
