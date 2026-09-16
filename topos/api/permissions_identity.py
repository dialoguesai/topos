"""Owner socket access to the same signed identity command as the CP relay."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, parse_json
from topos.permissions_v2.identity_dispatch import execute_signed_identity_command

router = APIRouter(prefix="/v1/permissions-beta/v2/identity", tags=["permissions-beta-owner-identity"])


@router.post("/command")
async def command(request: Request, principal=Depends(resolve_request_principal)):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise HTTPException(400, "identity_payload_invalid")
    try:
        payload = parse_json(bytes(body))
        if not isinstance(payload, dict) or set(payload) != {"envelope"}:
            raise PolicyError("identity_payload_invalid")
        ack = await execute_signed_identity_command(payload["envelope"], principal=principal)
        return JSONResponse({"ack": ack.model_dump()}, headers={"Cache-Control": "no-store"})
    except PolicyError as exc:
        code = 404 if exc.code in {"permissions_v2_disabled", "identity_attestations_disabled"} else 403
        raise HTTPException(code, "identity_disabled" if code == 404 else "identity_authority_invalid",
                            headers={"Cache-Control": "no-store"}) from None
    except Exception:
        raise HTTPException(503, "identity_unavailable", headers={"Cache-Control": "no-store"}) from None
