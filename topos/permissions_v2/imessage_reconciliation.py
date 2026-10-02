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
    FORMS_SLICES, MAX_MESSAGES, SnapshotRejected, _identifier, parse_imessage_snapshot,
    parse_imessage_attributed_snapshot, parse_imessage_forms_snapshot,
)
from .canonical import PolicyError, parse_json
from .evidence import _row_revision
from .fact_eligibility import canonical_utc_microseconds

CONTRACT = "imessage-existing-comparison/v1"
ATTRIBUTED_CONTRACT = "imessage-existing-comparison/v2"
# v3 reads what v2 reads, and two more forms of the owner's own sent text that v2 withheld:
# - A message Messages chained to the one before it. `reply_to_guid` is that chain: Messages writes it on
#   ordinary messages, the owner did not choose it, and it names another message without carrying a word
#   of it. v2 refused every row that had one; v3 neither requires nor compares it.
# - An inline reply (`thread_originator_guid`, with the part of the originator it answers). Its text is
#   only what the owner typed. v3 accepts it when the stored row names the same originator and part.
# - An attachment with a caption (`cache_has_attachments` 1, text besides the placeholders). The caption is
#   the owner's words; the attachment is not read. v3 accepts the row only when the stored body is the
#   caption exactly as the sync stores it (`caption_text`): no placeholder, so nothing about the attachment
#   (that there was one, its file, name or type) is in what a grant can release.
# Reactions, forwards and quotes, subjects, attachments without a caption, system, deleted and spam rows are
# withheld as before: each either carries someone else's words or carries none of the owner's.
FORMS_CONTRACT = "imessage-existing-comparison/v3"
# The readers whose exact matches can become private proof of an existing row.
RECONCILIATION_CONTRACTS = (ATTRIBUTED_CONTRACT, FORMS_CONTRACT)
_PARSERS = {CONTRACT: parse_imessage_snapshot, ATTRIBUTED_CONTRACT: parse_imessage_attributed_snapshot,
            FORMS_CONTRACT: parse_imessage_forms_snapshot}


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
    # v3 only: the inline reply this message is, or both None. Never set under v1 or v2.
    thread_originator_guid: str | None = None
    thread_originator_part: str | None = None
    # v3 only: a sent attachment read for its caption; `content` is then the native body with its
    # placeholders, and the stored row must hold `caption_text(content)`. Never True under v1 or v2.
    attachment_caption: bool = False


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
    if type(reader_contract) is not str or reader_contract not in _PARSERS:
        raise SnapshotRejected('snapshot_reader_unsupported')
    records = _PARSERS[reader_contract](data, "native-comparison", now=now)
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
        # v3 reads one capture of the longest grant window (owner_snapshot.FORMS_SLICES native reads' worth);
        # every statement here reads one row past that bound, so no row escapes a check.
        slices = FORMS_SLICES if reader_contract == FORMS_CONTRACT else 1
        limit = MAX_MESSAGES * slices + 1
        zero_columns = {"group_action_type", "is_forward", "is_forwarded", "is_spam"} & columns["message"]
        empty_columns = {"quoted_message_guid", "forwarded_from", "reply_to_guid"} & columns["message"]
        if reader_contract == FORMS_CONTRACT:
            # Messages' own chain to the preceding message is not a form of the message (see FORMS_CONTRACT).
            empty_columns.discard("reply_to_guid")
        for column in sorted(zero_columns | empty_columns):
            for (value,) in db.execute(f'SELECT "{column}" FROM message LIMIT ?', (limit,)):
                valid = value in (None, "") if column in empty_columns else value is None or (type(value) is int and value == 0)
                if not valid:
                    raise SnapshotRejected("snapshot_message_form_unsupported")
        deadline = time.monotonic() + 5 * slices
        steps = 0
        def budget():
            nonlocal steps
            steps += 1000
            return int(steps > 2_000_000 * slices or time.monotonic() > deadline)
        db.set_progress_handler(budget, 1000)
        db.set_authorizer(lambda action, _a, _b, _c, _d:
            sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ) else sqlite3.SQLITE_DENY)
        native = list(db.execute("SELECT m.ROWID,m.guid,c.guid,c.chat_identifier "
            "FROM message m JOIN chat_message_join j ON j.message_id=m.ROWID "
            "JOIN chat c ON c.ROWID=j.chat_id ORDER BY m.ROWID LIMIT ?", (limit,)))
        if len(native) != len(records) or len(native) > MAX_MESSAGES * slices:
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
                record.get('native_event_nanoseconds'), record.get('thread_originator_guid'),
                record.get('thread_originator_part'), record.get('attachment_caption', False)))
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


def stored_caption_matches(stored, native_content) -> bool:
    """The stored body is the native attachment message's caption exactly as the sync stores it.

    The sync removes the attachment placeholders and strips the surrounding whitespace; a body it read from
    the attributed archive also has its line ends normalised. Either form is a deterministic rewrite of the
    owner's own native text that adds nothing. Neither form holds a placeholder, so a stored body that kept
    one never matches: released, it would say that an attachment was there."""
    from topos.ingestion.imessage_attributed_text import caption_text
    if type(stored) is not str or type(native_content) is not str:
        return False
    return stored in (caption_text(native_content), native_content.replace('\ufffc', '').strip())


def _names(stored, native) -> bool:
    """A stored thread field names what the native row names: both unset, or the same string exactly."""
    if native is None:
        return stored in (None, "")
    return type(stored) is str and stored == native


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
    # The inline reply the native row is, which only the v3 reader reads. Under v1 and v2 a native row
    # never names one, and the stored row may not either.
    thread, part = native.thread_originator_guid, native.thread_originator_part
    if type(native.attachment_caption) is not bool or (native.attachment_caption and native.reader_contract != FORMS_CONTRACT):
        refuse("input_invalid")
    if native.reader_contract != FORMS_CONTRACT:
        if thread is not None or part is not None:
            refuse("input_invalid")
    elif (thread, part) != (None, None) and (not _identifier(thread) or not (part is None or _identifier(part))):
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
    if row.get("message_type") not in (None, "message") or row.get("event_type") not in (None, ""):
        refuse("message_form")
    # The stored row names the same thread as the native row, or neither names one. A stored reply to
    # another message, a stored reply the native row is not, and a native reply stored as an ordinary
    # message all refuse: the comparison never completes or corrects the stored row.
    if not _names(row.get("reply_to_message_id"), thread):
        refuse("message_form")
    if native.attachment_caption:
        if not stored_caption_matches(row.get("content"), native.content):
            refuse("content_mismatch")
    elif type(row.get("content")) is not str or row["content"] != native.content:
        refuse("content_mismatch")
    actual_time, expected_time = canonical_utc_microseconds(row.get("event_at")), canonical_utc_microseconds(native.event_at)
    if native.reader_contract in RECONCILIATION_CONTRACTS:
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
    if (not _names(metadata.get("thread_originator_guid"), thread) or not _names(metadata.get("thread_originator_part"), part)
            or metadata.get("associated_message_guid") not in (None, "")
            or any(key in metadata and (type(metadata[key]) is not int or metadata[key] != 0) for key in _ZERO_METADATA)):
        refuse("message_form")
    if native.reader_contract not in _PARSERS:
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
