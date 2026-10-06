"""Real native/canonical read logic on synthetic files, with transport negatives."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.permissions_v2.test_imessage_reconciliation import snapshot, sample
from topos.api.permissions_native_probe import router
from topos.auth import resolve_request_principal
from topos.permissions_v2 import native_imessage_probe as probe, runtime
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp

NOW = datetime(2023, 3, 9, tzinfo=timezone.utc)
ARGS = dict(dataset_id='dataset-native', owner_id='owner-synthetic',
    starts_at='2023-03-01T00:00:00.000000+00:00', ends_at=NOW.isoformat(timespec='microseconds'), now=NOW)


@pytest.fixture
def files(tmp_path):
    native = tmp_path / 'native.db'
    native.write_bytes(snapshot())
    canonical = tmp_path / 'canonical.db'
    row, _ = sample()
    with sqlite3.connect(canonical) as db:
        db.execute('CREATE TABLE conversation_messages (' + ','.join(
            name + (' INTEGER' if name == 'is_from_self' else ' TEXT') for name in row) + ')')
        db.execute('INSERT INTO conversation_messages VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
    return native, canonical


def run(files, **overrides):
    native, canonical = files
    db = sqlite3.connect(canonical.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    try:
        return probe.probe_native_messages(db, **(ARGS | overrides), _native_path=native)
    finally:
        db.close()


def test_exact_native_match_is_counts_only_and_mutates_neither_file(files):
    before = [path.read_bytes() for path in files]
    result = run(files)
    assert result == {'authority_created': False, 'counts': {
        'canonical_exact_match': 1, 'native_owner_sent': 1, 'native_text_supported': 1}}
    assert [path.read_bytes() for path in files] == before
    assert 'Synthetic message' not in json.dumps(result)
    assert 'imessage:' not in json.dumps(result)


@pytest.mark.parametrize('sql,key', [
    ("UPDATE message SET attributedBody=x'0102' WHERE ROWID=1", 'native_attributed_body_unsupported'),
    # An attachment is read for its caption; one that is only placeholders is only an attachment.
    ("UPDATE message SET cache_has_attachments=1, text='\ufffc' WHERE ROWID=1", 'native_message_form_unsupported'),
    ("UPDATE message SET cache_has_attachments=2 WHERE ROWID=1", 'native_message_form_unsupported'),
    ("UPDATE message SET associated_message_guid='quote' WHERE ROWID=1", 'native_message_form_unsupported'),
    ("UPDATE message SET date=date+1000000000 WHERE ROWID=1", 'reconciliation_time_mismatch'),
    ("UPDATE message SET text='changed' WHERE ROWID=1", 'reconciliation_content_mismatch'),
    ("INSERT INTO chat_message_join VALUES(7,1)", 'native_identity_ambiguous'),
])
def test_native_unsupported_or_changed_data_is_not_a_match(files, sql, key):
    with sqlite3.connect(files[0]) as db:
        db.execute(sql)
    result = run(files)
    assert result['counts'][key] == 1
    assert result['counts'].get('canonical_exact_match', 0) == 0


def test_wrong_dataset_cannot_borrow_another_context(files):
    result = run(files, dataset_id='another-dataset')
    assert result['counts']['reconciliation_source_binding'] == 1


def test_current_apple_archive_format_can_match_a_real_canonical_row(files):
    from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
    archive, text = ATTRIBUTED_BODY_FIXTURES['typedstream_plain']
    with sqlite3.connect(files[0]) as db:
        db.execute('UPDATE message SET text=NULL,attributedBody=? WHERE ROWID=1', (archive,))
    with sqlite3.connect(files[1]) as db:
        db.execute('UPDATE conversation_messages SET content=?', (text,))
    result = run(files)
    assert result['counts']['native_attributed_body_decoded'] == 1
    assert result['counts']['canonical_exact_match'] == 1
    assert result['authority_created'] is False
    assert text not in json.dumps(result)


def test_recent_window_excludes_older_native_history(files):
    result = run(files, starts_at='2023-03-08T21:00:00.000000+00:00')
    assert result['counts'] == {'native_owner_sent': 0}


@pytest.mark.parametrize('start,end', [
    ('2023-01-01T00:00:00.000000+00:00', ARGS['ends_at']),
    (ARGS['ends_at'], ARGS['starts_at']),
    (ARGS['starts_at'], '2023-03-10T00:00:00.000000+00:00'),
    ('yesterday', ARGS['ends_at']),
])
def test_invalid_or_unbounded_windows_cannot_read_native(files, start, end):
    with pytest.raises(PolicyError, match='native_probe_window_invalid'):
        run(files, starts_at=start, ends_at=end)


def test_wal_native_rows_are_read_without_backup(files):
    db = sqlite3.connect(files[0])
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("UPDATE message SET text='changed in WAL' WHERE ROWID=1")
        db.commit()
        assert run(files)['counts']['reconciliation_content_mismatch'] == 1
    finally:
        db.close()


def test_record_limit_refuses_without_partial_success(files):
    files[0].write_bytes(snapshot(count=1001, mutate=lambda db: db.execute('UPDATE message SET is_from_me=1')))
    with pytest.raises(PolicyError, match='native_probe_message_limit'):
        run(files)


@pytest.fixture
def api(files, monkeypatch):
    calls = []
    actual = probe.probe_native_messages
    def measured(conn, **kwargs):
        calls.append(True)
        return actual(conn, **(kwargs | {'now': NOW}), _native_path=files[0])
    monkeypatch.setattr(probe, 'probe_native_messages', measured)
    node = SimpleNamespace(protocol=SimpleNamespace(canonical_database=files[1],
        ledger=SimpleNamespace(identity=SimpleNamespace(owner_id='owner-synthetic'))))
    monkeypatch.setattr(runtime, 'get_runtime', lambda: node)
    app = FastAPI()
    app.include_router(router)
    return app, calls


BODY = {key: ARGS[key] for key in ('dataset_id', 'starts_at', 'ends_at')}
PATH = '/v1/sharing/imessage/preflight'


def test_verified_owner_socket_exercises_actual_native_reader(api):
    app, calls = api
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=BODY)
    assert response.status_code == 200 and calls == [True]
    assert response.json()['counts']['canonical_exact_match'] == 1
    assert response.headers['cache-control'] == 'no-store'


def test_network_headers_cannot_claim_owner_transport(api):
    app, calls = api
    with TestClient(app) as client:
        response = client.post(PATH, json=BODY, headers={'X-Transport': 'uds', 'X-Topos-Client': 'topos_home_chat'})
    assert response.status_code == 401 and calls == []


@pytest.mark.parametrize('principal', [
    Principal(THIRD_PARTY, 'uds'), Principal(THIRD_PARTY, 'local_http'),
    Principal(OWNER_APP, 'local_http'), Principal(OWNER_APP, 'cp_relay', acting_user='owner-synthetic'),
    Principal(OWNER_APP, 'uds', acting_user='another-owner'),
])
def test_no_nonlocal_or_wrong_owner_can_read_native(api, principal):
    app, calls = api
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    with TestClient(app) as client:
        response = client.post(PATH, json=BODY)
    assert response.status_code in (403, 503) and calls == []


def test_native_path_and_permission_overrides_are_not_inputs(api):
    app, calls = api
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(PATH, json=BODY | {'_native_path': '/other/private.db', 'owner_id': 'other'})
    assert response.status_code == 422 and calls == []


def test_capture_is_bounded_private_and_independently_readable(files, tmp_path, monkeypatch):
    import stat
    from topos.permissions_v2.imessage_reconciliation import parse_reconciliation_snapshot, ATTRIBUTED_CONTRACT
    root = tmp_path / 'private' / 'snapshots'
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    actual = probe.probe_native_messages
    monkeypatch.setattr(probe, 'probe_native_messages', lambda conn, **kw:
        actual(conn, **kw, _native_path=files[0]))
    before = [p.read_bytes() for p in files]
    with sqlite3.connect(files[1]) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        identifier, measured = probe.capture_matching_snapshot(conn, snapshot_root=root, **ARGS)
    path = root / (identifier + '.db')
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    parsed = parse_reconciliation_snapshot(path.read_bytes(), now=NOW, reader_contract=ATTRIBUTED_CONTRACT)
    assert len(parsed) == 1 and parsed[0].message_id == 'imessage:1'
    assert measured['authority_created'] is False
    assert [p.read_bytes() for p in files] == before


@pytest.mark.parametrize('channel', ['local_http', 'cp_relay'])
def test_recovery_is_not_a_remote_owner_or_recipient_operation(api, channel):
    from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
    app, calls = api
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, channel, acting_user='owner-synthetic')
    with TestClient(app) as client:
        response = client.post('/v1/sharing/imessage/recover', json=BODY | {'owner_attestation': OWNER_ATTESTATION})
    assert response.status_code == 403 and calls == []


# -- RD12: the unsupported-form census, count-only --------------------------------------------------

OPTIONAL_TEXT = ('thread_originator_guid', 'thread_originator_part', 'quoted_message_guid', 'forwarded_from', 'reply_to_guid')
OPTIONAL_INT = ('is_deleted', 'is_system_message', 'is_service_message', 'group_action_type', 'is_forward',
                'is_forwarded', 'is_spam', 'date_edited', 'date_retracted')


def full_native(files, count=1):
    """The native fixture with every optional column a current Messages database carries, all sent by the owner."""
    native, _ = files
    def adapt(db):
        for column in OPTIONAL_TEXT:
            db.execute(f'ALTER TABLE message ADD COLUMN "{column}" TEXT')
        for column in OPTIONAL_INT:
            db.execute(f'ALTER TABLE message ADD COLUMN "{column}" INTEGER DEFAULT 0')
        db.execute('UPDATE message SET is_from_me=1')
    native.write_bytes(snapshot(count=count, mutate=adapt))
    return native


def canonical_as_ingested(files):
    """Canonical rows as ingest wrote them from the native rows as they are now; change the native rows after."""
    from topos.permissions_v2.imessage_reconciliation import parse_reconciliation_snapshot
    native, canonical = files
    records = parse_reconciliation_snapshot(native.read_bytes(), now=NOW)
    with sqlite3.connect(canonical) as db:
        db.execute('DELETE FROM conversation_messages')
        columns = [r[1] for r in db.execute('PRAGMA table_info(conversation_messages)')]
        for record in records:
            row = {'message_id': record.message_id, 'source_record_id': record.message_id, 'source_id': 'imessage',
                   'dataset_id': 'dataset-native', 'owner_user_id': None, 'conversation_id': record.conversation_id,
                   'content': record.content, 'event_at': record.event_at, 'is_from_self': 1, 'sender_id': 'self',
                   'sender_type': 'human', 'actor_role': None, 'message_type': 'message',
                   'metadata_json': json.dumps({'message_guid': record.message_guid, 'chat_guid': record.chat_guid,
                                                'chat_identifier': record.chat_identifier, 'associated_message_type': 0})}
            db.execute('INSERT INTO conversation_messages VALUES (' + ','.join('?' for _ in columns) + ')',
                       [row.get(c) for c in columns])


def set_native(native, changes):
    with sqlite3.connect(native) as db:
        for rowid, columns in changes.items():
            for column, value in columns.items():
                db.execute(f'UPDATE message SET "{column}"=? WHERE ROWID=?', (value, rowid))


def form_buckets(counts):
    return {key: value for key, value in counts.items() if key.startswith('native_form_')}


def blob(name):
    from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
    return ATTRIBUTED_BODY_FIXTURES[name][0]


@pytest.mark.parametrize('column,value,bucket', [
    ('is_deleted', 1, 'native_form_deleted'),
    ('is_spam', 1, 'native_form_spam'),
    ('is_system_message', 1, 'native_form_system'),
    ('is_service_message', 1, 'native_form_system'),
    ('group_action_type', 1, 'native_form_system'),
    ('item_type', 1, 'native_form_system'),
    ('item_type', None, 'native_form_system'),
    ('associated_message_type', 2000, 'native_form_reaction'),
    ('associated_message_type', None, 'native_form_reaction'),
    ('associated_message_guid', 'p:0/synthetic', 'native_form_reaction'),
    ('is_forward', 1, 'native_form_forward_or_quote'),
    ('is_forwarded', 1, 'native_form_forward_or_quote'),
    ('forwarded_from', 'synthetic', 'native_form_forward_or_quote'),
    ('quoted_message_guid', 'synthetic', 'native_form_forward_or_quote'),
    # An inline reply is read (v3); the thread bucket holds only fields that are not a reply the reader reads.
    ('thread_originator_part', '0:0:10', 'native_form_thread_reply'),
    ('thread_originator_guid', 'two\nlines', 'native_form_thread_reply'),
    ('subject', 'Synthetic subject', 'native_form_subject'),
    # An attachment flag that is not 0 or 1: the reader does not read that body at all.
    ('cache_has_attachments', 2, 'native_form_attachment_unmeasured'),
    ('cache_has_attachments', None, 'native_form_attachment_unmeasured'),
])
def test_each_unsupported_form_lands_in_exactly_one_bucket(files, column, value, bucket):
    set_native(full_native(files), {1: {column: value}})
    counts = run(files)['counts']
    assert counts['native_message_form_unsupported'] == 1
    assert form_buckets(counts) == {bucket: 1}


@pytest.mark.parametrize('text,body,outcome', [
    # No caption: only an attachment, refused as a form.
    ('￼', None, 'native_form_attachment_only'),
    (None, 'typedstream_attachment', 'native_form_attachment_only'),
    (None, None, 'native_form_attachment_only'),
    # A caption: read, then compared (this fixture's stored row holds another body, so it mismatches).
    ('￼ look', None, 'native_observed_attachment_caption'),
    (None, 'typedstream_mixed', 'native_observed_attachment_caption'),
    # The two native representations must still agree, and an unreadable body is unreadable.
    ('￼', 'typedstream_mixed', 'native_body_representations_disagree'),
    (None, b'\x01\x02', 'native_attributed_body_unsupported'),
])
def test_an_attachment_is_read_for_its_caption_and_one_without_is_only_an_attachment(files, text, body, outcome):
    native = full_native(files)
    set_native(native, {1: {'cache_has_attachments': 1, 'text': text,
                            'attributedBody': blob(body) if isinstance(body, str) else body}})
    result = run(files)
    counts = result['counts']
    assert counts[outcome] == 1
    if outcome == 'native_form_attachment_only':
        assert form_buckets(counts) == {outcome: 1} and counts['native_message_form_unsupported'] == 1
    else:
        assert form_buckets(counts) == {} and 'native_message_form_unsupported' not in counts
    if outcome == 'native_observed_attachment_caption':
        assert counts['reconciliation_content_mismatch'] == 1
    assert 'look' not in json.dumps(result) and 'photo' not in json.dumps(result)


# Every adjacent pair of the order, so any reordering shows; then the fields a form check lets pass.
@pytest.mark.parametrize('columns,bucket', [
    ({'is_deleted': 1, 'is_spam': 1}, 'native_form_deleted'),
    ({'is_spam': 1, 'item_type': 3}, 'native_form_spam'),
    ({'is_system_message': 1, 'associated_message_type': 2000}, 'native_form_system'),
    ({'associated_message_type': 2000, 'is_forwarded': 1}, 'native_form_reaction'),
    ({'quoted_message_guid': 'synthetic', 'thread_originator_part': '0:0:10'}, 'native_form_forward_or_quote'),
    ({'thread_originator_part': '0:0:10', 'subject': 'Synthetic subject'}, 'native_form_thread_reply'),
    ({'subject': 'Synthetic subject', 'cache_has_attachments': 1}, 'native_form_subject'),
    ({'is_deleted': None, 'associated_message_type': 2000}, 'native_form_reaction'),
    ({'thread_originator_guid': '', 'subject': 'Synthetic subject'}, 'native_form_subject'),
    ({'quoted_message_guid': '', 'subject': 'Synthetic subject'}, 'native_form_subject'),
    # Neither Messages' chain to the preceding message nor a well-formed inline reply is a failing field.
    ({'reply_to_guid': 'synthetic', 'subject': 'Synthetic subject'}, 'native_form_subject'),
    ({'thread_originator_guid': 'synthetic', 'thread_originator_part': '0:0:10', 'subject': 'Synthetic subject'},
     'native_form_subject'),
])
def test_the_first_failing_field_wins(files, columns, bucket):
    set_native(full_native(files), {1: columns})
    assert form_buckets(run(files)['counts']) == {bucket: 1}


def test_edits_and_retractions_are_observed_beside_the_outcome(files):
    native = full_native(files, count=5)
    canonical_as_ingested(files)
    set_native(native, {1: {'date_edited': 5}, 2: {'text': 'changed natively'},
                        3: {'text': 'edited later', 'date_edited': 5, 'date_retracted': 7}})
    with sqlite3.connect(native) as db:
        db.execute('UPDATE message SET date=date+1000000000, date_edited=5 WHERE ROWID=5')
    counts = run(files)['counts']
    assert (counts['canonical_exact_match'], counts['reconciliation_content_mismatch'],
            counts['reconciliation_time_mismatch']) == (2, 2, 1)
    assert (counts['native_observed_edited'], counts['native_observed_retracted']) == (3, 1)
    assert (counts['native_observed_edited_exact_match'], counts['native_observed_edited_content_mismatch']) == (1, 1)


GARBAGE = b'\x01' * 65600  # counts toward the 4 MiB archive total, then fails to decode


def test_an_attachment_body_is_a_decision_and_counts_toward_the_archive_limit(files):
    """A caption is read inline, like any body: 63 archives stay under 4 MiB, a 64th attachment's pushes it over."""
    native = full_native(files, count=64)
    set_native(native, {row: {'text': None, 'attributedBody': GARBAGE} for row in range(2, 65)})
    assert run(files)['counts']['native_attributed_body_unsupported'] == 63
    set_native(native, {1: {'cache_has_attachments': 1, 'text': None, 'attributedBody': GARBAGE}})
    with pytest.raises(PolicyError) as refused:
        run(files)
    assert refused.value.code == 'native_probe_archive_limit'


