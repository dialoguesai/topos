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
from .imessage_reconciliation import ATTRIBUTED_CONTRACT, NativeMessage, compare_existing_message
from .fact_eligibility import canonical_utc_microseconds
from topos.ingestion.owner_snapshot import SnapshotRejected, _event_time_nanoseconds, _identifier
from topos.ingestion.imessage_attributed_text import decode_attributed_text

_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_REQUIRED = {
    'ROWID', 'guid', 'text', 'date', 'handle_id', 'is_from_me', 'subject',
    'attributedBody', 'associated_message_guid', 'associated_message_type',
    'cache_has_attachments', 'item_type',
}
_EMPTY = {'thread_originator_guid', 'thread_originator_part', 'quoted_message_guid',
          'forwarded_from', 'reply_to_guid'}
_ZERO = {'is_deleted', 'is_system_message', 'is_service_message', 'group_action_type',
         'is_forward', 'is_forwarded', 'is_spam'}


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
        selected = sorted(_REQUIRED | ((_EMPTY | _ZERO) & columns))
        # Schema-derived identifiers never enter SQL: selected is a closed set.
        expressions = {
            'attributedBody': 'CASE WHEN attributedBody IS NULL THEN NULL WHEN length(attributedBody)<=262144 THEN attributedBody ELSE 0 END AS attributedBody',
            'text': 'CASE WHEN length(CAST(text AS BLOB))<=65536 THEN text ELSE NULL END AS text',
        }
        sql = 'SELECT ' + ','.join(expressions.get(name, '"' + name + '"') for name in selected)
        sql += ' FROM message WHERE is_from_me=1 AND date>=? AND date<? ORDER BY ROWID LIMIT 1001'
        rows = db.execute(sql, (start, end))
        counts = Counter(native_owner_sent=0)
        total_bytes, archive_bytes = 0, 0
        for raw in rows:
            counts['native_owner_sent'] += 1
            if counts['native_owner_sent'] > 1000:
                raise PolicyError('native_probe_message_limit')
            if time.monotonic() > deadline:
                raise PolicyError('native_probe_time_limit')
            row = dict(raw)
            if type(row['is_from_me']) is not int or row['is_from_me'] != 1:
                counts['native_sender_invalid'] += 1
                continue
            if (any(row.get(key) not in (None, '') for key in _EMPTY | {'subject', 'associated_message_guid'})
                    or any(row.get(key) is not None and (type(row[key]) is not int or row[key] != 0) for key in _ZERO)
                    or any(type(row[key]) is not int or row[key] != 0 for key in
                           ('associated_message_type', 'cache_has_attachments', 'item_type'))):
                counts['native_message_form_unsupported'] += 1
                continue
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
                row['guid'], chats[0][1], chats[0][2], event, True, content, ATTRIBUTED_CONTRACT, row['date'])
            try:
                compare_existing_message(dict(matches[0]), observation, dataset_id=dataset_id, owner_id=owner_id)
                counts['canonical_exact_match'] += 1
                if _on_match is not None:
                    _on_match(row, tuple(chats[0]))
            except PolicyError as exc:
                counts[exc.code] += 1
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
        parsed = parse_reconciliation_snapshot(data, now=now, reader_contract=ATTRIBUTED_CONTRACT)
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
