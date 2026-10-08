"""Turn classifier lite (rule-based MVP)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .fingerprint import compute_retrieval_fingerprint
from .intent import compute_intent_hash
from .session import QuerySession, TurnOutcome
from .session_utils import build_cache_key
from .types import ClassificationResult, QueryTurn


def _artifact_field(artifact: Any, field: str) -> Any:
    if isinstance(artifact, dict):
        return artifact.get(field)
    return getattr(artifact, field, None)


class TurnClassifierLite:
    def classify(
        self,
        turn: QueryTurn,
        session: Optional[QuerySession] = None,
        *,
        filter_manifest: Optional[Dict[str, Any]] = None,
        source_ids: Optional[list[str]] = None,
        data_health_version: str = "mvp",
        disclosure_tier: str = "owner_raw",
        grant_id: str = "owner",
        field_transforms: Optional[Any] = None,
        packet_resolution: str = "scores_only",
        principal_cls: str = "",
    ) -> ClassificationResult:
        """BL-65: `packet_resolution` and `principal_cls` are the two disclosure dimensions the pipeline folds into
        the fingerprint and the cache key it STORES an artifact under (`compute_retrieval_fingerprint`,
        `build_cache_key`). The expected ones are built from the same values, so a turn with a principal is
        replayed from its own session, and only by the same principal class at the same resolution: any other
        class or resolution has another fingerprint (and, above `scores_only`, another key) and queries live."""
        if not turn.scope_id:
            return ClassificationResult(outcome=TurnOutcome.DENIED, deny_reason="missing_scope")

        intent_hash = turn.intent_hash or compute_intent_hash(
            scope_id=turn.scope_id,
            access_mode=turn.access_mode,
            query_text=turn.query_text,
        )

        cache_key = build_cache_key(
            scope_id=turn.scope_id, access_mode=turn.access_mode, intent_hash=intent_hash,
            packet_resolution=packet_resolution,
        )
        if session is None:
            return ClassificationResult(outcome=TurnOutcome.LIVE_QUERY, cache_key=cache_key)

        envelope = session.envelope_json or {}
        allowed_scopes = envelope.get("scopes") or []
        allowed_modes = envelope.get("access_modes") or []
        if allowed_scopes and turn.scope_id not in allowed_scopes:
            return ClassificationResult(outcome=TurnOutcome.EXPAND_BOUNDARY)
        if allowed_modes and turn.access_mode not in allowed_modes:
            return ClassificationResult(outcome=TurnOutcome.EXPAND_BOUNDARY)

        expected_fp = compute_retrieval_fingerprint(
            scope_id=turn.scope_id,
            access_mode=turn.access_mode,
            filter_manifest=filter_manifest,
            source_ids=source_ids,
            data_health_version=data_health_version,
            disclosure_tier=disclosure_tier,
            grant_id=grant_id,
            field_transforms=field_transforms,
            packet_resolution=packet_resolution,
            principal_cls=principal_cls,
        )
        for artifact in session.artifacts or []:
            if _artifact_field(artifact, "cache_key") != cache_key:
                continue
            fp = _artifact_field(artifact, "retrieval_fingerprint")
            if fp and fp != expected_fp:
                return ClassificationResult(outcome=TurnOutcome.LIVE_QUERY, cache_key=cache_key)
            return ClassificationResult(outcome=TurnOutcome.MEMORY_HIT, cache_key=cache_key)

        if session.intent_hash and session.intent_hash != intent_hash:
            last_scope = envelope.get("last_scope_id") or (allowed_scopes[0] if allowed_scopes else None)
            if last_scope and turn.scope_id != last_scope and turn.scope_id in allowed_scopes:
                return ClassificationResult(outcome=TurnOutcome.REQUALIFY, cache_key=cache_key)

        return ClassificationResult(outcome=TurnOutcome.LIVE_QUERY, cache_key=cache_key)


TurnClassifier = TurnClassifierLite
