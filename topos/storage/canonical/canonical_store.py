"""Canonical store — SQLite upsert for MVP wiki tables."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_metadata(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, str):
        return value
    return json.dumps(value)


@dataclass(frozen=True)
class CanonicalRef:
    record_id: str
    created: bool = True


class CanonicalStore:
    def upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str] = None) -> CanonicalRef:
        raise NotImplementedError


class SQLiteCanonicalStore(CanonicalStore):
    """Routes upserts to MVP canonical tables with provenance columns."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._defer_commit = False
        from ..db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)

    def upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str] = None) -> CanonicalRef:
        if table == "ai_chat_messages":
            ref = self._upsert_ai_chat_message(record, sync_batch_id=sync_batch_id)
        elif table == "ai_chat_conversations":
            ref = self._upsert_ai_chat_conversation(record, sync_batch_id=sync_batch_id)
        elif table == "conversation_messages":
            ref = self._upsert_conversation_message(record, sync_batch_id=sync_batch_id)
        elif table == "activity_events":
            ref = self._upsert_activity_event(record, sync_batch_id=sync_batch_id)
        else:
            raise ValueError(f"Unsupported canonical table: {table}")
        self._maybe_commit()
        return ref

    def upsert_batch(
        self,
        table: str,
        records: List[Dict[str, Any]],
        *,
        sync_batch_id: Optional[str] = None,
    ) -> List[CanonicalRef]:
        if not records:
            return []
        self._defer_commit = True
        try:
            return [self.upsert(table, record, sync_batch_id=sync_batch_id) for record in records]
        finally:
            self._defer_commit = False
            self._conn.commit()

    def _maybe_commit(self) -> None:
        if not self._defer_commit:
            self._conn.commit()

    def _upsert_ai_chat_message(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        message_id = str(record.get("message_id") or record.get("record_id") or "")
        if not message_id:
            raise ValueError("ai_chat_messages upsert requires message_id")
        existing = self._conn.execute(
            "SELECT message_id FROM ai_chat_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO ai_chat_messages (
                message_id, conversation_id, sender_type, sender_id, event_at,
                content, content_rendered, metadata_json, sequence, source_id,
                source_record_id, ingested_at, sync_batch_id, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                content=excluded.content,
                metadata_json=excluded.metadata_json,
                source_id=excluded.source_id,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
            """,
            (
                message_id,
                record.get("conversation_id"),
                record.get("sender_type"),
                record.get("sender_id"),
                record.get("event_at") or record.get("ts"),
                record.get("content"),
                record.get("content_rendered"),
                _json_metadata(record.get("metadata_json")),
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
        conversation_id = str(record.get("conversation_id") or "")
        if not conversation_id:
            raise ValueError("ai_chat_conversations upsert requires conversation_id")
        existing = self._conn.execute(
            "SELECT conversation_id FROM ai_chat_conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO ai_chat_conversations (
                conversation_id, owner_user_id, title, source_id, created_at, updated_at,
                source_record_id, ingested_at, sync_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                updated_at=excluded.updated_at,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
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
        message_id = str(record.get("message_id") or "")
        if not message_id:
            raise ValueError("conversation_messages upsert requires message_id")
        existing = self._conn.execute(
            "SELECT message_id FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        dataset_id = record.get("dataset_id") or ""
        event_at = record.get("event_at") or record.get("ts") or _utc_now()
        self._conn.execute(
            """
            INSERT OR IGNORE INTO conversation_messages (
                message_id, conversation_id, dataset_id, event_at, sender_type, sender_id,
                reply_to_message_id, message_type, event_type, content, source_id,
                metadata_json, is_from_self, owner_user_id,
                source_record_id, ingested_at, sync_batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                record.get("conversation_id") or record.get("thread_id"),
                dataset_id,
                event_at,
                record.get("sender_type"),
                record.get("sender_id"),
                record.get("reply_to_message_id"),
                record.get("message_type"),
                record.get("event_type"),
                record.get("content"),
                record.get("source_id"),
                _json_metadata(record.get("metadata_json")),
                1 if record.get("is_from_self") or record.get("from_self") else 0,
                record.get("owner_user_id"),
                record.get("source_record_id") or message_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
            ),
        )
        if existing is not None:
            self._conn.execute(
                """
                UPDATE conversation_messages
                SET sync_batch_id=COALESCE(?, sync_batch_id),
                    ingested_at=COALESCE(?, ingested_at)
                WHERE message_id=?
                """,
                (sync_batch_id or record.get("sync_batch_id"), record.get("ingested_at"), message_id),
            )
        return CanonicalRef(record_id=message_id, created=existing is None)

    def _upsert_activity_event(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        event_id = str(record.get("event_id") or record.get("source_record_id") or "")
        if not event_id:
            raise ValueError("activity_events upsert requires event_id")
        existing = self._conn.execute(
            "SELECT event_id FROM activity_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO activity_events (
                event_id, activity_type, url, title, occurred_at, source_id,
                source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
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
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=event_id, created=existing is None)


class InMemoryCanonicalStore(CanonicalStore):
    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.upsert_calls: list[tuple[str, Dict[str, Any]]] = []

    def upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str] = None) -> CanonicalRef:
        self.upsert_calls.append((table, dict(record)))
        record_id = str(record.get("message_id") or record.get("event_id") or record.get("conversation_id"))
        bucket = self._records.setdefault(table, {})
        created = record_id not in bucket
        bucket[record_id] = {**record, "sync_batch_id": sync_batch_id}
        return CanonicalRef(record_id=record_id, created=created)

    def upsert_batch(
        self,
        table: str,
        records: List[Dict[str, Any]],
        *,
        sync_batch_id: Optional[str] = None,
    ) -> List[CanonicalRef]:
        return [self.upsert(table, record, sync_batch_id=sync_batch_id) for record in records]