def test_the_archive_limit_refuses_past_four_mebibytes(files):
    native = full_native(files, count=64)
    set_native(native, {row: {'text': None, 'attributedBody': GARBAGE} for row in range(1, 64)})
    assert run(files)['counts']['native_attributed_body_unsupported'] == 63
    set_native(native, {64: {'text': None, 'attributedBody': GARBAGE}})
    with pytest.raises(PolicyError) as refused:
        run(files)
    assert refused.value.code == 'native_probe_archive_limit'


def test_the_text_limit_refuses_past_one_mebibyte_in_total(files):
    native = full_native(files, count=17)
    set_native(native, {row: {'text': 'x' * 64000} for row in range(1, 17)})
    assert run(files)['counts']['native_text_supported'] == 17  # 16 long rows and one short one
    set_native(native, {17: {'text': 'x' * 64000}})
    with pytest.raises(PolicyError) as refused:
        run(files)
    assert refused.value.code == 'native_probe_text_limit'


@pytest.mark.parametrize('name', ['typedstream_mixed', 'typedstream_attachment', 'typedstream_plain', 'keyed_plain',
                                  'typedstream_multiline', 'keyed_multiline'])
def test_a_caption_is_the_body_without_its_placeholders_exactly_as_the_sync_stores_it(name):
    """Both archive formats; the fixture's second value is the body the sync itself stores for that archive."""
    from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
    from topos.ingestion.imessage_attributed_text import caption_text, decode_attributed_caption, decode_attributed_text
    from topos.ingestion.owner_snapshot import SnapshotRejected
    raw, stored = ATTRIBUTED_BODY_FIXTURES[name]
    body = decode_attributed_caption(raw)
    assert (caption_text(body) or None) == stored
    if '\ufffc' in body:
        with pytest.raises(SnapshotRejected):
            decode_attributed_text(raw)
    else:
        assert decode_attributed_text(raw) == body


