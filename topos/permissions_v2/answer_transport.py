"""Dedicated, bounded WebSocket dispatch for signed answer submit and fetch."""
from __future__ import annotations

import asyncio
import logging

from topos.principal import THIRD_PARTY, reset_principal, set_principal
from topos.relay_stamp import verify_relay_stamp

from .answer_release import AnswerBusy
from .canonical import PolicyError, canonical_bytes
from .runtime import get_runtime

SUBMIT_TYPE = "permissions_v2_answer_submit"
FETCH_TYPE = "permissions_v2_answer_fetch"
#: How long this dispatch waits for the service before answering the uniform refusal. The work itself is not
#: cancelled (a thread cannot be): a fetch still running then hands its body to a reply nobody reads.
WAIT_SECONDS = 15
_log = logging.getLogger(__name__)


async def dispatch_answer(ws, message):
    request_id = message.get("id")
    try:
        kind = message.get("type")
        if kind not in (SUBMIT_TYPE, FETCH_TYPE) or not isinstance(request_id, str):
            raise PolicyError("permission_denied")
        principal = verify_relay_stamp(message)
        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"
                or not principal.acting_user or not principal.client_id):
            raise PolicyError("permission_denied")
        payload = message.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"envelope", "intent"}:
            raise PolicyError("permission_denied")
        token = set_principal(principal)
        try:
            def work():
                service = get_runtime().answers()
                method = service.submit if kind == SUBMIT_TYPE else service.fetch
                return method(envelope=payload["envelope"], payload=payload["intent"], request_id=request_id)

            try:
                result, output = await asyncio.wait_for(asyncio.to_thread(work), timeout=WAIT_SECONDS)
            except asyncio.TimeoutError:
                # BL-159, counts-only: which door, never an id. The service's own line follows with its seconds.
                _log.warning("permissions answer relay: no result within %d s (%s)", WAIT_SECONDS,
                             "fetch" if kind == FETCH_TYPE else "ask")
                raise
        finally:
            reset_principal(token)
        await ws.send(canonical_bytes({"id": request_id, "type": kind, "status": "ok",
                                       "payload": {"result": result, "output": output}}).decode("ascii"))
    except AnswerBusy:
        await ws.send(canonical_bytes({"id": request_id, "type": message.get("type"), "status": "error",
                                       "code": 429, "error": "answer_busy"}).decode("ascii"))
    except Exception:
        await ws.send(canonical_bytes({"id": request_id, "type": message.get("type"), "status": "error",
                                       "code": 403, "error": "permission_denied"}).decode("ascii"))
