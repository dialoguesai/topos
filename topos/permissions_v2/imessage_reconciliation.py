"""Bounded native/canonical comparison; deliberately confers no release authority.

No service calls this module from a recipient route. Native staging and matches
are observations, not provenance links. Historical collision and owner-binding
checks in the existing ingest/evidence lanes are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from topos.ingestion.owner_snapshot import (
    MAX_MESSAGES, SnapshotRejected, _identifier, parse_imessage_snapshot, parse_imessage_attributed_snapshot,
)
from .canonical import PolicyError, parse_json
from .evidence import _row_revision
from .fact_eligibility import canonical_utc_microseconds

CONTRACT = "imessage-existing-comparison/v1"
ATTRIBUTED_CONTRACT = "imessage-existing-comparison/v2"


@dataclass(frozen=True)
class NativeMessage:
    snapshot_sha256: str
    message_id: str
    conversation_id: str
    message_guid: str
    chat_guid: str
    chat_identifier: str
    event_at: str
    is_from_self: bool
    content: str = field(repr=False)
    reader_contract: str = CONTRACT
    native_event_nanoseconds: int | None = None


@dataclass(frozen=True)
class NativeMatch:
    """An exact comparison result. No owner attestation or permission is implied."""
    contract: str
    snapshot_sha256: str
    canonical_revision: str


def parse_reconciliation_snapshot(data: bytes, *, now: datetime, reader_contract=CONTRACT) -> tuple[NativeMessage, ...]:
    """The strict native reader plus GUID/conversation correspondence metadata.

    The fixed placeholder dataset is never returned and never selects a canonical
    context. A separate comparison requires the actual dataset explicitly.
    """
    if reader_contract not in (CONTRACT, ATTRIBUTED_CONTRACT):
        raise SnapshotRejected('snapshot_reader_unsupported')
    parser = parse_imessage_snapshot if reader_contract == CONTRACT else parse_imessage_attributed_snapshot
    records = parser(data, "native-comparison", now=now)
    snapshot_sha = hashlib.sha256(data).hexdigest()
    path, db = None, None
    try:
        fd, name = tempfile.mkstemp(prefix="topos_native_compare_", suffix=".db")
        path = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        columns = {table: {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                   for table in ("message", "chat")}
        if "guid" not in columns["message"] or not {"guid", "chat_identifier"} <= columns["chat"]:
            raise SnapshotRejected("snapshot_correspondence_missing")
        # Native forms the original reader predates must not be dropped while
        # reconciling old canonical rows, whose metadata may have lost them.
        zero_columns = {"group_action_type", "is_forward", "is_forwarded", "is_spam"} & columns["message"]
        empty_columns = {"quoted_message_guid", "forwarded_from", "reply_to_guid"} & columns["message"]
        for column in sorted(zero_columns | empty_columns):
            for (value,) in db.execute(f'SELECT "{column}" FROM message LIMIT 1001'):
                valid = value in (None, "") if column in empty_columns else value is None or (type(value) is int and value == 0)
                if not valid:
                    raise SnapshotRejected("snapshot_message_form_unsupported")
        deadline = time.monotonic() + 5
        steps = 0
        def budget():
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 or time.monotonic() > deadline)
        db.set_progress_handler(budget, 1000)
        db.set_authorizer(lambda action, _a, _b, _c, _d:
            sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ) else sqlite3.SQLITE_DENY)
        native = list(db.execute("SELECT m.ROWID,m.guid,c.guid,c.chat_identifier "
            "FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID "
            "JOIN chat c ON c.ROWID=j.chat_id ORDER BY m.ROWID LIMIT 1001"))
        if len(native) != len(records) or len(native) > MAX_MESSAGES:
            raise SnapshotRejected("snapshot_conversation_ambiguous")
        seen, result = set(), []
        for record, (rowid, guid, chat_guid, chat_identifier) in zip(records, native):
            if (record["message_id"] != f"imessage:{rowid}"
                    or not all(_identifier(v) for v in (guid, chat_guid, chat_identifier))
                    or guid.casefold() in seen):
                raise SnapshotRejected("snapshot_correspondence_ambiguous")
            seen.add(guid.casefold())
            result.append(NativeMessage(snapshot_sha, record["message_id"], record["conversation_id"],
                guid, chat_guid, chat_identifier, record["ts"], record["is_from_self"], record["content"], reader_contract,
                record.get('native_event_nanoseconds')))
        return tuple(result)
    except SnapshotRejected:
        raise
    except (sqlite3.Error, OSError, UnicodeError, ValueError):
        raise SnapshotRejected("snapshot_correspondence_invalid") from None
    finally:
        if db is not None:
            db.close()
        if path is not None:
            try:
                path.unlink()
            except OSError:
                raise SnapshotRejected("snapshot_cleanup_failed") from None


_EMPTY_METADATA = frozenset({"thread_originator_guid", "thread_originator_part", "associated_message_guid"})
_ZERO_METADATA = frozenset({"associated_message_type", "item_type", "group_action_type"})
_IDENTITY_METADATA = frozenset({"message_guid", "chat_guid", "chat_identifier"})


def compare_existing_message(row: dict, native: NativeMessage, *, dataset_id: str, owner_id: str) -> NativeMatch:
    """Compare one current canonical row without completing or mutating it.

    The caller is responsible for proving an unambiguous native/canonical lookup,
    snapshot ownership and durable provenance. This pure function proves none of
    those. Its result cannot bypass EvidenceResolver's owner/source checks.
    """
    def refuse(code):
        raise PolicyError("reconciliation_" + code)
    if type(row) is not dict or type(native) is not NativeMessage or not _identifier(dataset_id) or not _identifier(owner_id):
        refuse("input_invalid")
    if native.is_from_self is not True or type(row.get("is_from_self")) is not int or row["is_from_self"] != 1:
        refuse("not_owner_sent")
    if row.get("owner_user_id") not in (None, owner_id):
        refuse("owner_conflict")
    if row.get("source_id") != "imessage" or row.get("dataset_id") != dataset_id:
        refuse("source_binding")
    if (row.get("message_id") != native.message_id or row.get("source_record_id") != native.message_id
            or row.get("conversation_id") != native.conversation_id):
        refuse("native_identity")
    if row.get("sender_id") != "self" or row.get("sender_type") != "human" or row.get("actor_role") not in (None, "authored"):
        refuse("sender_conflict")
    if (row.get("message_type") not in (None, "message") or row.get("event_type") not in (None, "")
            or row.get("reply_to_message_id") not in (None, "")):
        refuse("message_form")
    if type(row.get("content")) is not str or row["content"] != native.content:
        refuse("content_mismatch")
    actual_time, expected_time = canonical_utc_microseconds(row.get("event_at")), canonical_utc_microseconds(native.event_at)
    if native.reader_contract == ATTRIBUTED_CONTRACT:
        from topos.ingestion.owner_snapshot import _event_time_nanoseconds
        value = native.native_event_nanoseconds
        if type(value) is not int or not 10**17 <= value <= 2**63 - 1:
            refuse('time_mismatch')
        # The old sync wrote a specific float conversion. Verify that exact
        # transformation rather than accepting an arbitrary timestamp tolerance.
        # Keep the native nanoseconds separately: a future release adapter must
        # enforce grant time bounds against them, not this rounded legacy cell.
        if native.event_at != _event_time_nanoseconds(value, datetime.max.replace(tzinfo=timezone.utc)):
            refuse('time_mismatch')
        converted = datetime.fromtimestamp(float(value) / 1_000_000_000.0 + 978307200, tz=timezone.utc)
        expected_time = canonical_utc_microseconds(converted.isoformat(timespec='microseconds'))
    if actual_time is None or expected_time is None or actual_time != expected_time:
        refuse("time_mismatch")
    try:
        metadata = parse_json(row.get("metadata_json"))
    except (PolicyError, TypeError):
        refuse("metadata_invalid")
    if type(metadata) is not dict or not set(metadata) <= _EMPTY_METADATA | _ZERO_METADATA | _IDENTITY_METADATA:
        refuse("metadata_unsupported")
    if any(metadata.get(key) != getattr(native, key) for key in _IDENTITY_METADATA):
        refuse("native_identity")
    if (any(metadata.get(key) not in (None, "") for key in _EMPTY_METADATA)
            or any(key in metadata and (type(metadata[key]) is not int or metadata[key] != 0) for key in _ZERO_METADATA)):
        refuse("message_form")
    if native.reader_contract not in (CONTRACT, ATTRIBUTED_CONTRACT):
        refuse('reader_unsupported')
    return NativeMatch(native.reader_contract, native.snapshot_sha256, _row_revision(row, table="conversation_messages"))


def preflight_existing_snapshot(conn: sqlite3.Connection, data: bytes, *, dataset_id: str,
        owner_id: str, starts_at: str, ends_at: str, now: datetime) -> dict:
    """Count native comparisons in one caller-owned read-only canonical snapshot.

    No IDs, hashes, bodies or matching records escape. Even a positive count is
    only a comparison: no fact qualification, enrollment or grant is performed.
    The maximum window is 31 days and the native reader caps input at 1,000 rows.
    """
    start, end = canonical_utc_microseconds(starts_at), canonical_utc_microseconds(ends_at)
    if (start is None or end is None or start >= end or end - start > 31 * 86400 * 1_000_000
            or not _identifier(dataset_id) or not _identifier(owner_id)):
        raise PolicyError("reconciliation_window_invalid")
    # Validate the native clock through the strict reader before using it here.
    native = parse_reconciliation_snapshot(data, now=now)
    if end > canonical_utc_microseconds(now.isoformat(timespec="microseconds")):
        raise PolicyError("reconciliation_window_invalid")
    if not conn.in_transaction or conn.execute("PRAGMA query_only").fetchone()[0] != 1:
        raise PolicyError("reconciliation_read_snapshot_required")
    table = conn.execute("SELECT type FROM sqlite_master WHERE name='conversation_messages'").fetchone()
    if table is None or table[0] != 'table':
        raise PolicyError("reconciliation_canonical_schema")
    counts = Counter()
    for message in native:
        event = canonical_utc_microseconds(message.event_at)
        if event is None or not start <= event < end:
            counts['outside_window'] += 1
            continue
        cursor = conn.execute('SELECT * FROM conversation_messages WHERE message_id=? LIMIT 2', (message.message_id,))
        columns = [column[0] for column in cursor.description]
        found = cursor.fetchall()
        if len(found) != 1:
            counts['canonical_missing' if not found else 'canonical_ambiguous'] += 1
            continue
        row = dict(zip(columns, found[0]))
        try:
            compare_existing_message(row, message, dataset_id=dataset_id, owner_id=owner_id)
            counts['matched'] += 1
        except PolicyError as exc:
            counts[exc.code] += 1
    return {'contract': CONTRACT, 'native_messages': len(native), 'counts': dict(sorted(counts.items())),
            'authority_created': False}
