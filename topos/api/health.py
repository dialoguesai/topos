from __future__ import annotations

from time import time

from fastapi import Depends, APIRouter
from fastapi.security import HTTPAuthorizationCredentials

from ..__version__ import __version__
from ..auth import bearer_scheme, require_api_key
from ..core.api_models import HealthResponse
from ..core.db_health import probe_db_health
from ..core import state

router = APIRouter()


@router.get("/healthcheck", response_model=HealthResponse)
async def healthcheck(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> HealthResponse:
    # Evaluate health auth dynamically instead of at import time so tests and
    # runtime env changes do not get locked to stale settings.
    from ..config.settings import settings as runtime_settings

    if runtime_settings.enable_health_auth:
        # By keyword: `require_api_key` grew a leading `request` parameter with the
        # principal fabric, and a positional call binds the credentials to it — the
        # credentials parameter then falls back to its `Depends(...)` default and
        # this route answers 500 where it means to answer 401.
        require_api_key(credentials=credentials)
    cp_status = (
        state.control_plane_client.get_connection_status()
        if state.control_plane_client and hasattr(state.control_plane_client, "get_connection_status")
        else None
    )
    sync_status = (
        state.sync_client.get_connection_status()
        if state.sync_client and hasattr(state.sync_client, "get_connection_status")
        else None
    )
    cloud_connected = None
    if cp_status is not None or sync_status is not None:
        cloud_connected = bool(
            (cp_status or {}).get("connected")
            or (sync_status or {}).get("connected")
        )
    db_ok, db_error = await probe_db_health()
    binding = state.active_database
    if binding is None:
        # No startup binding recorded (an injected test connection, or a build
        # that skipped startup): resolve it now, without side effects.
        from ..storage.db.paths import resolve_active_database

        try:
            binding = resolve_active_database()
        except Exception:  # noqa: BLE001 — health never fails on a diagnostic
            binding = None
    return HealthResponse(
        # Still "ok": this route answering at all is the liveness signal, and the
        # tray/shell treat any response as running. A database verdict is a
        # separate axis — see `db_ok` — not a reason to call the node down.
        status="ok",
        time=time(),
        cloud_connected=cloud_connected,
        control_plane_connection=cp_status,
        sync_connection=sync_status,
        db_ok=db_ok,
        db_error=db_error,
        database_path=str(binding.path) if binding is not None else None,
        database_source=binding.source if binding is not None else None,
        active_profile_id=binding.profile_id if binding is not None else None,
    )


@router.get("/version")
async def get_version() -> dict:
    return {
        "version": __version__,
        "name": "Topos Control Plane",
    }


@router.get("/")
async def root() -> dict:
    return {"status": "ok"}
