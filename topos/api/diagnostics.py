"""The owner's controls for voluntary diagnostics sharing (docs/DIAGNOSTICS_SHARING.md).

Reached on the owner socket only, and only by the owner's own app: the route checks the transport and the principal
itself, so it holds even if the socket wrapper's agent allowlist is widened or its door split is switched off. A TCP
request is refused whatever key or header it carries; there is no legacy-key, MCP or relay way in. An enrolled coding
agent or app cannot read the status, preview a report, or stop or start sharing.

``send`` and ``share-one-hour`` are ``dangerous`` in ``owner_door_routes.CENSUS`` (they need the Topos app or a
confirmation the owner just gave); ``preview`` and stopping are open to the owner because neither sends anything.

In this build both send doors answer 409 ``collection_unavailable``: the node has no recipient to send to.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..uds import current_transport
from ..auth import resolve_request_principal
from ..principal import OWNER_APP
from ..diagnostics.sharing import SharingError, sharing


def require_diagnostics_owner(request: Request):
    if current_transport() != "uds":
        raise HTTPException(403, "owner_socket_required")
    principal = resolve_request_principal(request, None)
    if principal is None or principal.cls != OWNER_APP:
        raise HTTPException(403, "owner_app_required")


router = APIRouter(prefix="/diagnostics/dialogues", dependencies=[Depends(require_diagnostics_owner)])


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_revision: str = Field(min_length=1, max_length=128)


class SendConsent(Consent):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


async def checked(operation):
    try:
        return await operation
    except SharingError as exc:
        # The detail is one of the module's fixed words (collection_unavailable, policy_changed, ...), never the
        # transport's own error text.
        raise HTTPException(409, str(exc)) from None


@router.get("")
async def status():
    return sharing.status()


@router.post("/preview")
async def preview():
    return sharing.preview()


@router.post("/send")
async def send(body: SendConsent):
    return await checked(sharing.send_preview(body.sha256, body.policy_revision))


@router.post("/share-one-hour")
async def share_one_hour(body: Consent):
    return await checked(sharing.start_hour(body.policy_revision))


@router.delete("")
async def revoke():
    return await sharing.revoke()
