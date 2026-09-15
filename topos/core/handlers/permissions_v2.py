"""Owner-only beta policy coordination relay, disabled unless explicitly paired."""
from __future__ import annotations

import asyncio
import time

from .registry import handles


async def _handle(message, operation):
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal
    from ...storage.db.write_gate import with_db_write

    req_id = message.get("id")
    principal = current_principal()
    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"envelope"}:
        return {"id": req_id, "status": "error", "code": 400, "error": "protocol_payload_invalid"}
    def apply():
        # A writer can hold this gate for longer than a signed request lives.
        # Take the trusted clock AFTER the wait, never at network receipt time.
        with with_db_write():
            runtime = get_runtime()
            if principal.channel == "cp_relay" and principal.acting_user != runtime.protocol.ledger.identity.owner_id:
                raise PolicyError("owner_binding")
            return getattr(runtime.protocol, operation)(payload["envelope"], now=int(time.time()))
    try:
        ack = await asyncio.to_thread(apply)
        return {"id": req_id, "status": "ok", "payload": {"ack": ack.model_dump()}}
    except PolicyError as exc:
        return {"id": req_id, "status": "error", "code": 403 if exc.code in {"owner_binding", "signature_invalid", "signing_key_unknown"} else 503, "error": exc.code}
    except Exception:
        # Configuration/storage failures must not echo private paths, key
        # material, candidate input, or exception chains to the relay.
        return {"id": req_id, "status": "error", "code": 503, "error": "permissions_v2_unavailable"}


@handles("permissions_v2_mutate", owner_only=True)
async def handle_permissions_v2_mutate(message):
    return await _handle(message, "mutate")


@handles("permissions_v2_status", owner_only=True)
async def handle_permissions_v2_status(message):
    return await _handle(message, "status")


async def _handle_evidence(message, operation):
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.evidence import EvidenceBinding
    from ...permissions_v2.evidence_reviews import EvidenceLookup, RecordEvidenceReview, RevokeEvidenceReview
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal
    from ...storage.db.write_gate import with_db_write

    req_id = message.get("id")
    principal = current_principal()
    if (principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}
        or not principal.acting_user):
        return {"id":req_id, "status":"error", "code":403, "error":"owner_authority_required"}
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"binding", "request"}:
        return {"id":req_id, "status":"error", "code":400, "error":"evidence_payload_invalid"}

    def apply():
        with with_db_write():
            binding = EvidenceBinding.parse(payload["binding"])
            request_type = {"preview":EvidenceLookup, "read":EvidenceLookup,
                "record":RecordEvidenceReview, "revoke":RevokeEvidenceReview}[operation]
            request = request_type.parse(payload["request"])
            runtime = get_runtime()
            actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
            if principal.acting_user != actual.owner_id:
                raise PolicyError("owner_authority_required")
            if binding != actual:
                raise PolicyError("evidence_target_binding")
            if operation == "record" and request.expected_snapshot.binding != actual:
                raise PolicyError("evidence_target_binding")
            service = runtime.evidence_reviews(require_existing=operation in {"record", "revoke"})
            if operation == "record":
                return service.record(request, now=int(time.time()))
            return getattr(service, operation)(request)

    try:
        response = await asyncio.to_thread(apply)
        return {"id":req_id, "status":"ok", "payload":response.model_dump()}
    except PolicyError as exc:
        if exc.code in {"owner_authority_required", "evidence_target_binding"}:
            code = 403
        elif exc.code in {"review_stale", "review_conflict", "review_id_conflict"}:
            code = 409
        elif exc.code in {"evidence_missing", "review_unknown"}:
            code = 404
        elif exc.code == "preview_too_large":
            code = 413
        elif exc.code == "schema_invalid" or exc.code.startswith("json_"):
            code = 400
        else:
            code = 503
        return {"id":req_id, "status":"error", "code":code, "error":exc.code}
    except Exception:
        return {"id":req_id, "status":"error", "code":503, "error":"evidence_review_unavailable"}


@handles("permissions_v2_evidence_preview", owner_only=True)
async def handle_permissions_v2_evidence_preview(message):
    return await _handle_evidence(message, "preview")


@handles("permissions_v2_evidence_review_read", owner_only=True)
async def handle_permissions_v2_evidence_review_read(message):
    return await _handle_evidence(message, "read")


@handles("permissions_v2_evidence_review_record", owner_only=True)
async def handle_permissions_v2_evidence_review_record(message):
    return await _handle_evidence(message, "record")


@handles("permissions_v2_evidence_review_revoke", owner_only=True)
async def handle_permissions_v2_evidence_review_revoke(message):
    return await _handle_evidence(message, "revoke")
