"""Synthetic native comparison: matching is never a provenance enrollment."""
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from tests.ingestion.test_owner_snapshot import NOW, native_snapshot
from topos.ingestion.owner_snapshot import SnapshotRejected
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.imessage_reconciliation import (
    CONTRACT, compare_existing_message, parse_reconciliation_snapshot, preflight_existing_snapshot,
)


def snapshot(*, mutate=None, count=2):
    def adapt(db):
        db.execute('ALTER TABLE message ADD COLUMN guid TEXT')
        db.execute("UPDATE message SET guid='synthetic-message-' || ROWID")
        db.execute('ALTER TABLE chat ADD COLUMN chat_identifier TEXT')
        db.execute("UPDATE chat SET chat_identifier='synthetic-conversation'")
        if mutate:
            mutate(db)
    return native_snapshot(count=count, mutate=adapt)


def sample():
    native = parse_reconciliation_snapshot(snapshot(), now=NOW)[0]
    row = {'message_id': native.message_id, 'source_record_id': native.message_id,
        'source_id': 'imessage', 'dataset_id': 'dataset-native', 'owner_user_id': None,
        'conversation_id': native.conversation_id, 'content': native.content,
        'event_at': native.event_at, 'is_from_self': 1, 'sender_id': 'self',
        'sender_type': 'human', 'actor_role': None, 'message_type': 'message',
        'metadata_json': json.dumps({'message_guid': native.message_guid,
            'chat_guid': native.chat_guid, 'chat_identifier': native.chat_identifier,
            'associated_message_type': 0})}
    return row, native


def compare(row, native):
    return compare_existing_message(row, native, dataset_id='dataset-native', owner_id='owner-synthetic')


def test_exact_correspondence_preserves_null_owner_and_all_canonical_cells():
    row, native = sample()
    before = deepcopy(row)
    matched = compare(row, native)
    assert matched.contract == CONTRACT and len(matched.canonical_revision) == 64
    assert matched.snapshot_sha256 == native.snapshot_sha256
    assert row == before and row['owner_user_id'] is None
    assert native.content not in repr(matched) and native.content not in repr(native)
    assert not hasattr(matched, 'owner_id') and not hasattr(matched, 'permit')


@pytest.mark.parametrize('field,value,reason', [
    ('owner_user_id', 'another-owner', 'owner_conflict'),
    ('owner_user_id', '', 'owner_conflict'),
    ('dataset_id', 'another-dataset', 'source_binding'),
    ('source_id', 'imported-imessage', 'source_binding'),
    ('message_id', 'imessage:9', 'native_identity'),
    ('source_record_id', 'imessage:9', 'native_identity'),
    ('conversation_id', 'other-chat', 'native_identity'),
    ('content', 'Changed body', 'content_mismatch'),
    ('content', 'Synthetic message 1 ', 'content_mismatch'),
    ('event_at', '2023-03-08T20:26:40.123457+00:00', 'time_mismatch'),
    ('event_at', '2023-03-08T20:26:40.123456', 'time_mismatch'),
    ('event_at', '2023-03-08T15:26:40.123456-05:00', 'time_mismatch'),
    ('sender_id', 'other', 'sender_conflict'),
    ('sender_type', 'assistant', 'sender_conflict'),
    ('actor_role', 'addressed', 'sender_conflict'),
    ('is_from_self', 0, 'not_owner_sent'),
    ('is_from_self', True, 'not_owner_sent'),
    ('message_type', 'system', 'message_form'),
    ('event_type', 'reaction', 'message_form'),
    ('reply_to_message_id', 'earlier-message', 'message_form'),
])
def test_contradictions_never_match(field, value, reason):
    row, native = sample()
    row[field] = value
    with pytest.raises(PolicyError, match='reconciliation_' + reason):
        compare(row, native)


def test_received_native_row_cannot_be_promoted_by_canonical_sender_flags():
    row, native = sample()
    with pytest.raises(PolicyError, match='not_owner_sent'):
        compare(row, replace(native, is_from_self=False))


@pytest.mark.parametrize('field,value', [
    ('message_guid', 'different-message'), ('chat_guid', 'different-chat'),
    ('chat_identifier', 'different-conversation'),
    ('associated_message_guid', 'quoted-message'), ('associated_message_type', 2000),
    ('thread_originator_guid', 'reply'), ('thread_originator_part', 0),
    ('is_forwarded', True), ('quoted_text', 'I work at Synthetic Corp'),
    ('unknown_future_flag', False), ('topos_owner_ingest', {'version': 'forged'}),
])
def test_metadata_cannot_supply_or_contradict_native_authority(field, value):
    row, native = sample()
    metadata = json.loads(row['metadata_json'])
    metadata[field] = value
    row['metadata_json'] = json.dumps(metadata)
    with pytest.raises(PolicyError):
        compare(row, native)


@pytest.mark.parametrize('raw', [None, '', '[]', 'null', '{"x":1,"x":2}', '{"x":NaN}'])
def test_malformed_metadata_refuses(raw):
    row, native = sample()
    row['metadata_json'] = raw
    with pytest.raises(PolicyError):
        compare(row, native)


def test_revision_pins_consent_surface_but_not_sync_receipts():
    row, native = sample()
    base = compare(row, native)
    row.update(sync_batch_id='later-sync', ingested_at='later', content_hash='recalculated')
    assert compare(row, native) == base
    row['new_privacy_annotation'] = 'review-required'
    assert compare(row, native).canonical_revision != base.canonical_revision


