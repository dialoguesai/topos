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


def socket_is_live(path: "Path") -> bool:
    """True when something is actually accepting on this socket path.

    A socket FILE proves nothing — a crashed or clobbered instance leaves the
    inode behind. Connecting is the only honest test, and it is what keeps us
    from unlinking a socket another live node owns.
    """
    import socket as _socket

    try:
        s_ = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s_.settimeout(0.5)
        try:
            s_.connect(str(path))
            return True
        finally:
            s_.close()
    except OSError:
        return False


def uds_enabled() -> bool:
    return str(os.environ.get("TOPOS_UDS_ENABLED") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


#: How often the supervisor re-checks that the owner socket is still ours and
#: still accepting. The lane went silently dead once (2026-08-26: the socket
#: file was replaced while this process kept a listener on the unlinked inode),
#: and with TCP demoted there is no other local owner lane — so a dead socket
#: must heal itself rather than wait for someone to notice.
_SUPERVISE_INTERVAL_S = 20.0


def start_uds_server(app) -> Optional[threading.Thread]:
    """Start the owner socket and keep it alive on a daemon thread; never raises."""
    if not uds_enabled():
        return None
    try:
        path = socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and socket_is_live(path):
            # Another live node owns this path. Unlinking it would sever THEIR
            # listener (the inode survives, the name does not) — the exact way
            # this lane died before. Stand down instead.
            logger.warning("owner socket already served by another instance at %s", path)
            return None

        def _supervise() -> None:
            server = None
            while True:
                try:
                    if server is None or not socket_is_live(path):
                        if server is not None:
                            logger.warning("owner socket stopped serving — rebinding")
                            server.should_exit = True
                        elif path.exists() and socket_is_live(path):
                            time.sleep(_SUPERVISE_INTERVAL_S)
                            continue
                        server = _bind_once(app, path)
                except Exception:  # noqa: BLE001 — the supervisor must never die
                    logger.debug("owner socket supervisor cycle failed", exc_info=True)
                time.sleep(_SUPERVISE_INTERVAL_S)

        thread = threading.Thread(target=_supervise, name="topos-uds", daemon=True)
        thread.start()
        return thread
    except Exception:  # noqa: BLE001 — the second door must never break startup
        logger.warning("owner socket startup failed", exc_info=True)
        return None


def _bind_once(app, path: "Path"):
    """Bind one uvicorn server to the socket; returns the server or None."""
    try:
        import uvicorn

        try:
            if path.exists() and not socket_is_live(path):
                path.unlink()  # dead inode from a crashed or clobbered run
        except OSError:
            logger.warning("could not remove stale socket at %s", path)
            return None

        from uvicorn.protocols.http.h11_impl import H11Protocol

        class _AttestingH11(H11Protocol):
            """P4.1: attest the peer PROCESS at accept, before any bytes parse."""

            def connection_made(self, transport):  # noqa: D102
                sock = transport.get_extra_info("socket")
                if sock is not None and not peer_admitted(sock):
                    transport.close()
                    return
                super().connection_made(transport)

        config = uvicorn.Config(
            UDSChannelApp(app),
            uds=str(path),
            lifespan="off",
            log_level="warning",
            http=_AttestingH11,
        )
        server = uvicorn.Server(config)

        def _run() -> None:
            try:
                server.run()
            except Exception:  # noqa: BLE001
                logger.warning("owner socket server exited", exc_info=True)

        threading.Thread(target=_run, name="topos-uds-serve", daemon=True).start()

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
        return server
    except Exception:  # noqa: BLE001 — a bind failure is retried by the supervisor
        logger.warning("owner socket bind failed", exc_info=True)
        return None


# ---------------------------------------------------------- P4.1 attestation
# Team ID attestation of the PEER PROCESS — the same-uid-malware defense. The
# 0600 kernel gate already restricts the socket to the owner's uid; this layer
# additionally asks WHICH of the owner's programs is connecting. Enforcement is
# opt-in via TOPOS_UDS_TEAM_IDS (comma-separated Apple Team IDs, e.g. the shell
# app's 25AMARRV2F): unset, every same-uid peer is admitted and merely logged —
# the dev lane's unsigned python/node processes must keep working by default.
# When set, an unsigned, unreadable, or non-allowlisted peer's connection is
# closed at accept, before a single request byte is parsed.

_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_TEAM_RE = None


def _peer_pid(sock) -> Optional[int]:
    try:
        pid = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
        return int(pid) if pid > 0 else None
    except OSError:
        return None


def _pid_executable(pid: int) -> Optional[str]:
    import ctypes
    import ctypes.util

    try:
        libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        n = libproc.proc_pidpath(ctypes.c_int(pid), buf, ctypes.c_uint32(len(buf)))
        if n <= 0:
            return None
        return buf.value.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _team_id_of(executable: str) -> Optional[str]:
    """TeamIdentifier from codesign; None for unsigned/adhoc/platform binaries."""
    global _TEAM_RE
    import re
    import subprocess

    if _TEAM_RE is None:
        _TEAM_RE = re.compile(r"^TeamIdentifier=(\S+)$", re.M)
    try:
        out = subprocess.run(
            ["codesign", "-dv", "--", executable],
            capture_output=True, text=True, timeout=5,
        )
        m = _TEAM_RE.search(out.stderr or "")
        if not m or m.group(1) == "not":  # "TeamIdentifier=not set"
            return None
        return m.group(1)
    except Exception:  # noqa: BLE001
        return None


def _allowed_team_ids() -> frozenset:
    raw = str(os.environ.get("TOPOS_UDS_TEAM_IDS") or "").strip()
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def peer_admitted(sock) -> bool:
    """Attestation decision for one accepted connection.

    Permissive-log without an allowlist; FAIL-CLOSED with one: any error on any
    step (no pid, unreadable executable, unsigned, wrong team) closes the door.
    """
    allowed = _allowed_team_ids()
    pid = _peer_pid(sock)
    if not allowed:
        logger.debug("owner socket peer pid=%s (attestation off)", pid)
        return True
    if pid is None:
        logger.warning("owner socket: peer pid unavailable; refusing (attestation on)")
        return False
    exe = _pid_executable(pid)
    team = _team_id_of(exe) if exe else None
    if team in allowed:
        return True
    logger.warning(
        "owner socket: refused peer pid=%s exe=%s team=%s (allowlist %s)",
        pid, exe, team, ",".join(sorted(allowed)),
    )
    return False
