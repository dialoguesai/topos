"""Owner-only beta policy coordination relay, disabled unless explicitly paired."""
from __future__ import annotations

import asyncio
import time

from .registry import handles


def _refresh_message_search(runtime) -> None:
    """p2c-v1: after an owner change that can move P(g), rebuild or drop every search index.

    Runs owner-side, after the owner's own operation committed, never inside a
    recipient request. It must not change the owner's answer, so it swallows its
    own failures; a failed rebuild leaves no index, and a missing index refuses.
    """
    import logging
    import os
    if os.environ.get("TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED", "").lower() != "true":
        return
    try:
        index = runtime.message_search_index()
        index.sweep()
        index.rebuild_all()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("permissions v2 message search index refresh failed")


def _forget_inactive_record_keys(runtime) -> None:
    """Every revoked or expired grant loses its opaque-id key, whatever its capability and
    whether or not search is enabled. Never changes the owner's answer."""
    import logging
    import time as _time
    try:
        from ...permissions_v2.search_index import forget_inactive_record_keys
        forget_inactive_record_keys(runtime.protocol.ledger, runtime.record_keys_root(), now=int(_time.time()))
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("permissions v2 record key cleanup failed")


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
            ack = getattr(runtime.protocol, operation)(payload["envelope"], now=int(time.time()))
            if operation == "mutate" and ack.outcome == "applied":
                _forget_inactive_record_keys(runtime)
                _refresh_message_search(runtime)
            return ack
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


async def _handle_evidence(message, operation, *, projection=False):
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.evidence import EvidenceBinding
    from ...permissions_v2.evidence_reviews import EvidenceLookup, RecordEvidenceReview, RevokeEvidenceReview
    from ...permissions_v2.fact_contract import FAMILY, OUTPUT_FAMILIES
    from ...permissions_v2.identity import LEGACY_CONTRACT, SUBJECT_CONTRACTS
    from ...permissions_v2.projection_reviews import RecordProjectionReview, RevokeProjectionReview
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal
    from ...storage.db.write_gate import with_db_write

    req_id = message.get("id")
    principal = current_principal()
    if (principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}
        or not principal.acting_user):
        return {"id":req_id, "status":"error", "code":403, "error":"owner_authority_required"}
    payload = message.get("payload")
    # `subject_contract` and `output_family` are accepted only on the output-review
    # surface, and only there because the projection candidate depends on both:
    # which owner-identity rule the owner is reviewing under, and which output
    # family they are reviewing FOR. Omitting either keeps the pre-binding rule
    # and the first family, so an older client is unchanged.
    #
    # The family has to be selectable here, not just inside the release path. A
    # release loads a review the owner already recorded; if this surface can only
    # ever record the first family's review, a grant for any other family finds
    # no review and is denied, and the capability is inert on every node.
    allowed = [{"binding", "request"}] + ([
        {"binding", "request", "subject_contract"},
        {"binding", "request", "output_family"},
        {"binding", "request", "subject_contract", "output_family"}] if projection else [])
    if not isinstance(payload, dict) or set(payload) not in allowed:
        return {"id":req_id, "status":"error", "code":400, "error":"evidence_payload_invalid"}

    def apply():
        with with_db_write():
            binding = EvidenceBinding.parse(payload["binding"])
            request_type = {"preview":EvidenceLookup, "read":EvidenceLookup,
                "record":RecordProjectionReview if projection else RecordEvidenceReview,
                "revoke":RevokeProjectionReview if projection else RevokeEvidenceReview}[operation]
            request = request_type.parse(payload["request"])
            runtime = get_runtime()
            actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
            if principal.acting_user != actual.owner_id:
                raise PolicyError("owner_authority_required")
            if binding != actual:
                raise PolicyError("evidence_target_binding")
            if operation == "record":
                snapshot = request.expected_candidate.snapshot if projection else request.expected_snapshot
                if snapshot.binding != actual:
                    raise PolicyError("evidence_target_binding")
            if projection:
                contract = payload.get("subject_contract", LEGACY_CONTRACT)
                if contract not in SUBJECT_CONTRACTS:
                    raise PolicyError("subject_contract_unknown")
                family = payload.get("output_family", FAMILY)
                if family not in OUTPUT_FAMILIES:
                    raise PolicyError("output_family_unknown")
                service = runtime.projection_reviews(require_existing=operation in {"record", "revoke"})
                return getattr(service, operation)(request, now=int(time.time()), contract=contract,
                                                   family=family)
            service = runtime.evidence_reviews(require_existing=operation in {"record", "revoke"})
            if operation == "record":
                result = service.record(request, now=int(time.time()))
                _refresh_message_search(runtime)
                return result
            result = getattr(service, operation)(request)
            if operation == "revoke":
                _refresh_message_search(runtime)
            return result

    try:
        response = await asyncio.to_thread(apply)
        return {"id":req_id, "status":"ok", "payload":response.model_dump()}
    except PolicyError as exc:
        if exc.code in {"owner_authority_required", "evidence_target_binding"}:
            code = 403
        elif exc.code in {"review_stale", "review_conflict", "review_id_conflict", "output_review_stale", "output_review_conflict", "output_review_id_conflict"}:
            code = 409
        elif exc.code in {"evidence_missing", "review_unknown", "output_review_unknown"}:
            code = 404
        elif exc.code == "preview_too_large":
            code = 413
        elif (exc.code in {"subject_contract_unknown", "output_family_unknown"}
              or exc.code == "schema_invalid" or exc.code.startswith("json_")):
            # A caller naming a contract or a family this node does not support
            # has sent a bad request, not found an unavailable service. These
            # used to fall through to 503, which tells the client to retry
            # something that can never succeed.
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


