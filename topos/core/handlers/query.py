"""Scoped query orchestration message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    logger,
)
from .registry import handles


@handles("verify_claim")
async def handle_verify_claim(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Mode-gated truthfulness check (PLAN_TRUTHFULNESS_PLUGIN.md).

    Deliberately NOT reachable from the /mcp gateway — the control plane
    forwards it only from the app-gated /v1/truth/verify-claim route, and this
    handler refuses any request that doesn't name an allowlisted-by-CP caller
    app (defense in depth against generic forwarding paths like local_mcp).
    """
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    caller_app_id = str(payload.get("caller_app_id") or "").strip()
    statement = str(payload.get("statement") or "")
    mode = str(payload.get("mode") or "fun")
    try:
        from ...query.verify import verify_claim

        result = verify_claim(
            hub.get_db_connection(),
            statement=statement,
            mode=mode,
            caller_app_id=caller_app_id,
        )
        audit = result.pop("_audit", None)
        if audit:
            # Content-free by construction: mode/category/counts/judge only.
            logger.info("truth verify_claim audit: %s", audit)
        if "error" in result:
            return {"id": req_id, "status": "error", "error": str(result["error"])}
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("truth_prompts")
async def handle_truth_prompts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """"Things you can ask me" seeds from the fun aperture — same door policy
    as verify_claim (app-stamped, mode-registered, never on /mcp)."""
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...query.truth_prompts import suggest_prompts

        result = suggest_prompts(
            hub.get_db_connection(),
            mode=str(payload.get("mode") or "fun"),
            caller_app_id=str(payload.get("caller_app_id") or "").strip(),
            limit=int(payload.get("limit") or 5),
        )
        audit = result.pop("_audit", None)
        if audit:
            logger.info("truth_prompts audit: %s", audit)
        if "error" in result:
            return {"id": req_id, "status": "error", "error": str(result["error"])}
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("truth_seed_fact")
async def handle_truth_seed_fact(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner-stated fun fact → FactStore, refused unless inside the fun
    aperture. The truth feature's only write (same door policy)."""
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...query.truth_facts import seed_fun_fact

        result = seed_fun_fact(
            hub.get_db_connection(),
            predicate=str(payload.get("predicate") or ""),
            value=str(payload.get("value") or ""),
            mode=str(payload.get("mode") or "fun"),
            caller_app_id=str(payload.get("caller_app_id") or "").strip(),
        )
        audit = result.pop("_audit", None)
        if audit:
            logger.info("truth_seed_fact audit: %s", audit)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("query", "query_live")
async def handle_query(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    raw_manifest = payload.get("manifest") or {}
    scope_id = str(payload.get("scope_id") or raw_manifest.get("scope_id") or "")
    # `query` (the owner's words) OUTRANKS `intent` (a keyword digest). It used to be
    # the other way round, which meant home chat's stopword-stripped fingerprint —
    # "how did I sleep this week?" arriving as "sleep week" — became the query text
    # every downstream stage saw. Measured cost of that on 2026-08-16: the planner's
    # `\bthis week\b` regex no longer matched, so the turn lost its time window
    # entirely; the scope classifier, trained on questions, read keyword soup and
    # abstained; and vector ranking embedded a fragment instead of a sentence.
    # Nothing wanted the digest: retrieval's own `_query_tokens` strips stopwords
    # where a bag of words is actually needed. Callers that send only `intent`
    # (MCP clients) are unaffected — it is still the fallback.
    intent = str(payload.get("query") or payload.get("intent") or "")
    logger.info(
        "query request: scope=%r mode=%r intent_chars=%d requester=%r",
        scope_id,
        str(payload.get("access_mode") or "summary"),
        len(intent),
        str(payload.get("requester_id") or "mcp"),
    )
    try:
        from ...query.manifest_validation import ManifestValidationError, resolve_scope_manifest
        from ...query.runtime import get_query_orchestrator

        filter_manifest = payload.get("filter_manifest") if isinstance(payload.get("filter_manifest"), dict) else None
        # The caller names its own scope_id as free text, and a small local model
        # invents plausible ones. Live 2026-08-26: "Who are my friends?" arrived as
        # scope_id "social_graph", which does not exist, so the turn was DENIED before
        # any retrieval and the owner was told "I don't have access to your contacts"
        # while 1,386 of them sat in the store. One forgiving retry onto the scope the
        # words actually name, recorded in the response so the substitution is never
        # silent — `resolve_scope_manifest` itself stays strict, and this is skipped
        # entirely when a grant is in play, so a grantee must still name what they
        # were granted exactly.
        scope_substituted_from = None
        if filter_manifest is None:
            from ...query.scope_registry_loader import suggest_scope_id

            suggested = suggest_scope_id(scope_id, intent)
            if suggested:
                scope_substituted_from, scope_id = scope_id, suggested
                logger.info("scope substituted: %r -> %r", scope_substituted_from, scope_id)
        try:
            manifest = resolve_scope_manifest(
                scope_id,
                client_manifest=raw_manifest if raw_manifest else None,
                filter_manifest=filter_manifest,
            )
        except ManifestValidationError as exc:
            # The one denial that never reaches the orchestrator, so nothing downstream
            # of here writes its ledger or its session id — this allow-list is the whole
            # response. It used to emit four keys and the contract declares eight; the
            # two it silently omitted (`query_session_id`, `narrowing`) are the two a
            # client needs to say WHY, which is the opacity the ledger exists to end.
            # `deny_reason` stays at top level: that is where every pipeline denial puts
            # it, and this path writes no audit block for a seam to recover it from.
            from ...query.narrowing import CAUSE_SCOPE_DENIED, NarrowingLedger, STAGE_GRANT

            session_id = payload.get("query_session_id") or payload.get("session_id")
            ledger = NarrowingLedger()
            # `exc.code` is a closed-set slug (`missing_scope`, `unknown_scope`,
            # `scope_mismatch`, ...) and `record` re-slugs it regardless.
            ledger.empty(CAUSE_SCOPE_DENIED, stage=STAGE_GRANT, reason=exc.code)
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "turn_outcome": "denied",
                    "deny_reason": exc.code,
                    "public_result": None,
                    "session_id": session_id,
                    "query_session_id": session_id,
                    "narrowing": {**ledger.as_public(), "result_empty": True},
                },
            }
        # Client-supplied reference instant (offset-aware ISO): "yesterday"
        # resolves against the USER's calendar day, not this server's UTC day.
        # Unparseable/absent → None → planner wall clock.
        query_now = None
        raw_now = str(payload.get("now") or "").strip()
        if raw_now:
            try:
                from datetime import datetime

                query_now = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("unparseable query now=%r ignored", raw_now)
        result = await get_query_orchestrator(conn=hub.get_db_connection()).execute(
            query_text=intent,
            # Optional companion to `query`: what to MATCH on, when the caller can name
            # the subject better than its own request text does. Absent for every client
            # that does not send it, which is every client today except home chat.
            retrieval_text=str(payload.get("retrieval_text") or "").strip() or None,
            # The same subject, split per PART of a multi-part request. The rare gate
            # runs once per entry, so one part's specific ask cannot empty another
            # part's lane. Absent for every client that does not send it.
            retrieval_parts=[
                str(p or "").strip()
                for p in (payload.get("retrieval_parts") or [])
                if str(p or "").strip()
            ]
            or None,
            scope_id=scope_id,
            access_mode=str(payload.get("access_mode") or "summary"),
            manifest=manifest,
            now=query_now,
            query_session_id=payload.get("query_session_id") or payload.get("session_id"),
            filter_manifest=filter_manifest,
            field_transforms=payload.get("field_transforms"),
            requester_id=str(payload.get("requester_id") or "mcp"),
            owner_id=str(payload.get("owner_user_id") or payload.get("owner_id") or "owner"),
            is_grantee_request=bool(payload.get("is_grantee_request")),
            disclosure_ceiling=str(payload.get("disclosure_ceiling") or "default"),
            explicit_disclosure_tier=str(payload.get("disclosure_tier")).strip()
            if payload.get("disclosure_tier")
            else None,
        )
        if scope_substituted_from and isinstance(result, dict):
            # Declared, never silent — the same rule as packet_resolution_reason
            # and empty_cause: a turn says what was done to it.
            result["scope_substituted_from"] = scope_substituted_from
        return {"id": req_id, "status": "ok", "payload": result}
    except KeyError as exc:
        return {"id": req_id, "status": "error", "error": f"Missing field: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
