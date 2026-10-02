"""Canonical store — SQLite upsert for MVP wiki tables."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..db.write_gate import commit_connection, with_db_write
from ...disclosure.nsfw_tags import TABLES as NSFW_TAGGED_TABLES, columns_present, tag_inserted, tag_stored

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


#: CanonicalRef.refused values for a write the store declined to apply.
#: ``rewrite``: a non-owner writer sent different values under an id whose row
#: the owner holds. ``duplicate``: the same, with nothing different — nothing to
#: change, and nothing that should be re-derived under the lesser writer.
REFUSED_OWNER_ROW_REWRITE = "owner_row_rewrite"
REFUSED_OWNER_ROW_DUPLICATE = "owner_row_duplicate"
#: The record is dated before its source's retention floor (``sources/retention.py``):
#: the owner keeps that source only from a date onward, so no door writes it back.
REFUSED_RETENTION_FLOOR = "retention_floor"

#: Canonical tables that record the door that wrote each row
#: (``features/provenance/writer_class.py``), with their primary key: every
#: table whose rows the role gate can read as the owner's own — chat speech,
#: journal and profile rows (authored by construction), and the posture-personal
#: families — and activity_events. conversation_messages records it in its own
#: upsert. Activity rows are ambient by table and never the owner's speech, but
#: the door is the only thing that tells the owner's own capture from a visit
#: another app wrote (OD-52 P1). Transcript rows record none.
WRITER_CLASS_TABLES: Dict[str, str] = {
    "ai_chat_messages": "message_id",
    "journal_entries": "entry_id",
    "profile_records": "record_id",
    "documents": "doc_id",
    "calendar_events": "event_id",
    "financial_transactions": "transaction_id",
    "location_events": "event_id",
    "activity_events": "event_id",
}

#: The switch for activity_events' writer (OD-52 P1). On by default since October 2026:
#: an activity write records its door, app and dataset and keeps an owner door's row the
#: way the other WRITER_CLASS_TABLES do. Off (``0``, ``false``, ``no`` or ``off``), it
#: records no writer and is never refused, as before migration 80; a door's write then
#: also clears a writer recorded while the switch was on, which would otherwise describe
#: values this write replaced.
#:
#: Why on by default: a row with no writer can be proven the owner's only by a receipt that
#: lists it, so with the switch off every visit the browser plugin pushes waits for the
#: owner's next receipt, on every node, forever; and a receipt over unrecorded rows cannot
#: tell the plugin's visits from visits another app holding a write grant sent to the same
#: source. On, the plugin's stamped visits record ``owner_app`` with its app id and count
#: once the owner has attested the plugin (``permissions_v2/capture_receipts.proven``), and
#: another app's visits record their own door and never count.
ACTIVITY_WRITER_FLAG = "TOPOS_ACTIVITY_WRITER_CLASS"
_SWITCH_OFF = frozenset({"0", "false", "no", "off"})


def activity_writer_recording_enabled(env=None) -> bool:
    """On unless the switch says off: unset, blank and any other value keep the default."""
    env = os.environ if env is None else env
    return str(env.get(ACTIVITY_WRITER_FLAG, "")).strip().lower() not in _SWITCH_OFF

#: Columns a write may change without it counting as a different row: the
#: provenance the store itself stamps, and derived or rendered copies.
_ROW_IDENTITY_IGNORED = frozenset({
    "writer_class", "writer_app_id", "writer_dataset_id", "ingested_at", "sync_batch_id", "source_record_id", "source_id",
    "metadata_json", "content_rendered", "content_hash", "sequence", "actor_role",
})


@dataclass(frozen=True)
class CanonicalRef:
    record_id: str
    created: bool = True
    refused: Optional[str] = None
    #: The row's writer class after this write: the incoming one, else the one
    #: already stored. None for a row no door has recorded (legacy).
    writer_class: Optional[str] = None


def _owner_holds_row(table: str, stored: Dict[str, Any]) -> bool:
    """Whether a stored row is the owner's to keep (see ``_upsert_recording_writer``)."""
    from ...features.provenance.roles import ROLE_ADDRESSED, ROLE_AUTHORED, record_role
    from ...features.provenance.writer_class import is_owner_writer, normalize_writer_class

    stored_writer = normalize_writer_class(stored.get("writer_class"))
    if stored_writer is not None:
        return is_owner_writer(stored_writer)
    # No door recorded. Posture is left out on purpose: it is the owner's
    # per-connector setting, not evidence about who wrote this row.
    return record_role(stored, table=table) in (ROLE_AUTHORED, ROLE_ADDRESSED)


