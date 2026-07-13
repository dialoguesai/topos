from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config.settings import settings

logger = logging.getLogger("topos.engine.usage_observation")


ACTION_TO_METRIC_KEY: Dict[str, str] = {
    "llm.generate": "llm_tokens",
    "ingestion.file_processed": "file_transfer_mb",
    "uma.permission_ticket.validated": "permission_tickets",
    "source.install.completed": "source_installs",
    "contacts.google.connect.started": "third_party_connections",
}

# Generative Engine subtypes whose chat-token usage should be observed.
GENERATIVE_LLM_SUBTYPES = frozenset(
    {
        "topic_extraction",
        "brief_update",
        "raw_to_summary",
        "goal_extraction",
        "query_inference",
        "emotion_classification",
        "emo_27",
    }
)

PURPOSE_INGESTION_PIPELINE = "ingestion_pipeline"
PURPOSE_USER_REQUEST = "user_request"


def map_action_to_metric_key(action: str) -> Optional[str]:
    return ACTION_TO_METRIC_KEY.get(str(action or "").strip())


def resolve_llm_usage_purpose(
    *,
    task_type: Optional[str] = None,
    subtype: Optional[str] = None,
    origin: Optional[str] = None,
    source_id: Optional[str] = None,
) -> str:
    """Map an engine task to a purpose tag for llm_usage_events.source."""
    origin_key = str(origin or "").strip().lower()
    if origin_key in {PURPOSE_INGESTION_PIPELINE, PURPOSE_USER_REQUEST, "routine_executor"}:
        return origin_key
    source_key = str(source_id or "").strip().lower()
    # Signal derivation helpers reuse query_inference plumbing but are ingestion work.
    if source_key in {"cluster_labeler", "cluster_labels", "topic_clusters"}:
        return PURPOSE_INGESTION_PIPELINE
    subtype_key = str(subtype or "").strip().lower()
    type_key = str(task_type or "").strip().lower()
    if type_key in {"enrichment", "derivation", "transformation"}:
        return PURPOSE_INGESTION_PIPELINE
    if subtype_key in {
        "topic_extraction",
        "brief_update",
        "raw_to_summary",
        "goal_extraction",
        "fact_llm_extract",
        "emotion_classification",
        "emo_27",
    }:
        return PURPOSE_INGESTION_PIPELINE
    if subtype_key == "query_inference":
        return PURPOSE_USER_REQUEST
    if subtype_key in GENERATIVE_LLM_SUBTYPES:
        return PURPOSE_INGESTION_PIPELINE
    return PURPOSE_INGESTION_PIPELINE


def _billing_source_for_provider(provider: Optional[str]) -> str:
    prov = str(provider or "").strip().lower()
    if prov == "ollama":
        return "ollama"
    return "platform"


def _record_local_usage_best_effort(
    *,
    provider: str,
    model: str,
    usage: Dict[str, int],
    source: str,
    idempotency_key: str,
    metadata: Dict[str, Any],
) -> None:
    try:
        from ..core.state import get_db_connection
        from ..llm_integrations_storage import record_local_llm_usage_event

        conn = get_db_connection()
        if conn is None:
            return
        record_local_llm_usage_event(
            conn,
            provider=provider,
            model=model,
            usage=usage,
            billing_source=_billing_source_for_provider(provider),
            source=source,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )
    except Exception:
        logger.debug("local llm_usage_events write failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_identity_hash(identity: Dict[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_usage_idempotency_key(
    *,
    producer: str,
    metric_key: str,
    action: str,
    canonical_action_identity: Dict[str, Any],
) -> str:
    return ":".join(
        [
            str(producer or "engine"),
            str(metric_key or "unknown"),
            str(action or "unknown"),
            _stable_identity_hash(canonical_action_identity),
        ]
    )


