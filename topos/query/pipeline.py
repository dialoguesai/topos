"""Full query pipeline orchestrator (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..storage.adapters.factory import AdapterFactory
from .audit import build_query_audit_event
from .ddr import StageTimings, build_disclosure_decision_record, now_ms
from .disclosure import DisclosureFilterPipeline
from .fingerprint import compute_retrieval_fingerprint
from .packet_resolution import effective_packet_resolution
from .game_layer import DefaultGameLayer
from .inference import run_query_inference
from .intent import compute_intent_hash
from .manifest import ScopeResolutionManifest
from .minimizer import DisclosureMinimizer
from .narrowing import NarrowingLedger, result_is_empty
from . import narrowing as _N
from .negotiation import DEFAULT_MAX_ROUNDS, build_narrow_request_response, qualify_intent
from .retrieval import DefaultSignalRetrievalAdapter, resolve_retrieval_source_ids
from .session import QueryArtifact, QuerySession, TurnOutcome
from .session_utils import build_cache_key, validate_public_result
from .source_generation import get_data_health_version, list_installed_source_ids
from .turn_classifier import TurnClassifierLite
from .types import AccessMode, QueryTurn, RetrievalError, RetrievalRequest

logger = logging.getLogger(__name__)

#: Turn outcomes that ended the turn without retrieving anything. An empty answer
#: here means "we never looked", which is the commonest false "no data" of all.
_NOT_QUERIED_OUTCOMES = frozenset(
    {
        TurnOutcome.EXPAND_BOUNDARY.value,
        TurnOutcome.REQUALIFY.value,
        TurnOutcome.NARROW_REQUEST.value,
    }
)


def _attach_narrowing(result: Dict[str, Any], ledger: NarrowingLedger) -> Dict[str, Any]:
    """Put the ledger on the response, and attribute any empty the body did not.

    Reads the turn outcome rather than trusting each return site to have recorded
    its own cause: a denial is a denial whichever of the ten early returns produced
    it, and a memory hit replays a cause that was written into the stored artifact.

    ``empty_cause`` is published only when the result really is empty. The cause is a
    turn-level verdict — *why this came back with nothing* — while the entries are
    stage-level admissions, and a stage can empty its own lane without emptying the
    turn: the rare gate returns ``[]`` for the evidence lane while summaries and
    scores from the derived lanes still reach the caller. Publishing
    ``gate_vetoed`` beside a result that has rows in it is the ledger and the payload
    telling two stories about one turn, and a consumer that believes the ledger —
    the front end's coverage map does, by construction — then reports absent data
    that is sitting in the same response. The gate's ledger ENTRY stays either way;
    only the turn-level claim is withheld. ``pipeline.py``'s other publish site
    already gates on exactly this condition.
    """
    if not isinstance(result, dict):
        return result
    try:
        outcome = str(result.get("turn_outcome") or "")
        public = result.get("public_result")
        if outcome == TurnOutcome.DENIED.value:
            ledger.empty(
                _N.CAUSE_SCOPE_DENIED,
                stage=_N.STAGE_GRANT,
                reason=str(result.get("deny_reason") or "denied"),
            )
        elif outcome in _NOT_QUERIED_OUTCOMES:
            ledger.empty(
                _N.CAUSE_NOT_QUERIED,
                stage=_N.STAGE_GRANT,
                reason=outcome,
            )
        elif isinstance(public, dict) and public.get("empty_cause"):
            # Replayed from a cached artifact (memory hit) — the cause outlives the
            # turn that discovered it.
            ledger.empty(str(public["empty_cause"]))
        empty = result_is_empty(public)
        record = ledger.as_public()
        if not empty:
            record.pop("empty_cause", None)
        result["narrowing"] = {**record, "result_empty": empty}
    except Exception as exc:  # noqa: BLE001 — telemetry may never cost a turn
        logger.debug("narrowing ledger attach skipped: %s", exc)
    return result


def _ddr_debug_enabled() -> bool:
    """When set, the Disclosure Decision Record is surfaced on the pipeline result for the
    eval harness. It is always attached to the (internal) audit event regardless."""
    return str(os.environ.get("TOPOS_QUERY_DDR") or "").strip().lower() in ("1", "true", "yes", "on")


def _negotiation_enabled() -> bool:
    """§C negotiation: when on, a grantee's under-specified/over-broad request gets a
    machine-readable counter-offer instead of proceeding (or bare-denying). Default off —
    additive and opt-in until the A/B eval validates it."""
    return str(os.environ.get("TOPOS_NEGOTIATION") or "").strip().lower() in ("1", "true", "yes", "on")


def _minimizer_enabled() -> bool:
    """§D on-device disclosure minimizer: when on, a grantee's post-filter disclosure is
    reduced to only the facts its intent needs, with a deterministic backstop after. Default
    off — additive and opt-in."""
    return str(os.environ.get("TOPOS_DISCLOSURE_MINIMIZER") or "").strip().lower() in ("1", "true", "yes", "on")


def _selector_enforcement_enabled() -> bool:
    """Selector-aware disclosure enforcement (plan A2 / A2.1 finish).

    Default ON. Safe because missing entity-policy keys on a grant leave
    `entity_selector_policy_active=False` (legacy unrestricted). Explicit empty
    allow-list denies named people. Set TOPOS_SELECTOR_ENFORCEMENT=0|false|off to disable.
    """
    raw = str(os.environ.get("TOPOS_SELECTOR_ENFORCEMENT") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return True


def _looks_like_named_person_ask(query_text: str) -> bool:
    """True when the query appears to select a named person (First Last + ask verbs).

    Used so fabricated / unlinked person asks suppress under an active entity
    allow-list — otherwise raw dumps return content for absent names while
    unauthorized reals return empty (existence-channel leak; D1.3 / A7).
    """
    q = str(query_text or "").strip()
    if not q:
        return False
    # Multi-token proper-name shape; single Capitalized tokens are too ambiguous.
    if not re.search(r"\b[A-Z][a-zA-Z'.\-]+(?:\s+[A-Z][a-zA-Z'.\-]+)+\b", q):
        return False
    return bool(
        re.search(
            r"(?i)\b("
            r"about|with|from|said|messages?|meeting|prep with|what has|tell me"
            r")\b",
            q,
        )
    )


# Thin A8 / A2.3 cohort vocabulary: permit non-entity-specific aggregate answers.
# Does NOT resolve to entity ids (Wave C1). Keep in sync with grant UX copy.
_COHORT_AGGREGATE_PERMIT_TOKENS = frozenset(
    {
        "contacts",
        "message_peers",
        "calendar_attendees",
        "stats_aggregate",
    }
)

# Mirror planner aggregate cues so pipeline can decide refuse-vs-aggregate without
# requiring the full query planner (and without importing planner regex privately).
_AGGREGATE_ASK_RE = re.compile(
    r"\b(how often|how much|how many|how long|average|avg|typically|usually|"
    r"most active|per (?:day|week|month)|trend|total spend|spend on|rhythm|habit|"
    r"people (?:do i|i)|across (?:my )?contacts|message(?:s|ing)? volume)\b",
    re.I,
)


def _looks_like_aggregate_ask(query_text: str) -> bool:
    """True for aggregate-shaped asks (how many / how often / typically…)."""
    return bool(_AGGREGATE_ASK_RE.search(str(query_text or "")))


def _cohort_aggregate_permitted(manifest) -> bool:
    """True when the grant lists a recognized cohort token (A8 aggregate permit).

    Membership tokens (`contacts`, `message_peers`, `calendar_attendees`) also
    widen `accessible_entity_ids` via C1 resolvers; `stats_aggregate` stays
    aggregate-only (no named-person widen).
    """
    for raw in getattr(manifest, "accessible_entity_cohorts", None) or []:
        key = str(raw or "").strip().lower()
        if key in _COHORT_AGGREGATE_PERMIT_TOKENS:
            return True
    return False


def _is_aggregate_only_ask(db_conn, query_text: str) -> bool:
    """Aggregate intent with no named-person selector (linked or person-shaped)."""
    if not _looks_like_aggregate_ask(query_text):
        return False
    if _looks_like_named_person_ask(query_text):
        return False
    if db_conn is None:
        return True
    try:
        from ..features.entities.linking import link_query_entities

        linked = link_query_entities(db_conn, query_text)
    except Exception:
        return True
    return not any(str(e.get("entity_type") or "").lower() == "person" for e in linked)


def _selector_cohort_aggregate_allowed(db_conn, query_text: str, manifest) -> bool:
    """A2.3 / A8: allow a non-entity-specific aggregate under an active selector.

    Triggers (either — both require aggregate-only + active policy):
      1. grant lists a recognized cohort token (`_cohort_aggregate_permitted`), or
      2. ask is clearly aggregate-only under an active entity-selector policy
         (enums-first / stats-style under enums; C1 widens membership separately).

    Named-person / person-shaped asks must NOT use this path (caller enforces
    `_selector_unauthorized` first → denial≡absence).
    """
    if not bool(getattr(manifest, "entity_selector_policy_active", False)):
        return False
    if not _is_aggregate_only_ask(db_conn, query_text):
        return False
    # Preferred grant signal is a recognized cohort token; active selector alone
    # still permits the non-entity-specific aggregate path.
    return True


def _selector_unauthorized(db_conn, query_text: str, manifest) -> bool:
    """Selector-aware disclosure (plan A2, the Maya problem): True when a GRANTEE query
    names a real third-party PERSON entity the grant does not authorize selecting.

    When entity_selector_policy_active is False (legacy grant — keys missing), do not
    suppress. When active and allow-list empty, deny ANY named person — because when the
    requester supplies the selector, response-side redaction is meaningless. Topic/place
    entities are NOT selectors.

    When active and the ask is person-shaped but links to no person (fabricated name),
    also suppress — denial≡absence vs unauthorized reals (CQE; closes raw-dump
    distinguishability on D13-GT-R*).

    Aggregate-only asks are not unauthorized here — A2.3 / A8 may answer them via the
    cohort-aggregate path instead of empty denial.
    """
    if not query_text or db_conn is None:
        return False
    if not bool(getattr(manifest, "entity_selector_policy_active", False)):
        return False
    try:
        from ..features.entities.linking import link_query_entities

        linked = link_query_entities(db_conn, query_text)
    except Exception:
        return False
    persons = [e for e in linked if str(e.get("entity_type") or "").lower() == "person"]
    accessible = {str(x).strip() for x in (getattr(manifest, "accessible_entity_ids", None) or [])}
    if persons:
        return any(str(e.get("entity_id") or "").strip() not in accessible for e in persons)
    # Unlinked person-shaped ask: suppress so fabricated ≡ unauthorized empty.
    return _looks_like_named_person_ask(query_text)


def _persist_negotiation_round(store, session_id, requester_id, session_data, neg_round) -> None:
    """Persist the negotiation round on the session so a repeat request advances the budget.
    Preserves any existing envelope (scopes/access_modes) and never stores disclosed data."""
    prior_env = (session_data or {}).get("envelope_json") or {}
    env = {**prior_env, "negotiation_round": int(neg_round)}
    ttl = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    try:
        store.put(
            {
                "session_id": session_id,
                "requester_id": requester_id,
                "intent_hash": (session_data or {}).get("intent_hash") or "",
                "envelope_json": env,
                "ttl_expires_at": ttl,
            }
        )
    except Exception:
        pass


def _session_from_store(session_id: str, data: Dict[str, Any], requester_id: str) -> QuerySession:
    return QuerySession(
        session_id=session_id,
        requester_id=data.get("requester_id") or requester_id,
        intent_hash=data.get("intent_hash") or "",
        envelope_json=data.get("envelope_json") or data.get("envelope") or {},
        ttl_expires_at=data.get("ttl_expires_at"),
        artifacts=[
            QueryArtifact(
                artifact_id=str(art.get("artifact_id") or ""),
                session_id=session_id,
                cache_key=str(art.get("cache_key") or ""),
                public_result_json=art.get("public_result_json") or {},
                retrieval_fingerprint=str(art.get("retrieval_fingerprint") or ""),
                game_layer_strategy=str(art.get("game_layer_strategy") or "direct"),
            )
            for art in data.get("artifacts") or []
        ],
    )


def _merge_envelope(existing: Optional[Dict[str, Any]], *, scope_id: str, access_mode: str) -> Dict[str, Any]:
    base = dict(existing or {})
    scopes = sorted(set(list(base.get("scopes") or []) + [scope_id]))
    modes = sorted(set(list(base.get("access_modes") or []) + [access_mode]))
    return {**base, "scopes": scopes, "access_modes": modes, "last_scope_id": scope_id}


def _db_conn_from_adapters(adapters) -> Optional[Any]:
    for store in (
        getattr(adapters, "canonical", None),
        getattr(adapters, "query_session", None),
        getattr(adapters, "signal", None),
        getattr(adapters, "graph", None),
    ):
        conn = getattr(store, "_conn", None)
        if conn is not None:
            return conn
    return None


class QueryPipelineOrchestrator:
    def __init__(self, adapters=None) -> None:
        self._adapters = adapters or AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
        self._retrieval = DefaultSignalRetrievalAdapter(self._adapters)
        self._disclosure = DisclosureFilterPipeline()
        self._game_layer = DefaultGameLayer()
        self._classifier = TurnClassifierLite()
        # §D — deterministic on-device minimizer by default (KeywordRelevanceSelector as both
        # selector and fail-closed fallback). Swap in an EngineSelector for the LLM variant.
        self._minimizer = DisclosureMinimizer()

    def _session_store(self):
        return self._adapters.query_session

    async def execute(self, **kwargs: Any) -> Dict[str, Any]:
        """Run one turn and hand back what narrowed it.

        The turn body is `_execute_turn`; this wrapper owns the ledger, which is the
        one thing every exit from that body has in common. Ten early returns end a
        turn — a mode ceiling, a classifier denial, an approval boundary, a session
        requalification, a retrieval error — and attributing each of them at its own
        return site would leave the eleventh unattributed the day someone adds it.
        Reading the outcome here catches every path, including future ones.
        """
        ledger = NarrowingLedger()
        try:
            result = await self._execute_turn(ledger=ledger, **kwargs)
        except TypeError:
            # An unknown keyword is the caller's bug, not the ledger's: re-raise it
            # from the real signature rather than from this wrapper.
            raise
        return _attach_narrowing(result, ledger)

    async def _execute_turn(
        self,
        *,
        ledger: Optional[NarrowingLedger] = None,
        query_text: str,
        scope_id: str,
        access_mode: AccessMode,
        manifest: ScopeResolutionManifest,
        #: What RETRIEVAL should match on, when the caller can say it better than the
        #: raw text can. `query_text` stays the owner's actual words — it is what the
        #: scope shadow classifies (Horos was trained on natural questions, not keyword
        #: bags) and what the audit trail records. This is the search string.
        #:
        #: The two diverge because a request is not a query: "summarize achievements …
        #: with any adjustments made" hands `_residual_content_tokens` a set of rare
        #: needles that no stored row contains, and the rare gate then correctly returns
        #: an empty lane. Measured live 2026-08-17 on one node, one window, one scope —
        #: 0 summaries for the full prompt, 25 for the same question distilled.
        #:
        #: Deliberately narrow: this feeds the rare-gate needles and NOTHING else. The
        #: planner, the vector ranking and the scope classifier keep the owner's
        #: sentence, because 2026-08-16 already measured what happens when they get a
        #: keyword digest instead — lost time windows, an abstaining classifier, and an
        #: embedding of a fragment (see handlers/query.py).
        #:
        #: None (the default) means "same as query_text", so every existing caller and
        #: every MCP client that never sends it is byte-identical.
        retrieval_text: Optional[str] = None,
        #: The same needles, split PER PART of a multi-part request (P3 slice 2). One
        #: flattened needle set cannot gate a multi-part ask: the rare gate vetoes on
        #: ANY unevidenced token, so a specific part empties every other part's lane and
        #: the gate is effectively off on exactly the requests that need it. None (every
        #: caller today) is one part and is byte-identical to before.
        retrieval_parts: Optional[list] = None,
        query_session_id: Optional[str] = None,
        filter_manifest: Optional[Dict[str, Any]] = None,
        field_transforms: Optional[list] = None,
        requester_id: str = "owner",
        owner_id: str = "owner",
        is_grantee_request: bool = False,
        disclosure_ceiling: Optional[str] = None,
        explicit_disclosure_tier: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        session_id = query_session_id or f"qs_{uuid.uuid4()}"
        turn_start_ms = now_ms()
        store = self._session_store()
        try:
            store.purge_expired()
        except Exception:
            pass

        session_data = store.get(session_id)
        owner = ""
        if session_data:
            owner = str(session_data.get("requester_id") or "").strip()
        if owner and owner != str(requester_id).strip():
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.DENIED,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason="session_requester_mismatch",
            )
            return {
                "turn_outcome": TurnOutcome.DENIED.value,
                "public_result": None,
                "audit": audit,
                "session_id": session_id,
                "deny_reason": "session_requester_mismatch",
            }

        if not str(query_text or "").strip():
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.DENIED,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason="empty_query",
            )
            return {
                "turn_outcome": TurnOutcome.DENIED.value,
                "public_result": None,
                "audit": audit,
                "session_id": session_id,
                "deny_reason": "empty_query",
            }

        # §C — negotiation: for grantee requests, press an under-specified / over-broad intent
        # toward a proportional one BEFORE touching any data (fast, leaks nothing). Owner
        # queries are never nagged. Off by default (TOPOS_NEGOTIATION).
        if _negotiation_enabled() and is_grantee_request:
            prior_env = (session_data or {}).get("envelope_json") or {}
            neg_round = int(prior_env.get("negotiation_round") or 0) + 1
            granted = list(prior_env.get("scopes") or []) or [scope_id]
            qualification = qualify_intent(
                scope_id=scope_id,
                access_mode=access_mode,
                query_text=query_text,
                grant_ceiling=manifest.access_mode_ceiling,
                granted_scopes=granted,
                filter_manifest=filter_manifest,
                round=neg_round,
                max_rounds=DEFAULT_MAX_ROUNDS,
            )
            if qualification.exhausted:
                audit = build_query_audit_event(
                    turn_outcome=TurnOutcome.DENIED,
                    scope_id=scope_id,
                    access_mode=access_mode,
                    session_id=session_id,
                    deny_reason="negotiation_exhausted",
                )
                return {
                    "turn_outcome": TurnOutcome.DENIED.value,
                    "public_result": None,
                    "deny_reason": "negotiation_exhausted",
                    "session_id": session_id,
                    "audit": audit,
                }
            if not qualification.ok and qualification.offer is not None:
                _persist_negotiation_round(store, session_id, requester_id, session_data, neg_round)
                audit = build_query_audit_event(
                    turn_outcome=TurnOutcome.NARROW_REQUEST,
                    scope_id=scope_id,
                    access_mode=access_mode,
                    session_id=session_id,
                    deny_reason=qualification.offer.reason,
                )
                return build_narrow_request_response(
                    offer=qualification.offer,
                    scope_id=scope_id,
                    access_mode=access_mode,
                    session_id=session_id,
                    audit=audit,
                )
            # qualification.ok → proceed to normal processing.

        from .retrieval import _mode_allowed

        if not _mode_allowed(access_mode, manifest.access_mode_ceiling):
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.DENIED,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason="mode_ceiling_exceeded",
            )
            return {
                "turn_outcome": TurnOutcome.DENIED.value,
                "public_result": None,
                "audit": audit,
                "session_id": session_id,
                "deny_reason": "mode_ceiling_exceeded",
            }

        session = _session_from_store(session_id, session_data, requester_id) if session_data else None

        needle_parts = [
            str(p or "").strip() for p in (retrieval_parts or []) if str(p or "").strip()
        ]
        # Both halves of P3's retrieval identity. The windows are resolved from the
        # sentence and `now` alone (no connection needed) so the hash covers what the
        # planner will search: the same differenced question asked in two different
        # weeks retrieves two different pairs of windows, and serving one from the
        # other's artifact is a memory_hit fabrication, not a stale answer.
        try:
            from .planner import comparison_windows as _comparison_windows

            plan_windows = _comparison_windows(query_text, now)
        except Exception as exc:  # noqa: BLE001 — hashing must never break a turn
            logger.debug("comparison window resolution skipped: %s", exc)
            plan_windows = []
        intent_hash = compute_intent_hash(
            scope_id=scope_id,
            access_mode=access_mode,
            query_text=query_text,
            retrieval_text=str(retrieval_text or ""),
            needle_parts=needle_parts or None,
            time_windows=plan_windows or None,
        )
        turn = QueryTurn(query_text=query_text, scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash)

        # Shadow-mode scope classification. OFF unless armed, never raises, and changes
        # nothing below this line. Records the head's prediction beside the scope the
        # INCUMBENT ROUTER chose — which is not ground truth, and is why the field is
        # `router_scope`: the router is a heuristic that has been observed picking
        # schedule+work_context for a meta-question that should have queried nothing.
        # Treating this as labels would teach the head to reproduce that.
        #
        # This hook fires only for turns that reached a query. The turn-arrival hook in
        # handlers/tool_index.py catches the rest; both are needed and neither suffices.
        from .scope_shadow import observe as _shadow_observe

        _shadow_observe(query_text, scope_id)

        from ..disclosure.tier import resolve_disclosure_tier
        from ..principal import current_principal

        # Channel-verified client class, stamped by the entry point that
        # authenticated this request (dispatcher contextvar). Read ONCE per
        # turn and threaded to both disclosure floors and the fingerprint —
        # never from the payload.
        _principal = current_principal()

        disclosure_tier = resolve_disclosure_tier(
            requester_id=requester_id,
            owner_id=owner_id,
            is_grantee_request=is_grantee_request,
            explicit_tier=explicit_disclosure_tier,  # type: ignore[arg-type]
            disclosure_ceiling=disclosure_ceiling,
            principal=_principal,
        )

        from ..core.state import get_db_connection
        from .cohort_resolvers import apply_cohort_membership

        db_conn = _db_conn_from_adapters(self._adapters) or get_db_connection()
        # C1: widen allow-list from grant cohorts at grant-apply time (before
        # selector / refuse-vs-aggregate). Fail closed when DB missing.
        manifest = apply_cohort_membership(manifest, db_conn)
        installed_source_ids = list_installed_source_ids(db_conn)
        resolved_source_ids = resolve_retrieval_source_ids(manifest, installed_source_ids or None)
        data_health_version = get_data_health_version(scope_id, resolved_source_ids, db_conn)

        # Packet resolution: computed ONCE per turn with both floors applied
        # (requester + model locality); threaded into retrieval (whether fact
        # content is loaded), the fingerprint and cache key (disclosure
        # dimension), and the inference packet builder. Failure ⇒ scores_only:
        # the conservative default is the safe one.
        try:
            _pr = effective_packet_resolution(
                db_conn, requester_id=str(requester_id), disclosure_tier=disclosure_tier,
                owner_id=str(owner_id or ""), principal=_principal,
            )
        except Exception:  # noqa: BLE001
            _pr = {"setting": "scores_only", "effective": "scores_only",
                   "reason": "resolver_error", "local": True, "provider": "", "model": ""}


        classification = self._classifier.classify(
            turn,
            session,
            filter_manifest=filter_manifest,
            source_ids=resolved_source_ids,
            data_health_version=data_health_version,
            disclosure_tier=disclosure_tier,
            grant_id=str(requester_id),
            field_transforms=field_transforms,
        )

        if classification.outcome == TurnOutcome.DENIED:
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.DENIED,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason=classification.deny_reason,
            )
            return {"turn_outcome": TurnOutcome.DENIED.value, "public_result": None, "audit": audit, "session_id": session_id}

        if classification.outcome == TurnOutcome.EXPAND_BOUNDARY:
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.EXPAND_BOUNDARY,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason="approval_required",
            )
            return {
                "turn_outcome": TurnOutcome.EXPAND_BOUNDARY.value,
                "public_result": None,
                "approval_required": True,
                "session_id": session_id,
                "query_session_id": session_id,
                "audit": audit,
            }

        if classification.outcome == TurnOutcome.REQUALIFY:
            new_session_id = f"qs_{uuid.uuid4()}"
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.REQUALIFY,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=new_session_id,
                deny_reason="intent_scope_change",
            )
            return {
                "turn_outcome": TurnOutcome.REQUALIFY.value,
                "public_result": None,
                "session_id": new_session_id,
                "query_session_id": new_session_id,
                "audit": audit,
            }

        if classification.outcome == TurnOutcome.MEMORY_HIT and session:
            cache_key = classification.cache_key or build_cache_key(
                scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash,
                packet_resolution=_pr["effective"],
            )
            for art in session.artifacts:
                if art.cache_key != cache_key:
                    continue
                strategy = art.game_layer_strategy or "direct"
                audit = build_query_audit_event(
                    turn_outcome=TurnOutcome.MEMORY_HIT,
                    scope_id=scope_id,
                    access_mode=access_mode,
                    session_id=session_id,
                    game_layer_strategy=strategy,
                    cache_keys=[cache_key],
                )
                return {
                    "turn_outcome": TurnOutcome.MEMORY_HIT.value,
                    "public_result": art.public_result_json or {},
                    "game_layer_strategy": art.game_layer_strategy,
                    "session_id": session_id,
                    "query_session_id": session_id,
                    "audit": audit,
                }

        # Selector-aware disclosure (plan A2 / A2.3):
        #   * unauthorized named person → empty denial≡absence (suppress)
        #   * aggregate-only under active selector / cohort grant → non-entity-specific
        #     aggregate (A8); never rewrite a named-person ask into an aggregate
        # Owner requests are never suppressed or forced onto the aggregate path.
        suppress_selectors = False
        cohort_aggregate = False
        if _selector_enforcement_enabled() and bool(is_grantee_request):
            if _selector_unauthorized(db_conn, query_text, manifest):
                suppress_selectors = True
            elif _selector_cohort_aggregate_allowed(db_conn, query_text, manifest):
                cohort_aggregate = True

        timings = StageTimings()
        _t0 = now_ms()
        try:
            bundle = self._retrieval.retrieve(
                RetrievalRequest(
                    manifest=manifest,
                    access_mode=access_mode,
                    query_text=query_text,
                    # Needle matching ONLY — see RetrievalRequest.needle_text. The
                    # planner, the embeddings and the classifier all keep the sentence.
                    needle_text=str(retrieval_text).strip() or None
                    if retrieval_text
                    else None,
                    # Per-part needles: the rare gate runs once per part so no part's
                    # specific ask can veto another's lane.
                    needle_parts=needle_parts or None,
                    filter_manifest=filter_manifest,
                    field_transforms=field_transforms,
                    skip_retrieval=False,
                    installed_source_ids=installed_source_ids or None,
                    disclosure_tier=disclosure_tier,
                    requester_id=requester_id,
                    packet_resolution=_pr["effective"],
                    suppress_selectors=suppress_selectors,
                    cohort_aggregate=cohort_aggregate,
                    # Wall clock by default; eval harnesses inject a fixed now so
                    # the planner's month/as-of arithmetic is reproducible.
                    now=now,
                    ledger=ledger,
                )
            )
        except RetrievalError as exc:
            audit = build_query_audit_event(
                turn_outcome=TurnOutcome.DENIED,
                scope_id=scope_id,
                access_mode=access_mode,
                session_id=session_id,
                deny_reason=exc.code,
            )
            return {
                "turn_outcome": TurnOutcome.DENIED.value,
                "public_result": None,
                "deny_reason": exc.code,
                "session_id": session_id,
                "audit": audit,
            }
        timings.retrieval_ms = now_ms() - _t0

        _t0 = now_ms()
        filtered = self._disclosure.apply(
            bundle,
            filter_manifest=filter_manifest,
            field_transforms=field_transforms,
            access_mode=access_mode,
            disclosure_tier=disclosure_tier,
            ledger=ledger,
        )
        timings.deterministic_filter_ms = now_ms() - _t0

        # §D — on-device minimizer: reduce a grantee's post-filter disclosure to only the facts
        # its intent needs, with the deterministic backstop running LAST. Between the
        # deterministic filter and the game layer; grantee-only; flag-gated.
        final_packet = filtered.context_packet
        minimize_result = None
        if _minimizer_enabled() and disclosure_tier == "default_disclosure":
            _t0 = now_ms()
            minimize_result = await asyncio.to_thread(
                self._minimizer.minimize,
                filtered.context_packet,
                intent=query_text,
                disclosure_tier=disclosure_tier,
            )
            final_packet = minimize_result.packet
            timings.minimizer_ms = now_ms() - _t0

        _t0 = now_ms()
        public = self._game_layer.apply(
            context_packet=final_packet,
            access_mode=access_mode,
            scope_id=scope_id,
            query_text=query_text,
        )
        timings.game_layer_ms = now_ms() - _t0
        if access_mode == "inference" and public.payload.get("answer_type") != "band":
            # C7 facts-direct (W3.1): a known-item ask with live facts gets its
            # exact values, validity dates and evidence counts with ZERO LLM —
            # same contract as `band`: deterministic answer_types are final.
            # Closeness asks rank by interaction, not by extracted role facts —
            # see closeness.py. Runs BEFORE facts-direct because both match the
            # same phrasings and only this one can see who the owner actually
            # talks to; "family" carries no closeness term, so it still lands on
            # the role facts below.
            try:
                from .closeness import try_close_circle
                direct = try_close_circle(
                    db_conn, query_text, packet_resolution=_pr["effective"])
            except Exception:  # noqa: BLE001 — this lane must never break a turn
                direct = None
            if direct is None:
                try:
                    from .facts_direct import try_facts_direct
                    direct = try_facts_direct(
                        db_conn, query_text, packet_resolution=_pr["effective"])
                except Exception:  # noqa: BLE001 — this lane must never break a turn
                    direct = None
            if direct is not None:
                public.payload.update(direct)
        if (access_mode == "inference"
                and public.payload.get("answer_type") not in ("band", "facts")):
            _t0 = now_ms()
            inf = await asyncio.to_thread(
                run_query_inference,
                query_text=query_text,
                context_packet=final_packet,
                scope_id=scope_id,
                packet_resolution=_pr["effective"],
            )
            public.payload.update(inf)
            timings.inference_ms = now_ms() - _t0

        public_dict = public.to_dict()
        # Declared, never silent: every turn says what resolution actually applied and
        # why (active / non_owner_floor / hosted_binding). Same post-hoc pattern as
        # `empty_cause` below.
        public_dict["packet_resolution"] = _pr["effective"]
        if _pr["reason"] != "active" and _pr["setting"] != _pr["effective"]:
            public_dict["packet_resolution_reason"] = _pr["reason"]
        # Introspection ("are we running ours?"): the caller's own verified class,
        # surfaced only when a channel stamped one — legacy turns stay byte-identical.
        if _pr.get("principal_cls"):
            public_dict["principal_cls"] = _pr["principal_cls"]
        # The model that writes the owner's answer reads `public_result`, not the
        # envelope — so the cause of an empty lane has to be IN it, or the answer is
        # "no data" again. Stored on the artifact too, so a memory hit replays the
        # cause instead of losing it.
        if ledger is not None and ledger.empty_cause and result_is_empty(public_dict):
            public_dict["empty_cause"] = ledger.empty_cause
        validate_public_result(public_dict)

        timings.total_ms = now_ms() - turn_start_ms
        # Module-level last-backend is telemetry-grade: concurrent queries can
        # interleave, but per-process it reliably surfaces sustained silent
        # degradation (brute-force scans when the ANN table is missing).
        try:
            from ..storage.adapters.sqlite.vector_search import last_search_backend

            retrieval_meta = {"vector_backend": last_search_backend()}
        except Exception:  # noqa: BLE001
            retrieval_meta = {}
        ddr = build_disclosure_decision_record(
            tier=disclosure_tier,
            mode=access_mode,
            scope=scope_id,
            retrieval_packet=bundle.context_packet,
            filtered_packet=final_packet,
            filters_applied=filtered.filters_applied,
            timings=timings,
            minimizer=minimize_result.ddr_summary() if minimize_result else None,
            backstop_hits=minimize_result.backstop_hits if minimize_result else None,
            retrieval=retrieval_meta,
        ).to_dict()

        fingerprint = compute_retrieval_fingerprint(
            scope_id=scope_id,
            access_mode=access_mode,
            filter_manifest=filter_manifest,
            source_ids=resolved_source_ids,
            data_health_version=data_health_version,
            disclosure_tier=disclosure_tier,
            grant_id=str(requester_id),
            field_transforms=field_transforms,
            packet_resolution=_pr["effective"],
            principal_cls=str(getattr(_principal, "cls", "") or ""),
        )
        cache_key = build_cache_key(scope_id=scope_id, access_mode=access_mode,
                                    intent_hash=intent_hash, packet_resolution=_pr["effective"])
        ttl = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        prior_envelope = session.envelope_json if session else {}
        store.put(
            {
                "session_id": session_id,
                "requester_id": requester_id,
                "intent_hash": intent_hash,
                "envelope_json": _merge_envelope(prior_envelope, scope_id=scope_id, access_mode=access_mode),
                "ttl_expires_at": ttl,
            }
        )
        # Turn-level total, in whole milliseconds, on the one durable per-turn record.
        # Spans the whole retrieve-to-persist path: retrieval, disclosure filtering,
        # the minimizer, the game layer, inference, DDR assembly and session write.
        # It cannot include the artifact INSERT that carries it — a value cannot time
        # its own write — so it is the turn's cost up to the moment of persistence.
        #
        # `timings.total_ms` (the DDR's) stops earlier, before the DDR is built and the
        # session is written; this is deliberately the later, more complete reading,
        # because the question it answers is what the *request* cost, not what the
        # measured stages summed to.
        turn_duration_ms = max(0, int(round(now_ms() - turn_start_ms)))
        store.append_artifact(
            session_id,
            {
                "artifact_id": str(uuid.uuid4()),
                "cache_key": cache_key,
                "public_result_json": public_dict,
                "retrieval_fingerprint": fingerprint,
                "game_layer_strategy": public.strategy,
                "duration_ms": turn_duration_ms,
            },
        )

        audit = build_query_audit_event(
            turn_outcome=TurnOutcome.LIVE_QUERY,
            scope_id=scope_id,
            access_mode=access_mode,
            session_id=session_id,
            game_layer_strategy=public.strategy,
            stores_touched=bundle.stores_touched,
            filters_applied=filtered.filters_applied,
            retrieval_metadata=bundle.retrieval_metadata,
        )
        audit["disclosure_tier"] = disclosure_tier
        audit["disclosure_decision_record"] = ddr
        if ledger is not None and ledger:
            # Counts and enums only — the audit trail leaves the node.
            audit["narrowing"] = ledger.as_telemetry()
            logger.debug("narrowing ledger: %s", ledger.as_local())
        result = {
            "turn_outcome": TurnOutcome.LIVE_QUERY.value,
            "public_result": public_dict,
            "game_layer_strategy": public.strategy,
            "session_id": session_id,
            "query_session_id": session_id,
            "audit": audit,
            "disclosure_tier": disclosure_tier,
        }
        if _ddr_debug_enabled():
            result["disclosure_decision_record"] = ddr
        return result


async def query_live(**kwargs) -> Dict[str, Any]:
    from .runtime import get_query_orchestrator

    return await get_query_orchestrator().execute(**kwargs)
