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


#: Whose queries are MCP requests for the owner's accounting: an approved agent and a third party
#: (DIGEST C5: before this only the five raw tools wrote a row). The owner's own app, the relay
#: (home chat) and the routine lane are not — their queries write no row here.
_RECORDED_QUERY_CLASSES = frozenset({"owner_agent", "third_party"})


async def _record_query_request(message: Dict[str, Any], payload: Dict[str, Any], tool: str = "query") -> None:
    """One request-log row per answered query from an agent or a third party (CONTRACTS §1.5): the
    claimed source and requester beside the channel-verified class and client id, so an approved
    agent's reads are named by its row and the owner's accounting sees them. Off the loop, on the
    worker's own connection; never fails the answer."""
    try:
        from ...core.state import mcp_source_for_principal
        from ...principal import current_principal
        from .common import record_mcp_request, run_db_write

        principal = current_principal()
        if getattr(principal, "cls", None) not in _RECORDED_QUERY_CLASSES:
            return
        caller = message.get("caller") if isinstance(message.get("caller"), dict) else {}
        source = (
            mcp_source_for_principal(principal)
            or str(caller.get("mcp_source") or "").strip()
            or "control_plane"
        )
        await run_db_write(
            record_mcp_request,
            tool,
            source=source,
            requester_id=str(payload.get("requester_id") or "") or None,
        )
    except Exception:  # noqa: BLE001
        logger.debug("query: audit record failed", exc_info=True)


def _log_query_failure(
    msg_type: str,
    req_id: Any,
    exc: BaseException,
    *,
    scope_id: str = "",
    access_mode: str = "",
) -> None:
    """One line per failed query, with the error's class and where it was raised.

    Never its text, never the intent: a query's error can quote what it was
    handling, and the owner's data must not reach the log. The frames are code
    locations only (the NR181 rule in control_plane_client). Before this the
    handlers returned the error and logged nothing, so a routine's "database is
    locked" on 2026-10-23 left no traceback to say which write raised it.
    """
    import traceback

    logger.error(
        "%s failed: id=%s scope=%r mode=%r error=%s\n%s",
        msg_type,
        req_id,
        scope_id,
        access_mode,
        type(exc).__name__,
        "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
    )


def _truth_door_refusal(req_id: Any, msg_type: str) -> Optional[Dict[str, Any]]:
    """Who may use the truth door — topos/query/truth_door.py, decided from the
    channel-verified principal on every entry point (local HTTP, relay, any
    future forwarder)."""
    from ...principal import current_principal
    from ...query.truth_door import truth_door_refusal

    return truth_door_refusal(current_principal(), msg_type, req_id)


