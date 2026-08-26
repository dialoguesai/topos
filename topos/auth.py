import secrets
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .principal import OWNER_APP, THIRD_PARTY, Principal

bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> None:
    """Validate incoming Bearer token against TOPOS_KEY or TOPOS_OWNER_KEY.

    Authentication only — WHO the caller is (client class) is the separate
    resolve_request_principal dependency. The owner key must authenticate here
    too, or first-party surfaces holding only the new credential would 401 on
    every plain-auth route.
    """
    # Resolve settings at call-time so tests that reload env/modules
    # see the latest TOPOS_KEY value.
    from .config.settings import settings as runtime_settings

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


def resolve_request_principal(
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

    Raises 401 exactly like require_api_key, so a route swapping dependencies
    keeps its auth semantics.
    """
    from .config.settings import settings as runtime_settings

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    presented = credentials.credentials
    legacy = str(runtime_settings.topos_key or "")
    owner = str(getattr(runtime_settings, "topos_owner_key", None) or "")

    if owner and secrets.compare_digest(presented.encode(), owner.encode()):
        return Principal(cls=OWNER_APP, channel="local_http")
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
