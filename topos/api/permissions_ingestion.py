"""Owner socket access to the same signed beta command as the CP relay."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import MAX_BYTES, PolicyError, parse_json
from topos.permissions_v2.ingest_dispatch import execute_signed_ingest

router = APIRouter(prefix="/v1/permissions-beta/v2/ingestion", tags=["permissions-beta-owner-ingestion"])


@router.post("/command")
async def command(request: Request, principal=Depends(resolve_request_principal)):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise HTTPException(400, "ingest_payload_invalid")
    try:
        payload = parse_json(bytes(body))
        if not isinstance(payload, dict) or set(payload) != {"envelope"}:
            raise PolicyError("ingest_payload_invalid")
        ack = await execute_signed_ingest(payload["envelope"], principal=principal)
        return JSONResponse({"ack": ack.model_dump()}, headers={"Cache-Control": "no-store"})
    except PolicyError as exc:
        code = 404 if exc.code in {"permissions_v2_disabled", "ingest_snapshots_disabled"} else 403
        raise HTTPException(code, "ingest_disabled" if code == 404 else "ingest_authority_invalid",
                            headers={"Cache-Control": "no-store"}) from None
    except Exception:
        raise HTTPException(503, "ingest_unavailable", headers={"Cache-Control": "no-store"}) from None
