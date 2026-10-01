"""Owner socket: attest that pre-stamp AI-chat rows came from the owner's own capture app (OD-39).

Rows the owner's capture app wrote before the node recorded writer classes carry
no writer, so nothing on the row can tell them from a grantee's. The owner says
so once, explicitly, and the node keeps the statement as an auditable receipt
(``topos/permissions_v2/ai_chat_capture.py``); the rows themselves are never
re-labelled. Two steps: ``preview`` returns counts and a digest of exactly the
rows it would cover (no ids, no content); ``attest`` records a receipt only for
that digest, with ``confirm: true``. ``revoke`` withdraws a receipt. Only the
owner's socket may call any of them: an owner key over TCP, a relay message and a
third party are refused.
"""
import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import PolicyError

router = APIRouter(prefix="/v1/permissions-beta/v2/ai-chat/capture-attestation",
                   tags=["permissions-owner-ai-chat-capture"])

_NO_STORE = {"Cache-Control": "no-store"}
_CONFLICT = {"capture_attestation_preview_stale", "capture_receipt_revoked"}
_INVALID = {"capture_attestation_invalid", "capture_attestation_unconfirmed", "capture_receipt_unknown",
            # The generalised receipts' named dataset (permissions_capture_receipts.py): not one install's to name.
            "capture_attestation_dataset_unknown", "capture_attestation_dataset_not_this_node",
            "capture_attestation_dataset_posture_unknown"}


def _require_owner_socket(principal) -> None:
    from topos.principal import OWNER_APP
    if principal is None or principal.cls != OWNER_APP or principal.channel != "uds":
        raise HTTPException(403, "owner_socket_required", headers=_NO_STORE)


def _run(operation, *, write: bool):
    """(owner_id, conn) -> result, on the served canonical database under the write gate."""
    from topos.permissions_v2.runtime import get_runtime
    from topos.storage.db.write_gate import with_db_write

    runtime = get_runtime()
    owner_id = runtime.protocol.ledger.identity.owner_id
    path = runtime.protocol.canonical_database
    with with_db_write():
        conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = operation(owner_id, conn)
            conn.commit() if write else conn.rollback()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


async def _respond(operation, *, write: bool):
    try:
        result = await asyncio.to_thread(_run, operation, write=write)
    except PolicyError as exc:
        if exc.code in {"permissions_v2_disabled", "beta_configuration_required"}:
            raise HTTPException(404, "permissions_v2_disabled", headers=_NO_STORE) from None
        code = 409 if exc.code in _CONFLICT else 400 if exc.code in _INVALID else 403
        raise HTTPException(code, exc.code, headers=_NO_STORE) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "capture_attestation_unavailable", headers=_NO_STORE) from None
    return JSONResponse(result, headers=_NO_STORE)


@router.post("/preview")
async def preview(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import ai_chat_capture
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: ai_chat_capture.preview(
        conn, owner_id=owner_id, source_id=payload.get("source_id"), app_id=payload.get("app_id")), write=False)


@router.post("/attest")
async def attest(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import ai_chat_capture
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: ai_chat_capture.attest(
        conn, owner_id=owner_id, source_id=payload.get("source_id"), app_id=payload.get("app_id"),
        preview_digest=payload.get("preview_digest"), confirm=payload.get("confirm")), write=True)


@router.post("/revoke")
async def revoke(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import ai_chat_capture
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: ai_chat_capture.revoke(
        conn, owner_id=owner_id, receipt_id=payload.get("receipt_id")), write=True)


@router.get("/receipts")
async def receipts(principal=Depends(resolve_request_principal)):
    from topos.permissions_v2 import ai_chat_capture
    _require_owner_socket(principal)
    return await _respond(lambda owner_id, conn: {"receipts": ai_chat_capture.receipts(conn, owner_id=owner_id)},
                          write=False)
