"""Owner socket: attest that rows of a canonical table other than AI chat are the owner's own (OD-50, OD-52).

The journal table's rows carry no owner and, before September 2026, no writer, so
nothing on a row can tell the owner's entry from one another writer sent to the
same source. The owner says so once, explicitly, and the node keeps the
statement as an auditable receipt (``topos/permissions_v2/capture_receipts.py``);
the rows themselves are never re-labelled. The flow is OD-39's
(``permissions_ai_chat_capture.py``) with the table named: ``preview`` returns
counts and a digest of exactly the rows it would cover (no ids, no content);
``attest`` records a receipt only for that digest, with ``confirm: true``;
``revoke`` withdraws one. Only the owner's socket may call any of them.
"""
from fastapi import APIRouter, Depends

from topos.auth import resolve_request_principal

from .permissions_ai_chat_capture import _require_owner_socket, _respond

router = APIRouter(prefix="/v1/permissions-beta/v2/capture-attestation", tags=["permissions-owner-capture"])


@router.post("/preview")
async def preview(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import capture_receipts
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: capture_receipts.preview(
        conn, owner_id=owner_id, table=payload.get("table"), source_id=payload.get("source_id"),
        app_id=payload.get("app_id")), write=False)


@router.post("/attest")
async def attest(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import capture_receipts
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: capture_receipts.attest(
        conn, owner_id=owner_id, table=payload.get("table"), source_id=payload.get("source_id"),
        app_id=payload.get("app_id"), preview_digest=payload.get("preview_digest"),
        confirm=payload.get("confirm")), write=True)


@router.post("/revoke")
async def revoke(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import capture_receipts
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: capture_receipts.revoke(
        conn, owner_id=owner_id, receipt_id=payload.get("receipt_id")), write=True)


@router.get("/receipts")
async def receipts(principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import capture_receipts
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: {"receipts": capture_receipts.receipts(conn, owner_id=owner_id)},
                          write=False)
