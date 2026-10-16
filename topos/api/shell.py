"""Shell contract endpoints.

Everything a menu-bar/tray shell (the pystray tray, the future Swift macOS
shell, the Windows shell) needs to supervise this node lives here, so shells
stay dumb renderers: identity + version for attach-don't-double-start, the
log file location for a "Show Logs" action, and cached update state with a
localhost-only trigger to apply an update.

Polling reads cached state. A manual check explicitly refreshes it from PyPI
off the event loop, without waiting for the six-hour runtime monitor.
"""

from __future__ import annotations

import asyncio
import os
import threading
from time import time

from fastapi import APIRouter, HTTPException, Request

from ..core.logging import get_log_file_path
from ..runtime_update import (
    DEFAULT_PACKAGE_NAME,
    UpdateCheckError,
    _set_update_info,
    apply_package_update,
    check_for_update,
    get_runtime_version,
    get_update_info,
)

router = APIRouter(prefix="/v1/shell", tags=["shell"])

_STARTED_AT = time()

_apply_lock = threading.Lock()
_apply_state: dict[str, object] = {"applying": False, "last_result": None}


def _db_connection():
    """The node's connection, or None — status must answer even without a database."""
    try:
        from ..core.state import get_db_connection

        return get_db_connection()
    except Exception:  # noqa: BLE001 — a status read never raises for the database
        return None


def _identity_fields() -> dict:
    """What `topos-node status --json` (CONTRACTS §4) reads from the node: the
    control plane's Topos id and whether a user id is bound. Ids only — never
    the user id itself and never a key."""
    topos_id = None
    user_id_present = False
    conn = _db_connection()
    if conn is not None:
        try:
            from ..core.state import get_engine_config_value, get_user_id

            topos_id = (get_engine_config_value(conn, "topos_id") or "").strip() or None
            user_id_present = bool(get_user_id(conn))
        except Exception:  # noqa: BLE001
            pass
    return {"topos_id": topos_id, "user_id_present": user_id_present}


def _client_is_local(request: Request) -> bool:
    # Applying an update is the one mutating action here; a LAN peer must not
    # be able to trigger it even though the node binds 0.0.0.0. ("testclient"
    # is FastAPI's TestClient peer name — unreachable over a real socket.)
    host = request.client.host if request.client else None
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


@router.get("/status")
async def shell_status() -> dict:
    from ..cli.service_cmd import current_supervisor
    from ..owner_key import owner_key_persisted
    from ..sanitization.prewarm import prewarm_status

    info = get_update_info()
    log_file = get_log_file_path()
    return {
        "name": "topos-node",
        "version": get_runtime_version(),
        "pid": os.getpid(),
        "started_at": _STARTED_AT,
        "log_file": str(log_file) if log_file else None,
        "update": {
            "available": info is not None,
            "installed": info.installed if info else None,
            "latest": info.latest if info else None,
            "applying": _apply_state["applying"],
            "last_result": _apply_state["last_result"],
        },
        # Additive: existing shells decode a fixed set of keys and ignore this.
        # The tray cannot show it until the Swift ShellStatus struct adds the
        # field (its CodingKeys are explicit), which needs its own signed build —
        # shipping the data first means that release is a display change only.
        "models": prewarm_status(),
        # Additive, for `topos-node status --json` and the plugin's SessionStart
        # hook (CONTRACTS §4): who supervises this process, whether the owner key
        # is on disk (DIGEST J6), and the identity ids. No secret, no user id.
        "supervisor": current_supervisor(),
        "owner_key_persisted": owner_key_persisted(),
        **_identity_fields(),
    }


@router.post("/update")
async def shell_apply_update(request: Request) -> dict:
    if not _client_is_local(request):
        raise HTTPException(
            status_code=403, detail="Shell update can only be triggered from localhost."
        )
    if get_update_info() is None:
        return {"started": False, "reason": "no_update_available"}
    with _apply_lock:
        if _apply_state["applying"]:
            return {"started": False, "reason": "already_applying"}
        _apply_state["applying"] = True
        _apply_state["last_result"] = None

    def worker() -> None:
        ok = False
        try:
            ok = apply_package_update(DEFAULT_PACKAGE_NAME)
        finally:
            with _apply_lock:
                _apply_state["applying"] = False
                # The running process keeps the old version until restart;
                # shells surface "restart to finish" off this value.
                _apply_state["last_result"] = "success" if ok else "failed"

    threading.Thread(target=worker, daemon=True, name="topos-shell-update").start()
    return {"started": True}


@router.post("/update/check")
async def shell_check_update(request: Request) -> dict:
    if not _client_is_local(request):
        raise HTTPException(status_code=403, detail="Shell update checks can only be triggered from localhost.")
    # Never replace the update state while an install/restart is pending.
    with _apply_lock:
        pending = _apply_state["applying"] or _apply_state["last_result"] == "success"
    if not pending:
        try:
            info = await asyncio.to_thread(check_for_update, DEFAULT_PACKAGE_NAME, strict=True)
        except UpdateCheckError as exc:
            # Keep the last known update; a failed network check is not proof
            # the running node is current.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        _set_update_info(info)
    return await shell_status()
