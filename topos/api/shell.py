"""Shell contract endpoints.

Everything a menu-bar/tray shell (the pystray tray, the future Swift macOS
shell, the Windows shell) needs to supervise this node lives here, so shells
stay dumb renderers: identity + version for attach-don't-double-start, the
log file location for a "Show Logs" action, and cached update state with a
localhost-only trigger to apply an update.

Update *checking* is not done here — the runtime update monitor started in
app startup keeps ``get_update_info()`` fresh; this router only reads it.
"""

from __future__ import annotations

import os
import threading
from time import time

from fastapi import APIRouter, HTTPException, Request

from ..core.logging import get_log_file_path
from ..runtime_update import (
    DEFAULT_PACKAGE_NAME,
    apply_package_update,
    get_runtime_version,
    get_update_info,
)

router = APIRouter(prefix="/v1/shell", tags=["shell"])

_STARTED_AT = time()

_apply_lock = threading.Lock()
_apply_state: dict[str, object] = {"applying": False, "last_result": None}


def _client_is_local(request: Request) -> bool:
    # Applying an update is the one mutating action here; a LAN peer must not
    # be able to trigger it even though the node binds 0.0.0.0. ("testclient"
    # is FastAPI's TestClient peer name — unreachable over a real socket.)
    host = request.client.host if request.client else None
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


@router.get("/status")
async def shell_status() -> dict:
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
