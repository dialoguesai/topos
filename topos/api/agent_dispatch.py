"""The agent's one door to the dispatcher: ``POST /api/local/agent/dispatch`` (AI-tool on-ramp, CONTRACTS §1.2).

An enrolled coding agent (``topos-node mcp --client <name>``, the stdio server of lane L3) talks to the node over
the owner socket and reaches the dispatcher only through this route. Body ``{"type": "<type>", "payload": {...}}``
becomes ``{id, type, payload}`` and goes to ``handle_control_plane_request`` under the request's channel-verified
principal (``resolve_request_principal``: the socket mints ``owner_app``, or ``owner_agent`` when the request
presents a valid label; lane L1). The answer is the handler's result dict as the dispatcher returned it.

The class decides, never the body:

* ``owner_agent`` reaches exactly ``OWNER_AGENT_ALLOWED_TYPES`` (``query``, ``aggregate``, ``get_sources``);
  any other type is 403 ``{"error": "owner_agent_not_allowed"}`` before anything is dispatched. The dispatcher
  refuses the same types again on its own (``_owner_agent_refusal``), so a second door cannot forget to.
* ``owner_app`` keeps today's rights through this door, so the node's own CLI can use it; the dispatcher's
  dangerous-door split still decides what an unattested socket process may change.
* every other class, and the legacy no-principal mode, is 403 ``owner_mode_required``: this door is the socket's.
* a 401 is the auth dependency's own, untouched.

A dispatcher refusal or handler error answers with its own ``code`` as the HTTP status (400 when it has none)
and the dict as the body, so the stdio server can tell "refused" from "answered".
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from .. import principal as _principal_module
from ..auth import resolve_request_principal
from ..core.handlers import OWNER_AGENT_ALLOWED_TYPES, handle_control_plane_request
from ..principal import OWNER_APP

OWNER_AGENT = getattr(_principal_module, "OWNER_AGENT", "owner_agent")

router = APIRouter(prefix="/api/local/agent", tags=["agent-dispatch"])


def _error(status_code: int, body: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=body)


@router.post("/dispatch")
async def agent_dispatch(
    body: dict = Body(default_factory=dict),  # noqa: B008
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> Any:
    cls: Optional[str] = getattr(principal, "cls", None)
    if cls not in (OWNER_APP, OWNER_AGENT):
        return _error(403, {"error": "owner_mode_required"})
    msg_type = str(body.get("type") or "").strip().lower()
    if not msg_type:
        return _error(400, {"error": "type_required"})
    payload = body.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error(400, {"error": "payload_must_be_an_object"})
    if cls == OWNER_AGENT and msg_type not in OWNER_AGENT_ALLOWED_TYPES:
        return _error(403, {"error": "owner_agent_not_allowed"})
    message = {"id": str(uuid.uuid4()), "type": msg_type, "payload": dict(payload)}
    out = await handle_control_plane_request(message, principal=principal)
    if not isinstance(out, dict):
        return _error(502, {"id": message["id"], "status": "error", "error": "no_response"})
    if out.get("status") == "error":
        code = out.get("code")
        status_code = code if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599 else 400
        return _error(status_code, out)
    return out
