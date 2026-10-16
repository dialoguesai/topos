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

The socket peer (AI-tool on-ramp, CONTRACTS §1.6; spike P0.11). A request on
this server also carries WHO connected, as a :class:`SocketPeer`: the accepted
connection's process (pid and executable, recorded at accept and looked up by
the ASGI wrapper), whether the request is the web app's dev proxy
(``x-topos-dev-proxy``) and the owner-confirmation token it presents
(``x-topos-owner-confirm``). The dispatcher's dangerous-door split
(``core/handlers`` ``_dangerous_door_allowed``) reads it through
:func:`current_socket_peer`. It is set ONLY by this server's wrapper, so a
principal a route or a loop mints in-process with ``channel="uds"`` has no
socket peer and passes the split by construction: the split is a property of
the real socket, never of a string on a principal.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("topos.uds")

SOCKET_PATH = "~/.topos/engine.sock"

_transport: contextvars.ContextVar[str] = contextvars.ContextVar(
    "topos_request_transport", default="tcp"
)


def current_transport() -> str:
    return _transport.get()


# ------------------------------------------------------------- the socket peer
#: The request header the web app's DEV proxy marks its hops with (topos-react-app
#: ``lib/api/devEngineProxy.ts``). NOTE: as of 15 Oct 2026 ``route.dev.ts`` strips it
#: before forwarding to the node; the FE lane has the one-line request to forward it.
DEV_PROXY_HEADER = b"x-topos-dev-proxy"
#: The request header a CLI path presents the owner-confirmation token in (``write_owner_confirmation``).
OWNER_CONFIRM_HEADER = b"x-topos-owner-confirm"


@dataclass(frozen=True)
class SocketPeer:
    """Who is on the other end of one request on the owner socket, as far as the node can tell.

    ``pid`` and ``executable`` come from the accepted connection (``_register_peer_connection``); None when the
    platform cannot say (Linux, or a scope with no connection record). ``team_id`` is the executable's codesign
    TeamIdentifier when already known (a test's simulated peer, or a cached lookup); ``attested`` resolves it
    lazily otherwise, so no ``codesign`` runs for a request that never asks.
    """

    pid: Optional[int]
    executable: Optional[str]
    dev_proxy: bool = False
    confirmation: str = ""
    team_id: Optional[str] = None
    #: The presented confirmation verified (and consumed) ONCE, where the peer was built (``socket_peer_from_scope``):
    #: the socket-layer gate and the dispatcher's split both read this and never verify twice.
    confirmed: bool = False

    def attested(self, allowed: frozenset) -> bool:
        """Whether this peer is one of the owner's signed apps: its team is on ``allowed``. Never with an empty
        allowlist, never without an executable: the leg is inert until the app's team is named."""
        if not allowed or not self.executable:
            return False
        team = self.team_id if self.team_id is not None else _team_of_peer(self.pid, self.executable)
        return team is not None and team in allowed


_peer: contextvars.ContextVar[Optional[SocketPeer]] = contextvars.ContextVar("topos_socket_peer", default=None)


def current_socket_peer() -> Optional[SocketPeer]:
    """The socket peer of the request in flight, or None: not a request on the owner socket."""
    return _peer.get()


@contextlib.contextmanager
def as_socket_peer(peer: Optional[SocketPeer]) -> Iterator[None]:
    """Run a block as if ``peer`` had sent the request on the socket (tests, and in-process simulations)."""
    token = _peer.set(peer)
    try:
        yield
    finally:
        _peer.reset(token)


class _PeerProcess:
    """One connected process: recorded at accept, dropped when its last connection closes."""

    __slots__ = ("pid", "executable", "team", "team_known", "connections")

    def __init__(self, pid: int, executable: Optional[str]) -> None:
        self.pid = pid
        self.executable = executable
        self.team: Optional[str] = None
        self.team_known = False
        self.connections = 0


#: pid -> the process behind the owner socket's open connections. Written on the accept path (the uvicorn
#: protocol's ``connection_made`` / ``connection_lost``, both on the server's loop thread) and read by the ASGI
#: wrapper in the request task on that same loop; the lock keeps a test's socketpair honest too.
_peer_processes: Dict[int, _PeerProcess] = {}
_peer_processes_lock = threading.Lock()


