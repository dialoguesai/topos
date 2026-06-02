from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import HTTPException, status

from ..__version__ import __version__
from ..config.settings import settings
from ..core import state
from ..core.api_models import (
    DeviceInfoResponse,
    DeviceNameResponse,
    PairDeviceResponse,
    PairingCodeResponse,
    StoreMessageResponse,
    SyncDatabaseResponse,
    SyncResponse,
)
from ..storage.db.postgres import (
    PostgresConfigurationError,
    connect_postgres,
    execute_query,
    fetch_all,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _tenant_from_dataset(dataset_id: Optional[str]) -> str:
    ds = (dataset_id or "").strip()
    if not ds:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "dataset_required", "dataset_id is required")
    if ":" in ds:
        tenant, _ = ds.split(":", 1)
        tenant = tenant.strip()
    else:
        tenant = ds
    if not tenant:
        raise _http_error(status.HTTP_400_BAD_REQUEST, "tenant_required", "dataset_id must include tenant scope")
    return tenant


def _assert_authenticated_tenant(tenant_id: str) -> None:
    expected = (settings.topos_user_id or "").strip()
    if not expected:
        return
    if tenant_id != expected:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "tenant_access_denied",
            "Requested dataset tenant does not match authenticated tenant",
        )


class PostgresDbService:
    async def store_message(self, payload: Dict[str, Any]) -> StoreMessageResponse:
        dataset_id = (payload.get("dataset_id") or "").strip()
        tenant_id = _tenant_from_dataset(dataset_id)
        _assert_authenticated_tenant(tenant_id)

        sender_type = (payload.get("sender_type") or "").strip()
        if not sender_type:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "sender_type_required", "sender_type is required")
        content = (payload.get("content") or "").strip()
        if not content:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "content_required", "content is required")

        message_id = (payload.get("message_id") or "").strip() or str(uuid4())
        op_id = str(uuid4())
        ts = (payload.get("ts") or "").strip() or _utc_now_iso()
        user_id = (payload.get("user_id") or "").strip() or None

        try:
            with connect_postgres() as conn:
                execute_query(
                    conn,
                    """
                    INSERT INTO messages (tenant_id, dataset_id, message_id, sender_type, content, ts, user_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, dataset_id, message_id, sender_type, content, ts, user_id),
                )
                execute_query(
                    conn,
                    """
                    INSERT INTO oplog (tenant_id, dataset_id, op_id, op_type, payload_json, hlc_ts)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tenant_id,
                        dataset_id,
                        op_id,
                        "store_message",
                        json.dumps(
                            {
                                "message_id": message_id,
                                "sender_type": sender_type,
                                "content": content,
                                "ts": ts,
                            }
                        ),
                        ts,
                    ),
                )
        except HTTPException:
            raise
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_write_failed", str(exc)) from exc

        return StoreMessageResponse(op_id=op_id, message_id=message_id, status="ok")

    async def get_oplog(self, dataset_id: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        tenant_id = _tenant_from_dataset(dataset_id)
        _assert_authenticated_tenant(tenant_id)
        page_limit = max(1, min(int(limit), 1000))
        page_offset = max(0, int(offset))
        try:
            with connect_postgres() as conn:
                rows = fetch_all(
                    conn,
                    """
                    SELECT op_id, op_type, payload_json, hlc_ts, dataset_id
                    FROM oplog
                    WHERE tenant_id = %s AND dataset_id = %s
                    ORDER BY hlc_ts DESC
                    LIMIT %s OFFSET %s
                    """,
                    (tenant_id, dataset_id, page_limit, page_offset),
                )
        except HTTPException:
            raise
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_read_failed", str(exc)) from exc

        items = []
        for row in rows:
            items.append(
                {
                    "op_id": row[0],
                    "op_type": row[1],
                    "payload": json.loads(row[2]) if row[2] else {},
                    "hlc_ts": row[3],
                    "dataset_id": row[4],
                }
            )
        return {"status": "ok", "dataset_id": dataset_id, "items": items, "limit": page_limit, "offset": page_offset}

    async def get_messages(self, dataset_id: Optional[str], limit: int, offset: int) -> Dict[str, Any]:
        tenant_id = _tenant_from_dataset(dataset_id)
        _assert_authenticated_tenant(tenant_id)
        page_limit = max(1, min(int(limit), 1000))
        page_offset = max(0, int(offset))
        try:
            with connect_postgres() as conn:
                rows = fetch_all(
                    conn,
                    """
                    SELECT message_id, sender_type, content, ts, user_id, dataset_id
                    FROM messages
                    WHERE tenant_id = %s AND dataset_id = %s
                    ORDER BY ts DESC
                    LIMIT %s OFFSET %s
                    """,
                    (tenant_id, dataset_id, page_limit, page_offset),
                )
        except HTTPException:
            raise
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_read_failed", str(exc)) from exc

        items = []
        for row in rows:
            items.append(
                {
                    "message_id": row[0],
                    "sender_type": row[1],
                    "content": row[2],
                    "ts": row[3],
                    "user_id": row[4],
                    "dataset_id": row[5],
                }
            )
        return {"status": "ok", "dataset_id": dataset_id, "messages": items, "limit": page_limit, "offset": page_offset}

    async def replay_projection(self, dataset_id: Optional[str]) -> Dict[str, Any]:
        tenant_id = _tenant_from_dataset(dataset_id)
        _assert_authenticated_tenant(tenant_id)
        try:
            with connect_postgres() as conn:
                rows = fetch_all(
                    conn,
                    """
                    SELECT COUNT(*) FROM messages
                    WHERE tenant_id = %s AND dataset_id = %s
                    """,
                    (tenant_id, dataset_id),
                )
        except HTTPException:
            raise
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_replay_failed", str(exc)) from exc
        replayed = int(rows[0][0]) if rows else 0
        return {"status": "ok", "dataset_id": dataset_id, "replayed_messages": replayed}

    async def reset_database(self) -> Dict[str, Any]:
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "reset_forbidden",
            "Hosted mode database reset is not allowed without tenant-scoped maintenance flow",
        )

    async def sync_database(self) -> SyncDatabaseResponse:
        return SyncDatabaseResponse(
            status="ok",
            message="Hosted mode uses server-backed Postgres and does not require local sync export",
        )

    async def backup_database(self, encrypted: bool) -> Any:
        _ = encrypted
        try:
            with connect_postgres() as conn:
                messages = fetch_all(
                    conn,
                    """
                    SELECT tenant_id, dataset_id, message_id, sender_type, content, ts, user_id
                    FROM messages
                    ORDER BY ts ASC
                    """,
                )
                oplog = fetch_all(
                    conn,
                    """
                    SELECT tenant_id, dataset_id, op_id, op_type, payload_json, hlc_ts
                    FROM oplog
                    ORDER BY hlc_ts ASC
                    """,
                )
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_backup_failed", str(exc)) from exc

        payload = {
            "messages": [
                {
                    "tenant_id": row[0],
                    "dataset_id": row[1],
                    "message_id": row[2],
                    "sender_type": row[3],
                    "content": row[4],
                    "ts": row[5],
                    "user_id": row[6],
                }
                for row in messages
            ],
            "oplog": [
                {
                    "tenant_id": row[0],
                    "dataset_id": row[1],
                    "op_id": row[2],
                    "op_type": row[3],
                    "payload_json": row[4],
                    "hlc_ts": row[5],
                }
                for row in oplog
            ],
        }
        return json.dumps(payload).encode("utf-8")

    async def restore_database(self, file, authenticated_user_id: str, encrypted: bool) -> Dict[str, Any]:
        _ = encrypted
        tenant_id = (authenticated_user_id or "").strip()
        if not tenant_id:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "authenticated_user_required", "authenticated_user_id is required")
        _assert_authenticated_tenant(tenant_id)

        if hasattr(file, "read"):
            raw = await file.read()
        elif isinstance(file, (bytes, bytearray)):
            raw = bytes(file)
        else:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_backup_file", "Backup payload must be bytes or readable file")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise _http_error(status.HTTP_400_BAD_REQUEST, "invalid_backup_payload", "Backup payload must be valid UTF-8 JSON") from exc

        restored_messages = 0
        restored_ops = 0
        try:
            with connect_postgres() as conn:
                execute_query(conn, "DELETE FROM messages WHERE tenant_id = %s", (tenant_id,))
                execute_query(conn, "DELETE FROM oplog WHERE tenant_id = %s", (tenant_id,))
                for row in payload.get("messages") or []:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("tenant_id") or "").strip() != tenant_id:
                        continue
                    execute_query(
                        conn,
                        """
                        INSERT INTO messages (tenant_id, dataset_id, message_id, sender_type, content, ts, user_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            row.get("dataset_id"),
                            row.get("message_id"),
                            row.get("sender_type"),
                            row.get("content"),
                            row.get("ts") or _utc_now_iso(),
                            row.get("user_id"),
                        ),
                    )
                    restored_messages += 1
                for row in payload.get("oplog") or []:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("tenant_id") or "").strip() != tenant_id:
                        continue
                    execute_query(
                        conn,
                        """
                        INSERT INTO oplog (tenant_id, dataset_id, op_id, op_type, payload_json, hlc_ts)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tenant_id,
                            row.get("dataset_id"),
                            row.get("op_id") or str(uuid4()),
                            row.get("op_type") or "restore_replay",
                            row.get("payload_json") or "{}",
                            row.get("hlc_ts") or _utc_now_iso(),
                        ),
                    )
                    restored_ops += 1
        except HTTPException:
            raise
        except PostgresConfigurationError as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_not_configured", str(exc)) from exc
        except Exception as exc:
            raise _http_error(status.HTTP_503_SERVICE_UNAVAILABLE, "postgres_restore_failed", str(exc)) from exc

        return {
            "status": "ok",
            "tenant_id": tenant_id,
            "restored_messages": restored_messages,
            "restored_oplog_entries": restored_ops,
        }


