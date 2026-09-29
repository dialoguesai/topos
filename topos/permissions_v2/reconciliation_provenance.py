"""Private, revocable proof for existing native iMessage rows.

Uses the existing rollback-pinned owner ledger, but a distinct reader/record
contract. It never writes canonical owner fields or permits historical collisions
in the normal snapshot importer. Match observations alone cannot reach a reader.
"""
from collections import Counter
from datetime import datetime, timezone
import secrets
import re
import time

from .canonical import PolicyError
from .evidence import _owner, _row_revision
from .fact_eligibility import canonical_utc_microseconds
from .imessage_reconciliation import ATTRIBUTED_CONTRACT, compare_existing_message, parse_reconciliation_snapshot
from .ingest_provenance import OWNER_ATTESTATION, _identifier, _json, _lane, _read_json

ORIGIN = 'owner-native-reconciliation/v1'
# A refresh may not drop, by its window choice alone, a link this young: a 30-day grant
# could still release that message. Narrowing below it is refused rather than reported.
REFRESH_MINIMUM_COVERAGE_SECONDS = 30 * 86400


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


def refresh_existing(service, conn, *, dataset_id, snapshot_id, snapshot_sha256, owner_attestation,
                     window_start_us=None, now_seconds=None):
    """Owner-only: re-prove a dataset's one recovery enrollment against a fresh capture (RD8).

    One enrollment per dataset proves only the rows its capture held, so under a rolling
    grant window its pool drains, and one move of the ingest source clock stales all of it.
    A refresh keeps the same enrollment row -- the same id, the same dataset -- and in one
    ledger transaction points it at the new capture, at the next revision and the current
    source generation, compares every captured row exactly again and links it at that
    revision.

    A link the new capture does not re-prove is removed. Its evidence is not in the snapshot
    the enrollment now names, so carrying it to the new revision would relabel provenance.
    A re-proven link keeps its whole-message ceiling only while the row revision that ceiling
    was computed on is unchanged: the ceiling only ever raises a release bar, so dropping it
    for unchanged content would widen, and keeping it for changed content would label text
    it never saw. A revoked enrollment is never refreshed. A window that starts too late is
    refused: the refresh must not drop, by window choice alone, a link younger than
    REFRESH_MINIMUM_COVERAGE_SECONDS (`reconciliation_refresh_window_too_short`); an empty
    capture is refused too. Any refusal or mismatch rolls the whole refresh back and leaves the
    previous proof exactly as it was. Like publication and revocation it advances the protection
    clock once. Returns counts only.
    """
    _owner(service.binding)
    _identifier(dataset_id)
    if owner_attestation != OWNER_ATTESTATION:
        raise PolicyError('ingest_owner_attestation_required')
    actual, data = service._snapshot(snapshot_id, ATTRIBUTED_CONTRACT)
    if actual['snapshot_sha256'] != snapshot_sha256:
        raise PolicyError('ingest_snapshot_changed')
    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=ATTRIBUTED_CONTRACT)
    if not native:
        raise PolicyError('reconciliation_empty')
    counts = Counter()
    with service._transaction(conn):
        found = conn.execute('SELECT enrollment_id FROM ingest_provenance_enrollments WHERE dataset_id=?',
                             (dataset_id,)).fetchall()
        if len(found) != 1:
            raise PolicyError('reconciliation_refresh_unenrolled')
        enrollment_id = found[0][0]
        # Not active=True: a stale enrollment is exactly what a refresh may bring current.
        enrollment = service._enrollment(conn, enrollment_id, source_id='imessage')
        if enrollment['lane'].reader_contract != ATTRIBUTED_CONTRACT:
            raise PolicyError('reconciliation_lane_required')
        if enrollment['state'] != 'active':
            raise PolicyError('reconciliation_enrollment_revoked')
        service._source_enabled(conn, dataset_id, 'imessage')
        previous = _read_json(enrollment['snapshot_json'])
        if previous == actual:
            raise PolicyError('reconciliation_refresh_unchanged')
        jobs = conn.execute('SELECT status,enrollment_revision FROM ingest_provenance_jobs WHERE enrollment_id=?',
                            (enrollment_id,)).fetchall()
        if [tuple(job) for job in jobs] != [('done', enrollment['revision'])]:
            raise PolicyError('reconciliation_refresh_incomplete')
        if conn.execute('SELECT 1 FROM ingest_provenance_records WHERE enrollment_id=? AND enrollment_revision!=?',
                        (enrollment_id, enrollment['revision'])).fetchone():
            raise PolicyError('reconciliation_refresh_incomplete')
        prior = {message_id: _read_json(identity) for message_id, identity in conn.execute(
            'SELECT message_id,row_identity FROM ingest_provenance_records WHERE enrollment_id=?', (enrollment_id,))}
        revision = enrollment['revision'] + 1
        generation = conn.execute('SELECT generation FROM ingest_provenance_state WHERE singleton=1').fetchone()[0]
        from topos.principal import current_principal
        moved = conn.execute("UPDATE ingest_provenance_enrollments SET snapshot_json=?,revision=?,source_generation=?,"
                             "authorized_at=?,channel=? WHERE enrollment_id=? AND revision=? AND state='active'",
                             (_json(actual), revision, generation, int(time.time()), current_principal().channel,
                              enrollment_id, enrollment['revision'])).rowcount
        if moved != 1:
            raise PolicyError('reconciliation_refresh_conflict')
        conn.execute('DELETE FROM ingest_provenance_jobs WHERE enrollment_id=?', (enrollment_id,))
        job_id = 'reconciliation-job-' + secrets.token_hex(16)
        conn.execute("INSERT INTO ingest_provenance_jobs VALUES(?,?,?,'running',NULL,NULL,NULL)",
                     (job_id, enrollment_id, revision))
        for record in native:
            row = _canonical_row(conn, record.message_id)
            match = compare_existing_message(row, record, dataset_id=dataset_id, owner_id=service.binding.owner_id)
            before = prior.pop(record.message_id, None)
            same_row = before is not None and before.get('row_revision') == match.canonical_revision
            identity = _json({'version': ORIGIN, 'row_revision': match.canonical_revision,
                              'native_event_nanoseconds': str(record.native_event_nanoseconds),
                              'classification': before.get('classification') if same_row else None})
            if before is not None:
                conn.execute('UPDATE ingest_provenance_records SET enrollment_revision=?,job_id=?,row_identity=? '
                             'WHERE message_id=? AND enrollment_id=?',
                             (revision, job_id, identity, record.message_id, enrollment_id))
                counts['reproven' if same_row else 'reproven_row_changed'] += 1
                if same_row and before.get('classification') is not None:
                    counts['ceiling_carried'] += 1
                continue
            if conn.execute('SELECT 1 FROM ingest_provenance_records WHERE message_id=?', (record.message_id,)).fetchone():
                # The capture leaves out rows another enrollment proves; one appearing now is a race.
                raise PolicyError('reconciliation_row_owned_elsewhere')
            conn.execute('INSERT INTO ingest_provenance_records VALUES(?,?,?,?,?)',
                         (record.message_id, enrollment_id, revision, job_id, identity))
            counts['linked_new'] += 1
        keep_after_us = ((int(time.time()) if now_seconds is None else now_seconds)
                         - REFRESH_MINIMUM_COVERAGE_SECONDS) * 1_000_000
        for message_id in prior:
            event = conn.execute('SELECT event_at FROM conversation_messages WHERE message_id=?', (message_id,)).fetchone()
            event_us = canonical_utc_microseconds(event[0]) if event is not None else None
            aged = window_start_us is not None and event_us is not None and event_us < window_start_us
            if aged and event_us >= keep_after_us:
                raise PolicyError('reconciliation_refresh_window_too_short')
            counts['dropped_before_window' if aged else 'dropped_unproven'] += 1
        conn.execute('DELETE FROM ingest_provenance_records WHERE enrollment_id=? AND enrollment_revision=?',
                     (enrollment_id, enrollment['revision']))
        if service._snapshot(actual['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != actual:
            raise PolicyError('ingest_snapshot_changed')
        service._enrollment(conn, enrollment_id, active=True, source_id='imessage')
        result = {'status': 'ok', 'messages_created': 0, 'conversations_created': 0,
                  'messages_processed': len(native), 'historical_skipped': 0}
        conn.execute("UPDATE ingest_provenance_jobs SET status='done',result_json=? WHERE job_id=?",
                     (_json(result), job_id))
        # Search statistics must be rebuilt after proof publication, as in publish_existing.
        conn.execute('UPDATE permissions_v2_protection_state SET generation=generation+1 WHERE singleton=1')
    counts['previous_capture_removed'] = int(discard_capture(service, conn, previous))
    return dict(sorted(counts.items()))


def discard_capture(service, conn, snapshot) -> bool:
    """Delete a private capture no enrollment names. Best effort, never raises.

    A refresh leaves its previous capture unreferenced, and a refused refresh leaves its own
    new one unreferenced. Neither is evidence of anything any more, and both hold message
    text, so neither is kept. A capture an enrollment still names is never touched: that is
    the only copy its links can be checked against.
    """
    try:
        named = {_read_json(value).get('snapshot_id') for (value,) in conn.execute(
            'SELECT snapshot_json FROM ingest_provenance_enrollments')}
        if snapshot['snapshot_id'] in named:
            return False
        path = service.root / (snapshot['snapshot_id'] + _lane(snapshot['reader_contract']).suffix)
        path.unlink()
        return True
    except (OSError, PolicyError, KeyError, TypeError):
        return False


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