def _insert_trusted_conversation_batch(
    conn: sqlite3.Connection,
    records: List[Dict[str, Any]],
    *,
    source_id: str,
    dataset_id: str,
    trusted_context: Any,
    sync_batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert an enrolled native snapshot inside its caller-owned transaction.

    This deliberately avoids the legacy store constructor and upsert path:
    migrations, parent replacement, content healing and internal commits would
    violate the snapshot transaction. Neither record fields nor duck typing can
    create the verified context. Existing unlinked rows remain unmodified.
    """
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.ingest_provenance import IngestProvenanceService, VerifiedIngestContext

    if type(trusted_context) is not VerifiedIngestContext or type(trusted_context.service) is not IngestProvenanceService:
        raise PolicyError("ingest_canonical_context_required")
    trusted_context.require_batch(conn)
    trusted_context.assert_current(conn, source_id=source_id, dataset_id=dataset_id)
    if source_id != "imessage" or type(records) is not list or len(records) > 1000:
        raise PolicyError("ingest_canonical_invalid")
    if sync_batch_id is not None and (type(sync_batch_id) is not str or not sync_batch_id):
        raise PolicyError("ingest_canonical_invalid")

    allowed_fields = {
        "message_id", "thread_id", "conversation_id", "dataset_id", "source_id",
        "source_record_id", "owner_user_id", "ts", "event_at", "sender_type",
        "sender_id", "from_self", "is_from_self", "role", "actor_role", "content",
        "reply_to_message_id", "message_type", "event_type", "_metadata",
    }

    def invalid() -> None:
        raise PolicyError("ingest_canonical_invalid") from None

    def text(value: Any) -> str:
        if type(value) is not str or not value or value != value.strip():
            invalid()
        try:
            value.encode("utf-8")
        except UnicodeError:
            invalid()
        return value

    def optional_text(value: Any) -> Optional[str]:
        return None if value is None else text(value)

    # Normalize and check the complete batch before looking at any insert. No
    # bool("false"), imported owner default, sender-role inference or clock
    # fallback is allowed in this native path.
    normalized: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if type(record) is not dict or set(record) - allowed_fields:
            invalid()
        message_id = text(record.get("message_id"))
        if not re.fullmatch(r"imessage:[1-9][0-9]*", message_id):
            invalid()
        conversation_id = text(record.get("conversation_id") or record.get("thread_id"))
        if any(record[key] != conversation_id for key in ("conversation_id", "thread_id") if key in record):
            invalid()
        for key, expected in (("dataset_id", dataset_id), ("source_id", source_id), ("source_record_id", message_id)):
            if key in record and record[key] != expected:
                invalid()
        if record.get("owner_user_id") is not None and record["owner_user_id"] != trusted_context.owner_id:
            invalid()
        flags = [record[key] for key in ("from_self", "is_from_self") if key in record]
        if not flags or any(type(flag) is not bool or flag != flags[0] for flag in flags):
            invalid()
        is_self = flags[0]
        sender_id = text(record.get("sender_id"))
        sender_type = record.get("sender_type")
        if (is_self and sender_id != "self") or (not is_self and sender_id.lower() == "self"):
            invalid()
        if type(sender_type) is not str or sender_type not in ({"human", "self"} if is_self else {"human", "contact"}):
            invalid()
        if "role" in record and record["role"] != ("user" if is_self else "other"):
            invalid()
        actor_role = "authored" if is_self else "observed"
        if "actor_role" in record and record["actor_role"] != actor_role:
            invalid()
        event_at = text(record.get("event_at") or record.get("ts"))
        if any(record[key] != event_at for key in ("event_at", "ts") if key in record):
            invalid()
        try:
            parsed_time = datetime.fromisoformat(event_at.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
                invalid()
        except (TypeError, ValueError):
            invalid()
        if type(record.get("content")) is not str:
            invalid()
        try:
            record["content"].encode("utf-8")
        except UnicodeError:
            invalid()
        metadata = {}
        if "_metadata" in record:
            if type(record["_metadata"]) is not dict or "topos_owner_ingest" in record["_metadata"]:
                invalid()
            metadata = dict(record["_metadata"])
        # This marker only locates the durable proof; readers must validate that
        # proof and its current enrollment, never trust this JSON by itself.
        metadata["topos_owner_ingest"] = {"version": "owner-attested-snapshot/v1",
            "enrollment_id": trusted_context.enrollment_id, "job_id": trusted_context.job_id}
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, allow_nan=False)
            metadata_json.encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            invalid()
        canonical = {
            "message_id": message_id, "conversation_id": conversation_id,
            "dataset_id": dataset_id, "source_id": source_id, "source_record_id": message_id,
            "owner_user_id": trusted_context.owner_id, "event_at": event_at,
            "sender_type": sender_type, "sender_id": sender_id, "is_from_self": int(is_self),
            "actor_role": actor_role, "content": record["content"], "metadata_json": metadata_json,
            "reply_to_message_id": optional_text(record.get("reply_to_message_id")),
            "message_type": optional_text(record.get("message_type")),
            "event_type": optional_text(record.get("event_type")),
        }
        if message_id in normalized and normalized[message_id] != canonical:
            raise PolicyError("ingest_canonical_collision")
        normalized[message_id] = canonical

    insertions: List[Dict[str, Any]] = []
    message_ids: List[str] = []
    parents: Dict[str, bool] = {}
    historical_skipped = 0
    # The lane never migrates; it tags its rows when the enrolled node's schema has the tag columns.
    tag_columns = columns_present(conn, "conversation_messages")
    columns = (
        "message_id", "conversation_id", "dataset_id", "source_id", "source_record_id",
        "owner_user_id", "event_at", "sender_type", "sender_id", "is_from_self", "actor_role",
        "content", "metadata_json", "reply_to_message_id", "message_type", "event_type",
    )
    for message_id, canonical in normalized.items():
        conversation_id = canonical["conversation_id"]
        if conversation_id not in parents:
            parent = conn.execute(
                "SELECT source_id FROM conversations WHERE conversation_id=? AND dataset_id=?",
                (conversation_id, dataset_id),
            ).fetchone()
            if parent is not None and parent[0] != source_id:
                raise PolicyError("ingest_canonical_collision")
            parents[conversation_id] = parent is not None
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM conversation_messages WHERE message_id=?", (message_id,),
        ).fetchone()
        linked = trusted_context.existing_record(conn, message_id)
        if row is None:
            if linked:
                raise PolicyError("ingest_canonical_collision")
            insertions.append(canonical)
            message_ids.append(message_id)
            continue
        stored = dict(zip(columns, row))
        if any(stored[key] != canonical[key] for key in ("source_id", "dataset_id", "conversation_id", "source_record_id")):
            raise PolicyError("ingest_canonical_collision")
        if stored["owner_user_id"] is not None and stored["owner_user_id"] != trusted_context.owner_id:
            raise PolicyError("ingest_canonical_collision")
        if not linked:
            historical_skipped += 1
            continue
        if stored != canonical or not parents[conversation_id]:
            raise PolicyError("ingest_canonical_collision")
        message_ids.append(message_id)

    # Revalidate after preflight, still under the same owner-held transaction.
    trusted_context.require_batch(conn)
    trusted_context.assert_current(conn, source_id=source_id, dataset_id=dataset_id)
    conversations_created = 0
    now = _utc_now()
    for canonical in insertions:
        conversation_id = canonical["conversation_id"]
        if not parents[conversation_id]:
            conn.execute(
                "INSERT INTO conversations (conversation_id,dataset_id,source_id,created_at,updated_at) VALUES (?,?,?,?,?)",
                (conversation_id, dataset_id, source_id, now, now),
            )
            parents[conversation_id] = True
            conversations_created += 1
        conn.execute(
            f"INSERT INTO conversation_messages ({', '.join(columns)}, ingested_at, sync_batch_id) VALUES ({', '.join('?' for _ in columns)}, ?, ?)",
            (*[canonical[key] for key in columns], now, sync_batch_id),
        )
        tag_inserted(conn, "conversation_messages", canonical["message_id"], canonical["content"], present=tag_columns)
        trusted_context.record_insert(conn, canonical["message_id"])
    return {"messages_created": len(insertions), "conversations_created": conversations_created,
            "message_ids": message_ids, "historical_skipped": historical_skipped}


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

    def _retention_refusal(self, message_id: str, record: Dict[str, Any]) -> Optional[CanonicalRef]:
        """A refusal for a message dated before its source's retention floor, else None.

        The floors are read once per store; a store lives for one batch.
        """
        from ...sources.retention import is_below_floor, record_event_time, retention_floors

        floors = getattr(self, "_retention_floors", None)
        if floors is None:
            floors = self._retention_floors = retention_floors(self._conn)
        floor = floors.get(str(record.get("source_id") or ""))
        if floor is None or not is_below_floor(record_event_time(record), floor):
            return None
        return CanonicalRef(record_id=message_id, created=False, refused=REFUSED_RETENTION_FLOOR)

    def _has_event_time_column(self) -> bool:
        cached = getattr(self, "_event_time_column", None)
        if cached is None:
            from ..db.migrations.temporal_fields_v1 import has_column

            cached = self._event_time_column = has_column(self._conn, "conversation_messages", "event_time_json")
        return cached

    def _may_heal(self, existing, record: Dict[str, Any]) -> bool:
        """Whether a re-ingest may replace a stored message body. Skips; never raises.

        Two cases are refused, and the rest of the upsert still happens:

        - the stored row belongs to another dataset or source. Message IDs such as
          ``imessage:<ROWID>`` carry no dataset, so a second database with the same
          ROWID would otherwise rewrite this row's body while it keeps this row's
          owner and authorship;
        - the stored row carries a durable owner-attested provenance link. Its
          proof covers the body, so a legacy heal would silently break the proof
          (fail-closed, but the owner's reviewed row would stop qualifying).
        """
        stored_dataset, stored_source = existing[2], existing[3]
        if (stored_dataset or "") != (record.get("dataset_id") or "") or (stored_source or "") != (record.get("source_id") or ""):
            logger.warning("[PIPELINE:CANONICAL] refused a cross-dataset content heal for %s", existing[0])
            return False
        for table in ("ingest_provenance_records",):
            found = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if found and self._conn.execute(f"SELECT 1 FROM {table} WHERE message_id=?", (existing[0],)).fetchone():
                logger.warning("[PIPELINE:CANONICAL] refused a content heal over an attested row %s", existing[0])
                return False
        return True

    def upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str] = None) -> CanonicalRef:
        # The _upsert_* INSERT takes SQLite's write lock at execute time, so it
        # must run under the same gate hold as the commit (write_gate lock-order
        # inversion). Reentrant, so batch callers already holding the gate nest.
        with with_db_write():
            ref = self._dispatch_upsert(table, record, sync_batch_id=sync_batch_id)
            if table in NSFW_TAGGED_TABLES and not ref.refused:
                # The NSFW tag is decided here, where every canonical write passes, from the text the row
                # actually holds after this upsert (an insert, a heal, a conflict update alike). Before, only
                # the pipeline's privacy stage tagged, and the node's own messenger sync never ran it.
                tag_stored(self._conn, table, ref.record_id, self.__dict__.setdefault("_nsfw_tag_columns", {}))
            self._maybe_commit()
        return ref

    def _dispatch_upsert(self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        if table in WRITER_CLASS_TABLES and self._has_writer_class_column(table) and (
            table != "activity_events" or activity_writer_recording_enabled()
        ):
            return self._upsert_recording_writer(table, record, sync_batch_id=sync_batch_id)
        return self._dispatch_table_upsert(table, record, sync_batch_id=sync_batch_id)

    def _has_writer_class_column(self, table: str) -> bool:
        return "writer_class" in self._writer_columns(table)

    def _writer_columns(self, table: str) -> frozenset:
        """Which of writer_class / writer_app_id / writer_dataset_id this table has."""
        cache = self.__dict__.setdefault("_writer_class_columns", {})
        if table not in cache:
            try:
                names = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            except sqlite3.Error:
                names = set()
            cache[table] = frozenset(names & {"writer_class", "writer_app_id", "writer_dataset_id"})
        return cache[table]

    def _upsert_recording_writer(
        self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str]
    ) -> CanonicalRef:
        """Upsert a row and record which door wrote it; keep the owner's rows the owner's.

        An id carries no writer, and every conflict update here replaces values
        under it. So, before the table's own upsert:

        - a writer that is not an owner class is refused — nothing changes, and
          the ref says why — over a row the owner holds: one an owner door wrote,
          or a row with no writer recorded that the role gate reads as authored
          or addressed by its own table and sender rules (chat speech, journal,
          profile). A legacy document or calendar row is not protected: external
          sync apps arrive unstamped, and refusing them would freeze every sync.
        - an owner door writing an id a non-owner wrote first replaces the row
          outright. Several conflict updates change only some columns, so an
          update would keep a pre-seeded sender, title or organisation under the
          owner's writer class.
        - a record with no writer class is an internal path (reprocess, upgrade
          replay): today's update, and the stored class stays.
        """
        from ...features.provenance.writer_class import is_owner_writer, normalize_writer_class

        id_col = WRITER_CLASS_TABLES[table]
        record_id = str(
            record.get(id_col)
            or (record.get("record_id") if table == "ai_chat_messages" else None)
            or record.get("source_record_id")
            or ""
        )
        if not record_id:
            return self._dispatch_table_upsert(table, record, sync_batch_id=sync_batch_id)
        incoming = normalize_writer_class(record.get("writer_class"))
        stored = self._stored_row(table, id_col, record_id)
        stored_writer = normalize_writer_class(stored.get("writer_class")) if stored else None
        # An export-lane row (live ingest-provenance link) is the lane's: _upsert_ai_chat_message
        # touches only its sync bookkeeping, so no door may record itself as the row's writer either.
        # A valued writer_class is part of the reviewed surface and would stale the owner's review.
        attested = table == "ai_chat_messages" and stored is not None and self._attested_link(record_id)
        if stored is not None and incoming is not None and not attested:
            if not is_owner_writer(incoming) and _owner_holds_row(table, stored):
                changed = sorted(
                    key for key, value in record.items()
                    if key in stored and key not in _ROW_IDENTITY_IGNORED
                    and value is not None and str(value) != str(stored[key])
                )
                if changed:
                    logger.warning(
                        "[PIPELINE:CANONICAL] refused a %s rewrite of an owner-held %s row %s",
                        incoming,
                        table,
                        record_id,
                    )
                return CanonicalRef(
                    record_id=record_id,
                    created=False,
                    refused=REFUSED_OWNER_ROW_REWRITE if changed else REFUSED_OWNER_ROW_DUPLICATE,
                    writer_class=stored_writer,
                )
            if is_owner_writer(incoming) and not is_owner_writer(stored_writer):
                self._conn.execute(f"DELETE FROM {table} WHERE {id_col}=?", (record_id,))
        ref = self._dispatch_table_upsert(table, {**record, "writer_class": incoming}, sync_batch_id=sync_batch_id)
        if attested:
            return dataclasses.replace(ref, writer_class=stored_writer)
        if incoming is not None:
            self._conn.execute(
                f"UPDATE {table} SET writer_class=? WHERE {id_col}=?",
                (incoming, ref.record_id),
            )
            # The app and the dataset travel with the class, as on ai_chat_messages (whose own
            # upsert writes them): a door that records a class records its app and dataset, or
            # none; an internal replay (no class) keeps all three.
            identity = [c for c in ("writer_app_id", "writer_dataset_id") if c in self._writer_columns(table)]
            if identity and table != "ai_chat_messages":
                self._conn.execute(
                    f"UPDATE {table} SET {', '.join(c + '=?' for c in identity)} WHERE {id_col}=?",
                    (*[(str(record.get(c) or "").strip() or None) for c in identity], ref.record_id),
                )
        effective = incoming if incoming is not None else stored_writer
        return dataclasses.replace(ref, writer_class=effective)

    def _stored_row(self, table: str, id_col: str, record_id: str) -> Optional[Dict[str, Any]]:
        cursor = self._conn.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (record_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip([col[0] for col in cursor.description], tuple(row)))

    def _dispatch_table_upsert(
        self, table: str, record: Dict[str, Any], *, sync_batch_id: Optional[str]
    ) -> CanonicalRef:
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
        elif table == "transcripts":
            ref = self._upsert_transcript(record, sync_batch_id=sync_batch_id)
        elif table == "transcript_speakers":
            ref = self._upsert_transcript_speaker(record, sync_batch_id=sync_batch_id)
        elif table == "transcript_segments":
            ref = self._upsert_transcript_segment(record, sync_batch_id=sync_batch_id)
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

    def _attested_link(self, message_id: str) -> bool:
        """Whether an owner-attested ingest lane holds a durable provenance link for this id."""
        found = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_provenance_records'"
        ).fetchone()
        return bool(found) and self._conn.execute(
            "SELECT 1 FROM ingest_provenance_records WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def _upsert_ai_chat_message(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        """Upsert one chat message. Writer-class protection is applied before this
        by ``_upsert_recording_writer``; the class itself is written here too so
        a new row never exists without it."""
        from ...features.provenance.writer_class import normalize_writer_class

        message_id = str(record.get("message_id") or record.get("record_id") or "")
        if not message_id:
            raise ValueError("ai_chat_messages upsert requires message_id")
        writer_class = normalize_writer_class(record.get("writer_class"))
        existing = self._conn.execute(
            "SELECT message_id FROM ai_chat_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if existing is not None and self._attested_link(message_id):
            # The lane's proof covers this row's body, role, conversation, source
            # and origin marker. Any writer re-sending the id (app_ingest, the
            # owner's own extension, a reprocess) may only touch sync bookkeeping;
            # replacing the body would silently turn a proven prompt into text
            # nobody attested.
            logger.warning("[PIPELINE:CANONICAL] refused an ai_chat rewrite over an attested row %s", message_id)
            self._conn.execute(
                "UPDATE ai_chat_messages SET sync_batch_id=COALESCE(?, sync_batch_id), ingested_at=COALESCE(?, ingested_at) "
                "WHERE message_id=?",
                (sync_batch_id or record.get("sync_batch_id"), record.get("ingested_at"), message_id),
            )
            return CanonicalRef(record_id=message_id, created=False)
        self._conn.execute(
            """
            INSERT INTO ai_chat_messages (
                message_id, conversation_id, sender_type, sender_id, event_at,
                content, content_rendered, metadata_json, sequence, source_id,
                source_record_id, ingested_at, sync_batch_id, content_hash, writer_class,
                writer_app_id, writer_dataset_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                content=excluded.content,
                metadata_json=excluded.metadata_json,
                source_id=excluded.source_id,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                writer_class=COALESCE(excluded.writer_class, ai_chat_messages.writer_class),
                -- The app travels with the class: a door that records a class
                -- records its app (or none); an internal replay keeps both.
                writer_app_id=CASE WHEN excluded.writer_class IS NULL
                    THEN ai_chat_messages.writer_app_id ELSE excluded.writer_app_id END,
                -- So does the dataset the door wrote into (RD5).
                writer_dataset_id=CASE WHEN excluded.writer_class IS NULL
                    THEN ai_chat_messages.writer_dataset_id ELSE excluded.writer_dataset_id END
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
                writer_class,
                (str(record.get("writer_app_id") or "").strip() or None) if writer_class is not None else None,
                (str(record.get("writer_dataset_id") or "").strip() or None) if writer_class is not None else None,
            ),
        )
        return CanonicalRef(record_id=message_id, created=existing is None)

    def _upsert_ai_chat_conversation(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        conversation_id = str(record.get("conversation_id") or "")
        if not conversation_id:
            raise ValueError("ai_chat_conversations upsert requires conversation_id")
        existing = self._conn.execute(
            "SELECT conversation_id, owner_user_id FROM ai_chat_conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        incoming_owner = record.get("owner_user_id")
        if (existing is not None and existing[1] not in (None, "") and incoming_owner not in (None, "")
                and str(incoming_owner) != existing[1]):
            # A conversation's owner is who its messages' authorship is bound to.
            # A writer naming someone else (for example a dataset id prefix) is
            # refused outright, bookkeeping included, never merged into it.
            logger.warning("[PIPELINE:CANONICAL] refused an owner re-bind of ai_chat conversation %s", conversation_id)
            return CanonicalRef(record_id=conversation_id, created=False)
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
        """Upsert one messenger/transcript message, keeping the owner's rows the owner's.

        ``is_from_self`` and ``sender_id == 'self'`` are the owner here, and both
        are whatever the writer sent. ``writer_class`` records which door wrote
        the row (``features/provenance/writer_class.py``); the role gate caps a
        non-owner writer. See :meth:`_conversation_writer_gate` for how a write
        under a message id another door already holds is decided. A record with
        no writer class is an internal path (the node's own messenger sync, a
        reprocess replay): it keeps the insert-or-heal below and never changes a
        stored row's class.
        """
        from ...features.provenance.writer_class import normalize_writer_class

        message_id = str(record.get("message_id") or "")
        if not message_id:
            raise ValueError("conversation_messages upsert requires message_id")
        writer_class = normalize_writer_class(record.get("writer_class"))
        refusal = self._retention_refusal(message_id, record) or self._conversation_writer_gate(
            message_id, record, writer_class)
        if refusal is not None:
            return refusal
        existing = self._conn.execute(
            "SELECT message_id, content, dataset_id, source_id FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        dataset_id = record.get("dataset_id") or ""
        supplied_time = record.get("event_at") or record.get("ts")
        event_at = supplied_time or _utc_now()
        # This writer can only vouch for its own fill, and for a fill a caller
        # declared. It never records a native source clock, whatever the record
        # says: only an attested native reader can, and it does not come here.
        substituted = not supplied_time or record.get("_event_time_substituted") is True
        columns = [
            "message_id", "conversation_id", "dataset_id", "event_at", "sender_type", "sender_id",
            "reply_to_message_id", "message_type", "event_type", "content", "source_id",
            "metadata_json", "is_from_self", "owner_user_id",
            "source_record_id", "ingested_at", "sync_batch_id",
        ]
        values = [
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
                # Only a typed flag is the owner: declared rows carry text, and "0"/"false" are truthy.
                1 if any(record.get(key) is True or (type(record.get(key)) is int and record.get(key) == 1)
                         for key in ("is_from_self", "from_self")) else 0,
                record.get("owner_user_id"),
                record.get("source_record_id") or message_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
        ]
        if self._has_event_time_column():
            from ...features.temporal.records import event_time

            columns.append("event_time_json")
            values.append(event_time(event_at, provenance="ingestion_clock_substitute" if substituted
                                     else "unverified_producer").to_json())
        self._conn.execute(
            f"INSERT OR IGNORE INTO conversation_messages ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        if writer_class is not None and existing is None:
            self._conn.execute(
                "UPDATE conversation_messages SET writer_class=? WHERE message_id=?",
                (writer_class, message_id),
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
            if incoming and str(incoming) != (existing[1] or "") and self._may_heal(existing, record):
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
                # exists. The PII disclosure sweep (disclosure_sweep) refills them.
                logger.debug(
                    "[PIPELINE:CANONICAL] healed conversation_messages.content for %s", message_id
                )
        return CanonicalRef(record_id=message_id, created=existing is None)

    def _conversation_writer_gate(
        self,
        message_id: str,
        record: Dict[str, Any],
        writer_class: Optional[str],
    ) -> Optional[CanonicalRef]:
        """Decide a door's write under a message id that already has a row.

        - A non-owner writer over a row an owner door wrote, or one that predates
          writer classes, changes nothing: not the body (the heal), not the batch
          or ingest time. The ref says why, and callers keep the text away from
          enrichment. Otherwise a grantee could put its own words under the
          owner's authored row.
        - An owner door over a row a non-owner wrote takes the message id: the
          non-owner row is deleted and the insert writes the owner's fresh,
          sender included. Keeping the seeded sender would make someone else's
          line the owner's; keeping the seeded writer would demote the owner's.

        Returns None when the write should proceed. A record with no writer
        class is never gated.
        """
        from ...features.provenance.writer_class import is_owner_writer

        if writer_class is None:
            return None
        stored = self._conn.execute(
            "SELECT writer_class, content FROM conversation_messages WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if stored is None:
            return None
        stored_by_owner = is_owner_writer(stored[0])
        if not is_owner_writer(writer_class) and stored_by_owner:
            # Same test as the heal: an empty or identical body would change nothing.
            incoming = record.get("content")
            rewrite = bool(incoming) and str(incoming) != (stored[1] or "")
            if rewrite:
                logger.warning(
                    "[PIPELINE:CANONICAL] refused a %s rewrite of owner-written conversation_messages row %s",
                    writer_class,
                    message_id,
                )
            return CanonicalRef(
                record_id=message_id,
                created=False,
                refused=REFUSED_OWNER_ROW_REWRITE if rewrite else REFUSED_OWNER_ROW_DUPLICATE,
            )
        if is_owner_writer(writer_class) and not stored_by_owner:
            self._conn.execute("DELETE FROM conversation_messages WHERE message_id=?", (message_id,))
        return None

    def _upsert_activity_event(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        """Upsert one activity row. Writer-class protection is applied before this by
        ``_upsert_recording_writer``; the class, app and dataset are written here too
        (activity_writer_columns_v1) so a new row never exists without them. All three
        only while ``ACTIVITY_WRITER_FLAG`` is on (the default)."""
        from ...features.provenance.writer_class import normalize_writer_class

        event_id = str(record.get("event_id") or record.get("source_record_id") or "")
        if not event_id:
            raise ValueError("activity_events upsert requires event_id")
        door = normalize_writer_class(record.get("writer_class"))
        writer_class = door if activity_writer_recording_enabled() else None
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
                content, hostname, writer_class, writer_app_id, writer_dataset_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                title=excluded.title,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=COALESCE(excluded.metadata_json, activity_events.metadata_json),
                content=COALESCE(excluded.content, activity_events.content),
                hostname=COALESCE(excluded.hostname, activity_events.hostname),
                writer_class=COALESCE(excluded.writer_class, activity_events.writer_class),
                -- As on ai_chat_messages: the app and the dataset travel with the
                -- class. A door records its own (or none); an internal replay keeps both.
                writer_app_id=CASE WHEN excluded.writer_class IS NULL
                    THEN activity_events.writer_app_id ELSE excluded.writer_app_id END,
                writer_dataset_id=CASE WHEN excluded.writer_class IS NULL
                    THEN activity_events.writer_dataset_id ELSE excluded.writer_dataset_id END
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
                writer_class,
                _text_or_none(record.get("writer_app_id")) if writer_class is not None else None,
                _text_or_none(record.get("writer_dataset_id")) if writer_class is not None else None,
            ),
        )
        if existing is not None and door is not None and writer_class is None:
            # Switched off, a door's write is not recorded; a writer recorded while the
            # switch was on would now name a door whose values this write replaced.
            self._conn.execute(
                "UPDATE activity_events SET writer_class=NULL, writer_app_id=NULL, writer_dataset_id=NULL "
                "WHERE event_id=?",
                (event_id,),
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

    def _journal_event_time_column(self) -> bool:
        cached = self.__dict__.get("_journal_event_time")
        if cached is None:
            try:
                names = {row[1] for row in self._conn.execute("PRAGMA table_info(journal_entries)").fetchall()}
            except sqlite3.Error:
                names = set()
            cached = self.__dict__["_journal_event_time"] = "event_time_json" in names
        return cached

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
        entry_at = self._journal_entry_at(record, ingested_at)
        if self._journal_event_time_column():
            # OD-53: a door write through a source that declares its zone records when the row
            # happened. `declared_time_zone` is the pipeline's, from the source definition; a
            # record never outlives the time text it was computed from, and a write that names
            # no zone (an internal replay, an undeclared source) keeps the one already stored.
            from ...features.temporal.records import declared_zone_event_time

            zone = record.get("declared_time_zone")
            self._conn.execute(
                "UPDATE journal_entries SET event_time_json=NULL WHERE entry_id=? AND entry_at IS NOT ?",
                (entry_id, entry_at),
            )
            stated = declared_zone_event_time(entry_at, zone) if zone else None
        else:
            stated = None
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
                entry_at,
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
        if stated is not None:
            # Written once per time text: a re-send of the same entry keeps the record it has.
            self._conn.execute(
                "UPDATE journal_entries SET event_time_json=? WHERE entry_id=? AND event_time_json IS NULL",
                (stated, entry_id),
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

    def _upsert_transcript(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        transcript_id = str(record.get("transcript_id") or record.get("source_record_id") or "")
        if not transcript_id:
            raise ValueError("transcripts upsert requires transcript_id")
        existing = self._conn.execute(
            "SELECT transcript_id FROM transcripts WHERE transcript_id=?",
            (transcript_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO transcripts (
                transcript_id, dataset_id, title, origin_url, origin_kind,
                started_at, ended_at, duration_sec, language_code, asr_model,
                asr_quality, is_generated, media_ref, participation_mode,
                source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(transcript_id) DO UPDATE SET
                title=excluded.title,
                origin_url=excluded.origin_url,
                origin_kind=excluded.origin_kind,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                duration_sec=excluded.duration_sec,
                language_code=excluded.language_code,
                asr_model=excluded.asr_model,
                asr_quality=excluded.asr_quality,
                is_generated=excluded.is_generated,
                media_ref=excluded.media_ref,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                transcript_id,
                record.get("dataset_id"),
                record.get("title"),
                record.get("origin_url"),
                record.get("origin_kind"),
                record.get("started_at"),
                record.get("ended_at"),
                record.get("duration_sec"),
                record.get("language_code"),
                record.get("asr_model"),
                record.get("asr_quality") or "unknown",
                record.get("is_generated"),
                record.get("media_ref"),
                "ambient",
                record.get("source_id"),
                record.get("source_record_id") or transcript_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=transcript_id, created=existing is None)

    def _upsert_transcript_speaker(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        speaker_id = str(record.get("speaker_id") or record.get("source_record_id") or "")
        if not speaker_id:
            raise ValueError("transcript_speakers upsert requires speaker_id")
        existing = self._conn.execute(
            "SELECT speaker_id FROM transcript_speakers WHERE speaker_id=?",
            (speaker_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO transcript_speakers (
                speaker_id, transcript_id, dataset_id, label, display_name,
                contact_id, is_owner, attribution_source, attribution_confidence,
                source_id, source_record_id, ingested_at, sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(speaker_id) DO UPDATE SET
                label=excluded.label,
                display_name=excluded.display_name,
                attribution_source=excluded.attribution_source,
                attribution_confidence=excluded.attribution_confidence,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                speaker_id,
                record.get("transcript_id"),
                record.get("dataset_id"),
                record.get("label"),
                record.get("display_name"),
                None,
                0,
                record.get("attribution_source"),
                record.get("attribution_confidence"),
                record.get("source_id"),
                record.get("source_record_id") or speaker_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=speaker_id, created=existing is None)

    def _upsert_transcript_segment(self, record: Dict[str, Any], *, sync_batch_id: Optional[str]) -> CanonicalRef:
        segment_id = str(record.get("segment_id") or record.get("source_record_id") or "")
        if not segment_id:
            raise ValueError("transcript_segments upsert requires segment_id")
        existing = self._conn.execute(
            "SELECT segment_id FROM transcript_segments WHERE segment_id=?",
            (segment_id,),
        ).fetchone()
        self._conn.execute(
            """
            INSERT INTO transcript_segments (
                segment_id, transcript_id, dataset_id, speaker_id, speaker_label,
                content, start_sec, duration_sec, event_at, actor_role, is_from_self,
                asr_confidence, source_id, source_record_id, ingested_at,
                sync_batch_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(segment_id) DO UPDATE SET
                content=excluded.content,
                speaker_id=excluded.speaker_id,
                speaker_label=excluded.speaker_label,
                start_sec=excluded.start_sec,
                duration_sec=excluded.duration_sec,
                event_at=excluded.event_at,
                sync_batch_id=excluded.sync_batch_id,
                ingested_at=excluded.ingested_at,
                metadata_json=excluded.metadata_json
            """,
            (
                segment_id,
                record.get("transcript_id"),
                record.get("dataset_id"),
                record.get("speaker_id"),
                record.get("speaker_label"),
                record.get("content"),
                record.get("start_sec"),
                record.get("duration_sec"),
                record.get("event_at"),
                "ambient",
                0,
                record.get("asr_confidence"),
                record.get("source_id"),
                record.get("source_record_id") or segment_id,
                record.get("ingested_at") or _utc_now(),
                sync_batch_id or record.get("sync_batch_id"),
                _json_metadata(record.get("metadata_json")),
            ),
        )
        return CanonicalRef(record_id=segment_id, created=existing is None)

    def drop_stale_transcript_segments(
        self, transcript_id: str, keep_ids: set[str]
    ) -> List[str]:
        """Delete segments for ``transcript_id`` that are not in the new keep set.

        Caption stitch changes segment ids (index → start_ms). A re-ingest
        that only upserts would leave the old fragments in place.
        """
        transcript_id = str(transcript_id or "").strip()
        if not transcript_id:
            return []
        existing = [
            str(row[0])
            for row in self._conn.execute(
                "SELECT segment_id FROM transcript_segments WHERE transcript_id=?",
                (transcript_id,),
            ).fetchall()
            if row[0]
        ]
        keep = {str(item) for item in keep_ids}
        stale = [sid for sid in existing if sid not in keep]
        if not stale:
            return []
        with with_db_write():
            for start in range(0, len(stale), 400):
                chunk = stale[start : start + 400]
                placeholders = ",".join("?" * len(chunk))
                self._conn.execute(
                    f"DELETE FROM transcript_segments WHERE segment_id IN ({placeholders})",
                    chunk,
                )
            self._maybe_commit()
        return stale


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
            or record.get("transcript_id")
            or record.get("segment_id")
            or record.get("speaker_id")
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
