"""Existing-row authority requires durable native proof, never an owner-field repair."""
import sqlite3
import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_imessage_reconciliation import snapshot, sample
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION, IngestProvenanceService
from topos.permissions_v2.reconciliation_provenance import publish_existing, validate_existing, native_time_within


@pytest.fixture
def legacy(ingest_fixture, request):
    service, conn, path = ingest_fixture
    path.chmod(0o600)
    content = ('I will be at Example Place!' if getattr(request, 'param', None) == 'visit'
               else 'I am working on Synthetic message at work.')
    if getattr(request, 'param', None) == 'goal':
        content = 'My goal is to finish the compiler at work by Friday.'
    path.write_bytes(snapshot(count=1, mutate=lambda db: db.execute('UPDATE message SET text=?', (content,))))
    path.chmod(0o400)
    row, _ = sample()
    row['content'] = content
    row['dataset_id'] = 'native-dataset'
    columns = [r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')]
    conn.execute('INSERT INTO conversation_messages VALUES(' + ','.join('?' for _ in columns) + ')',
                 [row.get(c) for c in columns])
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
    from topos.permissions_v2.protection_clock import resync_identity_coverage
    from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
    apply_wiki_entities_v1_up(conn)
    add_entity(conn, 'owner-entity')
    if getattr(request, 'param', None) == 'unattested':
        for i in range(3):
            add_entity(conn, 'ambiguous-self-' + str(i))
    conn.execute('CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)')
    conn.commit()
    clock = conn.execute('SELECT clock_id,generation FROM permissions_v2_protection_state').fetchone()
    resync_identity_coverage(service.resolver.path, owner_id='owner-1', expected_clock_id=clock[0], expected_generation=clock[1])
    if getattr(request, 'param', None) != 'unattested':
        do_attest(conn, 'owner-entity')
    conn.commit()
    with owner():
        desc = service.describe_snapshot(conn, snapshot_id='canary', reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id='canary', dataset_id='native-dataset',
            snapshot_sha256=desc['snapshot_sha256'], owner_attestation=OWNER_ATTESTATION,
            reader_contract=ATTRIBUTED_CONTRACT)
    return service, conn, path, enrollment['enrollment_id']


def validated(service, conn):
    return validate_existing(service, conn, message_id='imessage:1', dataset_id='native-dataset')


def publish(fixture, **kwargs):
    service, conn, _, enrollment = fixture
    with owner():
        return publish_existing(service, conn, enrollment_id=enrollment, **kwargs)


def test_old_row_is_unavailable_until_completed_proof_then_survives_service_restart(legacy):
    service, conn, _, _ = legacy
    before = conn.execute('SELECT * FROM conversation_messages').fetchall()
    with pytest.raises(PolicyError):
        validated(service, conn)
    assert publish(legacy)['reconciled'] == 1
    assert validated(service, conn) == '700000000123456000'
    reopened = IngestProvenanceService(canonical_database=service.resolver.path,
        binding=service.binding, snapshot_root=service.root)
    assert validated(reopened, conn) == validated(service, conn)
    assert conn.execute('SELECT * FROM conversation_messages').fetchall() == before
    assert conn.execute('SELECT owner_user_id FROM conversation_messages').fetchone()[0] is None


def test_resolver_requires_proof_for_null_owner_and_keeps_canonical_row_untouched(legacy):
    service, conn, _, _ = legacy
    resolver = service.resolver
    identity = resolver._identity('conversation_messages', 'imessage:1', 'imessage', 'native-dataset')
    conn.row_factory = sqlite3.Row
    with pytest.raises(PolicyError):
        resolver._load(conn, identity)
    publish(legacy)
    loaded = resolver._load(conn, identity)
    assert loaded['_p2b_native_event_nanoseconds'] == '700000000123456000'
    assert loaded['owner_user_id'] is None
    assert resolver._validate_native_origin(conn, identity, loaded) is True


@pytest.mark.parametrize('kwargs', [{'actor': 'another'}, {'channel': 'local_http'}, {'cls': 'third_party'}])
def test_only_authenticated_owner_can_publish(legacy, kwargs):
    service, conn, _, enrollment = legacy
    with owner(**kwargs), pytest.raises(PolicyError):
        publish_existing(service, conn, enrollment_id=enrollment)
    assert conn.execute('SELECT COUNT(*) FROM ingest_provenance_records').fetchone()[0] == 0


def test_revocation_advances_search_generation_and_withholds_link(legacy):
    service, conn, _, enrollment = legacy
    publish(legacy)
    generation = conn.execute('SELECT generation FROM permissions_v2_protection_state').fetchone()[0]
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
    assert conn.execute('SELECT generation FROM permissions_v2_protection_state').fetchone()[0] == generation + 1
    with pytest.raises(PolicyError):
        validated(service, conn)


@pytest.mark.parametrize('sql', [
    "UPDATE conversation_messages SET content='changed'",
    "UPDATE conversation_messages SET owner_user_id='another'",
    "UPDATE conversation_messages SET dataset_id='another'",
    "UPDATE conversation_messages SET actor_role='addressed'",
    'UPDATE conversation_messages SET is_from_self=0',
    "UPDATE conversation_messages SET metadata_json='{}'",
    'DELETE FROM ingest_provenance_records',
    "UPDATE ingest_provenance_jobs SET status='running'",
    "UPDATE user_ingestion_sources SET enabled=0",
])
def test_changed_canonical_proof_or_source_never_keeps_authority(legacy, sql):
    service, conn, _, _ = legacy
    publish(legacy)
    conn.execute(sql)
    conn.commit()
    with pytest.raises(PolicyError):
        validated(service, conn)


def test_snapshot_removal_and_marker_loss_cannot_fall_back_to_sender_flags(legacy):
    service, conn, path, _ = legacy
    publish(legacy)
    raw = path.read_bytes()
    path.unlink()
    with pytest.raises(PolicyError):
        validated(service, conn)
    path.write_bytes(raw)
    path.chmod(0o400)
    service.marker.unlink()
    with pytest.raises(PolicyError):
        validated(service, conn)


def test_derivation_failure_rolls_back_all_links_and_facts(legacy):
    service, conn, _, _ = legacy
    def failed(db, rows):
        assert len(rows) == 1
        db.execute("INSERT INTO engine_config VALUES('synthetic-fact-marker','pending')")
        raise ValueError('synthetic producer failed')
    with pytest.raises(ValueError):
        publish(legacy, derive=failed)
    assert conn.execute('SELECT COUNT(*) FROM ingest_provenance_records').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ingest_provenance_jobs').fetchone()[0] == 0
    assert conn.execute("SELECT 1 FROM engine_config WHERE key='synthetic-fact-marker'").fetchone() is None
    assert publish(legacy)['reconciled'] == 1


def test_new_legacy_writes_cannot_gain_proof_by_copying_existing_flags(legacy):
    service, conn, _, _ = legacy
    publish(legacy)
    conn.execute("UPDATE conversation_messages SET message_id='imessage:2'")
    conn.commit()
    with pytest.raises(PolicyError):
        validate_existing(service, conn, message_id='imessage:2', dataset_id='native-dataset')


def test_native_boundary_prevents_float_rounding_from_crossing_window():
    epoch_ns = 978307200 * 1_000_000_000
    event = 700_000_000_000_000_123
    floor_us = (event + epoch_ns) // 1000
    row = {'_p2b_native_event_nanoseconds': str(event)}
    assert native_time_within(row, floor_us, floor_us + 1)
    assert not native_time_within(row, floor_us, floor_us)
    assert not native_time_within(row, floor_us + 1, floor_us + 2)
    assert not native_time_within({'_p2b_native_event_nanoseconds': True}, 0, 2**63)
