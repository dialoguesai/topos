"""Postgres canonical store — minimal hosted parity (Phase 1)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .canonical_store import CanonicalRef


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostgresCanonicalStore:
    """MVP table upserts for hosted_database profile."""

    MVP_TABLES = frozenset(
        {
            "ai_chat_messages",
            "ai_chat_conversations",
            "conversation_messages",
            "activity_events",
        }
    )

    def __init__(self, conn) -> None:
        self.conn = conn

    def upsert(
        self,
        table: str,
        record: Dict[str, Any],
        *,
        sync_batch_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> CanonicalRef:
        _ = idempotency_key
        if table not in self.MVP_TABLES:
            raise ValueError(f"Unsupported canonical table: {table}")

        if table == "ai_chat_messages":
            return self._upsert_ai_chat_message(record, sync_batch_id=sync_batch_id)
        if table == "ai_chat_conversations":
            return self._upsert_ai_chat_conversation(record, sync_batch_id=sync_batch_id)
        if table == "conversation_messages":
            return self._upsert_conversation_message(record, sync_batch_id=sync_batch_id)
        return self._upsert_activity_event(record, sync_batch_id=sync_batch_id)

    def _execute(self, sql: str, params: tuple) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            self.conn.commit()
        finally:
            cur.close()

    def _fetchone(self, sql: str, params: tuple):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchone()
        finally:
            cur.close()

    def _upsert_ai_chat_message(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        message_id = str(record["message_id"])
        existing = self._fetchone(
            "SELECT message_id FROM ai_chat_messages WHERE message_id=%s",
            (message_id,),
        )
        metadata = record.get("metadata_json")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)
        self._execute(
            """
            INSERT INTO ai_chat_messages (
                message_id, conversation_id, sender_type, sender_id, event_at,
                content, content_rendered, metadata_json, sequence, source_id,
                source_record_id, ingested_at, sync_batch_id, content_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO UPDATE SET
                content=EXCLUDED.content,
                metadata_json=EXCLUDED.metadata_json,
                sync_batch_id=EXCLUDED.sync_batch_id,
                ingested_at=EXCLUDED.ingested_at
            """,
            (
                message_id,
                record.get("conversation_id"),
                record.get("sender_type"),
                record.get("sender_id"),
                record.get("event_at") or record.get("ts"),
                record.get("content"),
                record.get("content_rendered"),
                metadata,
                record.get("sequence") or record.get("seq") or 0,
                record.get("source_id"),
                record.get("source_record_id") or message_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                record.get("content_hash"),
            ),
        )
        return CanonicalRef(record_id=message_id, created=existing is None)

    def _upsert_ai_chat_conversation(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        conversation_id = str(record["conversation_id"])
        existing = self._fetchone(
            "SELECT conversation_id FROM ai_chat_conversations WHERE conversation_id=%s",
            (conversation_id,),
        )
        self._execute(
            """
            INSERT INTO ai_chat_conversations (
                conversation_id, owner_user_id, title, source_id, created_at, updated_at,
                source_record_id, ingested_at, sync_batch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (conversation_id) DO UPDATE SET
                updated_at=EXCLUDED.updated_at,
                sync_batch_id=EXCLUDED.sync_batch_id
            """,
            (
                conversation_id,
                record.get("owner_user_id"),
                record.get("title"),
                record.get("source_id") or record.get("source"),
                record.get("created_at"),
                record.get("updated_at"),
                record.get("source_record_id") or conversation_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
            ),
        )
        return CanonicalRef(record_id=conversation_id, created=existing is None)

    def _upsert_conversation_message(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        message_id = str(record["message_id"])
        existing = self._fetchone(
            "SELECT message_id FROM conversation_messages WHERE message_id=%s",
            (message_id,),
        )
        metadata = record.get("metadata_json")
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)
        self._execute(
            """
            INSERT INTO conversation_messages (
                message_id, conversation_id, dataset_id, event_at, sender_type, sender_id,
                content, source_id, metadata_json, source_record_id, ingested_at, sync_batch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (
                message_id,
                record.get("conversation_id") or record.get("thread_id"),
                record.get("dataset_id") or "",
                record.get("event_at") or record.get("ts") or _utc_now(),
                record.get("sender_type"),
                record.get("sender_id"),
                record.get("content"),
                record.get("source_id"),
                metadata,
                record.get("source_record_id") or message_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
            ),
        )
        return CanonicalRef(record_id=message_id, created=existing is None)

    def _upsert_activity_event(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        event_id = str(record.get("event_id") or record.get("source_record_id"))
        existing = self._fetchone(
            "SELECT event_id FROM activity_events WHERE event_id=%s",
            (event_id,),
        )
        meta = record.get("metadata_json")
        if isinstance(meta, dict):
            meta = json.dumps(meta)
        self._execute(
            """
            INSERT INTO activity_events (
                event_id, activity_type, url, title, occurred_at, source_id,
                source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
                title=EXCLUDED.title,
                sync_batch_id=EXCLUDED.sync_batch_id
            """,
            (
                event_id,
                record.get("activity_type"),
                record.get("url"),
                record.get("title"),
                record.get("occurred_at"),
                record.get("source_id"),
                record.get("source_record_id") or event_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                meta,
            ),
        )
        return CanonicalRef(record_id=event_id, created=existing is None)
