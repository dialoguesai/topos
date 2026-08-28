"""Canonical store — SQLite upsert for MVP wiki tables."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.storage.canonical.canonical_store")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_or_none(value: Any) -> Optional[str]:
    """Text column value; blank/absent stays NULL so a COALESCE upsert never
    overwrites a stored value with an empty string."""
    text = "" if value is None else str(value).strip()
    return text or None


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
        # The _upsert_* INSERT takes SQLite's write lock at execute time, so it
        # must run under the same gate hold as the commit (write_gate lock-order
        # inversion). Reentrant, so batch callers already holding the gate nest.
        with with_db_write():
            ref = self._dispatch_upsert(table, record, sync_batch_id=sync_batch_id)
            self._maybe_commit()
        return ref

    def _dispatch_upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        if table == "ai_chat_messages":
            ref = self._upsert_ai_chat_message(record, sync_batch_id=sync_batch_id)
        elif table == "ai_chat_conversations":
            ref = self._upsert_ai_chat_conversation(record, sync_batch_id=sync_batch_id)
        elif table == "conversation_messages":
            ref = self._upsert_conversation_message(record, sync_batch_id=sync_batch_id)
        elif table == "activity_events":
            ref = self._upsert_activity_event(record, sync_batch_id=sync_batch_id)
        elif table == "calendar_events":
            ref = self._upsert_calendar_event(record, sync_batch_id=sync_batch_id)
        elif table == "journal_entries":
            ref = self._upsert_journal_entry(record, sync_batch_id=sync_batch_id)
        elif table == "profile_records":
            ref = self._upsert_profile_record(record, sync_batch_id=sync_batch_id)
        elif table == "financial_transactions":
            ref = self._upsert_financial_transaction(record, sync_batch_id=sync_batch_id)
        elif table == "location_events":
            ref = self._upsert_location_event(record, sync_batch_id=sync_batch_id)
        elif table == "documents":
            ref = self._upsert_document(record, sync_batch_id=sync_batch_id)
        else:
            raise ValueError(f"Unsupported canonical table: {table}")
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
        # Hold the gate across the whole batch: every upsert takes SQLite's
        # write lock, and the deferred commit at the end must happen under the
        # same hold to avoid queuing on the gate with the lock already taken.
        with with_db_write():
            try:
                return [self.upsert(table, record, sync_batch_id=sync_batch_id) for record in records]
            finally:
                self._defer_commit = False
                commit_connection(self._conn)

    def _maybe_commit(self) -> None:
        if not self._defer_commit:
            commit_connection(self._conn)

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
            "SELECT message_id, content FROM conversation_messages WHERE message_id=?",
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
            # Re-ingest must be able to correct a body the reader got wrong.
            # This was the only canonical table whose upsert left `content`
            # frozen at whatever the first sync wrote -- ai_chat_messages,
            # activity_events and the rest all carry content in their DO UPDATE
            # set. That asymmetry meant the iMessage attributedBody decode fix
            # could not reach the 3,722 rows (49% of the corpus) already
            # holding archive bytes: re-syncing read them correctly and then
            # discarded the result at the write.
            incoming = record.get("content")
            if incoming and str(incoming) != (existing[1] or ""):
                self._conn.execute(
                    """
                    UPDATE conversation_messages
                    SET content=?,
                        content_hash=NULL,
                        content_disclosure=NULL,
                        content_disclosure_hash=NULL,
                        content_disclosure_model=NULL
                    WHERE message_id=?
                    """,
                    (str(incoming), message_id),
                )
                # The disclosure columns hold a scrub of the *old* body, so they
                # are cleared rather than left to describe text that no longer
                # exists. `scripts/backfill_disclosure.py --source-id <source>`
                # refills them.
                logger.debug(
                    "[PIPELINE:CANONICAL] healed conversation_messages.content for %s", message_id
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
        # content/hostname (activity_events_content_v1) are written here: the
        # P2.1 browser mapper and the §5a declared field maps both produce them,
        # and until this INSERT carried the columns every value they computed was
        # discarded at the write (0/4,444 rows populated on the first live node
        # checked). They are in the DO UPDATE set too, so a re-ingest or a
        # reprocess-from-raw heals rows that were written before this fix.
        self._conn.execute(
            """
            INSERT INTO activity_events (
                event_id, activity_type, url, title, occurred_at, source_id,
                source_record_id, ingested_at, sync_batch_id, metadata_json,
                content, hostname
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=COALESCE(excluded.metadata_json, activity_events.metadata_json),
                content=COALESCE(excluded.content, activity_events.content),
                hostname=COALESCE(excluded.hostname, activity_events.hostname)
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
                _text_or_none(record.get("content")),
                _text_or_none(record.get("hostname")),
            ),
        )
        return CanonicalRef(record_id=event_id, created=existing is None)

    def _upsert_calendar_event(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        event_id = str(record.get("event_id") or record.get("source_record_id") or "")
        if not event_id:
            raise ValueError("calendar_events upsert requires event_id")
        existing = self._conn.execute(
            "SELECT event_id FROM calendar_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO calendar_events (
                event_id, title, starts_at, ends_at,
                is_busy, status, is_all_day, self_response_status, is_organizer,
                is_recurring, event_type, timezone, location, description, url,
                attendee_count, accepted_count, created_at, updated_at,
                attendance_priority, movability_score, value_score, value_reason,
                priority_confidence,
                source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                starts_at=excluded.starts_at,
                ends_at=excluded.ends_at,
                is_busy=excluded.is_busy,
                status=excluded.status,
                is_all_day=excluded.is_all_day,
                self_response_status=excluded.self_response_status,
                is_organizer=excluded.is_organizer,
                is_recurring=excluded.is_recurring,
                event_type=excluded.event_type,
                timezone=excluded.timezone,
                location=excluded.location,
                description=excluded.description,
                url=excluded.url,
                attendee_count=excluded.attendee_count,
                accepted_count=excluded.accepted_count,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                attendance_priority=excluded.attendance_priority,
                movability_score=excluded.movability_score,
                value_score=excluded.value_score,
                value_reason=excluded.value_reason,
                priority_confidence=excluded.priority_confidence,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                event_id,
                record.get("title"),
                record.get("starts_at"),
                record.get("ends_at"),
                record.get("is_busy"),
                record.get("status"),
                record.get("is_all_day"),
                record.get("self_response_status"),
                record.get("is_organizer"),
                record.get("is_recurring"),
                record.get("event_type"),
                record.get("timezone"),
                record.get("location"),
                record.get("description"),
                record.get("url"),
                record.get("attendee_count"),
                record.get("accepted_count"),
                record.get("created_at"),
                record.get("updated_at"),
                record.get("attendance_priority"),
                record.get("movability_score"),
                record.get("value_score"),
                record.get("value_reason"),
                record.get("priority_confidence"),
                record.get("source_id"),
                record.get("source_record_id") or event_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=event_id, created=existing is None)

    @staticmethod
    def _journal_entry_at(record: Dict[str, Any], ingested_at: str) -> Any:
        """Event time for a journal row, preferring its own session start.

        Grow's journal producers stamped ``entry_at`` with the import clock
        while carrying the true session time in ``starts_at``: 127 rows landed
        on 2026-08-08T03:34:44 — equal to ``ingested_at`` to the second — and
        171 more on 2026-06-28T23:28:45, so sessions spanning months all claimed
        to have happened the instant they were imported.

        Downstream this is not cosmetic. The entity graph dates its edges from
        canonical event time, so those entries pulled years-old relationships
        into the "last 6 days" view of /data/graph.

        A journal entry whose stated time matches the ingest second to the
        second, while it separately knows when the session started, is reporting
        the importer's clock rather than its own — so prefer ``starts_at``.
        Records that omit ``entry_at`` fall back to it as well.
        """
        entry_at = record.get("entry_at")
        starts_at = record.get("starts_at")
        if not starts_at:
            return entry_at
        if not entry_at:
            return starts_at
        if str(entry_at)[:19] == str(ingested_at or "")[:19]:
            return starts_at
        return entry_at

    def _upsert_journal_entry(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        entry_id = str(record.get("entry_id") or record.get("source_record_id") or "")
        if not entry_id:
            raise ValueError("journal_entries upsert requires entry_id")
        ingested_at = record.get("ingested_at") or _utc_now()
        existing = self._conn.execute(
            "SELECT entry_id FROM journal_entries WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO journal_entries (
                entry_id, entry_at, starts_at, ends_at, mood_tag, category, content, duration, people, place_name, source_id,
                source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                content=excluded.content,
                mood_tag=excluded.mood_tag,
                category=excluded.category,
                duration=excluded.duration,
                people=excluded.people,
                place_name=excluded.place_name,
                entry_at=excluded.entry_at,
                starts_at=excluded.starts_at,
                ends_at=excluded.ends_at,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                entry_id,
                self._journal_entry_at(record, ingested_at),
                record.get("starts_at"),
                record.get("ends_at"),
                record.get("mood_tag"),
                record.get("category"),
                record.get("content"),
                record.get("duration"),
                record.get("people"),
                record.get("place_name"),
                record.get("source_id"),
                record.get("source_record_id") or entry_id,
                ingested_at,
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=entry_id, created=existing is None)

    def _upsert_profile_record(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        record_id = str(record.get("record_id") or record.get("source_record_id") or "")
        if not record_id:
            raise ValueError("profile_records upsert requires record_id")
        existing = self._conn.execute(
            "SELECT record_id FROM profile_records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO profile_records (
                record_id, record_type, title, organization, start_date, end_date,
                description, source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO UPDATE SET
                description=excluded.description,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
            """,
            (
                record_id,
                record.get("record_type"),
                record.get("title"),
                record.get("organization"),
                record.get("start_date"),
                record.get("end_date"),
                record.get("description"),
                record.get("source_id"),
                record.get("source_record_id") or record_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=record_id, created=existing is None)

    def _upsert_financial_transaction(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        transaction_id = str(record.get("transaction_id") or record.get("source_record_id") or "")
        if not transaction_id:
            raise ValueError("financial_transactions upsert requires transaction_id")
        existing = self._conn.execute(
            "SELECT transaction_id FROM financial_transactions WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO financial_transactions (
                transaction_id, account_type, account_name, posted_at, amount, currency,
                category, description, source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transaction_id) DO UPDATE SET
                amount=excluded.amount,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
            """,
            (
                transaction_id,
                record.get("account_type"),
                record.get("account_name"),
                record.get("posted_at"),
                record.get("amount"),
                record.get("currency") or "USD",
                record.get("category"),
                record.get("description"),
                record.get("source_id"),
                record.get("source_record_id") or transaction_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=transaction_id, created=existing is None)

    def _upsert_location_event(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        event_id = str(record.get("event_id") or record.get("source_record_id") or "")
        if not event_id:
            raise ValueError("location_events upsert requires event_id")
        existing = self._conn.execute(
            "SELECT event_id FROM location_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO location_events (
                event_id, place_name, city, region, country, event_at, event_type,
                source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                place_name=excluded.place_name,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at
            """,
            (
                event_id,
                record.get("place_name"),
                record.get("city"),
                record.get("region"),
                record.get("country"),
                record.get("event_at"),
                record.get("event_type"),
                record.get("source_id"),
                record.get("source_record_id") or event_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=event_id, created=existing is None)

    def _upsert_document(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        doc_id = str(record.get("doc_id") or record.get("source_record_id") or "")
        if not doc_id:
            raise ValueError("documents upsert requires doc_id")
        existing = self._conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO documents (
                doc_id, title, content, url, mime_type, author, created_at, modified_at,
                source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                url=excluded.url,
                mime_type=excluded.mime_type,
                author=excluded.author,
                created_at=excluded.created_at,
                modified_at=excluded.modified_at,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                doc_id,
                record.get("title"),
                record.get("content"),
                record.get("url"),
                record.get("mime_type"),
                record.get("author"),
                record.get("created_at"),
                record.get("modified_at"),
                record.get("source_id"),
                record.get("source_record_id") or doc_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=doc_id, created=existing is None)


class InMemoryCanonicalStore(CanonicalStore):
    def __init__(self) -> None:
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.upsert_calls: list[tuple[str, Dict[str, Any]]] = []

    def upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str] = None) -> CanonicalRef:
        self.upsert_calls.append((table, dict(record)))
        record_id = str(
            record.get("message_id")
            or record.get("event_id")
            or record.get("doc_id")
            or record.get("conversation_id")
        )
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
