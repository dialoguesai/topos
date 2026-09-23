"""The owner's shadow-audit re-score, answered on the node (confidence program C6).

Owner-only, like the other permissions-v2 coordination handlers: reached over the relay with an owner principal
whose acting user is this node's own owner, or over the owner's socket. A recipient has no path to it, and it
reads the owner's own rows directly rather than through any recipient surface.

The answer is a verdict and its provenance. Every failure -- no labeler, no index row, a pointer that will not
open, a labeler that raised -- is `unresolved` with a reason, never `agree`: an item nobody checked must not be
counted as one that was.
"""
from __future__ import annotations

import asyncio

from .registry import handles


@handles("permissions_v2_shadow_rescore", owner_only=True)
async def handle_permissions_v2_shadow_rescore(message):
    from ...permissions_v2 import shadow_index, shadow_rescore
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal

    req_id = message.get("id")
    principal = current_principal()
    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
    payload = message.get("payload")
    if not isinstance(payload, dict) or set(payload) != {"request"}:
        return {"id": req_id, "status": "error", "code": 400, "error": "shadow_rescore_payload_invalid"}

    def work():
        runtime = get_runtime()
        if principal.channel == "cp_relay" and principal.acting_user != runtime.protocol.ledger.identity.owner_id:
            raise PolicyError("owner_binding")
        return shadow_rescore.rescore(runtime, payload["request"])

    try:
        result = await asyncio.to_thread(work)
    except PolicyError as exc:
        return {"id": req_id, "status": "error", "code": 403 if exc.code == "owner_binding" else 503,
                "error": exc.code}
    except Exception:
        # Configuration and storage failures must not echo private paths, key material or exception chains.
        return {"id": req_id, "status": "error", "code": 503, "error": "permissions_v2_unavailable"}
    return {"id": req_id, "type": shadow_rescore.MESSAGE_TYPE, "status": "ok",
            "payload": {"result": result.model_dump()},
            # The owner's own count of releases this process could not make auditable. Not a recipient's business
            # and not on any recipient path; it rides here so the control plane can show the hole beside the card.
            "index_failures": shadow_index.failures()}
