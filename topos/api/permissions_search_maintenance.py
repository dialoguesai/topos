"""Owner socket maintenance of existing signed message-search indexes."""
from fastapi import APIRouter, Depends, HTTPException

from topos.auth import resolve_request_principal
from topos.core.handlers.permissions_v2 import handle_permissions_v2_message_search_rebuild

router = APIRouter(prefix="/v1/permissions-beta/v2/message-search", tags=["permissions-owner-maintenance"])


@router.post("/rebuild")
async def rebuild(principal=Depends(resolve_request_principal)):
    from topos.principal import set_principal, reset_principal

    token = set_principal(principal)
    try:
        result = await handle_permissions_v2_message_search_rebuild({"id":"owner-search-rebuild"})
    finally:
        reset_principal(token)
    if result.get("status") != "ok":
        raise HTTPException(result.get("code",503), result.get("error","permissions_v2_unavailable"),
                            headers={"Cache-Control":"no-store"})
    from fastapi.responses import JSONResponse
    return JSONResponse(result["payload"], headers={"Cache-Control":"no-store"})


@router.post("/message-review")
async def message_review(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.principal import set_principal, reset_principal
    from topos.core.handlers.permissions_v2 import handle_permissions_v2_message_review
    from fastapi.responses import JSONResponse
    from topos.principal import OWNER_APP
    if principal.cls == OWNER_APP and principal.channel == "uds" and not principal.acting_user:
        from dataclasses import replace
        from topos.permissions_v2.runtime import get_runtime
        principal = replace(principal, acting_user=get_runtime().protocol.ledger.identity.owner_id)
    token = set_principal(principal)
    try:
        result = await handle_permissions_v2_message_review({"id":"owner-message-review","payload":payload})
    finally:
        reset_principal(token)
    if result.get("status") != "ok":
        raise HTTPException(result.get("code",503),result.get("error","message_review_unavailable"),
                            headers={"Cache-Control":"no-store"})
    return JSONResponse(result["payload"],headers={"Cache-Control":"no-store"})
