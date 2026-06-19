"""Full query pipeline orchestrator (Phase 3)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..storage.adapters.factory import AdapterFactory
from .audit import build_query_audit_event
from .disclosure import DisclosureFilterPipeline
from .fingerprint import compute_retrieval_fingerprint
from .game_layer import DefaultGameLayer
from .inference import run_query_inference
from .intent import compute_intent_hash
from .manifest import ScopeResolutionManifest
from .retrieval import DefaultSignalRetrievalAdapter
from .session import QueryArtifact, QuerySession, TurnOutcome
from .session_utils import build_cache_key, validate_public_result
from .turn_classifier import TurnClassifierLite
from .types import AccessMode, QueryTurn, RetrievalError, RetrievalRequest


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


class QueryPipelineOrchestrator:
    def __init__(self, adapters=None) -> None:
        self._adapters = adapters or AdapterFactory.from_runtime({"database_hosting_mode": "memory"})
        self._retrieval = DefaultSignalRetrievalAdapter(self._adapters)
        self._disclosure = DisclosureFilterPipeline()
        self._game_layer = DefaultGameLayer()
        self._classifier = TurnClassifierLite()

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
    ) -> Dict[str, Any]:
        session_id = query_session_id or f"qs_{uuid.uuid4()}"
        store = self._session_store()
        try:
            store.purge_expired()
        except Exception:
            pass

        session_data = store.get(session_id)
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

        session = _session_from_store(session_id, session_data, requester_id) if session_data else None

        intent_hash = compute_intent_hash(scope_id=scope_id, access_mode=access_mode, query_text=query_text)
        turn = QueryTurn(query_text=query_text, scope_id=scope_id, access_mode=access_mode, intent_hash=intent_hash)
        classification = self._classifier.classify(turn, session, filter_manifest=filter_manifest)

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

        try:
            bundle = self._retrieval.retrieve(
                RetrievalRequest(
                    manifest=manifest,
                    access_mode=access_mode,
                    query_text=query_text,
                    filter_manifest=filter_manifest,
                    field_transforms=field_transforms,
                    skip_retrieval=False,
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

        filtered = self._disclosure.apply(
            bundle,
            filter_manifest=filter_manifest,
            field_transforms=field_transforms,
            access_mode=access_mode,
        )
        public = self._game_layer.apply(
            context_packet=filtered.context_packet,
            access_mode=access_mode,
            scope_id=scope_id,
            query_text=query_text,
        )
        if access_mode == "inference":
            inf = await asyncio.to_thread(
                run_query_inference,
                query_text=query_text,
                context_packet=filtered.context_packet,
                scope_id=scope_id,
            )
            public.payload.update(inf)

        public_dict = public.to_dict()
        validate_public_result(public_dict)

        fingerprint = compute_retrieval_fingerprint(
            scope_id=scope_id, access_mode=access_mode, filter_manifest=filter_manifest
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
        return {
            "turn_outcome": TurnOutcome.LIVE_QUERY.value,
            "public_result": public_dict,
            "game_layer_strategy": public.strategy,
            "session_id": session_id,
            "query_session_id": session_id,
            "audit": audit,
        }


async def query_live(**kwargs) -> Dict[str, Any]:
    from .runtime import get_query_orchestrator

    return await get_query_orchestrator().execute(**kwargs)
