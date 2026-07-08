"""Full query pipeline orchestrator (Phase 3)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..storage.adapters.factory import AdapterFactory
from .audit import build_query_audit_event
from .ddr import StageTimings, build_disclosure_decision_record, now_ms
from .disclosure import DisclosureFilterPipeline
from .fingerprint import compute_retrieval_fingerprint
from .game_layer import DefaultGameLayer
from .inference import run_query_inference
from .intent import compute_intent_hash
from .manifest import ScopeResolutionManifest
from .minimizer import DisclosureMinimizer
from .negotiation import DEFAULT_MAX_ROUNDS, build_narrow_request_response, qualify_intent
from .retrieval import DefaultSignalRetrievalAdapter, resolve_retrieval_source_ids
from .session import QueryArtifact, QuerySession, TurnOutcome
from .session_utils import build_cache_key, validate_public_result
from .source_generation import get_data_health_version, list_installed_source_ids
from .turn_classifier import TurnClassifierLite
from .types import AccessMode, QueryTurn, RetrievalError, RetrievalRequest


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
    """Selector-aware disclosure enforcement (plan A2). Default OFF: the safe-floor policy
    (deny a grantee ANY named person) over-blocks legitimate flows where a person is named
    incidentally within an authorized task ("prep the launch meeting with Alex"). Turning it
    on by default awaits the A2.1 grant-schema, which will carry per-grant
    accessible_entity_ids so authorized people pass while unauthorized selection is denied.
    The mechanism is proven by the SEL eval lane, which sets this flag."""
    return str(os.environ.get("TOPOS_SELECTOR_ENFORCEMENT") or "").strip().lower() in ("1", "true", "yes", "on")


def _selector_unauthorized(db_conn, query_text: str, manifest) -> bool:
    """Selector-aware disclosure (plan A2, the Maya problem): True when a GRANTEE query
    names a real third-party PERSON entity the grant does not authorize selecting.

    Default-deny floor: with no entity-level grant (manifest.accessible_entity_ids empty),
    a grantee may not select ANY named person — because when the requester supplies the
    selector, response-side redaction is meaningless (they conditioned the whole answer on
    that person). Topic/place entities are NOT selectors and pass through. Fabricated names
    don't link, so they never trigger this — which is exactly what makes the real-person
    refusal indistinguishable from an absent-person query (both yield an empty result)."""
    if not query_text or db_conn is None:
        return False
    try:
        from ..features.entities.linking import link_query_entities

        linked = link_query_entities(db_conn, query_text)
    except Exception:
        return False
    persons = [e for e in linked if str(e.get("entity_type") or "").lower() == "person"]
    if not persons:
        return False
    accessible = {str(x).strip() for x in (getattr(manifest, "accessible_entity_ids", None) or [])}
    return any(str(e.get("entity_id") or "").strip() not in accessible for e in persons)


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

    async def execute(
        self,
        *,
        query_text: str,
        scope_id: str,
        access_mode: AccessMode,
        manifest: ScopeResolutionManifest,
        query_session_id: Optional[str] = None,
        filter_manifest: Optional[Dict[str, Any]] = None,
        field_transforms: Optional[list] = None,
        requester_id: str = "owner",
        owner_id: str = "owner",
        is_grantee_request: bool = False,
        disclosure_ceiling: Optional[str] = None,
        explicit_disclosure_tier: Optional[str] = None,
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

        intent_hash = compute_intent_hash(scope_id=scope_id, access_mode=access_mode, query_text=query_text)
        turn = QueryTurn(query_text=query_text, scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash)

        from ..disclosure.tier import resolve_disclosure_tier

        disclosure_tier = resolve_disclosure_tier(
            requester_id=requester_id,
            owner_id=owner_id,
            is_grantee_request=is_grantee_request,
            explicit_tier=explicit_disclosure_tier,  # type: ignore[arg-type]
            disclosure_ceiling=disclosure_ceiling,
        )

        from ..core.state import get_db_connection

        db_conn = _db_conn_from_adapters(self._adapters) or get_db_connection()
        installed_source_ids = list_installed_source_ids(db_conn)
        resolved_source_ids = resolve_retrieval_source_ids(manifest, installed_source_ids or None)
        data_health_version = get_data_health_version(scope_id, resolved_source_ids, db_conn)

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
                scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash
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

        # Selector-aware disclosure (plan A2): a grantee naming an unauthorized third-party
        # person is answered as if that person is absent — empty, mode-appropriate, and
        # byte-identical to a fabricated-name query. Owner requests are never suppressed.
        suppress_selectors = (
            _selector_enforcement_enabled()
            and bool(is_grantee_request)
            and _selector_unauthorized(db_conn, query_text, manifest)
        )

        timings = StageTimings()
        _t0 = now_ms()
        try:
            bundle = self._retrieval.retrieve(
                RetrievalRequest(
                    manifest=manifest,
                    access_mode=access_mode,
                    query_text=query_text,
                    filter_manifest=filter_manifest,
                    field_transforms=field_transforms,
                    skip_retrieval=False,
                    installed_source_ids=installed_source_ids or None,
                    disclosure_tier=disclosure_tier,
                    requester_id=requester_id,
                    suppress_selectors=suppress_selectors,
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
        if access_mode == "inference":
            _t0 = now_ms()
            inf = await asyncio.to_thread(
                run_query_inference,
                query_text=query_text,
                context_packet=final_packet,
                scope_id=scope_id,
            )
            public.payload.update(inf)
            timings.inference_ms = now_ms() - _t0

        public_dict = public.to_dict()
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
        )
        cache_key = build_cache_key(scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash)
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
        store.append_artifact(
            session_id,
            {
                "artifact_id": str(uuid.uuid4()),
                "cache_key": cache_key,
                "public_result_json": public_dict,
                "retrieval_fingerprint": fingerprint,
                "game_layer_strategy": public.strategy,
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
