"""Owner-only beta policy coordination relay, disabled unless explicitly paired."""
from __future__ import annotations

import asyncio
import time

from .registry import handles


async def _handle(message, operation):
    from ...permissions_v2.canonical import PolicyError
    from ...permissions_v2.runtime import get_runtime
    from ...principal import OWNER_APP, current_principal
    from ...storage.db.write_gate import with_db_write

    req_id = message.get("id")
    principal = current_principal()
    if principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}:
        return {"id": req_id, "status": "error", "code": 403, "error": "owner_mode_required"}
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
            return getattr(runtime.protocol, operation)(payload["envelope"], now=int(time.time()))
    try:
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
