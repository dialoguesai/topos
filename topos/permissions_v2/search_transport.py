"""Dedicated bounded WebSocket dispatch for p2c-v1 search.

Same doors as the locator transport (release_transport.py): a feature flag, the
CP relay stamp of a THIRD_PARTY recipient, a payload of exactly
{envelope, intent}, request-id binding, one uniform error frame. One deliberate
difference (design §7 R12): the adapter checkpoints and signs, returns, and only
then is the frame sent, so no node gate is held through the network write.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from .canonical import PolicyError, canonical_bytes
from .runtime import get_runtime
from .search_release import parse_search_envelope
from .signing import verify_current_signature

MESSAGE_TYPE = "permissions_v2_message_search"
SEND_TIMEOUT_SECONDS = 5
FLAG = "TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED"


def _enabled() -> bool:
    return os.environ.get(FLAG, "").lower() == "true"


async def dispatch_message_search(ws, message) -> None:
    request_id = message.get("id")
    cancelled = threading.Event()
    try:
        if not _enabled() or message.get("type") != MESSAGE_TYPE:
            raise PolicyError("message_search_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        body = message.get("payload")
        if not isinstance(body, dict) or set(body) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        signed = parse_search_envelope(body["envelope"])
        if request_id != signed.request_id:
            raise PolicyError("request_binding")

        def work():
            token = set_principal(principal)
            try:
                runtime = get_runtime()
                adapter = runtime.message_search()
                if cancelled.is_set():
                    raise PolicyError("release_cancelled_or_expired")
                return runtime, adapter.dispatch(envelope=body["envelope"], payload=body["intent"], request_id=request_id)
            finally:
                reset_principal(token)

        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            runtime, (result, output) = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
        # Every node gate is released here. The checkpoint already decided this send.
        now = int(time.time())
        if (cancelled.is_set() or result["expires_at"] <= now or not _enabled() or get_runtime() is not runtime):
            raise PolicyError("release_cancelled_or_expired")
        verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=now)
        # Narrow the window the gate release opens (design §7 R12): a grant revoked, expired or
        # re-policied since the checkpoint no longer sends. A brief ledger read, then no gate.
        ledger = runtime.protocol.ledger

        def current_authority():
            with ledger._transaction() as db:
                return ledger._authority(db, signed.grant_id, now)[0]
        authority = await asyncio.to_thread(current_authority)
        if authority.model_dump() != result["authority"]:
            raise PolicyError("authority_stale")
        frame = {"id": request_id, "type": MESSAGE_TYPE, "status": "ok", "payload": {"result": result, "output": output}}
        await asyncio.wait_for(ws.send(canonical_bytes(frame).decode("ascii")), SEND_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Recipient errors reveal no fact existence, index state, review/protection
        # state, credential/config paths, query text or exception diagnostics.
        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}
        try:
            await asyncio.wait_for(ws.send(canonical_bytes(error).decode("ascii")), SEND_TIMEOUT_SECONDS)
        except Exception:
            pass
