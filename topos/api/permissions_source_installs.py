"""Owner socket: list a source's runtime installs and retire a duplicate one (owner maintenance).

A source with two active installs has no resolvable posture, so the permissions
reader withholds every row of it (``evidence._source_posture``). The owner picks
which install stays; ``topos/sources/install_maintenance.py`` does the rest.
``GET`` lists ids, dates, status, declared posture and scope, no source content.
``POST /deactivate`` is a dry run unless the body says ``"dry_run": false`` and
``"confirm": true``; a dry run makes the change inside a transaction, reads what
the permissions reader would then see, and rolls it back. Only the owner's
socket may call either: an owner key over TCP, a relay message and a third party
are refused.
"""
import asyncio
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal
from topos.permissions_v2.canonical import PolicyError

from .permissions_ai_chat_capture import _NO_STORE, _require_owner_socket
from .permissions_capture_receipts import _node_resource_id

router = APIRouter(prefix="/v1/sharing/source-installs", tags=["permissions-owner-maintenance"])

_INVALID = {"source_id_required", "install_id_required", "install_deactivation_unconfirmed"}
_CONFLICT = {"install_not_active", "install_last_active"}


def _run(operation, *, commit: bool):
    """(owner_id, conn) -> result on the served canonical database, under the write gate; commits only if asked."""
    from topos.permissions_v2.runtime import get_runtime
    from topos.storage.db.write_gate import with_db_write

    runtime = get_runtime()
    owner_id = runtime.protocol.ledger.identity.owner_id
    path = runtime.protocol.canonical_database
    with with_db_write():
        conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = operation(owner_id, conn)
            if commit(result) if callable(commit) else commit:
                conn.commit()
            else:
                conn.rollback()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


async def _respond(operation, *, commit):
    from topos.sources.install_maintenance import MaintenanceError
    try:
        result = await asyncio.to_thread(_run, operation, commit=commit)
    except MaintenanceError as exc:
        code = 400 if exc.code in _INVALID else 409 if exc.code in _CONFLICT else 404
        raise HTTPException(code, exc.code, headers=_NO_STORE) from None
    except PolicyError as exc:
        if exc.code in {"permissions_v2_disabled", "beta_configuration_required"}:
            raise HTTPException(404, "permissions_v2_disabled", headers=_NO_STORE) from None
        raise HTTPException(403, exc.code, headers=_NO_STORE) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "source_install_maintenance_unavailable", headers=_NO_STORE) from None
    return result


@router.get("")
async def installs(source_id: str, principal=Depends(resolve_request_principal)):
    from topos.sources import install_maintenance
    _require_owner_socket(principal)
    result = await _respond(lambda owner_id, conn: install_maintenance.installs(conn, source_id=source_id),
                            commit=False)
    return JSONResponse(result, headers=_NO_STORE)


@router.post("/deactivate")
async def deactivate(payload: dict, principal=Depends(resolve_request_principal)):
    from topos.sources import install_maintenance
    _require_owner_socket(principal)
    result = await _respond(lambda owner_id, conn: install_maintenance.deactivate(
        conn, owner_id=owner_id, source_id=payload.get("source_id"), install_id=payload.get("install_id"),
        dry_run=payload.get("dry_run", True), confirm=payload.get("confirm"), resource_id=_node_resource_id()),
        commit=lambda answer: answer["deactivated"])
    scope_key = result.pop("_scope_key")
    if result["deactivated"]:
        install_maintenance.forget_runtime_handle(scope_key=scope_key, source_id=result["source_id"])
    return JSONResponse(result, headers=_NO_STORE)
