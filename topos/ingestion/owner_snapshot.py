"""Bounded owner-attested iMessage snapshot ingestion; never a live reader.

``imessage-owner-snapshot/v1`` accepts ordinary, unambiguous plain-text native
messages. It fixes Apple's modern nanosecond epoch and requires exact
microsecond representation; older units and sub-microsecond values withhold.
It does not infer an Apple account, classify content, run enrichment or the
LLM fact pass, or make the resulting records permission-qualified. The one
derivation it runs is the rules fact floor over the rows the job itself linked
(IngestProvenanceService.derive_owner_facts), in the same transaction. Durable
authority is supplied only by IngestProvenanceService, independently of the
snapshot's fields.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

READER_CONTRACT = "imessage-owner-snapshot/v1"
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_MESSAGES = 1000
MAX_TEXT_BYTES = 64 * 1024
MAX_TOTAL_TEXT_BYTES = 1024 * 1024
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_REQUIRED = {
    "message": {
        "ROWID", "text", "date", "handle_id", "is_from_me", "subject",
        "attributedBody", "associated_message_guid", "associated_message_type",
        "cache_has_attachments", "item_type",
    },
    "chat": {"ROWID"},
    "handle": {"ROWID", "id"},
    "chat_message_join": {"chat_id", "message_id"},
}


class SnapshotRejected(ValueError):
    """Content-free reason from the closed reader contract."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(code: str) -> None:
    raise SnapshotRejected(code)


