"""Signed truth-pack read using the existing grant ledger and release proof.

Owner-app grants and recipient grants take exactly the same path. The answer
is selected by a fixed pack/question key; there is no retrieval or model call.
"""
import asyncio
import sqlite3
import time
from contextlib import closing

from topos.principal import THIRD_PARTY
from topos.relay_stamp import verify_relay_stamp
from topos.storage.db.write_gate import with_db_write
from topos.features.calibration.store import verify
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
from . import switches
from .canonical import PolicyError, canonical_bytes, digest
from .forwarding import ReleaseBody, sign_node_result
from .runtime import get_runtime
from .signing import TruthRequestContext, parse_envelope, verify_current_signature
from .truth_contract import VERIFY, authorize_truth, parse_intent

MESSAGE_TYPE = 'permissions_v2_verify_claim'

def read(runtime, intent):
    with closing(sqlite3.connect(runtime.protocol.canonical_database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return verify(conn, intent, guard=BlackholeGuard(conn, caller_class=CallerClass.GRANTEE))

def dispatch(runtime, principal, envelope, payload, request_id):
    signed = parse_envelope(envelope)
    if signed.request_type != VERIFY or signed.request_id != request_id:
        raise PolicyError('request_binding')
    if principal.cls != THIRD_PARTY or principal.channel != 'cp_relay' or not principal.acting_user or not principal.client_id:
        raise PolicyError('recipient_relay_required')
    intent = parse_intent(payload)  # a claim, or a claim that confirms a reading shown by truth_statement
    ledger = runtime.protocol.ledger
    request = TruthRequestContext.parse({**ledger.identity.model_dump(), 'actor_id': principal.acting_user,
        'client_id': principal.client_id, 'grant_id': signed.grant_id, 'assignment_id': signed.assignment_id,
        'request_id': request_id, 'request_type': VERIFY})
    with with_db_write():
        with ledger._transaction() as db:
            runtime.protocol._sync_protection(db)
        # Admission claims the request id and charges the existing daily read budget.
        lease = ledger.admit(signed.model_dump(), request=request, payload=intent.model_dump(), now=int(time.time()))
        with ledger._transaction() as db:
            current, policy = ledger._authority(db, signed.grant_id, int(time.time()))
            authorize_truth(policy, intent)
        output, revision = read(runtime, intent)
        from .truth_statement import provenance
        linked = provenance(intent, grant_id=signed.grant_id, now=int(time.time()))
        current = ledger.checkpoint_truth(lease, intent=intent, output=output, revision=revision, now=int(time.time()),
                                          provenance=linked)
    checked_at = int(time.time())
    verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=checked_at)
    result = sign_node_result(ReleaseBody(version='topos-node-disclosure/v1', kid=runtime.protocol.node_signing_kid,
        envelope_hash=digest(signed.model_dump()), request_id=request_id, request_hash=signed.request_hash,
        authority=current, output_hash=digest(output.model_dump()), checked_at=checked_at,
        expires_at=signed.expires_at), runtime.protocol.node_signing_key)
    return result.model_dump(), output.model_dump(), revision

async def dispatch_verify_claim(ws, message):
    request_id = message.get('id')
    try:
        if message.get('type') != MESSAGE_TYPE or not switches.on(switches.MESSAGE_SEARCH):
            raise PolicyError('truth_disabled')
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != 'cp_relay':
            raise PolicyError('recipient_relay_required')
        body = message.get('payload')
        if not isinstance(body, dict) or set(body) != {'envelope', 'intent'}:
            raise PolicyError('release_payload_invalid')
        runtime = get_runtime()
        signed = parse_envelope(body['envelope'])
        intent = parse_intent(body['intent'])
        result, output, revision = await asyncio.to_thread(dispatch, runtime, principal, body['envelope'], body['intent'], request_id)
        def current():
            now = int(time.time())
            with with_db_write():
                ledger = runtime.protocol.ledger
                with ledger._transaction() as db:
                    runtime.protocol._sync_protection(db)
                    authority, policy = ledger._authority(db, signed.grant_id, now)
                    authorize_truth(policy, intent)
                value, latest = read(runtime, intent)
                if authority.model_dump() != result['authority'] or latest != revision or value.model_dump() != output:
                    raise PolicyError('authority_stale')
                verify_current_signature(signed, trusted_keys=ledger.trusted_keys, now=now)
        await asyncio.to_thread(current)
        async def send():
            if get_runtime() is not runtime or not switches.on(switches.MESSAGE_SEARCH) or result['expires_at'] <= int(time.time()):
                raise PolicyError('authority_stale')
            verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=int(time.time()))
            await ws.send(canonical_bytes({'id': request_id, 'type': MESSAGE_TYPE, 'status': 'ok',
                                          'payload': {'result': result, 'output': output}}).decode('ascii'))
        await asyncio.wait_for(send(), 5)
    except asyncio.CancelledError:
        raise
    except Exception:
        await asyncio.wait_for(ws.send(canonical_bytes({'id': request_id, 'type': MESSAGE_TYPE, 'status': 'error',
            'code': 403, 'error': 'permission_denied'}).decode('ascii')), 5)
