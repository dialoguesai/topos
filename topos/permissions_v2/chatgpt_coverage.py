"""Bounded, read-only coverage census of existing ChatGPT ingestion receipts.

Receipts are not native account proof. A match neither attests ownership nor
qualifies text for release. In particular, a receipt's created_at may be a
capture clock rather than a message clock; it must not repair event_at.
"""
from collections import Counter
from datetime import datetime, timezone
import math
import time

from .canonical import PolicyError
from .evidence import _json
from .fact_eligibility import canonical_utc_microseconds

SOURCES = ('chatgpt_ui_conversation', 'chatgpt_file_ingestion')
LIMIT = 1000


def receipt_comparison(row, parent, raw, *, owner_id):
    """A diagnostic result only; deliberately no 'qualified' or permit result."""
    if (type(row) is not dict or type(parent) is not dict
            or row.get('source_id') not in SOURCES
            or parent.get('source_id') != row.get('source_id')
            or parent.get('owner_user_id') != owner_id
            or parent.get('conversation_id') != row.get('conversation_id')):
        return 'owner_or_source_mismatch'
    if row.get('sender_type') not in ('human', 'user') or row.get('actor_role') not in (None, 'authored'):
        return 'not_owner_role'
    try:
        receipt = _json(raw, dict)
        metadata = _json(row.get('metadata_json') or '{}', dict)
    except PolicyError:
        return 'malformed_receipt_or_metadata'
    if set(receipt) != {'id', 'thread_id', 'role', 'content', 'created_at'}:
        return 'unsupported_receipt_shape'
    if receipt['role'] != 'user':
        return 'not_owner_role'
    if (any(type(receipt[key]) is not str or not receipt[key] for key in ('id', 'thread_id', 'content'))
            or row.get('message_id') != receipt['id'] or row.get('source_record_id') != receipt['id']
            or row.get('conversation_id') != 'chatgpt:' + receipt['thread_id']):
        return 'identity_mismatch'
    if (set(metadata) != {'thread_id', 'original_source'}
            or metadata != {'thread_id':receipt['thread_id'], 'original_source':'chatgpt'}):
        return 'unsupported_message_metadata'
    if row.get('content') != receipt['content']:
        return 'content_mismatch'
    stamp = receipt['created_at']
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        return 'receipt_time_invalid'
    try:
        captured = canonical_utc_microseconds(datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='microseconds'))
    except (ValueError, OverflowError, OSError):
        return 'receipt_time_invalid'
    stored = canonical_utc_microseconds(row.get('event_at'))
    if stored is None:
        return 'canonical_time_invalid'
    if captured != stored:
        return 'matching_text_different_clock'
    return 'matching_receipt_needs_native_proof'


def coverage(conn, *, owner_id, source_id, starts_at, ends_at, now):
    if source_id not in SOURCES or type(owner_id) is not str or not owner_id:
        raise PolicyError('coverage_binding_invalid')
    start, end = canonical_utc_microseconds(starts_at), canonical_utc_microseconds(ends_at)
    current = canonical_utc_microseconds(now.isoformat(timespec='microseconds'))
    if (start is None or end is None or current is None or start >= end
            or end > current or end-start > 31 * 86400 * 1_000_000):
        raise PolicyError('coverage_window_invalid')
    if not conn.in_transaction or conn.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise PolicyError('coverage_read_snapshot_required')
    for table in ('ai_chat_messages', 'ai_chat_conversations', 'raw_chat_messages_chatgpt'):
        objects = conn.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchall()
        if len(objects) != 1 or objects[0][0] != 'table':
            raise PolicyError('coverage_schema_invalid')
    deadline = time.monotonic() + 10
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    try:
        rows = conn.execute("SELECT * FROM ai_chat_messages WHERE source_id=? AND sender_type IN ('human','user') "
            "AND julianday(event_at)>=julianday(?) AND julianday(event_at)<julianday(?) "
            "ORDER BY event_at,message_id LIMIT ?", (source_id, starts_at, ends_at, LIMIT+1)).fetchall()
        if len(rows) > LIMIT:
            raise PolicyError('coverage_limit')
        counts = Counter()
        for stored in rows:
            row = dict(stored)
            parents = conn.execute('SELECT * FROM ai_chat_conversations WHERE conversation_id=?',
                                   (row['conversation_id'],)).fetchmany(2)
            raw = conn.execute('SELECT payload_json FROM raw_chat_messages_chatgpt WHERE source_system=? AND source_record_id=?',
                               (source_id, row['source_record_id'])).fetchmany(2)
            if len(parents) != 1 or len(raw) != 1:
                counts['missing_or_ambiguous_lineage'] += 1
                continue
            counts[receipt_comparison(row, dict(parents[0]), raw[0][0], owner_id=owner_id)] += 1
        return {'authority_created':False, 'source_id':source_id, 'observed':len(rows),
                'counts':dict(sorted(counts.items())), 'scope':'receipt_comparison_only'}
    finally:
        conn.set_progress_handler(None, 0)