def _hosted_dataset_id(context: Optional[Dict[str, Any]], user_id: Optional[str]) -> Optional[str]:
    ctx = context if isinstance(context, dict) else {}
    explicit = str(ctx.get("dataset_id") or "").strip()
    if explicit:
        return explicit
    if not user_id:
        return None
    tenant_id = str(ctx.get("tenant_id") or "").strip()
    if tenant_id:
        return f"{user_id}:default:{tenant_id}"
    default_dataset = (settings.topos_default_dataset_id or "default").strip() or "default"
    return f"{user_id}:{default_dataset}"


class HostedDeviceService:
    async def get_pairing_code(self) -> PairingCodeResponse:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pairing is not available for hosted runtimes",
        )

    async def pair_device(self, pairing_code: str, keep_existing_data: bool) -> PairDeviceResponse:
        _ = (pairing_code, keep_existing_data)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Pairing is not available for hosted runtimes",
        )

    async def get_device_info(self, context: Optional[Dict[str, Any]] = None) -> DeviceInfoResponse:
        ctx = context if isinstance(context, dict) else {}
        user_id = str(ctx.get("owner_user_id") or settings.topos_user_id or "").strip() or None
        dataset_id = _hosted_dataset_id(ctx, user_id)
        engine_mode = state.get_engine_mode()
        device_name = (settings.engine_name or "").strip() or None

        return DeviceInfoResponse(
            user_id=user_id,
            dataset_id=dataset_id,
            sync_connected=False,
            sync_enabled=False,
            engine_class=state.get_engine_class(),
            engine_mode=engine_mode,
            llm_enabled=bool(settings.enable_llm and engine_mode == "full"),
            database_mode=settings.topos_database_mode or "postgres",
            database_version=None,
            engine_name=device_name,
            engine_version=__version__,
            system={},
            last_sync_at=None,
            last_received_hlc_ts=None,
            last_received_op_id=None,
            oplog_count=None,
            oplog_bytes=None,
            ops_since_last_sync=None,
            oplog_bytes_since_last_sync=None,
        )

    async def set_device_name(self, device_name: str) -> DeviceNameResponse:
        _ = device_name
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Device rename is not available for hosted runtimes",
        )


class HostedSyncService:
    async def trigger_sync(self) -> SyncResponse:
        raise NotImplementedError("HostedSyncService not implemented yet")