def build_usage_observation_envelope(
    *,
    action: str,
    quantity: int,
    producer: str,
    canonical_action_identity: Dict[str, Any],
    topos_id: Optional[str] = None,
    source: Optional[str] = None,
    observed_by: str = "engine",
    trust_class: str = "observe_only",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    metric_key = map_action_to_metric_key(action)
    if not metric_key:
        raise ValueError(f"Unknown usage action: {action}")
    idempotency_key = derive_usage_idempotency_key(
        producer=str(producer or "engine"),
        metric_key=metric_key,
        action=action,
        canonical_action_identity=canonical_action_identity,
    )
    resolved_topos_id = str(topos_id or "").strip() or f"engine:{str(settings.topos_key or '')[:12]}"
    return {
        "event_id": f"eng_usage_{uuid.uuid4().hex}",
        "topos_id": resolved_topos_id,
        "metric_key": metric_key,
        "quantity": int(max(0, quantity)),
        "event_at": _now_iso(),
        "source": str(source or f"engine.{producer}"),
        "observed_by": observed_by,
        "idempotency_key": idempotency_key,
        "producer": producer,
        "action": action,
        "trust_class": trust_class,
        "metadata": metadata or {},
    }


def _build_usage_observation_message(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"usage_obs_{uuid.uuid4().hex}",
        "type": "usage_observation",
        "payload": envelope,
    }


async def emit_usage_observation(
    *,
    action: str,
    quantity: int,
    producer: str,
    canonical_action_identity: Dict[str, Any],
    topos_id: Optional[str] = None,
    source: Optional[str] = None,
    observed_by: str = "engine",
    trust_class: str = "observe_only",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    envelope = build_usage_observation_envelope(
        action=action,
        quantity=quantity,
        producer=producer,
        canonical_action_identity=canonical_action_identity,
        topos_id=topos_id,
        source=source,
        observed_by=observed_by,
        trust_class=trust_class,
        metadata=metadata,
    )

    # Use existing control-plane transport abstraction (WS unsolicited message).
    try:
        from ..core import state as engine_state

        cp_client = getattr(engine_state, "control_plane_client", None)
        if cp_client is not None:
            await cp_client.send_message(_build_usage_observation_message(envelope))
    except Exception:
        # Observation emission must be non-blocking for product flows.
        pass
    return envelope


def emit_usage_observation_sync(
    *,
    action: str,
    quantity: int,
    producer: str,
    canonical_action_identity: Dict[str, Any],
    topos_id: Optional[str] = None,
    source: Optional[str] = None,
    observed_by: str = "engine",
    trust_class: str = "observe_only",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fire-and-forget observation emit safe to call from sync Engine.run threads."""
    envelope = build_usage_observation_envelope(
        action=action,
        quantity=quantity,
        producer=producer,
        canonical_action_identity=canonical_action_identity,
        topos_id=topos_id,
        source=source,
        observed_by=observed_by,
        trust_class=trust_class,
        metadata=metadata,
    )
    message = _build_usage_observation_message(envelope)
    try:
        from ..core import state as engine_state

        cp_client = getattr(engine_state, "control_plane_client", None)
        if cp_client is None:
            return envelope

        # Prefer thread-safe queue (Engine.run often runs in asyncio.to_thread).
        enqueue = getattr(cp_client, "enqueue_unsolicited_message_threadsafe", None)
        if callable(enqueue):
            enqueue(message)
            return envelope

        async def _send() -> None:
            await cp_client.send_message(message)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
            return envelope
        except RuntimeError:
            pass

        client_task = getattr(cp_client, "_task", None)
        client_loop = None
        if client_task is not None:
            try:
                client_loop = client_task.get_loop()
            except Exception:
                client_loop = None
        if client_loop is not None and client_loop.is_running():
            asyncio.run_coroutine_threadsafe(_send(), client_loop)
    except Exception:
        logger.debug("emit_usage_observation_sync failed", exc_info=True)
    return envelope


def emit_engine_llm_usage_observation(
    *,
    task_id: str,
    task_type: Optional[str],
    subtype: Optional[str],
    provider: Optional[str],
    model: Optional[str],
    usage: Dict[str, Any],
    origin: Optional[str] = None,
    source_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    ttfb_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Record + emit llm.generate usage for a completed generative Engine task.

    Any Engine completion that returns chat token usage is recorded under a purpose
    tag (ingestion pipeline by default for enrichment/derivation work).
    """
    subtype_key = str(subtype or "").strip()
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens <= 0 and (prompt_tokens > 0 or completion_tokens > 0):
        total_tokens = prompt_tokens + completion_tokens
    if total_tokens <= 0:
        return None

    purpose = resolve_llm_usage_purpose(
        task_type=task_type,
        subtype=subtype_key,
        origin=origin,
        source_id=source_id,
    )
    provider_key = str(provider or "unknown").strip().lower() or "unknown"
    model_key = str(model or "unknown").strip() or "unknown"
    producer = (
        f"engine.enrichment.{subtype_key or 'generative'}"
        if purpose == PURPOSE_INGESTION_PIPELINE
        else f"engine.{subtype_key or 'generative'}"
    )
    identity = {
        "task_id": str(task_id or ""),
        "subtype": subtype_key or "generative",
        "provider": provider_key,
        "model": model_key,
    }
    # Non-streaming Engine.run: first token/body arrives with the completed response.
    resolved_duration = int(duration_ms) if duration_ms is not None else None
    resolved_ttfb = int(ttfb_ms) if ttfb_ms is not None else resolved_duration
    metadata: Dict[str, Any] = {
        "purpose": purpose,
        "provider": provider_key,
        "model": model_key,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "task_id": str(task_id or ""),
        "subtype": subtype_key,
        "task_type": str(task_type or ""),
        "source_id": str(source_id or ""),
    }
    if resolved_ttfb is not None:
        metadata["ttfb_ms"] = max(0, resolved_ttfb)
    if resolved_duration is not None:
        metadata["duration_ms"] = max(0, resolved_duration)
    envelope = build_usage_observation_envelope(
        action="llm.generate",
        quantity=total_tokens,
        producer=producer,
        canonical_action_identity=identity,
        source=purpose,
        trust_class="cp_observed_self_hosted",
        metadata=metadata,
    )
    # Local dual-write so billing UI sees ingestion tokens even if WS→CP is delayed.
    _record_local_usage_best_effort(
        provider=provider_key,
        model=model_key,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        source=purpose,
        idempotency_key=str(envelope.get("idempotency_key") or ""),
        metadata=metadata,
    )
    # Also notify CP (hosted metering / Supabase / plan ledger).
    try:
        from ..core import state as engine_state

        cp_client = getattr(engine_state, "control_plane_client", None)
        if cp_client is not None:
            message = _build_usage_observation_message(envelope)
            enqueue = getattr(cp_client, "enqueue_unsolicited_message_threadsafe", None)
            if callable(enqueue):
                enqueue(message)
            else:
                emit_usage_observation_sync(
                    action="llm.generate",
                    quantity=total_tokens,
                    producer=producer,
                    canonical_action_identity=identity,
                    source=purpose,
                    trust_class="cp_observed_self_hosted",
                    metadata=metadata,
                )
    except Exception:
        logger.debug("Failed to enqueue engine llm usage observation", exc_info=True)
    return envelope
