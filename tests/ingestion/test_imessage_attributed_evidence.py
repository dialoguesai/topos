"""Evidence decoding uses Foundation-generated synthetic archives, never owner data."""
import plistlib
import pytest

from tests.fixtures.imessage.attributed_body_blobs import ATTRIBUTED_BODY_FIXTURES
from tests.permissions_v2.test_imessage_reconciliation import snapshot
from tests.ingestion.test_owner_snapshot import NOW
from topos.ingestion.imessage_attributed_text import decode_attributed_text, MAX_ARCHIVE_BYTES
from topos.ingestion.owner_snapshot import SnapshotRejected, parse_imessage_snapshot, parse_imessage_attributed_snapshot
from topos.permissions_v2.imessage_reconciliation import parse_reconciliation_snapshot, ATTRIBUTED_CONTRACT
from dataclasses import replace


@pytest.mark.parametrize('name', sorted(ATTRIBUTED_BODY_FIXTURES))
def test_exact_supported_foundation_backing_text(name):
    raw, expected = ATTRIBUTED_BODY_FIXTURES[name]
    if expected is None or name == 'typedstream_mixed':
        with pytest.raises(SnapshotRejected):
            decode_attributed_text(raw)
    else:
        assert decode_attributed_text(raw) == expected


def test_v1_stays_closed_and_v2_preserves_every_other_native_check():
    raw, expected = ATTRIBUTED_BODY_FIXTURES['typedstream_plain']
    data = snapshot(count=1, mutate=lambda db: db.execute('UPDATE message SET text=NULL,attributedBody=?', (raw,)))
    with pytest.raises(SnapshotRejected):
        parse_imessage_snapshot(data, 'synthetic', now=NOW)
    parsed = parse_imessage_attributed_snapshot(data, 'synthetic', now=NOW)
    assert parsed[0]['content'] == expected and parsed[0]['is_from_self'] is True
    observations = parse_reconciliation_snapshot(data, now=NOW, reader_contract=ATTRIBUTED_CONTRACT)
    assert observations[0].content == expected
    assert observations[0].reader_contract == ATTRIBUTED_CONTRACT


@pytest.mark.parametrize('sql', [
    "UPDATE message SET text='a different body'", 'UPDATE message SET cache_has_attachments=1',
    "UPDATE message SET associated_message_guid='quoted'", 'UPDATE message SET associated_message_type=2000',
    'UPDATE message SET item_type=1',
])
def test_archive_decoder_does_not_excuse_conflicting_or_unsupported_forms(sql):
    def mutate(db):
        db.execute('UPDATE message SET text=NULL,attributedBody=?', (ATTRIBUTED_BODY_FIXTURES['keyed_plain'][0],))
        db.execute(sql)
    with pytest.raises(SnapshotRejected):
        parse_imessage_attributed_snapshot(snapshot(count=1, mutate=mutate), 'synthetic', now=NOW)


@pytest.mark.parametrize('raw', [
    b'', b'x' * (MAX_ARCHIVE_BYTES + 1), b'not-an-archive',
    b'\x04\x0bstreamtyped NSString\x84\x01+\x07forged!',
    ATTRIBUTED_BODY_FIXTURES['typedstream_plain'][0][:-1],
])
def test_malformed_or_scraped_bodies_never_become_evidence(raw):
    with pytest.raises(SnapshotRejected, match='snapshot_attributed_text_unsupported'):
        decode_attributed_text(raw)


def test_keyed_root_cannot_point_to_an_attribute_or_forged_class():
    base = ATTRIBUTED_BODY_FIXTURES['keyed_plain'][0]
    archive = plistlib.loads(base)
    archive['$top']['root'] = plistlib.UID(5)
    with pytest.raises(SnapshotRejected):
        decode_attributed_text(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))
    archive = plistlib.loads(base)
    root = archive['$objects'][archive['$top']['root'].data]
    archive['$objects'][root['$class'].data]['$classname'] = 'UnknownExecutableClass'
    with pytest.raises(SnapshotRejected):
        decode_attributed_text(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))


def test_keyed_text_whitespace_is_not_silently_normalized():
    archive = plistlib.loads(ATTRIBUTED_BODY_FIXTURES['keyed_plain'][0])
    archive['$objects'][2]['NS.string'] = '  original\r\ntext  '
    assert decode_attributed_text(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)) == '  original\r\ntext  '


def test_native_nanoseconds_are_preserved_and_legacy_rounding_is_exactly_bound():
    from tests.ingestion.test_owner_snapshot import DATE
    from tests.permissions_v2.test_imessage_reconciliation import sample
    from topos.permissions_v2.imessage_reconciliation import compare_existing_message
    from topos.permissions_v2.canonical import PolicyError
    data = snapshot(count=1, mutate=lambda db: db.execute('UPDATE message SET date=?', (DATE + 123,)))
    records = parse_imessage_attributed_snapshot(data, 'synthetic', now=NOW)
    assert records[0]['native_event_nanoseconds'] == DATE + 123
    assert records[0]['ts'].endswith('.123456123+00:00')
    with pytest.raises(SnapshotRejected, match='snapshot_time_unsupported'):
        parse_imessage_snapshot(data, 'synthetic', now=NOW)
    native = parse_reconciliation_snapshot(data, now=NOW, reader_contract=ATTRIBUTED_CONTRACT)[0]
    row, _ = sample()
    assert compare_existing_message(row, native, dataset_id='dataset-native', owner_id='owner-synthetic')
    row['event_at'] = '2023-03-08T20:26:40.123457+00:00'
    with pytest.raises(PolicyError, match='time_mismatch'):
        compare_existing_message(row, native, dataset_id='dataset-native', owner_id='owner-synthetic')
    row, _ = sample()
    with pytest.raises(PolicyError, match='time_mismatch'):
        compare_existing_message(row, replace(native, event_at='2023-03-08T20:26:40.123456124+00:00'),
            dataset_id='dataset-native', owner_id='owner-synthetic')
