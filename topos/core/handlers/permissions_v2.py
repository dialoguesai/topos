"""Owner-only beta policy coordination relay, disabled unless explicitly paired."""
from __future__ import annotations

import asyncio
import time

from .registry import handles


def _refresh_message_search(runtime, grant_ids=None) -> None:
    """After an owner change that can move P(g): ask for the index work, never do it here (N3).

    Called in the change's own critical section, under the write gate, after the change committed and never inside a
    recipient request. `grant_ids` names the one grant a mutation changed; None is a review change, which can move
    every grant's permitted set: the indexes whose basis cannot see one are dropped first, here
    (`search_index.drop_unguarded`; if that cannot run, every index goes, as a sweep that cannot check does), and every
    search grant is queued, most-read first. The builds run on the runtime's rebuild thread (index_rebuilds.py) once
    the gate is released and the answer has left: one share at a time, each holding the gate only for its two brief
    steps. Until a share's new index is published, its searches are refused by the unchanged guard. It must not change
    the owner's answer, so it swallows its own failures; a failed rebuild leaves no index, and a missing index refuses.
    """
    import logging
    from ...permissions_v2 import switches
    if not switches.on(switches.MESSAGE_SEARCH):
        return
    log = logging.getLogger(__name__)
    if grant_ids is None:
        from ...permissions_v2.search_index import drop_unguarded, purge_all
        try:
            drop_unguarded(runtime.protocol.ledger, runtime.record_keys_root(), now=int(time.time()))
        except Exception:  # noqa: BLE001 -- fail closed: no index the guard might not see through survives
            log.warning("permissions v2 message search index drop failed")
            try:
                purge_all(runtime.record_keys_root())
            except Exception:  # noqa: BLE001
                pass
    try:
        runtime.index_rebuilds().request(grant_ids)
    except Exception:  # noqa: BLE001
        log.warning("permissions v2 message search index refresh failed")


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

    from ...permissions_v2.protection_doorbell import AUTO_RESYNC_CLIENT

    req_id = message.get("id")
    principal = current_principal()
    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
    # The control plane's automatic re-sync after a protection change (owner decision 2) asks a status and
    # nothing else: it re-signs the owner's unchanged grants, and may never change one.
    if principal.client_id == AUTO_RESYNC_CLIENT and operation != "status":
        return {"id": req_id, "status": "error", "code": 403, "error": "automation_status_only"}
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
                # N3: only the grant this command changed, and only queued here: the ack (signed above, 120 s life)
                # leaves with this gate hold, and its index is rebuilt after, off the gate.
                _refresh_message_search(runtime, [ack.receipt.authority.grant_id])
            elif operation == "mutate" and ack.outcome == "already_applied":
                # A retried command whose ack was lost: the build it asked for may have been lost with it (a restart
                # in between). Queue that grant again: at worst one more build of an index that is already current.
                _refresh_message_search(runtime, [ack.receipt.authority.grant_id])
            return ack
    try:
        # N3: returns as soon as the change is applied and signed; no index is built on this path.
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
    from ...permissions_v2.evidence_reviews import (EvidenceLookup, FactOptIn, FactOptOut, RecordEvidenceReview,
        ReviewQueueRequest, ReviewTotalsRequest, RevokeEvidenceReview)
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
                "revoke":RevokeProjectionReview if projection else RevokeEvidenceReview,
                # Implicit review's owner surfaces: the queue, the totals, the deselection and its undo.
                "queue":ReviewQueueRequest, "totals":ReviewTotalsRequest, "opt_out":FactOptOut, "opt_in":FactOptIn}[operation]
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
            if operation in {"record", "opt_out"}:
                result = getattr(service, operation)(request, now=int(time.time()))
                _refresh_message_search(runtime)
                return result
            result = getattr(service, operation)(request)
            if operation in {"revoke", "opt_in"}:
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


@handles("permissions_v2_evidence_review_queue", owner_only=True)
async def handle_permissions_v2_evidence_review_queue(message):
    """Owner-only: a page of current facts, least confident first, with labels, standing and source counts."""
    return await _handle_evidence(message, "queue")


@handles("permissions_v2_evidence_totals", owner_only=True)
async def handle_permissions_v2_evidence_totals(message):
    """Owner-only: qualifying / deselected / withheld counts, overall and per source."""
    return await _handle_evidence(message, "totals")


