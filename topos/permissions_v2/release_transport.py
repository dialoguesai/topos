"""Dedicated bounded WebSocket dispatch; no generic late-response queue.

Only ControlPlaneClient's actual relay socket calls this function. HTTP/MCP
dispatch cannot inject a socket or a trusted callback through a JSON message.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from .canonical import PolicyError, canonical_bytes
from .release import SourceMessageRelease, parse_source_envelope
from .runtime import get_runtime
from .signing import verify_current_signature

MESSAGE_TYPE = "permissions_v2_source_read"
SEND_TIMEOUT_SECONDS = 5


async def dispatch_source_message(ws, message) -> None:
    """Send one checkpointed disclosure on this socket, with no node gate held.

    The adapter checkpoints under the node write gate, releases it, re-reads the
    grant's authority, then calls `send`; the worker waits for completion of
    ws.send on the socket's loop. No outbox, retry, or reconnect sends a
    disclosure later. On cancellation we drain the worker before returning.
    """
    request_id = message.get("id")
    cancelled = threading.Event()
    try:
        if (os.environ.get("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "").lower() != "true"
            or message.get("type") != MESSAGE_TYPE):
            raise PolicyError("source_release_disabled")
        principal = verify_relay_stamp(message)
        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":
            raise PolicyError("recipient_relay_required")
        body = message.get("payload")
        if not isinstance(body, dict) or set(body) != {"envelope", "intent"}:
            raise PolicyError("release_payload_invalid")
        signed = parse_source_envelope(body["envelope"])
        if request_id != signed.request_id:
            raise PolicyError("request_binding")
        loop = asyncio.get_running_loop()

        def work():
            token = set_principal(principal)
            try:
                runtime = get_runtime()
                review_service = runtime.evidence_reviews(require_existing=True)
                adapter = SourceMessageRelease(protocol=runtime.protocol, resolver=review_service.resolver,
                    reviews=review_service.reviews, clock=lambda: int(time.time()))

                def send(result, output):
                    async def actual_send():
                        # Mutable checks belong to the exact task invoking the
                        # socket, after wait_for has scheduled it.
                        now = int(time.time())
                        if (cancelled.is_set() or result["expires_at"] <= now
                            or os.environ.get("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "").lower() != "true"
                            or get_runtime() is not runtime):
                            raise PolicyError("release_cancelled_or_expired")
                        verify_current_signature(signed, trusted_keys=runtime.protocol.ledger.trusted_keys, now=now)
                        frame = {"id": request_id, "type": MESSAGE_TYPE, "status": "ok",
                                 "payload": {"result": result, "output": output}}
                        await ws.send(canonical_bytes(frame).decode("ascii"))

                    async def transmit():
                        await asyncio.wait_for(actual_send(), SEND_TIMEOUT_SECONDS)
                    # wait_for owns cancellation; the worker must not abandon a
                    # still-running send (it would report a send that may still happen).
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
        # Recipient errors reveal no fact existence, review/protection state,
        # credential/config paths, source text, or exception diagnostics.
        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}
        try:
            await asyncio.wait_for(ws.send(canonical_bytes(error).decode("ascii")), SEND_TIMEOUT_SECONDS)
        except Exception:
            pass
