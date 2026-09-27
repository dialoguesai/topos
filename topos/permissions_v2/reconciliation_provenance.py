"""Private, revocable proof for existing native iMessage rows.

Uses the existing rollback-pinned owner ledger, but a distinct reader/record
contract. It never writes canonical owner fields or permits historical collisions
in the normal snapshot importer. Match observations alone cannot reach a reader.
"""
from datetime import datetime, timezone
import secrets
import re

from .canonical import PolicyError
from .evidence import _owner, _row_revision
from .imessage_reconciliation import ATTRIBUTED_CONTRACT, compare_existing_message, parse_reconciliation_snapshot
from .ingest_provenance import _json, _read_json

ORIGIN = 'owner-native-reconciliation/v1'


def _canonical_row(conn, message_id):
    cursor = conn.execute('SELECT * FROM conversation_messages WHERE message_id=? LIMIT 2', (message_id,))
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise PolicyError('reconciliation_canonical_ambiguous')
    return dict(zip((column[0] for column in cursor.description), rows[0]))


def publish_existing(service, conn, *, enrollment_id, derive=None, classifications=None):
    """Owner-only atomic links + optional trusted fact producer + completion.

    `derive` is an in-process function, never a request field or a serialized
    permission. It runs after comparison in the same transaction; any exception
    rolls back every link and fact. Callers must prepare model work beforehand.
    """
    _owner(service.binding)
    enrollment = service._enrollment(conn, enrollment_id, active=True, source_id='imessage')
    if enrollment['lane'].reader_contract != ATTRIBUTED_CONTRACT:
        raise PolicyError('reconciliation_lane_required')
    expected = _read_json(enrollment['snapshot_json'])
    actual, data = service._snapshot(expected['snapshot_id'], ATTRIBUTED_CONTRACT)
    if actual != expected:
        raise PolicyError('ingest_snapshot_changed')
    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=ATTRIBUTED_CONTRACT)
    if not native:
        raise PolicyError('reconciliation_empty')
    with service._transaction(conn):
        enrollment = service._enrollment(conn, enrollment_id, active=True, source_id='imessage')
        if conn.execute('SELECT 1 FROM ingest_provenance_jobs WHERE enrollment_id=?', (enrollment_id,)).fetchone():
            raise PolicyError('reconciliation_already_published')
        job_id = 'reconciliation-job-' + secrets.token_hex(16)
        conn.execute("INSERT INTO ingest_provenance_jobs VALUES(?,?,?,'running',NULL,NULL,NULL)",
                     (job_id, enrollment_id, enrollment['revision']))
        rows = []
        for record in native:
            row = _canonical_row(conn, record.message_id)
            match = compare_existing_message(row, record, dataset_id=enrollment['dataset_id'], owner_id=service.binding.owner_id)
            identity = {'version': ORIGIN, 'row_revision': match.canonical_revision,
                        'native_event_nanoseconds': str(record.native_event_nanoseconds),
                        'classification': (classifications or {}).get(record.message_id)}
            if identity['classification'] is not None:
                from .reconciliation_facts import validated_classification
                validated_classification(identity['classification'])
            conn.execute('INSERT INTO ingest_provenance_records VALUES(?,?,?,?,?)',
                (record.message_id, enrollment_id, enrollment['revision'], job_id, _json(identity)))
            rows.append(row)
        if derive is not None:
            derive(conn, tuple(rows))
        # The producer cannot alter the native consent surface during derivation.
        for record in native:
            row = _canonical_row(conn, record.message_id)
            match = compare_existing_message(row, record, dataset_id=enrollment['dataset_id'], owner_id=service.binding.owner_id)
            linked = _read_json(conn.execute('SELECT row_identity FROM ingest_provenance_records WHERE message_id=?',
                (record.message_id,)).fetchone()[0])
            if linked['row_revision'] != match.canonical_revision:
                raise PolicyError('reconciliation_canonical_changed')
        if service._snapshot(expected['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != expected:
            raise PolicyError('ingest_snapshot_changed')
        service._enrollment(conn, enrollment_id, active=True, source_id='imessage')
        result = {'status': 'ok', 'messages_created': 0, 'conversations_created': 0,
                  'messages_processed': len(rows), 'historical_skipped': 0}
        conn.execute("UPDATE ingest_provenance_jobs SET status='done',result_json=? WHERE job_id=?",
                     (_json(result), job_id))
        # Search statistics must be rebuilt after proof publication/revocation.
        conn.execute('UPDATE permissions_v2_protection_state SET generation=generation+1 WHERE singleton=1')
    return {'job_id': job_id, 'reconciled': len(rows)}


def validate_existing(service, conn, *, message_id, dataset_id, with_classification=False):
    """Current proof or refusal; no missing-link fallback for a NULL owner."""
    service._check(conn)
    link = conn.execute('SELECT enrollment_id,enrollment_revision,job_id,row_identity '
                        'FROM ingest_provenance_records WHERE message_id=?', (message_id,)).fetchone()
    if link is None:
        raise PolicyError('reconciliation_origin_unavailable')
    enrollment = service._enrollment(conn, link[0], active=True, source_id='imessage')
    if (enrollment['lane'].reader_contract != ATTRIBUTED_CONTRACT
            or enrollment['dataset_id'] != dataset_id or enrollment['revision'] != link[1]):
        raise PolicyError('reconciliation_origin_unavailable')
    job = conn.execute('SELECT enrollment_id,enrollment_revision,status FROM ingest_provenance_jobs WHERE job_id=?',
                       (link[2],)).fetchone()
    if job is None or tuple(job) != (link[0], link[1], 'done'):
        raise PolicyError('reconciliation_origin_unavailable')
    evidence = _read_json(link[3])
    if (set(evidence) != {'version', 'row_revision', 'native_event_nanoseconds', 'classification'}
            or evidence['version'] != ORIGIN or type(evidence['native_event_nanoseconds']) is not str
            or re.fullmatch(r'[1-9][0-9]{17,18}', evidence['native_event_nanoseconds']) is None
            or not 10**17 <= int(evidence['native_event_nanoseconds']) <= 2**63 - 1):
        raise PolicyError('reconciliation_origin_unavailable')
    row = _canonical_row(conn, message_id)
    if (row.get('owner_user_id') not in (None, service.binding.owner_id)
            or row.get('dataset_id') != dataset_id or row.get('source_id') != 'imessage'
            or _row_revision(row, table='conversation_messages') != evidence['row_revision']):
        raise PolicyError('reconciliation_origin_unavailable')
    snapshot = _read_json(enrollment['snapshot_json'])
    if service._snapshot(snapshot['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != snapshot:
        raise PolicyError('ingest_snapshot_changed')
    if with_classification:
        return {'_p2b_native_event_nanoseconds': evidence['native_event_nanoseconds'],
                '_p2b_native_classification': _json(evidence['classification']) if evidence['classification'] is not None else None}
    return evidence['native_event_nanoseconds']


def native_time_within(row, lower_us, upper_us):
    """Additional conservative ceiling for a verified native timestamp.

    Callers still enforce the signed canonical-time semantics too. Checking both
    prevents legacy float rounding from moving a native event across a boundary.
    A caller-supplied row cannot establish this field's provenance; the resolver
    is the only adapter that can add it to authenticated evidence.
    """
    key = '_p2b_native_event_nanoseconds'
    if key not in row:
        return True
    value = row[key]
    return (type(value) is str and re.fullmatch(r'[1-9][0-9]{17,18}', value) is not None
            and 10**17 <= int(value) <= 2**63 - 1
            and lower_us * 1000 <= int(value) + 978307200 * 1_000_000_000 <= upper_us * 1000)