@handles("verify_claim")
async def handle_verify_claim(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Mode-gated truthfulness check (PLAN_TRUTHFULNESS_PLUGIN.md).

    Deliberately NOT reachable from the /mcp gateway — the control plane
    forwards it only from the app-gated /v1/truth/verify-claim route. Who may
    ask is the truth door's decision; `caller_app_id` is still required, but it
    is caller-asserted and names the app for the audit line only.
    """
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "verify_claim")
    if refused:
        return refused
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
        _log_query_failure("verify_claim", req_id, exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


_V2_EFFORTS = frozenset({"low", "high"})
#: The longest wait a caller may ask for. 30 s: a verdict takes 11-15 s at the median on the owner's node and the
#: p95 runs to 20-30 s (23 Oct 2026), and the Fun Camera lane keeps late verdicts rather than dropping them.
_V2_MAX_DEADLINE_S = 30.0


def _owner_claim_request(payload: Any):
    """Validate a verify_claim_owner payload into a ClaimRequest, or None. Never echoes the input back.

    ``read_timeout_s`` (optional; the engine's READ_TIMEOUT_S when absent, which the engine bounds to the deadline) is
    the reader's own cap, from 0.5 s up to the request's ``deadline_s``. ``source_kind`` (optional, default
    ``spoken``) names what the statement is: a spoken turn, a chat message, a transcript segment or a journal entry."""
    from ...query.verify_v2 import DEFAULT_DEADLINE_S, SOURCE_KINDS, ClaimRequest

    if not isinstance(payload, dict) or not isinstance(payload.get("statement"), str):
        return None
    context = payload.get("context") or []
    if not isinstance(context, list) or len(context) > 12 or not all(
        isinstance(t, dict) and isinstance(t.get("text", ""), str) and isinstance(t.get("speaker", ""), str) for t in context
    ):
        return None
    said_at, speaker = payload.get("said_at"), payload.get("speaker", "owner")
    effort, deadline = payload.get("effort", "high"), payload.get("deadline_s", DEFAULT_DEADLINE_S)
    if (said_at is not None and not isinstance(said_at, str)) or not isinstance(speaker, str) or effort not in _V2_EFFORTS:
        return None
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0.5 <= float(deadline) <= _V2_MAX_DEADLINE_S:
        return None
    about_others = payload.get("about_others", False)
    if not isinstance(about_others, bool):
        return None
    extra: Dict[str, Any] = {}
    if "read_timeout_s" in payload:
        read_timeout = payload["read_timeout_s"]
        if (isinstance(read_timeout, bool) or not isinstance(read_timeout, (int, float))
                or not 0.5 <= float(read_timeout) <= float(deadline)):
            return None
        extra["read_timeout_s"] = float(read_timeout)
    if "source_kind" in payload:
        if payload["source_kind"] not in SOURCE_KINDS:
            return None
        extra["source_kind"] = payload["source_kind"]
    turns = [{"speaker": str(t.get("speaker") or "")[:60], "text": str(t.get("text") or "")[:1000]} for t in context]
    return ClaimRequest(statement=payload["statement"], context=turns, said_at=said_at, speaker=speaker[:60] or "owner",
                        effort=effort, deadline_s=float(deadline), about_others=about_others, **extra)


@handles("verify_claim_owner")
async def handle_verify_claim_owner(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """verify_claim v2: the owner's own check of a claim against all their data (VERIFY_CLAIM_V2_DESIGN.md).

    Owner class only: the truth door admits an unlisted truth type for OWNER_APP alone, so neither the control-plane
    relay nor an enrolled client reaches this. Model stages run in threads, retrieval on a DB worker's own
    connection, all under one deadline; a timeout answers "unable", never a guess.
    """
    import asyncio

    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "verify_claim_owner")
    if refused:
        return refused
    request = _owner_claim_request(message.get("payload"))
    if request is None:
        return {"id": req_id, "status": "error", "error": "invalid_truth_request"}
    from ...config.verify_claim_model import device_pick
    from ...query.verify_v2 import VERSION, verify_owner_claim
    from ...query.verify_v2_models import chat_for_setting, judges_for_setting
    from .common import run_db_read

    try:
        # The owner's model pick is a row in engine_config: read it on the worker, not the loop.
        pick = await run_db_read(device_pick)
        result = await asyncio.wait_for(
            verify_owner_claim(request, chat=chat_for_setting(pick), judges=judges_for_setting(pick),
                               gather_in_worker=run_db_read, to_thread=asyncio.to_thread),
            timeout=request.deadline_s + 2.0,
        )
    except asyncio.TimeoutError:
        result = {"version": VERSION, "verdict": "unable", "reason": "deadline"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner claim check failed: %s", type(exc).__name__)
        return {"id": req_id, "status": "error", "error": "verification_unavailable"}
    return {"id": req_id, "status": "ok", "payload": result}


@handles("truth_prompts")
async def handle_truth_prompts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """"Things you can ask me" seeds from the fun aperture — same door policy
    as verify_claim (truth door, mode-registered, never on /mcp)."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_prompts")
    if refused:
        return refused
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
        _log_query_failure("truth_prompts", req_id, exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("truth_seed_fact", owner_only=True)
async def handle_truth_seed_fact(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner-stated fun fact → FactStore, refused unless inside the fun
    aperture. The truth feature's only write, so the door admits the owner
    class alone: an owner-stated fact is authored by the owner or not at all."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_seed_fact")
    if refused:
        return refused
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
        _log_query_failure("truth_seed_fact", req_id, exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


# --- the inbound truth lane's owner doors (VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a) -----------------------------------------
# Owner class only: each asks the truth door first, which admits a truth type it does not list as a read for OWNER_APP
# alone, so the control-plane relay, an enrolled client and a third party are refused before the payload is read.
# Errors are fixed codes, never the input. The two reads are open on the owner socket (OWNER_DOOR_OPEN_TYPES); the
# settings change and the label are changes, so on the socket they need the app, the web app, the dev proxy or a
# confirmation (they are dangerous by subtraction, like put_facts_llm_config).


def _truth_error(req_id: Any, code: str) -> Dict[str, Any]:
    return {"id": req_id, "status": "error", "error": code}


@handles("truth_inbound_settings")
async def handle_truth_inbound_settings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"get": true}`` or ``{"set": {enabled?, sources?, model?, rate_per_minute?}}`` -> the settings (plus
    ``paused_until``). Turning the lane on queues its first batch on the catch-up lane at once."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_inbound_settings")
    if refused:
        return refused
    payload = message.get("payload")
    if not isinstance(payload, dict) or not set(payload) <= {"get", "set"}:
        return _truth_error(req_id, "invalid_truth_settings")
    from ...config.truth_inbound import InvalidTruthSettings, public_view, save
    from .common import run_db_read, run_db_write

    def apply(conn: Any, patch: Any) -> None:
        stored = save(conn, patch)
        if stored["enabled"]:
            from ...ingestion.truth_inbound import enqueue_truth_checks

            enqueue_truth_checks(conn)

    try:
        if "set" in payload:
            await run_db_write(apply, payload["set"])
        return {"id": req_id, "status": "ok", "payload": await run_db_read(public_view)}
    except InvalidTruthSettings:
        return _truth_error(req_id, "invalid_truth_settings")
    except Exception as exc:  # noqa: BLE001
        _log_query_failure("truth_inbound_settings", req_id, exc)
        return _truth_error(req_id, "truth_lane_unavailable")


@handles("truth_weekly_stats")
async def handle_truth_weekly_stats(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"weeks": 1..8}`` -> counts per ISO week, newest first, and whether the lane is on. Aggregates only."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_weekly_stats")
    if refused:
        return refused
    payload = message.get("payload")
    weeks = payload.get("weeks", 1) if isinstance(payload, dict) else None
    from ...config.truth_inbound import load
    from ...query.truth_checks import TruthRequestError, weekly_stats
    from .common import run_db_read

    def read(conn: Any) -> Dict[str, Any]:
        return {"enabled": load(conn)["enabled"], **weekly_stats(conn, weeks)}

    try:
        return {"id": req_id, "status": "ok", "payload": await run_db_read(read)}
    except TruthRequestError as exc:
        return _truth_error(req_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        _log_query_failure("truth_weekly_stats", req_id, exc)
        return _truth_error(req_id, "truth_lane_unavailable")


@handles("truth_claim_cards")
async def handle_truth_claim_cards(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"limit": <=500, "unlabelled_only": true}`` -> the claims the lane found, newest first, with snippets cut from
    the cited records' own text (400 characters)."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_claim_cards")
    if refused:
        return refused
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else None
    if payload is None:
        return _truth_error(req_id, "invalid_truth_request")
    from ...query.truth_checks import TruthRequestError, claim_cards
    from .common import run_db_read

    try:
        result = await run_db_read(claim_cards, payload.get("limit", 20), payload.get("unlabelled_only", True),
                                   payload.get("speakers", "owner"))
        return {"id": req_id, "status": "ok", "payload": result}
    except TruthRequestError as exc:
        return _truth_error(req_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        _log_query_failure("truth_claim_cards", req_id, exc)
        return _truth_error(req_id, "truth_lane_unavailable")


@handles("truth_claim_label")
async def handle_truth_claim_label(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"check_id", "label": "true"|"partly"|"false", "note"?, "evidence_ok"?}`` -> ``{"ok": true, "label_id"}``, or
    ``{"check_id", "relevance": "dismissed", "dismiss_reason", "note"?}`` -> ``{"ok": true, "dismissal_id"}``. A second
    submit for the same check replaces the first (and a label replaces a dismissal, and the reverse); a label writes
    the owner-only ``verified_claim`` object, a dismissal removes it. ``{"check_id", "relevance": "skipped",
    "skip_reason"?, "evidence_opened"?}`` -> ``{"ok": true, "skip_id"}`` records a skip and changes nothing else.
    ``{"check_id", "relevance": "split"}`` -> ``{"ok": true, "check_id"}``: "Not the same": the utterance leaves the
    group its card put it in and becomes its own; its label, if any, stays (never a dismissal)."""
    req_id = message.get("id")
    if not req_id:
        return None
    refused = _truth_door_refusal(req_id, "truth_claim_label")
    if refused:
        return refused
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else None
    if payload is None:
        return _truth_error(req_id, "invalid_truth_label")
    from ...query.truth_checks import TruthRequestError, dismiss_claim, label_claim, skip_claim, split_claim
    from .common import run_db_write

    try:
        if payload.get("relevance") == "skipped":
            result = await run_db_write(skip_claim, payload.get("check_id"), payload.get("skip_reason"),
                                        payload.get("evidence_opened"))
        elif payload.get("relevance") == "split":
            result = await run_db_write(split_claim, payload.get("check_id"))
        elif payload.get("relevance") == "dismissed":
            result = await run_db_write(dismiss_claim, payload.get("check_id"), payload.get("dismiss_reason"),
                                        payload.get("note"))
        elif payload.get("relevance") not in (None, "relevant"):
            return _truth_error(req_id, "invalid_truth_label")
        else:
            result = await run_db_write(label_claim, payload.get("check_id"), payload.get("label"), payload.get("note"),
                                        payload.get("evidence_ok"))
        return {"id": req_id, "status": "ok", "payload": result}
    except TruthRequestError as exc:
        return _truth_error(req_id, str(exc))
    except Exception as exc:  # noqa: BLE001
        _log_query_failure("truth_claim_label", req_id, exc)
        return _truth_error(req_id, "truth_lane_unavailable")


# --- the owner-agent reflection read (GRANT_DECISIONS G2, DISCLOSURE_RULES DR-61) ---------------------------------
# The one read the Moments app makes: the enabled reflection packs' facts, filtered for an agent
# (``query/reflection_facts.py``). The owner's app, and an owner agent on the socket; the dispatcher has already held
# an agent to its own type list (``owner_agent_allowed_types``: Moments sends this and nothing else), so a coding
# agent never reaches it. Every other class is refused before the payload is read. A read: writes nothing.
def _reflection_facts_reader(principal: Any) -> Optional[str]:
    """"owner" or "agent" for a class this read serves, None for every other."""
    from ...principal import OWNER_AGENT, OWNER_APP

    cls = getattr(principal, "cls", None)
    if cls == OWNER_APP:
        return "owner"
    if cls == OWNER_AGENT and getattr(principal, "channel", None) == "uds":
        return "agent"
    return None


@handles("reflection_facts")
async def handle_reflection_facts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``{"packs"?, "fact_ids"?, "since"?, "limit"?, "include_excerpts"?}`` -> the active facts of the reflection packs
    the owner switched on: value (people as display names), the owner's quote, date, sources; plus which reflection
    packs are on and which are missing. Fixed error codes, never the input."""
    req_id = message.get("id")
    if not req_id:
        return None
    from ...principal import current_principal

    principal = current_principal()
    reader = _reflection_facts_reader(principal)
    if reader is None:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
    from ...query.reflection_facts import ReflectionFactsRequestError, parse_request, read
    from .common import run_db_read

    try:
        request = parse_request(message.get("payload"))
    except ReflectionFactsRequestError as exc:
        return _truth_error(req_id, str(exc))

    def run(conn: Any) -> Dict[str, Any]:
        guard = None
        if reader == "agent":
            from ...features.lifecycle.blackhole_guard import guard_from_message

            guard = guard_from_message(conn, message)
        return read(conn, request, agent=reader == "agent", guard=guard)

    try:
        result = await run_db_read(run)
    except Exception as exc:  # noqa: BLE001
        _log_query_failure("reflection_facts", req_id, exc)
        return _truth_error(req_id, "reflection_facts_unavailable")
    await _record_query_request(message, {}, tool="reflection_facts")
    return {"id": req_id, "status": "ok", "payload": result}


@handles("query", "query_live")
async def handle_query(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    from ...serve.spans import finish_request, start_request

    started, span_token = start_request()
    outcome = "error"
    payload = message.get("payload") or {}
    # Phase 4: the older person-to-person query lane is retired. A delayed or
    # direct relay must not re-enter it after the control plane removes its tool.
    # Read from both places a control plane has said it: the payload field the
    # shared-query tool set, and the `caller` block it stamps on a relayed frame
    # (the one blackhole_guard reads). Anything else here runs as the owner's own
    # query, so a grantee's request must never get past this line.
    caller = message.get("caller") if isinstance(message.get("caller"), dict) else {}
    if not isinstance(payload, dict) or payload.get("is_grantee_request") or caller.get("is_grantee_request"):
        finish_request(started, outcome, span_token)
        return {"id": req_id, "status": "error", "error": "retired_grantee_query"}
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
    access_mode = str(payload.get("access_mode") or "summary")
    msg_type = str(message.get("type") or "query")
    logger.info(
        "query request: scope=%r mode=%r intent_chars=%d requester=%r",
        scope_id,
        access_mode,
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
            outcome = "ok"
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
            dataset_id=str(payload.get("dataset_id") or "").strip() or None,
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
            access_mode=access_mode,
            manifest=manifest,
            now=query_now,
            query_session_id=payload.get("query_session_id") or payload.get("session_id"),
            filter_manifest=filter_manifest,
            field_transforms=payload.get("field_transforms"),
            requester_id=str(payload.get("requester_id") or "mcp"),
            owner_id=str(payload.get("owner_user_id") or payload.get("owner_id") or "owner"),
            is_grantee_request=False,
            disclosure_ceiling=str(payload.get("disclosure_ceiling") or "default"),
            explicit_disclosure_tier=str(payload.get("disclosure_tier")).strip()
            if payload.get("disclosure_tier")
            else None,
        )
        if scope_substituted_from and isinstance(result, dict):
            # Declared, never silent — the same rule as packet_resolution_reason
            # and empty_cause: a turn says what was done to it.
            result["scope_substituted_from"] = scope_substituted_from
        await _record_query_request(message, payload)
        outcome = "ok"
        return {"id": req_id, "status": "ok", "payload": result}
    except KeyError as exc:
        _log_query_failure(msg_type, req_id, exc, scope_id=scope_id, access_mode=access_mode)
        return {"id": req_id, "status": "error", "error": f"Missing field: {exc}"}
    except Exception as exc:  # noqa: BLE001
        _log_query_failure(msg_type, req_id, exc, scope_id=scope_id, access_mode=access_mode)
        return {"id": req_id, "status": "error", "error": str(exc)}
    finally:
        finish_request(started, outcome, span_token)
