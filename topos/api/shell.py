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
import re
import sqlite3
import threading
from time import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..core import state
from ..core.logging import get_log_file_path
from ..owner_key import _env_path as owner_env_path
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


_OWNER_KEY_LINE_RE = re.compile(r"^TOPOS_OWNER_KEY=\S+$", re.M)


def _control_plane_state(status: Optional[dict]) -> str:
    """`topos-node status`'s word for the control-plane link (CONTRACTS §4).

    Derived from ``ControlPlaneClient.get_connection_status()``: a rejected key
    is the ``auth`` failure category whatever the backoff state says, since
    that is the one a human must fix by pairing again rather than by waiting.
    """
    if not status:
        return "unknown"
    if status.get("connected") or status.get("state") == "connected":
        return "connected"
    if status.get("last_failure_category") == "auth":
        return "rejected"
    if status.get("state") == "degraded":
        return "degraded"
    return "offline"


def _owner_key_persisted() -> bool:
    """Whether ~/.topos/.env holds a TOPOS_OWNER_KEY line — the value itself is never read out."""
    try:
        text = owner_env_path().read_text(encoding="utf-8")
    except OSError:
        return False
    return _OWNER_KEY_LINE_RE.search(text) is not None


def _identity_present() -> tuple[bool, bool]:
    """``(user_id present, topos_id present)`` from engine_config, as bools only."""
    try:
        conn = state.get_db_connection()
    except Exception:  # noqa: BLE001 — a status never fails on the database
        conn = None
    if conn is None:
        return False, False
    try:
        rows = conn.execute(
            "SELECT key FROM engine_config WHERE key IN ('user_id', 'topos_id')"
            " AND value IS NOT NULL AND value != ''"
        ).fetchall()
    except sqlite3.Error:
        return False, False
    keys = {str(r[0]) for r in rows}
    return "user_id" in keys, "topos_id" in keys

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
    cp_client = state.control_plane_client
    cp_status = cp_client.get_connection_status() if hasattr(cp_client, "get_connection_status") else None
    user_id_present, topos_id_present = _identity_present()
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
        # Additive, for `topos-node status` / `doctor` over the owner socket and the
        # plugin's SessionStart hook (CONTRACTS §4): the control-plane link as a word,
        # who supervises this process, whether the owner key is on disk (DIGEST J6),
        # and the identity ids. Never a key, a user id value or a body from elsewhere.
        "control_plane": _control_plane_state(cp_status),
        "supervisor": current_supervisor(),
        "owner_key_persisted": owner_key_persisted(),
        "topos_id_present": topos_id_present,
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
