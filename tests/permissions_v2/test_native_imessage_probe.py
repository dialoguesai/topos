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
    ("UPDATE message SET cache_has_attachments=1 WHERE ROWID=1", 'native_message_form_unsupported'),
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
PATH = '/v1/permissions-beta/v2/imessage/preflight'


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
        response = client.post('/v1/permissions-beta/v2/imessage/recover', json=BODY | {'owner_attestation': OWNER_ATTESTATION})
    assert response.status_code == 403 and calls == []