def _identifier(value: Any) -> bool:
    return (type(value) is str and 0 < len(value) <= 512 and value == value.strip()
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def _positive_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


def _event_time(value: Any, now: datetime) -> str:
    # No magnitude-based conversion: every accepted value has one fixed unit.
    # The lower bound excludes old seconds/milliseconds/microseconds layouts.
    if type(value) is not int or not 10**17 <= value <= 2**63 - 1 or value % 1000:
        _reject("snapshot_time_unsupported")
    event = _MAC_EPOCH + timedelta(microseconds=value // 1000)
    if event > now:
        _reject("snapshot_time_future")
    return event.isoformat(timespec="microseconds")


def parse_imessage_snapshot(data: bytes, dataset_id: str, *, now: datetime) -> list[dict[str, Any]]:
    """Parse immutable snapshot bytes. Returned staging is not authority.

    Only the four named ordinary native tables are queried. Views, ambiguous
    joins, unsupported message forms, malformed flags/times, and excessive
    bodies reject the entire snapshot before canonical writes.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_SNAPSHOT_BYTES:
        _reject("snapshot_size_unsupported")
    if not _identifier(dataset_id):
        _reject("snapshot_binding_invalid")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        _reject("snapshot_clock_invalid")
    # Python 3.10 has no Connection.deserialize. Only these already verified
    # immutable bytes enter the private temporary file; native paths are never
    # accepted here. Immutable read-only mode prevents journal/WAL access.
    db, snapshot_path = None, None
    try:
        fd, snapshot_path = tempfile.mkstemp(prefix="topos_owner_snapshot_", suffix=".db")
        with os.fdopen(fd, "wb") as snapshot:
            snapshot.write(data)
        db = sqlite3.connect(Path(snapshot_path).as_uri() + "?mode=ro&immutable=1", uri=True)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        if hasattr(db, "setlimit"):
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SNAPSHOT_BYTES)
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 32 * 1024)
            db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 1024)
        deadline, steps = time.monotonic() + 5.0, 0

        def budget() -> int:
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 or time.monotonic() > deadline)

        db.set_progress_handler(budget, 1000)
        schema = {r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN ('message','chat','handle','chat_message_join')"
        )}
        message_columns: set[str] = set()
        for table, required in _REQUIRED.items():
            definition = schema.get(table)
            if (not definition or definition[0] != "table" or type(definition[1]) is not str
                    or not definition[1].lstrip().upper().startswith("CREATE TABLE")):
                _reject("snapshot_schema_unsupported")
            columns = list(db.execute(f'PRAGMA table_info("{table}")'))
            if table == "message":
                message_columns = {r[1] for r in columns}
            if not required.issubset({r[1] for r in columns}):
                _reject("snapshot_schema_unsupported")
            if table != "chat_message_join" and not any(
                r[1] == "ROWID" and str(r[2]).upper() == "INTEGER" and r[5] == 1 for r in columns
            ):
                _reject("snapshot_schema_unsupported")
        # Allow read-only fixed queries only after setup; no extension, ATTACH,
        # writes, or invocation of functions from malicious schema objects.
        db.set_authorizer(lambda action, _a, _b, _c, _d:
                          sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ)
                          else sqlite3.SQLITE_DENY)
        # These native columns identify unsupported replies/deleted/system
        # records on schema versions that have them. Absence is not replaced
        # by a derived reply, timestamp, or authorship guess.
        for column in ("thread_originator_guid", "thread_originator_part", "is_deleted", "is_system_message", "is_service_message"):
            if column not in message_columns:
                continue
            for (value,) in db.execute(f'SELECT "{column}" FROM message LIMIT 1001'):
                if column in ("thread_originator_guid", "thread_originator_part"):
                    if value not in (None, ""):
                        _reject("snapshot_message_form_unsupported")
                elif value is not None and (type(value) is not int or value != 0):
                    _reject("snapshot_message_form_unsupported")
        native = list(db.execute("""SELECT ROWID,text,date,handle_id,is_from_me,subject,attributedBody,
            associated_message_guid,associated_message_type,cache_has_attachments,item_type
            FROM message ORDER BY ROWID LIMIT 1001"""))
        if len(native) > MAX_MESSAGES:
            _reject("snapshot_message_limit")
        native_ids = {row[0] for row in native}
        joins: dict[int, int] = {}
        for message_id, chat_id in db.execute("SELECT message_id,chat_id FROM chat_message_join LIMIT 1001"):
            if (not _positive_id(message_id) or not _positive_id(chat_id)
                    or message_id not in native_ids or message_id in joins):
                _reject("snapshot_conversation_ambiguous")
            joins[message_id] = chat_id
        records: list[dict[str, Any]] = []
        text_bytes = 0
        for row in native:
            rowid, content, date, handle_id, from_me, subject, attributed, associated, reaction, attachments, item = row
            if not _positive_id(rowid) or type(from_me) is not int or from_me not in (0, 1):
                _reject("snapshot_native_identity_invalid")
            if (subject not in (None, "") or attributed is not None or associated not in (None, "")
                    or any(type(flag) is not int or flag != 0 for flag in (reaction, attachments, item))):
                _reject("snapshot_message_form_unsupported")
            if type(content) is not str or not content.strip() or "\x00" in content:
                _reject("snapshot_text_unsupported")
            try:
                size = len(content.encode("utf-8"))
            except UnicodeError:
                _reject("snapshot_text_unsupported")
            text_bytes += size
            if size > MAX_TEXT_BYTES or text_bytes > MAX_TOTAL_TEXT_BYTES:
                _reject("snapshot_text_limit")
            if rowid not in joins:
                _reject("snapshot_conversation_ambiguous")
            chat_id = joins[rowid]
            if db.execute("SELECT ROWID FROM chat WHERE ROWID=?", (chat_id,)).fetchone() is None:
                _reject("snapshot_conversation_missing")
            if from_me == 1:
                sender_id = "self"
            else:
                if not _positive_id(handle_id):
                    _reject("snapshot_sender_missing")
                handle = db.execute("SELECT id FROM handle WHERE ROWID=?", (handle_id,)).fetchone()
                if not handle or not _identifier(handle[0]) or handle[0].casefold() == "self":
                    _reject("snapshot_sender_missing")
                sender_id = handle[0]
            records.append({
                "message_id": f"imessage:{rowid}", "source_record_id": f"imessage:{rowid}",
                "dataset_id": dataset_id, "source_id": "imessage", "thread_id": str(chat_id),
                "conversation_id": str(chat_id), "ts": _event_time(date, now),
                "sender_type": "human", "sender_id": sender_id,
                "from_self": from_me == 1, "is_from_self": from_me == 1,
                "message_type": "message", "content": content,
            })
        return records
    except SnapshotRejected:
        raise
    except (sqlite3.Error, UnicodeError, ValueError, OverflowError, OSError):
        raise SnapshotRejected("snapshot_database_invalid") from None
    finally:
        if db is not None:
            db.close()
        if snapshot_path is not None:
            try:
                os.unlink(snapshot_path)
            except OSError:
                raise SnapshotRejected("snapshot_cleanup_failed") from None


async def run_snapshot_job(service: Any, conn_factory: Callable[[], Any], job_id: str) -> dict[str, Any]:
    """Run one separately enrolled job with a worker-owned explicit connection.

    Claim and failure receipts use service CAS. Canonical writes and completion
    share ``ctx.batch``; a completion failure therefore rolls back all rows.
    A disconnected caller does not revoke the separately durable owner job.
    """
    def run() -> dict[str, Any]:
        conn, context = None, None
        try:
            conn = conn_factory()
            if conn is None:
                _reject("snapshot_database_unavailable")
            context = service.claim(conn, job_id)
            data = service.snapshot_bytes(conn, context)
            service.assert_current(conn, context, source_id="imessage", dataset_id=context.dataset_id)
            records = parse_imessage_snapshot(data, context.dataset_id, now=datetime.now(timezone.utc))
            from ..storage.canonical import ConversationsTablesManager

            manager = ConversationsTablesManager(conn)
            # The enrolled node has initialized canonical schemas. The trusted
            # manager validates those schemas and never runs migrations here.
            with context.batch(conn):
                service.assert_current(conn, context, source_id="imessage", dataset_id=context.dataset_id)
                counts = manager.upsert_message_batch(
                    records, context.dataset_id, "imessage", trusted_context=context
                )
                result: dict[str, Any] = {"status": "ok", "messages_processed": len(records)}
                for field in ("messages_created", "conversations_created", "historical_skipped"):
                    value = counts.get(field)
                    if type(value) is not int or not 0 <= value <= len(records):
                        _reject("snapshot_result_invalid")
                    result[field] = value
                # Before completion and in the same batch: a crash or failure
                # here leaves no row, link or fact behind, and no finished job.
                service.derive_owner_facts(conn, context)
                service.finish(conn, context, result)
            return result
        except Exception as error:
            reason = error.reason_code if isinstance(error, SnapshotRejected) else "snapshot_job_unavailable"
            if conn is not None and context is not None:
                try:
                    service.fail(conn, context, reason)
                except Exception:
                    # A stale worker must not overwrite the newer claim's state.
                    pass
            return {"status": "error", "reason_code": reason}
        finally:
            if conn is not None:
                conn.close()

    return await asyncio.to_thread(run)