@pytest.mark.parametrize('sql', [
    "UPDATE message SET guid=NULL WHERE ROWID=1",
    "UPDATE message SET guid='same-guid'",
    "UPDATE chat SET chat_identifier=NULL",
    "UPDATE chat SET guid=''",
    "INSERT INTO chat_message_join VALUES(7,1)",
    "UPDATE message SET attributedBody=x'0102'",
    "UPDATE message SET cache_has_attachments=1",
])
def test_ambiguous_identity_and_unsupported_native_forms_refuse_whole_snapshot(sql):
    with pytest.raises(SnapshotRejected):
        parse_reconciliation_snapshot(snapshot(mutate=lambda db: db.execute(sql)), now=NOW)


@pytest.mark.parametrize('column,value', [
    ('group_action_type', 1), ('is_forwarded', 1), ('is_forward', 1),
    ('is_spam', 1), ('quoted_message_guid', 'earlier-message'),
    ('forwarded_from', 'somebody'), ('reply_to_guid', 'earlier-message'),
])
def test_new_native_restriction_columns_are_not_discarded(column, value):
    def mutate(db):
        db.execute(f'ALTER TABLE message ADD COLUMN {column}')
        db.execute(f'UPDATE message SET {column}=? WHERE ROWID=1', (value,))
    with pytest.raises(SnapshotRejected, match='snapshot_message_form_unsupported'):
        parse_reconciliation_snapshot(snapshot(mutate=mutate), now=NOW)


def test_missing_correspondence_columns_do_not_fall_back_to_rowid():
    with pytest.raises(SnapshotRejected, match='snapshot_correspondence_missing'):
        parse_reconciliation_snapshot(native_snapshot(), now=NOW)


def test_empty_and_bounded_snapshots():
    assert parse_reconciliation_snapshot(snapshot(count=0), now=NOW) == ()
    with pytest.raises(SnapshotRejected, match='snapshot_message_limit'):
        parse_reconciliation_snapshot(snapshot(count=1001), now=NOW)


def test_unrecognized_file_is_a_content_free_failure():
    with pytest.raises(SnapshotRejected) as failure:
        parse_reconciliation_snapshot(b'private-message-body', now=NOW)
    assert 'private-message-body' not in str(failure.value)


@pytest.fixture
def canonical(tmp_path):
    path = tmp_path / 'canonical.db'
    row, native = sample()
    with sqlite3.connect(path) as db:
        # No uniqueness constraint: the ambiguity test models multiple matches.
        db.execute('CREATE TABLE conversation_messages (' + ','.join(
            name + (' INTEGER' if name == 'is_from_self' else ' TEXT') for name in row) + ')')
        db.execute('INSERT INTO conversation_messages VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
    return path, row, native


def preflight(db, **kwargs):
    return preflight_existing_snapshot(db, snapshot(), dataset_id='dataset-native', owner_id='owner-synthetic',
        starts_at=kwargs.get('starts_at', '2023-03-08T00:00:00Z'),
        ends_at=kwargs.get('ends_at', '2023-03-09T00:00:00Z'), now=NOW)


def test_preflight_counts_matches_without_writing_or_creating_authority(canonical):
    path, row, _ = canonical
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        before = db.execute('SELECT * FROM conversation_messages').fetchall()
        report = preflight(db)
        assert report['counts'] == {'canonical_missing': 1, 'matched': 1}
        assert report['authority_created'] is False and db.total_changes == 0
        assert row['content'] not in str(report) and row['message_id'] not in str(report)
        assert db.execute('SELECT * FROM conversation_messages').fetchall() == before
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'ingest_provenance_%'").fetchone()[0] == 0


def test_duplicate_canonical_identity_is_counted_as_ambiguous(canonical):
    path, _, _ = canonical
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO conversation_messages SELECT * FROM conversation_messages')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        assert preflight(db)['counts'] == {'canonical_ambiguous': 1, 'canonical_missing': 1}


def test_preflight_requires_explicit_read_snapshot(canonical):
    path, _, _ = canonical
    with sqlite3.connect(path) as db:
        with pytest.raises(PolicyError, match='read_snapshot_required'):
            preflight(db)
        db.execute('BEGIN')
        with pytest.raises(PolicyError, match='read_snapshot_required'):
            preflight(db)


@pytest.mark.parametrize('start,end', [
    ('2023-01-01T00:00:00Z', '2023-03-09T00:00:00Z'),
    ('2023-03-09T00:00:00Z', '2023-03-08T00:00:00Z'),
    ('2023-03-08T00:00:00', '2023-03-09T00:00:00Z'),
    ('2026-09-15T00:00:00Z', '2026-09-16T00:00:00Z'),
])
def test_preflight_refuses_unbounded_naive_reversed_or_future_windows(canonical, start, end):
    path, _, _ = canonical
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        with pytest.raises(PolicyError, match='window_invalid'):
            preflight(db, starts_at=start, ends_at=end)


def test_preflight_excludes_messages_outside_the_requested_window(canonical):
    path, _, _ = canonical
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        assert preflight(db, starts_at='2023-03-09T00:00:00Z', ends_at='2023-03-10T00:00:00Z')['counts'] == {'outside_window': 2}


def test_generated_native_identity_is_rejected_before_evaluation():
    def mutate(db):
        db.execute("ALTER TABLE message ADD COLUMN generated_identity TEXT GENERATED ALWAYS AS (hex(zeroblob(32))) VIRTUAL")
    with pytest.raises(SnapshotRejected, match='snapshot_schema_unsupported'):
        parse_reconciliation_snapshot(snapshot(mutate=mutate), now=NOW)
