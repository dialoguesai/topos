from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ..config.settings import settings


ACTION_TO_METRIC_KEY: Dict[str, str] = {
    "llm.generate": "llm_tokens",
    "ingestion.file_processed": "file_transfer_mb",
    "uma.permission_ticket.validated": "permission_tickets",
    "source.install.completed": "source_installs",
    "contacts.google.connect.started": "third_party_connections",
}


def map_action_to_metric_key(action: str) -> Optional[str]:
    return ACTION_TO_METRIC_KEY.get(str(action or "").strip())


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
            await cp_client.send_message(
                {
                    "id": f"usage_obs_{uuid.uuid4().hex}",
                    "type": "usage_observation",
                    "payload": envelope,
                }
            )
    except Exception:
        # Observation emission must be non-blocking for product flows.
        pass
    return envelope