def _register_peer_connection(sock) -> Optional[int]:
    """Record the process behind one accepted connection; its pid, or None where the platform cannot say."""
    pid = _peer_pid(sock)
    if pid is None:
        return None
    with _peer_processes_lock:
        proc = _peer_processes.get(pid)
        if proc is not None:
            proc.connections += 1
            return pid
    executable = _pid_executable(pid)
    team, team_known = None, False
    if executable and door_team_ids():
        # Attested AT ACCEPT, against the executable of the process that connected (security review F4): the
        # lazy lookup codesigned a path later, and a path can be swapped under a pid. Only with an allowlist,
        # so a node without one never runs codesign.
        team, team_known = _team_id_of(executable), True
    with _peer_processes_lock:
        proc = _peer_processes.get(pid)
        if proc is None:
            proc = _peer_processes[pid] = _PeerProcess(pid, executable)
            proc.team, proc.team_known = team, team_known
        proc.connections += 1
    return pid


def _release_peer_connection(pid: Optional[int]) -> None:
    if pid is None:
        return
    with _peer_processes_lock:
        proc = _peer_processes.get(pid)
        if proc is None:
            return
        proc.connections -= 1
        if proc.connections <= 0:
            _peer_processes.pop(pid, None)


def _team_of_peer(pid: Optional[int], executable: str) -> Optional[str]:
    """The peer's codesign team, looked up once per connected process and remembered while it is connected."""
    proc = _peer_processes.get(pid) if pid is not None else None
    if proc is not None and proc.team_known:
        return proc.team
    team = _team_id_of(executable)
    if proc is not None:
        proc.team, proc.team_known = team, True
    return team


def socket_peer_from_scope(scope: Dict[str, Any]) -> SocketPeer:
    """The peer of one ASGI request on the socket server: the connection record its ``client`` names (the
    protocol writes ``("uds", pid)`` there at accept) and the two headers the legs read."""
    pid: Optional[int] = None
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and len(client) == 2 and client[0] == "uds":
        try:
            pid = int(client[1]) or None
        except (TypeError, ValueError):
            pid = None
    proc = _peer_processes.get(pid) if pid is not None else None
    dev_proxy = False
    confirmation = ""
    for name, value in scope.get("headers") or ():
        lowered = bytes(name).lower()
        if lowered == DEV_PROXY_HEADER:
            dev_proxy = True
        elif lowered == OWNER_CONFIRM_HEADER:
            confirmation = bytes(value).decode("ascii", "replace").strip()
    return SocketPeer(
        pid=pid,
        executable=proc.executable if proc is not None else None,
        dev_proxy=dev_proxy,
        confirmation=confirmation,
        team_id=proc.team if (proc is not None and proc.team_known) else None,
        confirmed=bool(confirmation) and verify_owner_confirmation(confirmation),
    )


def peer_may_change(peer: SocketPeer) -> bool:
    """The socket-side legs of the dangerous-door split, shared by the socket-layer gate and the dispatcher's
    split: an attested app peer, the dev proxy on a node that allows it, or a confirmation the human just gave."""
    return (peer.attested(door_team_ids())
            or (peer.dev_proxy and dev_proxy_allowed())
            or bool(peer.confirmed))


class UDSChannelApp:
    """ASGI wrapper marking every request on this server as transport=uds, with its socket peer; the socket's
    principal step; and the socket-layer dangerous-door gate for the HTTP routes that never reach the dispatcher
    (``owner_door_routes``).

    The principal step (L1 verifier, 15 Oct 2026): there is no principal middleware on the socket, so a labelled
    agent hitting a ``require_api_key``-only route was verified by auth and then served with NO principal, which
    ``install_authority`` and the tier heuristic read as the legacy owner. Every http request on the socket now
    resolves its principal first (``auth.resolve_request_principal``: a label mints ``owner_agent`` or raises L1's
    401; no label is ``owner_app``), sets it as the request's principal for the route, and an ``owner_agent`` reaches
    only ``owner_door_routes.OWNER_AGENT_ROUTES`` (403 ``owner_agent_not_allowed`` elsewhere). Then the door gate."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        token = _transport.set("uds")
        peer = socket_peer_from_scope(scope) if isinstance(scope, dict) else None
        peer_token = _peer.set(peer)
        principal_token = None
        try:
            if peer is not None and scope.get("type") == "http":
                principal, refusal = await resolve_socket_principal(scope)
                if refusal is not None:
                    await _send_json(send, refusal[0], refusal[1])
                    return
                from .principal import set_principal

                principal_token = set_principal(principal)
                if not owner_agent_route_allowed(principal, scope):
                    await _send_json(send, 403, {"error": "owner_agent_not_allowed"})
                    return
                refused, receive = await owner_door_gate(scope, receive, peer)
                if refused:
                    await _send_owner_door_refusal(send)
                    return
            await self.app(scope, receive, send)
        finally:
            if principal_token is not None:
                from .principal import reset_principal

                reset_principal(principal_token)
            _peer.reset(peer_token)
            _transport.reset(token)


async def resolve_socket_principal(scope):
    """(the request's channel-verified principal, None), or (None, (status, body)) when auth refuses it.

    The same resolution a route's ``Depends(resolve_request_principal)`` does, done once for every request on the
    socket so that no route runs with ``None`` for a labelled caller. A refusal's body is the exception's detail
    when it is already an object (L1's ``AgentLabelRefused``: ``{"error", "message"}`` at top level), else FastAPI's
    ``{"detail": ...}``."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from . import auth

    request = Request({**scope, "headers": scope.get("headers") or []})
    try:
        credentials = await auth.bearer_scheme(request)
        principal = auth.resolve_request_principal(request, credentials)
    except HTTPException as exc:
        detail = exc.detail
        return None, (exc.status_code, detail if isinstance(detail, dict) else {"detail": detail})
    return principal, None


def owner_agent_route_allowed(principal, scope) -> bool:
    """Every class but ``owner_agent`` passes here (their routes decide for themselves); an agent reaches only the
    §1.2 list."""
    from . import owner_door_routes, principal as principal_module

    if getattr(principal, "cls", None) != principal_module.OWNER_AGENT:
        return True
    return owner_door_routes.owner_agent_route_allowed(str(scope.get("method") or ""), str(scope.get("path") or ""))


async def _send_json(send, status: int, body: Dict[str, Any]) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]})
    await send({"type": "http.response.body", "body": payload})


