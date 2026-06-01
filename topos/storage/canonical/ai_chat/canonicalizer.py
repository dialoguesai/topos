"""Canonicalizer - orchestrates canonicalization of staging data.

Migrated from engine/canonical/canonicalizer.py (commit 7b709af).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .mapper import get_mapper
from .model import CanonicalAIChatConversation
from .tables import CanonicalTablesManager

logger = logging.getLogger("topos.storage.canonical.ai_chat.canonicalizer")


class Canonicalizer:
    """Orchestrates canonicalization of staging data to canonical models."""

    def __init__(self, tables_manager: CanonicalTablesManager):
        """Initialize with canonical tables manager."""
        self.tables_manager = tables_manager

    def canonicalize_staging_batch(
        self,
        staging_records: List[Dict[str, Any]],
        source: str,
        batch_size: int = 1000,
    ) -> Dict[str, Any]:
        """Canonicalize a batch of staging records.

        Args:
            staging_records: List of records from staging table
            source: Source identifier (e.g., "chatgpt")
            batch_size: Batch size for writing canonical records

        Returns:
            Dict with canonicalization results:
            {
                "conversations_created": int,
                "messages_created": int,
                "canonical_messages": List[Dict],
                "errors": List[Dict]
            }
        """
        if not staging_records:
            return {
                "conversations_created": 0,
                "messages_created": 0,
                "canonical_messages": [],
                "errors": [],
            }

        try:
            mapper = get_mapper(source)
        except ValueError as exc:
            logger.error("No mapper found for source %s: %s", source, exc)
            return {
                "conversations_created": 0,
                "messages_created": 0,
                "canonical_messages": [],
                "errors": [{"error": str(exc), "source": source}],
            }

        canonical_messages: List[Any] = []
        conversation_owners: Dict[str, str] = {}
        errors: List[Dict[str, Any]] = []

        for record in staging_records:
            try:
                messages = mapper.map_to_canonical(record, source)
                canonical_messages.extend(messages)
                dataset_id = record.get("dataset_id", "")
                owner_user_id = dataset_id.split(":")[0] if ":" in dataset_id else ""
                for msg in messages:
                    if msg.conversation_id not in conversation_owners:
                        conversation_owners[msg.conversation_id] = owner_user_id
            except Exception as exc:
                logger.error("Failed to map staging record to canonical: %s", exc)
                errors.append({
                    "record": record,
                    "error": str(exc),
                    "source": source,
                })

        if not canonical_messages:
            return {
                "conversations_created": 0,
                "messages_created": 0,
                "canonical_messages": [],
                "errors": errors,
            }

        conversations_dict: Dict[str, List[Any]] = defaultdict(list)
        for msg in canonical_messages:
            conversations_dict[msg.conversation_id].append(msg)

        conversations: List[CanonicalAIChatConversation] = []
        for conversation_id, messages in conversations_dict.items():
            owner_user_id = conversation_owners.get(conversation_id, "")
            timestamps = [msg.ts for msg in messages if msg.ts]
            created_at = min(timestamps) if timestamps else ""
            updated_at = max(timestamps) if timestamps else ""
            conversation = CanonicalAIChatConversation(
                conversation_id=conversation_id,
                owner_user_id=owner_user_id,
                title=None,
                source=source,
                created_at=created_at,
                updated_at=updated_at,
            )
            conversations.append(conversation)

        conversations_created = 0
        try:
            conversations_created = self.tables_manager.write_conversations_batch(
                conversations, batch_size=batch_size
            )
        except Exception as exc:
            logger.error("Failed to write conversations: %s", exc)
            errors.append({"error": f"Failed to write conversations: {exc}", "source": source})

        messages_created = 0
        try:
            messages_created = self.tables_manager.write_messages_batch(
                canonical_messages, batch_size=batch_size
            )
        except Exception as exc:
            logger.error("Failed to write messages: %s", exc)
            errors.append({"error": f"Failed to write messages: {exc}", "source": source})

        for conversation_id in conversations_dict.keys():
            try:
                self.tables_manager.update_message_sequences(conversation_id)
            except Exception as exc:
                logger.warning("Failed to update sequences for conversation %s: %s", conversation_id, exc)

        canonical_messages_dicts = [msg.to_dict() for msg in canonical_messages]

        return {
            "conversations_created": conversations_created,
            "messages_created": messages_created,
            "canonical_messages": canonical_messages_dicts,
            "errors": errors,
        }
