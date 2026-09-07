from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from ..storage.db.paths import sqlite_on_disk_size_bytes
from ..storage.db.storage_breakdown import compute_local_storage_breakdown
from fastapi import HTTPException, status


def _upgrade_summary_from_runner(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Compact upgrade fields for device_info (fleet nudge / consent UX)."""
    try:
        from ..upgrades.runner import runner_status

        status_payload = runner_status(conn)
    except Exception:
        return {
            "upgrade_baseline": None,
            "pending_upgrade_steps": None,
            "pending_consent_steps": None,
        }
    pending_consent = status_payload.get("pending_consent_steps") or []
    compact_consent: List[Any] = []
    for step in pending_consent:
        if isinstance(step, dict):
            compact_consent.append(
                {
                    "id": step.get("id"),
                    "title": step.get("title"),
                    "cost": step.get("cost") or "slow",
                }
            )
        elif step:
            compact_consent.append(str(step))
    return {
        "upgrade_baseline": status_payload.get("baseline"),
        "pending_upgrade_steps": list(status_payload.get("pending_steps") or []),
        "pending_consent_steps": compact_consent,
    }


#: ``dbstat`` plus a raw-file walk is seconds on a real Topos. The macOS
#: shell used to ask ``/device_info`` on every 5s health poll, on the event
#: loop, which is what made ``/healthcheck`` miss a 3s idle timeout.
_STORAGE_SNAPSHOT_TTL_S = 60.0
_storage_snapshot_cache: Optional[Tuple[float, str, Optional[int], Optional[Dict[str, Any]]]] = None


def _compute_storage_snapshot(db_path: Path) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """On-disk size + category breakdown. Own connection, never the loop handle."""
    size = sqlite_on_disk_size_bytes(db_path)
    breakdown = None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            breakdown = compute_local_storage_breakdown(conn, db_path)
        finally:
            conn.close()
    except Exception:
        breakdown = None
    if breakdown is not None:
        size = int(breakdown.get("total_bytes") or size or 0)
    return size, breakdown


def _cached_storage_snapshot(db_path: Path) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    """Reuse a fresh snapshot so a 5s poller does not rescan ``dbstat``."""
    global _storage_snapshot_cache
    now = time.monotonic()
    key = str(db_path)
    cached = _storage_snapshot_cache
    if cached is not None and cached[1] == key and (now - cached[0]) < _STORAGE_SNAPSHOT_TTL_S:
        return cached[2], cached[3]
    size, breakdown = _compute_storage_snapshot(db_path)
    _storage_snapshot_cache = (now, key, size, breakdown)
    return size, breakdown


def _resolve_device_database_path() -> Optional[Path]:
    """Return the SQLite file the running node is actually using."""
    if state.db_conn:
        try:
            row = state.db_conn.execute("PRAGMA database_list").fetchone()
            if row is not None:
                db_file = str(row[2] or "").strip()
                if db_file and db_file not in {"", ":memory:"}:
                    return Path(db_file)
        except Exception:
            pass
    return state._resolve_database_path_from_settings()


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

    async def get_device_info(self, context: Optional[Dict[str, Any]] = None) -> DeviceInfoResponse:
        _ = context
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
        database_size_bytes = None
        storage_breakdown = None
        if settings.topos_database_mode in {"local", "sqlite"}:
            db_path = _resolve_device_database_path()
            if db_path is not None:
                # dbstat + a raw-file walk on the event-loop handle stalled
                # /healthcheck (2026-09-04 tray flicker). Own connection, off
                # the loop; cache so a 5s poller does not rescan the whole DB.
                database_size_bytes, storage_breakdown = await asyncio.to_thread(
                    _cached_storage_snapshot, db_path
                )

        upgrade_fields: Dict[str, Any] = {
            "upgrade_baseline": None,
            "pending_upgrade_steps": None,
            "pending_consent_steps": None,
        }
        if state.db_conn is not None:
            upgrade_fields = _upgrade_summary_from_runner(state.db_conn)

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
            database_size_bytes=database_size_bytes,
            storage_breakdown=storage_breakdown,
            upgrade_baseline=upgrade_fields.get("upgrade_baseline"),
            pending_upgrade_steps=upgrade_fields.get("pending_upgrade_steps"),
            pending_consent_steps=upgrade_fields.get("pending_consent_steps"),
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