# ------------------------------------------------- the socket-layer gate (P0.11b)
OWNER_DOOR_SPLIT_ENV = "TOPOS_OWNER_DOOR_SPLIT"
OWNER_CONFIRMATION_MESSAGE = "This change needs the Topos app."


def owner_door_split_enabled() -> bool:
    """``TOPOS_OWNER_DOOR_SPLIT``: on unless set to 0/false/no/off. Read at call time, like the socket's other flags.
    One flag for both halves of the split: the dispatcher's (``core/handlers``) and this gate."""
    return str(os.environ.get(OWNER_DOOR_SPLIT_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


async def _buffer_body(receive, limit: int):
    """Read the request body so a ``body_rule`` can look at it; (bytes or None when over ``limit``, a receive that
    replays what was read, then passes through)."""
    chunks = []
    size = 0
    over = False
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"") or b""
            size += len(chunk)
            if size > limit:
                over = True  # no rule reads it; the gate refuses, so nothing past the limit is kept
            if not over:
                chunks.append(chunk)
            if not message.get("more_body", False):
                break
        else:
            break
    body = b"".join(chunks)
    replayed = False

    async def replay():
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return (None if over else body), replay


async def owner_door_gate(scope, receive, peer: SocketPeer):
    """(refuse?, the receive to hand the app) for one http request on the socket.

    A mutating method on a route the census names dangerous — or does not name at all — is refused unless the peer
    may change things (``peer_may_change``) or the entry's ``body_rule`` says this body is not a change. Every GET
    and every open, dispatcher-backed or carved-out route passes untouched. Off with the flag."""
    if not owner_door_split_enabled():
        return False, receive
    from . import owner_door_routes

    rule = owner_door_routes.lookup(str(scope.get("method") or "GET"), str(scope.get("path") or ""))
    if rule.verdict not in (owner_door_routes.DANGEROUS, owner_door_routes.UNKNOWN):
        return False, receive
    if peer_may_change(peer):
        return False, receive
    if rule.body_rule is not None:
        body, receive = await _buffer_body(receive, owner_door_routes.BODY_RULE_MAX_BYTES)
        if body is not None:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001 -- not JSON: no body rule can read it
                parsed = None
            if parsed is not None and rule.body_rule(parsed):
                return False, receive
    logger.info("owner socket: refused %s %s (owner confirmation required)", scope.get("method"), scope.get("path"))
    return True, receive


async def _send_owner_door_refusal(send) -> None:
    await _send_json(send, 403, {"error": "owner_confirmation_required", "message": OWNER_CONFIRMATION_MESSAGE})


# ---------------------------------------------------- the dangerous-door legs
def door_team_ids() -> frozenset:
    """The teams whose signed apps pass the dangerous doors without a confirmation: ``TOPOS_OWNER_DOOR_TEAM_IDS``,
    else the global P4.1 allowlist ``TOPOS_UDS_TEAM_IDS`` when one is set (a peer that got past accept under it
    is attested already). Empty by default: the leg is inert, and naming a team here switches NOTHING on at
    accept — uv's unsigned Python and curl keep the socket for everything else."""
    raw = str(os.environ.get("TOPOS_OWNER_DOOR_TEAM_IDS") or "").strip()
    if raw:
        return frozenset(t.strip() for t in raw.split(",") if t.strip())
    return _allowed_team_ids()


def dev_proxy_allowed() -> bool:
    """Whether this node lets the web app's dev proxy through the dangerous doors (``TOPOS_DEV_PROXY_ALLOWED=1``,
    a developer's own setting; off by default). The request must also carry ``x-topos-dev-proxy``."""
    return str(os.environ.get("TOPOS_DEV_PROXY_ALLOWED") or "").strip().lower() in ("1", "true", "yes", "on")


#: The owner-confirmation token file: next to the socket, in the owner's private directory
#: (``~/.topos/owner-confirm.token`` by default; ``TOPOS_UDS_PATH`` moves both).
OWNER_CONFIRM_FILE = "owner-confirm.token"
#: The longest a confirmation may live. A file that promises more is no confirmation at all.
OWNER_CONFIRM_MAX_LIFE_S = 600.0
#: What ``write_owner_confirmation`` gives a confirmation unless asked otherwise.
OWNER_CONFIRM_DEFAULT_TTL_S = 120.0


def owner_confirmation_path() -> Path:
    return socket_path().parent / OWNER_CONFIRM_FILE


def write_owner_confirmation(*, ttl_s: float = OWNER_CONFIRM_DEFAULT_TTL_S, path: Optional[Path] = None) -> str:
    """Write a fresh confirmation after the human said yes on a TTY (the CLI's side, lane L7) and return the
    token the request must present in ``X-Topos-Owner-Confirm``. The file is 0600 from its first byte, replaced
    atomically, and holds ``{"token", "expires_at"}``; ``ttl_s`` is clamped to the cap. One token, one change:
    a successful verification consumes it."""
    target = Path(path) if path is not None else owner_confirmation_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    expires_at = time.time() + max(0.0, min(float(ttl_s), OWNER_CONFIRM_MAX_LIFE_S))
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(4)}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "expires_at": expires_at}, fh)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(target))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(str(tmp))
        raise
    return token


