from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from ..__version__ import __version__
from ..core.api_models import (
    DeviceInfoResponse,
    DeviceNameResponse,
    PairDeviceResponse,
    PairingCodeResponse,
    StoreMessageResponse,
    SyncDatabaseResponse,
    SyncResponse,
)
from ..core import state
from ..config.settings import settings
from fastapi import HTTPException, status


class LocalDbService:
    async def store_message(self, payload: Dict[str, Any]) -> StoreMessageResponse:
        _ = payload
        raise NotImplementedError("LocalDbService not implemented yet")

    async def get_oplog(self, dataset_id: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        _ = (dataset_id, limit, offset)
        raise NotImplementedError("LocalDbService not implemented yet")

    async def get_messages(self, dataset_id: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        _ = (dataset_id, limit, offset)
        raise NotImplementedError("LocalDbService not implemented yet")

    async def replay_projection(self, dataset_id: Optional[str]) -> Dict[str, Any]:
        _ = dataset_id
        raise NotImplementedError("LocalDbService not implemented yet")

    async def reset_database(self) -> Dict[str, Any]:
        raise NotImplementedError("LocalDbService not implemented yet")

    async def sync_database(self) -> SyncDatabaseResponse:
        raise NotImplementedError("LocalDbService not implemented yet")

    async def backup_database(self, encrypted: bool) -> Any:
        _ = encrypted
        raise NotImplementedError("LocalDbService not implemented yet")

    async def restore_database(self, file, authenticated_user_id: str, encrypted: bool) -> Dict[str, Any]:
        _ = (file, authenticated_user_id, encrypted)
        raise NotImplementedError("LocalDbService not implemented yet")


class LocalSyncService:
    async def trigger_sync(self) -> SyncResponse:
        raise NotImplementedError("LocalSyncService not implemented yet")


class LocalDeviceService:
    async def get_pairing_code(self) -> PairingCodeResponse:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Pairing not implemented")

    async def pair_device(self, pairing_code: str, keep_existing_data: bool) -> PairDeviceResponse:
        _ = (pairing_code, keep_existing_data)
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Pairing not implemented")

    async def get_device_info(self) -> DeviceInfoResponse:
        # Get user_id from database (set by connection_info handler) or fall back to settings
        user_id = None
        if state.db_conn:
            from ..core.state import get_user_id
            user_id = get_user_id(state.db_conn)
        if not user_id:
            user_id = settings.topos_user_id
        dataset_id = f"{user_id}:{settings.topos_default_dataset_id}" if user_id else None
        sync_connected = state.sync_client.is_connected() if state.sync_client else False
        sync_enabled = settings.enable_sync and settings.get_sync_url() is not None

        last_sync_at = None
        last_received_hlc_ts = None
        last_received_op_id = None
        if state.db_conn:
            last_sync_at = state.get_engine_config_value(state.db_conn, "last_sync_at")
            last_received_hlc_ts = state.get_engine_config_value(state.db_conn, "last_received_hlc_ts")
            last_received_op_id = state.get_engine_config_value(state.db_conn, "last_received_op_id")

        device_name = settings.engine_name or state.get_system_info().get("hostname")

        return DeviceInfoResponse(
            user_id=user_id,
            dataset_id=dataset_id,
            sync_connected=sync_connected,
            sync_enabled=sync_enabled,
            engine_class=state.get_engine_class(),
            engine_mode=state.get_engine_mode(),
            llm_enabled=settings.enable_llm and state.get_engine_mode() == "full",
            database_mode=settings.topos_database_mode,
            database_version=sqlite3.sqlite_version if settings.topos_database_mode in {"local", "sqlite"} else None,
            engine_name=device_name,
            engine_version=__version__,
            system=state.get_system_info(),
            last_sync_at=last_sync_at,
            last_received_hlc_ts=last_received_hlc_ts,
            last_received_op_id=last_received_op_id,
            oplog_count=None,
            oplog_bytes=None,
            ops_since_last_sync=None,
            oplog_bytes_since_last_sync=None,
        )

    async def set_device_name(self, device_name: str) -> DeviceNameResponse:
        name = device_name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device name cannot be empty")
        if len(name) > 64:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device name cannot exceed 64 characters")

        if state.db_conn:
            state.set_engine_config_value(state.db_conn, "device_name", name)

        return DeviceNameResponse(status="ok", device_name=name)


class LocalLLMService:
    async def generate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        _ = payload
        raise NotImplementedError("LocalLLMService not implemented yet")