@handles("permissions_v2_evidence_opt_out", owner_only=True)
async def handle_permissions_v2_evidence_opt_out(message):
    """Owner-only: deselect one fact; every search index is rebuilt without it."""
    return await _handle_evidence(message, "opt_out")


@handles("permissions_v2_evidence_opt_in", owner_only=True)
async def handle_permissions_v2_evidence_opt_in(message):
    """Owner-only: reselect one fact; every search index is rebuilt with it."""
    return await _handle_evidence(message, "opt_in")


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
        from dataclasses import replace
        from ...principal import set_principal, reset_principal

        token = None
        try:
            with with_db_write():
                runtime = get_runtime()
                owner_id = runtime.protocol.ledger.identity.owner_id
                if (principal.channel == "cp_relay" and principal.acting_user != owner_id
                    or principal.channel == "uds" and principal.acting_user and principal.acting_user != owner_id):
                    raise PolicyError("owner_binding")
                if principal.channel == "uds":
                    # The authenticated local owner transport identifies this
                    # paired node. Bind its account from trusted runtime state,
                    # never from a request body/header, for deeper owner checks.
                    token = set_principal(replace(principal, acting_user=owner_id))
                index = runtime.message_search_index()
            # The index owns its publication/check gates. Fact qualification
            # and embedding must not hold the writer gate for the whole build.
            index.sweep()
            states = index.rebuild_all()
            return {"grants": len(states), "ready": sum(state == "ready" for state in states.values())}
        finally:
            if token is not None:
                reset_principal(token)
    try:
        return {"id": req_id, "status": "ok", "payload": await asyncio.to_thread(apply)}
    except PolicyError as exc:
        return {"id": req_id, "status": "error", "code": 403 if exc.code == "owner_binding" else 503, "error": exc.code}
    except Exception:
        return {"id": req_id, "status": "error", "code": 503, "error": "permissions_v2_unavailable"}


@handles("permissions_v2_entailment_review", owner_only=True)
async def handle_permissions_v2_entailment_review(message):
    """Owner-only OD-38 list: candidates beside their cited messages, and confirm / reject / revoke."""
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.entailment_grounding import OwnerEntailmentReview
    from ...permissions_v2.evidence import EvidenceBinding, _owner
    from ...permissions_v2.runtime import get_runtime
    from ...storage.db.write_gate import with_db_write

    req_id, payload = message.get("id"), message.get("payload")
    operations = {"list": set(), "confirm": {"candidate_id"}, "reject": {"candidate_id"}, "revoke": {"candidate_id"}}
    if (not isinstance(payload, dict) or payload.get("operation") not in operations
            or set(payload) != {"binding", "operation"} | operations[payload.get("operation")]
            or any(type(payload[k]) is not str or len(payload[k]) != 64 for k in operations[payload["operation"]])):
        return {"id": req_id, "status": "error", "code": 400, "error": "entailment_review_payload_invalid"}

    def apply():
        with with_db_write():
            runtime = get_runtime()
            actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
            _owner(actual)
            if EvidenceBinding.parse(payload["binding"]) != actual:
                raise PolicyError("evidence_target_binding")
            review = OwnerEntailmentReview(runtime.message_search_index())
        op = payload["operation"]
        if op == "list":
            return review.candidates()
        if op == "revoke":
            return review.revoke(payload["candidate_id"])
        return review.decide(payload["candidate_id"], op)
    try:
        return {"id": req_id, "status": "ok", "payload": await asyncio.to_thread(apply)}
    except PolicyError as exc:
        code = (403 if exc.code in {"owner_authority_required", "evidence_target_binding"} else
                404 if exc.code == "entailment_grounding_disabled" else
                409 if exc.code in {"entailment_candidate_stale", "entailment_owner_rejected",
                                    "entailment_verdict_unknown"} else 503)
        return {"id": req_id, "status": "error", "code": code, "error": exc.code}
    except Exception:
        return {"id": req_id, "status": "error", "code": 503, "error": "entailment_review_unavailable"}


