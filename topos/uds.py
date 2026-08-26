"""Owner-privilege Unix socket — principal fabric P4 (engine).

The structural lane: a second uvicorn server serves the SAME FastAPI app on
``~/.topos/engine.sock`` with mode 0600, so the KERNEL admits only the owner's
own processes — the filesystem ACL is the credential, and there is no secret a
third-party client, a leaked log, or a backup could ever carry. Requests
arriving on this socket resolve to the ``owner_app`` principal with no bearer
at all; requests on TCP keep every rule they have today.

Structure, not string-checks: the transport marker is set by an ASGI wrapper
that exists only on the UDS server, so nothing a TCP request contains — headers,
payloads, spoofed markers — can reach the owner branch. The wrapper runs inside
the request task, so the contextvar scopes correctly per request.

The UDS server runs with ``lifespan="off"``: the app's startup/shutdown belongs
to the primary server; this one only accepts connections. Failure to bind is
logged and swallowed — the node must never fail to start over its second door.

Team ID attestation of the peer process (the same-uid-malware defense from the
Who's Asking doc) is the explicit follow-up P4.1; the 0600 kernel gate is the
load-bearing boundary this module ships.
"""
from __future__ import annotations

import contextvars
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("topos.uds")

SOCKET_PATH = "~/.topos/engine.sock"

_transport: contextvars.ContextVar[str] = contextvars.ContextVar(
    "topos_request_transport", default="tcp"
)


def current_transport() -> str:
    return _transport.get()


class UDSChannelApp:
    """ASGI wrapper marking every request on this server as transport=uds."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _transport.set("uds")
        try:
            await self.app(scope, receive, send)
        finally:
            _transport.reset(token)


def socket_path() -> Path:
    return Path(os.path.expanduser(os.environ.get("TOPOS_UDS_PATH") or SOCKET_PATH))


def uds_enabled() -> bool:
    return str(os.environ.get("TOPOS_UDS_ENABLED") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def start_uds_server(app) -> Optional[threading.Thread]:
    """Start the owner socket on a daemon thread; never raises."""
    if not uds_enabled():
        return None
    try:
        import uvicorn

        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists():
                path.unlink()  # stale socket from a previous run
        except OSError:
            logger.warning("could not remove stale socket at %s", path)
            return None

        config = uvicorn.Config(
            UDSChannelApp(app),
            uds=str(path),
            lifespan="off",
            log_level="warning",
        )
        server = uvicorn.Server(config)

        def _run() -> None:
            try:
                server.run()
            except Exception:  # noqa: BLE001
                logger.warning("owner socket server exited", exc_info=True)

        thread = threading.Thread(target=_run, name="topos-uds", daemon=True)
        thread.start()

        def _tighten() -> None:
            # uvicorn creates the socket with the process umask; clamp to 0600
            # the moment it appears. ~/.topos itself typically shields the
            # window, but the clamp is the contract.
            for _ in range(100):
                if path.exists():
                    try:
                        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                        logger.info("owner socket listening at %s (mode 0600)", path)
                    except OSError:
                        logger.warning("could not chmod owner socket", exc_info=True)
                    return
                time.sleep(0.05)
            logger.warning("owner socket did not appear at %s", path)

        threading.Thread(target=_tighten, name="topos-uds-chmod", daemon=True).start()
        return thread
    except Exception:  # noqa: BLE001 — the second door must never break startup
        logger.warning("owner socket startup failed", exc_info=True)
        return None
