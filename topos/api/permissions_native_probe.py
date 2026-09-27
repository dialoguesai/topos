"""Local-owner-only diagnostic. No native paths, text or release authority on the wire."""
import asyncio
from datetime import datetime, timezone
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.contract import Identifier, StrictModel

router = APIRouter(prefix='/v1/permissions-beta/v2/imessage', tags=['permissions-owner-maintenance'])


class NativeProbeRequest(StrictModel):
    dataset_id: Identifier
    starts_at: str
    ends_at: str


@router.post('/preflight')
async def preflight(body: NativeProbeRequest, principal=Depends(resolve_request_principal)):
    from topos.principal import OWNER_APP
    if principal is None or principal.cls != OWNER_APP or principal.channel != 'uds':
        raise HTTPException(403, 'owner_socket_required')

    def apply():
        from topos.permissions_v2.runtime import get_runtime
        from topos.permissions_v2.native_imessage_probe import probe_native_messages
        runtime = get_runtime()
        identity = runtime.protocol.ledger.identity
        if principal.acting_user and principal.acting_user != identity.owner_id:
            raise PolicyError('owner_binding')
        path = runtime.protocol.canonical_database
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            return probe_native_messages(conn, dataset_id=body.dataset_id, owner_id=identity.owner_id,
                starts_at=body.starts_at, ends_at=body.ends_at, now=datetime.now(timezone.utc))
        finally:
            conn.close()
    try:
        result = await asyncio.to_thread(apply)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except PolicyError as exc:
        raise HTTPException(503, exc.code, headers={'Cache-Control': 'no-store'}) from None
    except Exception:
        raise HTTPException(503, 'native_probe_unavailable', headers={'Cache-Control': 'no-store'}) from None


class NativeRecoveryRequest(NativeProbeRequest):
    owner_attestation: str


# Recovery is a bounded maintenance operation, never a sync loop or a recipient
# API. One run per process avoids duplicate paid/local preparation on retries.
import threading
_RECOVERY_LOCK = threading.Lock()


@router.post('/recover')
async def recover(body: NativeRecoveryRequest, principal=Depends(resolve_request_principal)):
    from topos.principal import OWNER_APP
    from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION
    if principal is None or principal.cls != OWNER_APP or principal.channel != 'uds':
        raise HTTPException(403, 'owner_socket_required')
    if body.owner_attestation != OWNER_ATTESTATION:
        raise HTTPException(422, 'ingest_owner_attestation_required')
    if not _RECOVERY_LOCK.acquire(blocking=False):
        raise HTTPException(409, 'native_recovery_running')

    def apply():
        from dataclasses import replace
        from topos.permissions_v2.runtime import get_runtime
        from topos.permissions_v2.native_imessage_probe import capture_matching_snapshot
        from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
        from topos.permissions_v2.reconciliation_provenance import publish_existing
        from topos.permissions_v2.reconciliation_facts import prepare_facts, derive_prepared
        from topos.permissions_v2.reconciliation_facts import owner_subject
        from topos.principal import set_principal, reset_principal
        runtime = get_runtime()
        identity = runtime.protocol.ledger.identity
        if principal.acting_user and principal.acting_user != identity.owner_id:
            raise PolicyError('owner_binding')
        token = set_principal(replace(principal, acting_user=identity.owner_id))
        db = None
        try:
            service = runtime.ingestion()
            db = runtime.ingestion_connection()
            owner_subject(db)
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_enrollments'").fetchone():
                if db.execute('SELECT 1 FROM ingest_provenance_enrollments WHERE dataset_id=?', (body.dataset_id,)).fetchone():
                    raise PolicyError('native_recovery_already_enrolled')
            # The configured private directories are installed by the operator,
            # not picked or chmodded from request fields.
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            snapshot_id, measured = capture_matching_snapshot(db, snapshot_root=service.root,
                dataset_id=body.dataset_id, owner_id=identity.owner_id, starts_at=body.starts_at,
                ends_at=body.ends_at, now=datetime.now(timezone.utc))
            description, data = service._snapshot(snapshot_id, ATTRIBUTED_CONTRACT)
            records = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=ATTRIBUTED_CONTRACT)
            boundary = service.resolver.entity_boundary(db)
            rows = []
            withheld = 0
            for record in records:
                row = dict(db.execute('SELECT * FROM conversation_messages WHERE message_id=?', (record.message_id,)).fetchone())
                try:
                    boundary.check(table='conversation_messages', record_id=record.message_id,
                                   source_id='imessage', dataset_id=body.dataset_id, row=row)
                    rows.append(row)
                except PolicyError:
                    withheld += 1
            db.rollback()
            db.execute('PRAGMA query_only=OFF')
            # No writer lock or live DB transaction during local inference.
            prepared, stats = asyncio.run(prepare_facts(rows))
            if not prepared:
                return {'authority_created': False, 'counts': measured['counts'], 'preparation': stats,
                        'boundary_withheld': withheld}
            enrollment = service.enroll(db, snapshot_id=snapshot_id, dataset_id=body.dataset_id,
                snapshot_sha256=description['snapshot_sha256'], owner_attestation=body.owner_attestation,
                reader_contract=ATTRIBUTED_CONTRACT)
            derived = {}
            def derive(conn, current):
                derived.update(derive_prepared(conn, current, prepared))
            result = publish_existing(service, db, enrollment_id=enrollment['enrollment_id'], derive=derive,
                classifications={key: item['classification'] for key, item in prepared.items()})
            return {'authority_created': True, 'reconciled': result['reconciled'], 'preparation': stats,
                    'derivation': derived, 'boundary_withheld': withheld}
        finally:
            if db is not None:
                db.close()
            reset_principal(token)
    try:
        # The worker owns the lock until it actually stops, even if HTTP cancels.
        def guarded():
            try:
                return apply()
            finally:
                _RECOVERY_LOCK.release()
        result = await asyncio.to_thread(guarded)
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})
    except PolicyError as exc:
        raise HTTPException(503, exc.code, headers={'Cache-Control': 'no-store'}) from None
    except Exception:
        raise HTTPException(503, 'native_recovery_unavailable', headers={'Cache-Control': 'no-store'}) from None