@handles("permissions_v2_permitted_derivation", owner_only=True)
async def handle_permissions_v2_permitted_derivation(message):
    """Owner-only OD-46 pass: derive facts and goals from every active p2c-v3 grant's permitted messages.

    Off unless TOPOS_PERMISSIONS_V2_PERMITTED_DERIVATION=true (404). Returns counts and codes only, never a
    claim, a value or an identifier. The model runs with no database open; the writes and the index rebuild
    happen inside the pass, under the node write gate. Operation "journal_goal_field" runs the lane's model-free
    journal goal-field step instead (`_journal_goal_field`).
    """
    from ...permissions_v2 import permitted_derivation as pd
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.evidence import EvidenceBinding, _owner
    from ...permissions_v2.runtime import get_runtime

    req_id, payload = message.get("id"), message.get("payload")
    if not pd.enabled():
        return {"id": req_id, "status": "error", "code": 404, "error": "permitted_derivation_disabled"}
    if isinstance(payload, dict) and payload.get("operation") == "journal_goal_field":
        return await _journal_goal_field(req_id, payload)
    allowed = {"binding", "operation", "packs", "goals", "budget"}
    packs = payload.get("packs", list(pd.DEFAULT_PACKS)) if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get("operation") != "run" or not {"binding", "operation"} <= set(payload)
            or not set(payload) <= allowed
            or type(packs) is not list or len(set(packs)) != len(packs)
            or not all(type(p) is str and p in pd.ALLOWED_PACKS for p in packs)
            or type(payload.get("goals", True)) is not bool
            or type(payload.get("budget", pd.DEFAULT_BUDGET)) is not int
            or not 1 <= payload.get("budget", pd.DEFAULT_BUDGET) <= pd.DEFAULT_BUDGET):
        return {"id": req_id, "status": "error", "code": 400, "error": "permitted_derivation_payload_invalid"}

    def apply():
        runtime = get_runtime()
        actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
        _owner(actual)
        if EvidenceBinding.parse(payload["binding"]) != actual:
            raise PolicyError("evidence_target_binding")
        index = runtime.message_search_index()
        with index.resolver._read(gated=False) as (conn, _floor):
            extractor, model_extractor = pd.node_extractor(conn, packs=tuple(packs), goals=payload.get("goals", True))
        counts = pd.PermittedDerivationPass(index, extractor=extractor,
                                            budget=payload.get("budget", pd.DEFAULT_BUDGET)).run()
        return {"counts": counts, "extractor": dict(model_extractor.counts), "packs": sorted(packs)}
    try:
        return {"id": req_id, "status": "ok", "payload": await asyncio.to_thread(apply)}
    except PolicyError as exc:
        code = (403 if exc.code in {"owner_authority_required", "evidence_target_binding"} else
                400 if exc.code == "permitted_derivation_pack_unsupported" else 503)
        return {"id": req_id, "status": "error", "code": code, "error": exc.code}
    except Exception:
        return {"id": req_id, "status": "error", "code": 503, "error": "permitted_derivation_unavailable"}


async def _journal_goal_field(req_id, payload):
    """The lane's model-free step (IF-5 Lane H1): store the structured goal field of every qualifying journal entry.

    Payload exactly {binding, operation: "journal_goal_field"}. 404 unless TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD
    and the journal family are on as well; 403 for anyone but the owner or a foreign binding. Counts and codes only.
    """
    from ...permissions_v2 import journal_goal_field
    from ...permissions_v2 import permitted_derivation as pd
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.evidence import EvidenceBinding, _owner
    from ...permissions_v2.runtime import get_runtime

    if set(payload) != {"binding", "operation"}:
        return {"id": req_id, "status": "error", "code": 400, "error": "permitted_derivation_payload_invalid"}

    def apply():
        runtime = get_runtime()
        actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
        _owner(actual)
        if EvidenceBinding.parse(payload["binding"]) != actual:
            raise PolicyError("evidence_target_binding")
        counts = pd.JournalGoalFieldPass(runtime.message_search_index()).run()
        return {"counts": counts, "rule": journal_goal_field.VERSION}
    try:
        return {"id": req_id, "status": "ok", "payload": await asyncio.to_thread(apply)}
    except PolicyError as exc:
        code = (403 if exc.code in {"owner_authority_required", "evidence_target_binding"} else
                404 if exc.code == "journal_goal_field_disabled" else 503)
        return {"id": req_id, "status": "error", "code": code, "error": exc.code}
    except Exception:
        return {"id": req_id, "status": "error", "code": 503, "error": "permitted_derivation_unavailable"}


