"""Owner-local native coverage measurement, without importing or authorizing rows.

Read only the requested recent window from the native Messages database inside
the Topos process (which owns its macOS permission). Never copy chat.db, change a
sync cursor, emit message text, or turn a diagnostic match into release authority.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time

from .canonical import PolicyError
from .imessage_reconciliation import FORMS_CONTRACT, NativeMessage, compare_existing_message
from .fact_eligibility import canonical_utc_microseconds
from topos.ingestion.owner_snapshot import (THREAD_COLUMNS, SnapshotRejected, _event_time_nanoseconds, _identifier,
                                            thread_reply)
from topos.ingestion.imessage_attributed_text import decode_attributed_text, has_text_besides_attachments

_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_REQUIRED = {
    'ROWID', 'guid', 'text', 'date', 'handle_id', 'is_from_me', 'subject',
    'attributedBody', 'associated_message_guid', 'associated_message_type',
    'cache_has_attachments', 'item_type',
}
_EMPTY = {'quoted_message_guid', 'forwarded_from'}
_ZERO = {'is_deleted', 'is_system_message', 'is_service_message', 'group_action_type',
         'is_forward', 'is_forwarded', 'is_spam'}
# An inline reply's two fields, read together (`_thread`): the reply is the owner's own text, and the
# comparison requires the stored row to name the same thread.
_THREAD = frozenset(THREAD_COLUMNS)
# Counted, never decided on and never captured: read in the same statement under an alias, then
# set aside before anything else sees the row. `reply_to_guid` is Messages' own chain from a message
# to the one before it, which the reader used to refuse as a reply; it is not one (FORMS_CONTRACT).
_OBSERVED = ('date_edited', 'date_retracted', 'reply_to_guid')
# The census reads archived attachment bodies only after the last decision, within its own
# budget, so its cost never counts against the decision deadline. Past either bound a body is
# counted as unmeasured.
_CENSUS_BYTES = 4 * 1024 * 1024
_CENSUS_SECONDS = 1
# Where a sent-by-me row in an unsupported native form goes, first failing field first. The
# order ranks what a reader extension could recover: deleted, spam and system rows are never
# the owner's words; a reaction quotes someone else's message; a forward or a quote carries
# someone else's words; a subject line and an attachment's caption are the owner's own text in a
# form the reader does not accept yet. An inline reply is read, so the thread bucket now holds only
# a row whose two thread fields are not a reply the reader reads (a part with no originator).
_FORM_ORDER = (
    ('native_form_deleted', ('is_deleted',)),
    ('native_form_spam', ('is_spam',)),
    ('native_form_system', ('is_system_message', 'is_service_message', 'group_action_type', 'item_type')),
    ('native_form_reaction', ('associated_message_type', 'associated_message_guid')),
    ('native_form_forward_or_quote', ('is_forward', 'is_forwarded', 'forwarded_from', 'quoted_message_guid')),
    ('native_form_thread_reply', ('thread_originator_guid', 'thread_originator_part')),
    ('native_form_subject', ('subject',)),
    ('native_form_attachment', ('cache_has_attachments',)),
)


def _thread(row):
    """The inline reply a row is: (originator, part), (None, None) for a row in no thread, or None when the
    two fields are not a reply the reader reads (`owner_snapshot.thread_reply`, the capture's own rule)."""
    try:
        return thread_reply(row.get('thread_originator_guid'), row.get('thread_originator_part'))
    except SnapshotRejected:
        return None


def _stored_without_surrounding_whitespace(stored, native):
    """Count-only: the stored body is the native body without its leading and trailing whitespace.

    The sync's reader stores a message that way, so such a row can never match exactly. This sizes that
    loss; it decides nothing, and the comparison has already refused the row when it is asked."""
    return type(stored) is str and type(native) is str and stored != native and stored == native.strip()


def _form_fails(row, key):
    """The existing form check, one field at a time and with the same semantics."""
    if key in _THREAD:
        return _thread(row) is None
    if key in _EMPTY or key in ('subject', 'associated_message_guid'):
        return row.get(key) not in (None, '')
    if key in _ZERO:
        return row.get(key) is not None and (type(row[key]) is not int or row[key] != 0)
    return type(row.get(key)) is not int or row[key] != 0


def _form_bucket(row):
    """Count-only: the bucket of an unsupported form. Never read by a decision.

    `native_form_attachment` itself means the caption can only be seen inside the archived body;
    the caller measures those after every decision is taken.
    """
    for bucket, keys in _FORM_ORDER:
        if not any(_form_fails(row, key) for key in keys):
            continue
        if bucket != 'native_form_attachment':
            return bucket
        text = row.get('text')
        if type(text) is str and text.replace('\ufffc', '').strip():
            return 'native_form_attachment_with_text'
        body = row.get('attributedBody')
        if type(body) is bytes:
            return bucket
        return 'native_form_attachment_only' if type(text) is str or body is None else 'native_form_attachment_unmeasured'
    return 'native_form_other'


def _attachment_bucket(visible):
    if visible is None:
        return 'native_form_attachment_unmeasured'
    return 'native_form_attachment_with_text' if visible else 'native_form_attachment_only'


def window(starts_at, ends_at, now):
    start, end = canonical_utc_microseconds(starts_at), canonical_utc_microseconds(ends_at)
    current = canonical_utc_microseconds(now.isoformat(timespec='microseconds'))
    if (start is None or end is None or current is None or start >= end
            or end > current or end - start > 31 * 86400 * 1_000_000):
        raise PolicyError('native_probe_window_invalid')
    epoch = canonical_utc_microseconds(_EPOCH.isoformat(timespec='microseconds'))
    return (start - epoch) * 1000, (end - epoch) * 1000


def probe_native_messages(canonical, *, dataset_id, owner_id, starts_at, ends_at, now,
                          _native_path=None, _on_match=None):
    """Counts only. `_native_path` is a code-only synthetic-test seam, never API input."""
    start, end = window(starts_at, ends_at, now)
    if not _identifier(dataset_id) or not _identifier(owner_id):
        raise PolicyError('native_probe_binding_invalid')
    if not canonical.in_transaction or canonical.execute('PRAGMA query_only').fetchone()[0] != 1:
        raise PolicyError('native_probe_read_snapshot_required')
    native_path = _native_path or Path.home() / 'Library' / 'Messages' / 'chat.db'
    db = None
    try:
        db = sqlite3.connect(Path(native_path).as_uri() + '?mode=ro', uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')  # Includes WAL; no full-history backup/copy.
        deadline = time.monotonic() + 10
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        for table in ('message', 'chat', 'chat_message_join'):
            schema = db.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
            if schema is None or schema[0] != 'table':
                raise PolicyError('native_probe_schema_unsupported')
            if any(row[6] != 0 for row in db.execute(f'PRAGMA table_xinfo("{table}")')):
                raise PolicyError('native_probe_schema_unsupported')
        columns = {row[1] for row in db.execute('PRAGMA table_info(message)')}
        if not _REQUIRED <= columns:
            raise PolicyError('native_probe_schema_unsupported')
        selected = sorted(_REQUIRED | ((_EMPTY | _ZERO | _THREAD) & columns))
        observed = [name for name in _OBSERVED if name in columns]
        # Schema-derived identifiers never enter SQL: selected and observed are closed sets.
        expressions = {
            'attributedBody': 'CASE WHEN attributedBody IS NULL THEN NULL WHEN length(attributedBody)<=262144 THEN attributedBody ELSE 0 END AS attributedBody',
            'text': 'CASE WHEN length(CAST(text AS BLOB))<=65536 THEN text ELSE NULL END AS text',
        }
        sql = 'SELECT ' + ','.join(expressions.get(name, '"' + name + '"') for name in selected)
        sql += ''.join(',"' + name + '" AS "_observed_' + name + '"' for name in observed)
        sql += ' FROM message WHERE is_from_me=1 AND date>=? AND date<? ORDER BY ROWID LIMIT 1001'
        rows = db.execute(sql, (start, end))
        counts = Counter(native_owner_sent=0)
        total_bytes, archive_bytes = 0, 0
        census, census_bytes = [], 0
        for raw in rows:
            counts['native_owner_sent'] += 1
            if counts['native_owner_sent'] > 1000:
                raise PolicyError('native_probe_message_limit')
            if time.monotonic() > deadline:
                raise PolicyError('native_probe_time_limit')
            row = dict(raw)
            seen = {name: row.pop('_observed_' + name) for name in observed}
            edited = type(seen.get('date_edited')) is int and seen['date_edited'] != 0
            if edited:
                counts['native_observed_edited'] += 1
            if type(seen.get('date_retracted')) is int and seen['date_retracted'] != 0:
                counts['native_observed_retracted'] += 1
            if type(row['is_from_me']) is not int or row['is_from_me'] != 1:
                counts['native_sender_invalid'] += 1
                continue
            pointer = seen.get('reply_to_guid') not in (None, '')
            thread = _thread(row)
            if (thread is None
                    or any(row.get(key) not in (None, '') for key in _EMPTY | {'subject', 'associated_message_guid'})
                    or any(row.get(key) is not None and (type(row[key]) is not int or row[key] != 0) for key in _ZERO)
                    or any(type(row[key]) is not int or row[key] != 0 for key in
                           ('associated_message_type', 'cache_has_attachments', 'item_type'))):
                counts['native_message_form_unsupported'] += 1
                bucket = _form_bucket(row)
                if bucket == 'native_form_attachment' and census_bytes + len(row['attributedBody']) <= _CENSUS_BYTES:
                    census_bytes += len(row['attributedBody'])
                    census.append(row['attributedBody'])
                else:
                    counts[_attachment_bucket(None) if bucket == 'native_form_attachment' else bucket] += 1
                continue
            # Count-only: the two forms the reader used to refuse. A row that is both counts as the reply.
            replied = thread[0] is not None
            if replied:
                counts['native_observed_thread_reply'] += 1
            elif pointer:
                counts['native_observed_reply_pointer'] += 1
            content = row['text']
            if row['attributedBody'] is not None:
                archive = row['attributedBody']
                archive_bytes += len(archive) if type(archive) is bytes else 0
                if archive_bytes > 4 * 1024 * 1024:
                    raise PolicyError('native_probe_archive_limit')
                try:
                    decoded = decode_attributed_text(archive)
                except SnapshotRejected:
                    counts['native_attributed_body_unsupported'] += 1
                    continue
                if content not in (None, '', decoded):
                    counts['native_body_representations_disagree'] += 1
                    continue
                content = decoded
                counts['native_attributed_body_decoded'] += 1
            if type(content) is not str or not content.strip() or '\x00' in content:
                counts['native_text_unsupported'] += 1
                continue
            size = len(content.encode('utf-8'))
            total_bytes += size
            if size > 64 * 1024 or total_bytes > 1024 * 1024:
                raise PolicyError('native_probe_text_limit')
            try:
                event = _event_time_nanoseconds(row['date'], now)
            except SnapshotRejected as exc:
                counts[exc.reason_code] += 1
                continue
            chats = db.execute('SELECT c.ROWID,c.guid,c.chat_identifier FROM chat c '
                'JOIN chat_message_join j ON j.chat_id=c.ROWID WHERE j.message_id=? LIMIT 2',
                (row['ROWID'],)).fetchall()
            if (len(chats) != 1 or not _identifier(row['guid'])
                    or not all(_identifier(chats[0][i]) for i in (1, 2))):
                counts['native_identity_ambiguous'] += 1
                continue
            counts['native_text_supported'] += 1
            message_id = 'imessage:' + str(row['ROWID'])
            matches = canonical.execute('SELECT * FROM conversation_messages WHERE message_id=? LIMIT 2',
                                        (message_id,)).fetchall()
            if len(matches) != 1:
                counts['canonical_missing_or_ambiguous'] += 1
                continue
            observation = NativeMessage('diagnostic-not-a-snapshot', message_id, str(chats[0][0]),
                row['guid'], chats[0][1], chats[0][2], event, True, content, FORMS_CONTRACT, row['date'], *thread)
            stored = dict(matches[0])
            try:
                compare_existing_message(stored, observation, dataset_id=dataset_id, owner_id=owner_id)
                counts['canonical_exact_match'] += 1
                if edited:
                    counts['native_observed_edited_exact_match'] += 1
                if replied:
                    counts['native_observed_thread_reply_exact_match'] += 1
                elif pointer:
                    counts['native_observed_reply_pointer_exact_match'] += 1
                if _on_match is not None:
                    _on_match(row, tuple(chats[0]))
            except PolicyError as exc:
                counts[exc.code] += 1
                if edited and exc.code == 'reconciliation_content_mismatch':
                    counts['native_observed_edited_content_mismatch'] += 1
                if (exc.code == 'reconciliation_content_mismatch'
                        and _stored_without_surrounding_whitespace(stored.get('content'), content)):
                    counts['native_observed_content_mismatch_whitespace'] += 1
        stop = time.monotonic() + _CENSUS_SECONDS
        for body in census:
            counts[_attachment_bucket(has_text_besides_attachments(body) if time.monotonic() < stop else None)] += 1
        return {'authority_created': False, 'counts': dict(sorted(counts.items()))}
    except (sqlite3.Error, OSError, UnicodeError):
        raise PolicyError('native_probe_unavailable') from None
    finally:
        if db is not None:
            db.close()


def capture_matching_snapshot(canonical, *, snapshot_root, dataset_id, owner_id, starts_at, ends_at, now, skip=None):
    """Stage only exact matches, inside the owner process. No authority is minted.

    `skip`, when given, is asked about each exact match's message id on the same read
    snapshot and returns a reason code to leave that row out, or None. It is a code-only
    seam (the refresh uses it for rows another enrollment proves), never a request field.
    Skipped rows are counted as `excluded_<reason>`.
    """
    import os
    import secrets
    import stat
    from .imessage_reconciliation import parse_reconciliation_snapshot

    # The capture is written and read back under the reader that made it (FORMS_CONTRACT): its rows
    # keep their two thread fields, and it never holds `reply_to_guid`, an observed column.
    root = Path(snapshot_root)
    for directory in (root, root.parent):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise PolicyError('ingest_snapshot_private_required')
    captured, excluded = [], Counter()

    def on_match(row, chat):
        reason = skip('imessage:' + str(row['ROWID'])) if skip is not None else None
        if reason is not None:
            excluded['excluded_' + reason] += 1
            return
        captured.append((row, chat))
    result = probe_native_messages(canonical, dataset_id=dataset_id, owner_id=owner_id,
        starts_at=starts_at, ends_at=ends_at, now=now, _on_match=on_match)
    if excluded:
        result = {**result, 'counts': dict(sorted((result['counts'] | excluded).items()))}
    if not captured:
        raise PolicyError('reconciliation_empty')
    snapshot_id = 'native-' + secrets.token_hex(16)
    path = root / (snapshot_id + '.db')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    os.close(fd)
    db = None
    try:
        db = sqlite3.connect(path)
        columns = sorted(captured[0][0])
        if any(sorted(row) != columns for row, _ in captured):
            raise PolicyError('native_probe_schema_unsupported')
        db.execute('CREATE TABLE message (' + ','.join('"' + key + '"' +
            (' INTEGER PRIMARY KEY' if key == 'ROWID' else '') for key in columns) + ')')
        db.execute('CREATE TABLE chat (ROWID INTEGER PRIMARY KEY,guid TEXT,chat_identifier TEXT)')
        db.execute('CREATE TABLE handle (ROWID INTEGER PRIMARY KEY,id TEXT)')
        db.execute('CREATE TABLE chat_message_join (chat_id INTEGER,message_id INTEGER)')
        seen = {}
        for row, chat in captured:
            if chat[0] in seen and seen[chat[0]] != chat:
                raise PolicyError('native_probe_schema_unsupported')
            if chat[0] not in seen:
                db.execute('INSERT INTO chat VALUES(?,?,?)', chat)
                seen[chat[0]] = chat
            db.execute('INSERT INTO message VALUES(' + ','.join('?' for _ in columns) + ')',
                       [row[key] for key in columns])
            db.execute('INSERT INTO chat_message_join VALUES(?,?)', (chat[0], row['ROWID']))
        db.commit()
        db.close()
        db = None
        with path.open('rb') as stream:
            data = stream.read(16 * 1024 * 1024 + 1)
            os.fsync(stream.fileno())
        # The publisher uses this same independent closed parser. Verify the
        # serialized result now, including uniqueness of native GUIDs and joins.
        parsed = parse_reconciliation_snapshot(data, now=now, reader_contract=FORMS_CONTRACT)
        if len(parsed) != len(captured):
            raise PolicyError('reconciliation_snapshot_changed')
        path.chmod(0o400)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return snapshot_id, result
    except BaseException:
        if db is not None:
            db.close()
        path.unlink(missing_ok=True)
        raise
