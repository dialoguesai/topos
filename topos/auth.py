import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .principal import OWNER_AGENT, OWNER_APP, THIRD_PARTY, Principal

bearer_scheme = HTTPBearer(auto_error=False)

#: The owner's approved coding agent names itself on the socket with its
#: enrolment label (mcp_clients.mint_agent_label). AI-tool on-ramp, CONTRACTS
#: §1.1: on the socket a valid label mints OWNER_AGENT, an invalid or revoked
#: one is a 401 and never falls back to owner_app; on any TCP channel the header
#: is a 401 outright and never a third party. Both dependencies below read it,
#: so a label can neither widen nor ride a plain-auth route.
AGENT_HEADER = "X-Topos-Agent"
AGENT_DISCONNECTED = {"error": "agent_disconnected", "message": "This agent was disconnected from Topos."}
AGENT_LABEL_OVER_TCP = {
    "error": "agent_label_over_tcp",
    "message": "An agent label is accepted only on the local Topos socket.",
}


class AgentLabelRefused(HTTPException):
    """A 401 whose body is the error object itself (``{"error", "message"}``)
    once ``agent_label_refusal_handler`` is registered on the app; without the
    handler FastAPI wraps the same object under ``detail``."""


async def agent_label_refusal_handler(request: Request, exc: AgentLabelRefused):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content=exc.detail)


def _agent_label(request: "Optional[Request]") -> str:
    headers = getattr(request, "headers", None) if request is not None else None
    if headers is None:
        return ""
    try:
        return str(headers.get(AGENT_HEADER.lower()) or "").strip()
    except Exception:  # noqa: BLE001 — a header store that cannot be read holds no label
        return ""


def _owner_agent_on_socket(label: str) -> Principal:
    """The agent principal for a label presented on the socket, or the 401.

    Verified against the registry on every call: a revoked row refuses the very
    next request, and there is no cache anywhere for a stale admission to live in.
    """
    from .core.state import get_db_connection
    from .mcp_clients import verify_agent_label

    conn = get_db_connection()
    row = verify_agent_label(conn, label) if conn else None
    if row is None:
        raise AgentLabelRefused(status_code=status.HTTP_401_UNAUTHORIZED, detail=dict(AGENT_DISCONNECTED))
    return Principal(cls=OWNER_AGENT, channel="uds", client_id=str(row.get("client_id") or ""))


def _refuse_label_over_tcp(request: "Optional[Request]") -> None:
    if _agent_label(request):
        raise AgentLabelRefused(status_code=status.HTTP_401_UNAUTHORIZED, detail=dict(AGENT_LABEL_OVER_TCP))

#: Peers that are unambiguously this machine. The owner CLASS is confined to
#: them: a credential that leaked into a log, a backup, or a synced dotfile
#: must not confer owner privilege from somewhere else on the network. The
#: node also binds loopback by default now, so this is the second layer.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"})


def _peer_is_loopback(request: "Optional[Request]") -> bool:
    client = getattr(request, "client", None) if request is not None else None
    host = str(getattr(client, "host", "") or "").strip().lower()
    if not host:
        # No peer info (test client, ASGI transports without a peer): treat as
        # local — the UDS lane and the test suite both land here, and neither
        # is a network path.
        return True
    return host in _LOOPBACK_HOSTS