@handles("permissions_v2_message_review", owner_only=True)
async def handle_permissions_v2_message_review(message):
    from ...permissions_v2.canonical import PolicyError, digest
    from ...permissions_v2.evidence import EvidenceBinding, _owner
    from ...permissions_v2.message_review_contract import (MessageLookup, RecordMessageReview,
        MessageReviewQueue, MessageQueuePageRequest, MessageReviewPreview, MessageOptOutResult, MessageReviewResult,
        AutomaticReviewRequest, AutomaticReviewLookup)
    from ...permissions_v2.message_evidence import (preview_message, record_message_review, queue_messages,
        queue_message_page, queue_journal_entries, message_key)
    from ...permissions_v2.runtime import get_runtime
    from ...storage.db.write_gate import with_db_write

    req_id, payload = message.get("id"), message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"binding", "operation", "request"}:
        return {"id":req_id,"status":"error","code":400,"error":"message_review_payload_invalid"}
    def apply():
        with with_db_write():
            runtime = get_runtime()
            actual = EvidenceBinding.parse(runtime.protocol.ledger.identity.model_dump())
            _owner(actual)
            if EvidenceBinding.parse(payload["binding"]) != actual:
                raise PolicyError("evidence_target_binding")
            op = payload["operation"]
            model = {"queue":MessageReviewQueue, "queue_page":MessageQueuePageRequest,
                     "journal_queue":MessageReviewQueue, "preview":MessageLookup,
                     "record":RecordMessageReview,
                     "opt_out":MessageLookup, "opt_in":MessageLookup,
                     "automatic_start":AutomaticReviewRequest, "automatic_status":AutomaticReviewLookup,
                     "automatic_cancel":AutomaticReviewLookup}.get(op)
            if model is None:
                raise PolicyError("message_review_operation_invalid")
            request = model.parse(payload["request"])
            if op.startswith("automatic_"):
                worker = runtime.automatic_message_reviews()
                if op == "automatic_start":
                    return worker.start(request)
                return worker.cancel() if op == "automatic_cancel" else worker.status()
            service = runtime.evidence_reviews(require_existing=True)
            resolver, reviews = service.resolver, service.reviews
            if op == "queue":
                return queue_messages(resolver, reviews, request, now=int(time.time()))
            if op == "queue_page":
                return queue_message_page(resolver, reviews, request, now=int(time.time()))
            if op == "journal_queue":
                return queue_journal_entries(resolver, reviews, request, now=int(time.time()))
            if op == "record":
                review = record_message_review(resolver, reviews, **request.model_dump(), reviewed_at=int(time.time()))
                result = MessageReviewResult(review=review, review_revision=digest(review.model_dump()))
            else:
                if request.identity.binding != actual or request.identity.table == "signal_objects":
                    raise PolicyError("evidence_target_binding")
                if op == "preview":
                    return MessageReviewPreview.parse(preview_message(resolver, reviews, request.identity))
                key = message_key(request.identity)
                if op == "opt_out":
                    reviews.opt_out(key, now=int(time.time()))
                else:
                    reviews.opt_in(key)
                result = MessageOptOutResult(identity=request.identity, opted_out=op == "opt_out")
            # N3: in the change's own critical section, so an index the guard cannot see the change in is gone before
            # the gate is released; the rebuilds are only queued.
            _refresh_message_search(runtime)
        return result
    try:
        result = await asyncio.to_thread(apply)
        return {"id":req_id,"status":"ok","payload":result.model_dump()}
    except PolicyError as exc:
        code = (403 if exc.code in {"owner_authority_required", "evidence_target_binding"} else
                409 if exc.code in {"review_stale", "review_conflict", "review_id_conflict"} else
                400 if exc.code in {"schema_invalid", "message_review_window_invalid", "message_review_operation_invalid"} else 503)
        return {"id":req_id,"status":"error","code":code,"error":exc.code}
    except Exception:
        return {"id":req_id,"status":"error","code":503,"error":"message_review_unavailable"}
