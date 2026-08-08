"""Phase B remote compute transport endpoint."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..engine.engine import Engine
from ..engine.tasks import ModelRequest, ProcessingTask
from ..storage.db.write_gate import commit_connection, with_db_write

router = APIRouter(tags=["compute-remote"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(conn) -> None:
    # DDL takes SQLite's write lock at execute time — gate it with the commit
    # (write_gate lock-order inversion).
    with with_db_write():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_compute_results (
                idempotency_key TEXT PRIMARY KEY,
                compute_task_id TEXT NOT NULL,
                response_json TEXT NOT NULL,
                requested_tier TEXT NOT NULL,
                correlation_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_compute_audit (
                audit_id TEXT PRIMARY KEY,
                compute_task_id TEXT NOT NULL,
                idempotency_key TEXT,
                principal TEXT NOT NULL,
                requested_tier TEXT NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        commit_connection(conn)


class RemoteComputeTaskRequest(BaseModel):
    compute_task_id: str = Field(..., min_length=1)
    topos_id: str = Field(..., min_length=1)
    resource_id: str = Field(..., min_length=1)
    operation: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=200)
    correlation_id: Optional[str] = Field(default=None, min_length=8, max_length=200)
    requested_tier: Literal["basic", "pro", "premium"] = "basic"


@router.post("/v1/compute/run", dependencies=[Depends(require_api_key)])
async def remote_compute_run(body: RemoteComputeTaskRequest) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    _ensure_tables(conn)
    principal = "control-plane"
    if body.idempotency_key:
        row = conn.execute(
            "SELECT response_json FROM remote_compute_results WHERE idempotency_key = ?",
            (body.idempotency_key,),
        ).fetchone()
        if row:
            cached = json.loads(str(row["response_json"]))
            with with_db_write():
                conn.execute(
                    """
                    INSERT INTO remote_compute_audit
                    (audit_id, compute_task_id, idempotency_key, principal, requested_tier, outcome, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"rca_{uuid.uuid4().hex}",
                        body.compute_task_id,
                        body.idempotency_key,
                        principal,
                        body.requested_tier,
                        "idempotent_replay",
                        _now_iso(),
                    ),
                )
                commit_connection(conn)
            cached["idempotent_replay"] = True
            return cached

    try:
        if body.operation == "healthcheck":
            response = {
                "compute_task_id": body.compute_task_id,
                "status": "completed",
                "result": {"status": "ok"},
                "error_class": None,
                "requested_tier": body.requested_tier,
                "correlation_id": body.correlation_id,
                "idempotent_replay": False,
            }
        else:
            task = ProcessingTask(
                id=body.compute_task_id,
                type=body.operation,
                input=body.payload,
                model_request=ModelRequest(
                    provider=str(body.payload.get("provider") or "huggingface"),
                    model=str(body.payload.get("model") or "") or None,
                ),
            )
            result = Engine().run(task)
            response = {
                "compute_task_id": body.compute_task_id,
                "status": result.status,
                "result": result.model_dump(mode="json"),
                "error_class": None if result.status == "completed" else "runtime_failure",
                "requested_tier": body.requested_tier,
                "correlation_id": body.correlation_id,
                "idempotent_replay": False,
            }
    except Exception as exc:  # noqa: BLE001
        response = {
            "compute_task_id": body.compute_task_id,
            "status": "failed",
            "result": {},
            "error_class": "runtime_failure",
            "error_message": str(exc),
            "requested_tier": body.requested_tier,
            "correlation_id": body.correlation_id,
            "idempotent_replay": False,
        }

    with with_db_write():
        if body.idempotency_key:
            conn.execute(
                """
                INSERT OR REPLACE INTO remote_compute_results
                (idempotency_key, compute_task_id, response_json, requested_tier, correlation_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    body.idempotency_key,
                    body.compute_task_id,
                    json.dumps(response),
                    body.requested_tier,
                    body.correlation_id,
                    _now_iso(),
                ),
            )
        conn.execute(
            """
            INSERT INTO remote_compute_audit
            (audit_id, compute_task_id, idempotency_key, principal, requested_tier, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rca_{uuid.uuid4().hex}",
                body.compute_task_id,
                body.idempotency_key,
                principal,
                body.requested_tier,
                str(response.get("status") or "unknown"),
                _now_iso(),
            ),
        )
        commit_connection(conn)
    return response