def clear_owner_confirmation(path: Optional[Path] = None) -> None:
    target = Path(path) if path is not None else owner_confirmation_path()
    with contextlib.suppress(OSError):
        target.unlink()


def verify_owner_confirmation(presented: Optional[str], *, path: Optional[Path] = None,
                              now: Optional[float] = None, consume: bool = True) -> bool:
    """Whether ``presented`` is the live confirmation on disk. Fail-closed on every step: no file, a symlink
    or a non-regular file, a mode with group or other bits, another owner, a body that is not the JSON shape,
    an expiry in the past, or one further out than the cap lets the file's own mtime promise. A match consumes
    the file (``consume``), so a token serves one change."""
    if not isinstance(presented, str) or not presented.strip():
        return False
    target = Path(path) if path is not None else owner_confirmation_path()
    try:
        st = target.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) & 0o077:
            return False
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return False
        body = json.loads(target.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- what cannot be read is no confirmation
        return False
    if not isinstance(body, dict):
        return False
    token = body.get("token")
    expires_at = body.get("expires_at")
    if not isinstance(token, str) or not token or isinstance(expires_at, bool) \
            or not isinstance(expires_at, (int, float)):
        return False
    moment = time.time() if now is None else float(now)
    if moment > float(expires_at) or float(expires_at) - st.st_mtime > OWNER_CONFIRM_MAX_LIFE_S:
        return False
    if not secrets.compare_digest(presented.strip().encode("utf-8"), token.encode("utf-8")):
        return False
    if consume:
        clear_owner_confirmation(target)
    return True


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
            """P4.1: attest the peer PROCESS at accept, before any bytes parse; and record who connected, so the
            dangerous-door split can ask (``socket_peer_from_scope``): the connection's pid rides in the ASGI
            scope's ``client`` as ``("uds", pid)``, where uvicorn puts None for a unix socket."""

            _topos_peer_pid: Optional[int] = None

            def connection_made(self, transport):  # noqa: D102
                sock = transport.get_extra_info("socket")
                if sock is not None and not peer_admitted(sock):
                    transport.close()
                    return
                super().connection_made(transport)
                if sock is not None:
                    try:
                        self._topos_peer_pid = _register_peer_connection(sock)
                    except Exception:  # noqa: BLE001 -- an unrecorded peer is an unattested peer, never a closed door
                        logger.debug("owner socket: peer not recorded", exc_info=True)
                        self._topos_peer_pid = None
                self.client = ("uds", self._topos_peer_pid or 0)

            def connection_lost(self, exc):  # noqa: D102
                pid, self._topos_peer_pid = self._topos_peer_pid, None
                _release_peer_connection(pid)
                super().connection_lost(exc)

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
