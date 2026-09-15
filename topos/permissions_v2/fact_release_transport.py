"""Dedicated fact WebSocket dispatch while all release gates remain held."""
from __future__ import annotations

import asyncio
import os
import threading
import time

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from .canonical import PolicyError, canonical_bytes
from .fact_release import FactProjectionRelease
from .runtime import get_runtime
from .signing import SignedFactEnvelope, verify_current_signature

MESSAGE_TYPE = "permissions_v2_fact_read"
FLAG = "TOPOS_PERMISSIONS_V2_FACT_RELEASE_ENABLED"
SEND_TIMEOUT_SECONDS = 5


async def dispatch_fact_message(ws, message) -> None:
    """No returned data, generic outbox, retry or deferred sender is permitted."""
    request_id = message.get("id")
    cancelled = threading.Event()
    try:
        if os.environ.get(FLAG, "").lower() != "true" or message.get("type") != MESSAGE_TYPE:
            raise PolicyError("fact_release_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        body = message.get("payload")
        if not isinstance(body, dict) or set(body) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        signed = SignedFactEnvelope.parse(body["envelope"])
        if request_id != signed.request_id:
            raise PolicyError("request_binding")
        loop = asyncio.get_running_loop()

        def work():
            token = set_principal(principal)
            try:
                runtime = get_runtime()
                adapter = FactProjectionRelease(protocol=runtime.protocol,
                    projections=runtime.projection_reviews(require_existing=True), clock=lambda: int(time.time()))

                def send(result, output):
                    async def actual_send():
                        # This check runs inside the task that invokes ws.send,
                        # with no intervening task scheduling after validation.
                        now = int(time.time())
                        if (cancelled.is_set() or result["expires_at"] <= now
                            or os.environ.get(FLAG, "").lower() != "true" or get_runtime() is not runtime):
                            raise PolicyError("release_cancelled_or_expired")
                        verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=now)
                        frame = {"id": request_id, "type": MESSAGE_TYPE, "status": "ok",
                            "payload": {"result": result, "output": output}}
                        await ws.send(canonical_bytes(frame).decode("ascii"))

                    async def transmit():
                        await asyncio.wait_for(actual_send(), SEND_TIMEOUT_SECONDS)
                    # The worker retains the write/review gates until the actual
                    # send task finishes or acknowledges its own cancellation.
                    asyncio.run_coroutine_threadsafe(transmit(), loop).result()

                if cancelled.is_set():
                    raise PolicyError("release_cancelled_or_expired")
                adapter.dispatch(envelope=body["envelope"], payload=body["intent"], request_id=request_id, send=send)
            finally:
                reset_principal(token)

        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
    except asyncio.CancelledError:
        raise
    except Exception:
        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}
        try:
            await asyncio.wait_for(ws.send(canonical_bytes(error).decode("ascii")), SEND_TIMEOUT_SECONDS)
        except Exception:
            pass