def test_the_caption_decoder_keeps_every_other_refusal():
    from topos.ingestion.imessage_attributed_text import MAX_ARCHIVE_BYTES, caption_text, decode_attributed_caption
    from topos.ingestion.owner_snapshot import SnapshotRejected
    for raw in (None, 'text', b'', b'\x01\x02', b'bplist00' + b'\x00' * 8, b'\x01' * (MAX_ARCHIVE_BYTES + 1),
                blob('typedstream_empty')):
        with pytest.raises(SnapshotRejected, match='snapshot_attributed_text_unsupported'):
            decode_attributed_caption(raw)
    assert caption_text(None) == '' and caption_text('\ufffc \ufffc') == '' and caption_text(' a\r\nb\ufffc ') == 'a\nb'


def test_the_census_changes_no_decision_and_no_capture(files, monkeypatch):
    """Disabling every new counter leaves matched rows, captured row dicts and existing counts identical."""
    native = full_native(files, count=8)
    canonical_as_ingested(files)
    set_native(native, {1: {'date_edited': 1}, 2: {'date_edited': 1},
                        3: {'cache_has_attachments': 1, 'text': None, 'attributedBody': blob('typedstream_mixed')},
                        4: {'associated_message_type': 2000}, 5: {'thread_originator_guid': 'synthetic'},
                        6: {'text': 'changed natively', 'date_retracted': 1}})

    def census(**patches):
        for name, value in patches.items():
            monkeypatch.setattr(probe, name, value)
        seen = []
        result = run(files, _on_match=lambda row, chat: seen.append((sorted(row.items()), chat)))
        return seen, {k: v for k, v in result['counts'].items() if not k.startswith(('native_form_', 'native_observed_'))}, result['counts']
    matched, kept, full = census()
    assert matched and all(not key.startswith('_observed_') for row, _ in matched for key, _value in row)
    assert sum(form_buckets(full).values()) == full['native_message_form_unsupported']
    assert full['native_observed_edited'] == 2 and full['native_observed_retracted'] == 1
    plain_matched, plain_kept, _ = census(_OBSERVED=(), _form_bucket=lambda row: 'native_form_other')
    assert (matched, kept) == (plain_matched, plain_kept)
