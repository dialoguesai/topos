"""Pipeline stage audit storage (Phase 1)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .stages import PipelineStage
from ..storage.db.write_gate import commit_connection, with_db_write


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StageAuditRow:
    sync_batch_id: str
    source_id: str
    stage: PipelineStage
    status: str
    job_id: Optional[str] = None
    records_in: int = 0
    records_out: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class IngestAuditStore:
    def append_stage(self, row: StageAuditRow) -> int:
        raise NotImplementedError

    def query_by_batch(self, sync_batch_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class SQLiteIngestAuditStore(IngestAuditStore):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        from ..storage.db.migrations import ensure_migrations_applied

        ensure_migrations_applied(conn)

    def append_stage(self, row: StageAuditRow) -> int:
        with with_db_write():
            cur = self._conn.execute(
                """
                INSERT INTO ingest_audit (
                    job_id, sync_batch_id, source_id, stage, status,
                    started_at, finished_at, records_in, records_out, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.job_id,
                    row.sync_batch_id,
                    row.source_id,
                    row.stage.value,
                    row.status,
                    row.started_at or _utc_now(),
                    row.finished_at,
                    row.records_in,
                    row.records_out,
                    row.error,
                ),
            )
            commit_connection(self._conn)
        return int(cur.lastrowid or 0)

    def query_by_batch(self, sync_batch_id: str) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT job_id, sync_batch_id, source_id, stage, status,
                   started_at, finished_at, records_in, records_out, error
            FROM ingest_audit WHERE sync_batch_id=? ORDER BY audit_id
            """,
            (sync_batch_id,),
        ).fetchall()
        return [
            {
                "job_id": r[0],
                "sync_batch_id": r[1],
                "source_id": r[2],
                "stage": r[3],
                "status": r[4],
                "started_at": r[5],
                "finished_at": r[6],
                "records_in": r[7],
                "records_out": r[8],
                "error": r[9],
            }
            for r in rows
        ]


class InMemoryIngestAuditStore(IngestAuditStore):
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def append_stage(self, row: StageAuditRow) -> int:
        payload = {
            "job_id": row.job_id,
            "sync_batch_id": row.sync_batch_id,
            "source_id": row.source_id,
            "stage": row.stage.value,
            "status": row.status,
            "started_at": row.started_at or _utc_now(),
            "finished_at": row.finished_at,
            "records_in": row.records_in,
            "records_out": row.records_out,
            "error": row.error,
        }
        self.rows.append(payload)
        return len(self.rows)

    def query_by_batch(self, sync_batch_id: str) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r["sync_batch_id"] == sync_batch_id]


@contextmanager
def stage_context(
    audit: IngestAuditStore,
    *,
    stage: PipelineStage,
    sync_batch_id: str,
    source_id: str,
    job_id: Optional[str] = None,
    records_in: int = 0,
) -> Iterator[Dict[str, int]]:
    """Track stage lifecycle; mutate yielded dict['records_out'] before exit."""
    outcome: Dict[str, int] = {"records_out": 0}
    audit.append_stage(
        StageAuditRow(
            sync_batch_id=sync_batch_id,
            source_id=source_id,
            stage=stage,
            status="running",
            job_id=job_id,
            records_in=records_in,
        )
    )
    try:
        yield outcome
        audit.append_stage(
            StageAuditRow(
                sync_batch_id=sync_batch_id,
                source_id=source_id,
                stage=stage,
                status="completed",
                job_id=job_id,
                records_in=records_in,
                records_out=outcome["records_out"],
                finished_at=_utc_now(),
            )
        )
    except Exception as exc:
        audit.append_stage(
            StageAuditRow(
                sync_batch_id=sync_batch_id,
                source_id=source_id,
                stage=stage,
                status="failed",
                job_id=job_id,
                records_in=records_in,
                records_out=outcome["records_out"],
                error=str(exc),
                finished_at=_utc_now(),
            )
        )
        raise
