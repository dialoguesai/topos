from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> None:
    """Validate incoming Bearer token against TOPOS_KEY."""
    # Resolve settings at call-time so tests that reload env/modules
    # see the latest TOPOS_KEY value.
    from .config.settings import settings as runtime_settings

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization")

    if credentials.credentials != str(runtime_settings.topos_key or ""):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization token")
