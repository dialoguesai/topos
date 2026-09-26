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