@handles("permissions_v2_projection_preview", owner_only=True)
async def handle_permissions_v2_projection_preview(message):
    return await _handle_evidence(message, "preview", projection=True)


@handles("permissions_v2_projection_review_read", owner_only=True)
async def handle_permissions_v2_projection_review_read(message):
    return await _handle_evidence(message, "read", projection=True)


@handles("permissions_v2_projection_review_record", owner_only=True)
async def handle_permissions_v2_projection_review_record(message):
    return await _handle_evidence(message, "record", projection=True)


@handles("permissions_v2_projection_review_revoke", owner_only=True)
async def handle_permissions_v2_projection_review_revoke(message):
    return await _handle_evidence(message, "revoke", projection=True)


@handles("permissions_v2_fact_read")
async def handle_permissions_v2_fact_read(message):
    # The socket interceptor alone owns the actual send under release gates.
    # Generic dispatch cannot return a payload for deferred forwarding.
    return {"id":message.get("id"), "status":"error", "code":403, "error":"permission_denied"}


@handles("permissions_v2_message_search_rebuild", owner_only=True)
async def handle_permissions_v2_message_search_rebuild(message):
    """Owner-only: rebuild every p2c-v1 index now. Answers states only, never ids or reasons."""
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal
    from ...storage.db.write_gate import with_db_write

    req_id = message.get("id")
    principal = current_principal()
    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}

    def apply():
        with with_db_write():
            runtime = get_runtime()
            if principal.acting_user != runtime.protocol.ledger.identity.owner_id:
                raise PolicyError("owner_binding")
            index = runtime.message_search_index()
            index.sweep()
            states = index.rebuild_all()
            return {"grants": len(states), "ready": sum(state == "ready" for state in states.values())}
    try:
        return {"id": req_id, "status": "ok", "payload": await asyncio.to_thread(apply)}
    except PolicyError as exc:
        return {"id": req_id, "status": "error", "code": 403 if exc.code == "owner_binding" else 503, "error": exc.code}
    except Exception:
        return {"id": req_id, "status": "error", "code": 503, "error": "permissions_v2_unavailable"}
