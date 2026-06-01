"""Phase B authoritative data-plane commit API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import require_api_key
from ..core.state import get_db_connection

router = APIRouter(tags=["data-commit"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_commit_artifacts (
            commit_id TEXT PRIMARY KEY,
            compute_task_id TEXT NOT NULL,
            topos_id TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            artifact_schema_version TEXT NOT NULL,
            artifact_json TEXT NOT NULL,
            idempotency_key TEXT UNIQUE,
            correlation_id TEXT,
            requested_by TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_commit_audit (
            audit_id TEXT PRIMARY KEY,
            commit_id TEXT,
            compute_task_id TEXT NOT NULL,
            idempotency_key TEXT,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


class CommitArtifactRequest(BaseModel):
    compute_task_id: str = Field(..., min_length=1)
    topos_id: str = Field(..., min_length=1)
    resource_id: str = Field(..., min_length=1)
    artifact: Dict[str, Any] = Field(default_factory=dict)
    artifact_schema_version: str = Field(default="phase_b_v1", min_length=1)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=200)
    correlation_id: Optional[str] = Field(default=None, min_length=8, max_length=200)
    requested_by: Optional[str] = Field(default=None, min_length=1, max_length=200)


@router.post("/v1/data/commit-artifact", dependencies=[Depends(require_api_key)])
async def commit_artifact(body: CommitArtifactRequest) -> Dict[str, Any]:
    if not isinstance(body.artifact, dict) or not body.artifact:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="artifact must be a non-empty object")
    conn = get_db_connection()
    if conn is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    _ensure_tables(conn)
    commit_id = f"commit_{uuid.uuid4().hex}"
    now = _now_iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if body.idempotency_key:
            existing = conn.execute(
                """
                SELECT commit_id, correlation_id FROM data_commit_artifacts
                WHERE idempotency_key = ?
                LIMIT 1
                """,
                (body.idempotency_key,),
            ).fetchone()
            if existing:
                existing_commit_id = str(existing["commit_id"])
                conn.execute(
                    """
                    INSERT INTO data_commit_audit
                    (audit_id, commit_id, compute_task_id, idempotency_key, outcome, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"dca_{uuid.uuid4().hex}",
                        existing_commit_id,
                        body.compute_task_id,
                        body.idempotency_key,
                        "idempotent_replay",
                        now,
                    ),
                )
                conn.commit()
                return {
                    "commit_id": existing_commit_id,
                    "deduped": True,
                    "compute_task_id": body.compute_task_id,
                    "correlation_id": str(existing["correlation_id"] or body.correlation_id or ""),
                }

        conn.execute(
            """
            INSERT INTO data_commit_artifacts
            (commit_id, compute_task_id, topos_id, resource_id, artifact_schema_version, artifact_json, idempotency_key, correlation_id, requested_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                commit_id,
                body.compute_task_id,
                body.topos_id,
                body.resource_id,
                body.artifact_schema_version,
                json.dumps(body.artifact),
                body.idempotency_key,
                body.correlation_id,
                body.requested_by,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO data_commit_audit
            (audit_id, commit_id, compute_task_id, idempotency_key, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"dca_{uuid.uuid4().hex}",
                commit_id,
                body.compute_task_id,
                body.idempotency_key,
                "committed",
                now,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"commit_failed:{exc}",
        ) from exc
    return {
        "commit_id": commit_id,
        "deduped": False,
        "compute_task_id": body.compute_task_id,
        "correlation_id": body.correlation_id,
    }