def require_api_key(
    request: Request = None,  # noqa: B008 — populated by FastAPI
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> None:
    """Validate incoming Bearer token against TOPOS_KEY or TOPOS_OWNER_KEY.

    Authentication only — WHO the caller is (client class) is the separate
    resolve_request_principal dependency. The owner key must authenticate here
    too, or first-party surfaces holding only the new credential would 401 on
    every plain-auth route.
    """
    # Resolve settings at call-time so tests that reload env/modules
    # see the latest TOPOS_KEY value.
    from .config.settings import settings as runtime_settings
    from .uds import current_transport

    if current_transport() == "uds":
        # P4: the owner socket (0600) — the kernel already established the
        # caller is the owner's own process; no bearer exists to check. An agent
        # that names itself must still be known and unrevoked, or the label would
        # be a way onto every plain-auth route as the owner.
        label = _agent_label(request)
        if label:
            _owner_agent_on_socket(label)
        return

    _refuse_label_over_tcp(request)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    presented = credentials.credentials
    legacy = str(runtime_settings.topos_key or "")
    owner = str(getattr(runtime_settings, "topos_owner_key", None) or "")
    if secrets.compare_digest(presented.encode(), legacy.encode()):
        return
    if owner and secrets.compare_digest(presented.encode(), owner.encode()):
        return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization token")


def require_owner_unless_legacy(
    request: Request = None,  # noqa: B008 — populated by FastAPI
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> Optional[Principal]:
    """For HTTP routes that write canonical rows or durable jobs: the owner only.

    Authenticates exactly as require_api_key first, so every 401 is unchanged,
    then refuses any principal that is not owner_app. Once TOPOS_OWNER_KEY
    exists that is every TCP bearer (the legacy key AND the owner key, which TCP
    demotes) — the owner writes through the 0600 socket. A legacy ``None``
    principal (owner key not yet minted) is refused too: the owner-only doors
    never open to a bare TCP bearer (AI-tool on-ramp, 16 Oct 2026; the same hole
    was closed in ``sources/install_authority``).
    """
    require_api_key(request, credentials)
    principal = resolve_request_principal(request, credentials)
    if principal is None or principal.cls != OWNER_APP:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner_mode_required")
    return principal


def resolve_request_principal(
    request: Request = None,  # noqa: B008 — populated by FastAPI
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> Optional[Principal]:
    """Authenticate the Bearer and resolve the channel-verified Principal.

    The class comes from WHICH credential authenticated — never from any payload
    field (payload identity is requester-influenced; see topos/principal.py):

    - TOPOS_OWNER_KEY  -> owner_app  (first-party surface: app, FE)
    - TOPOS_KEY        -> third_party (legacy shared key, demoted)
    - TOPOS_OWNER_KEY unset -> None: legacy mode, consumers keep today's
      behavior byte-for-byte (install-flow invariant — never demote the owner's
      app before it has learned the new key).
    - the socket + X-Topos-Agent -> owner_agent (an approved coding agent,
      named by its enrolment row); an invalid or revoked label is a 401, never
      owner_app; the header on any TCP channel is a 401, never third_party.

    Raises 401 exactly like require_api_key, so a route swapping dependencies
    keeps its auth semantics.
    """
    from .config.settings import settings as runtime_settings
    from .uds import current_transport

    if current_transport() == "uds":
        # P4: owner by TRANSPORT — the 0600 socket is the credential, and no
        # bearer (or lack of one) can demote or promote a connection there. A
        # label only NAMES the caller as one of the owner's agents, a narrower
        # class than the socket alone would mint.
        label = _agent_label(request)
        if label:
            return _owner_agent_on_socket(label)
        return Principal(cls=OWNER_APP, channel="uds")

    _refuse_label_over_tcp(request)
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    presented = credentials.credentials
    legacy = str(runtime_settings.topos_key or "")
    owner = str(getattr(runtime_settings, "topos_owner_key", None) or "")

    if owner and secrets.compare_digest(presented.encode(), owner.encode()):
        # TCP DEMOTION (P4 endgame): the owner CLASS is no longer mintable by
        # any bearer on any TCP peer — loopback included. Owner privilege is a
        # CHANNEL property: the 0600 socket locally, the signed relay stamp
        # remotely. The key still AUTHENTICATES (require_api_key accepts it,
        # every plain-auth route keeps working), but as a third party — so a
        # key that leaks into a log, a backup, or a synced dotfile is worth
        # nothing more than the legacy key it replaced. The FE reaches the
        # socket through its local proxy lane and needs no key at all.
        channel = "local_http" if _peer_is_loopback(request) else "remote_http"
        return Principal(cls=THIRD_PARTY, channel=channel)
    # P2: per-client enrolled tokens (tpk_<client_id>.<secret>). Resolved before
    # the shared legacy key so an enrolled client is NAMED in its principal —
    # and note these authenticate only on principal-aware routes: require_api_key
    # does not accept them, so a tpk holder's surface is the MCP tool set, not
    # every REST endpoint the god key could reach.
    if presented.startswith("tpk_"):
        from .core.state import get_db_connection
        from .mcp_clients import verify_client_token

        conn = get_db_connection()
        row = verify_client_token(conn, presented) if conn else None
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization token"
            )
        return Principal(
            cls=THIRD_PARTY, channel="local_http", client_id=str(row.get("client_id") or "")
        )
    if secrets.compare_digest(presented.encode(), legacy.encode()):
        if not owner:
            return None  # legacy mode: single-key world, no principal enforcement
        return Principal(cls=THIRD_PARTY, channel="local_http")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization token")


async def resolve_request_writer_class(request: Request, *, owner_class: Optional[str] = None) -> str:
    """Writer class for an HTTP write door (features/provenance/writer_class.py).

    For routes that authenticate with require_api_key alone and are also called
    as plain functions (so a ``Depends`` default would never resolve): the class
    comes from the same credential resolution as resolve_request_principal —
    the socket is the owner, the shared key in legacy mode is ``local_legacy``,
    any bearer over TCP once an owner key exists is a third party.
    """
    from .features.provenance.writer_class import WRITER_OWNER_APP, writer_class_for_principal

    credentials = await bearer_scheme(request)
    principal = resolve_request_principal(request, credentials)
    return writer_class_for_principal(principal, owner_class=owner_class or WRITER_OWNER_APP)
