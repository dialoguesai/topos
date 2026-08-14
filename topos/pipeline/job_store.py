"""SQLite-backed durable pipeline job store."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..storage.db.write_gate import begin_immediate, commit_connection, sqlite_retry_busy, with_db_write

JOB_STATUSES = frozenset({"queued", "running", "done", "failed"})
DEFAULT_LEASE_SECONDS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_pipeline_jobs_schema(conn: sqlite3.Connection) -> None:
    from ..storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    apply_pipeline_jobs_v1_up(conn)


def enqueue_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: Dict[str, Any],
    job_id: Optional[str] = None,
    source_id: Optional[str] = None,
    write_id: Optional[str] = None,
    sync_batch_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Persist a job before acknowledgement. Returns job_id."""
    ensure_pipeline_jobs_schema(conn)
    jid = str(job_id or uuid.uuid4())
    key = str(idempotency_key or "").strip() or None

    with with_db_write():
        if key:
            existing = conn.execute(
                "SELECT job_id, status FROM pipeline_jobs WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                existing_id, status = str(existing[0]), str(existing[1])
                if status in ("queued", "running"):
                    return existing_id
                if status == "done":
                    return existing_id
                if status == "failed":
                    # Re-queue failed jobs so derivation can retry after lock errors.
                    conn.execute(
                        """
                        UPDATE pipeline_jobs
                        SET status='queued', payload_json=?, source_id=COALESCE(?, source_id),
                            write_id=COALESCE(?, write_id), sync_batch_id=COALESCE(?, sync_batch_id),
                            lease_owner=NULL, lease_expires_at=NULL,
                            started_at=NULL, finished_at=NULL,
                            updated_at=datetime('now')
                        WHERE job_id=?
                        """,
                        (
                            json.dumps(payload),
                            source_id,
                            write_id,
                            sync_batch_id,
                            existing_id,
                        ),
                    )
                    commit_connection(conn)
                    return existing_id

        conn.execute(
            """
            INSERT INTO pipeline_jobs (
                job_id, kind, status, payload_json, source_id, write_id,
                sync_batch_id, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(job_id) DO UPDATE SET
                payload_json=excluded.payload_json,
                updated_at=datetime('now')
            """,
            (
                jid,
                kind,
                json.dumps(payload),
                source_id,
                write_id,
                sync_batch_id,
                key,
            ),
        )
        commit_connection(conn)
    return jid


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    ensure_pipeline_jobs_schema(conn)
    row = conn.execute(
        """
        SELECT job_id, kind, status, lease_owner, lease_expires_at, payload_json,
               progress_json, detail_json, sync_batch_id, source_id, write_id,
               idempotency_key, started_at, finished_at, created_at, updated_at
        FROM pipeline_jobs WHERE job_id=?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def update_job_progress(
    conn: sqlite3.Connection,
    job_id: str,
    progress: Dict[str, Any],
) -> None:
    ensure_pipeline_jobs_schema(conn)
    with with_db_write():
        conn.execute(
            """
            UPDATE pipeline_jobs
            SET progress_json=?, updated_at=datetime('now')
            WHERE job_id=?
            """,
            (json.dumps(progress), job_id),
        )
        commit_connection(conn)


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    lease_owner: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    kinds: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Claim the oldest queued job, or return None."""
    ensure_pipeline_jobs_schema(conn)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()

    with with_db_write():
        begin_immediate(conn)
        try:
            query = "SELECT job_id FROM pipeline_jobs WHERE status='queued'"
            params: list[Any] = []
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                query += f" AND kind IN ({placeholders})"
                params.extend(kinds)
            query += " ORDER BY created_at ASC LIMIT 1"
            row = conn.execute(query, tuple(params)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                return None
            job_id = str(row[0])
            updated = conn.execute(
                """
                UPDATE pipeline_jobs
                SET status='running', lease_owner=?, lease_expires_at=?,
                    started_at=COALESCE(started_at, datetime('now')),
                    updated_at=datetime('now')
                WHERE job_id=? AND status='queued'
                """,
                (lease_owner, expires, job_id),
            )
            if int(updated.rowcount or 0) == 0:
                conn.execute("ROLLBACK")
                return None
            sqlite_retry_busy(conn.commit)
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return get_job(conn, job_id)


def claim_matching_queued_jobs(
    conn: sqlite3.Connection,
    *,
    lease_owner: str,
    kind: str,
    source_id: Optional[str],
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Claim every queued job of ``kind`` for ``source_id``, oldest first.

    Lets the worker coalesce a backlog — e.g. 40 one-record inbox deliveries —
    into a single derive batch so the per-batch jobs (dimension briefs, topic
    work) run once instead of once per delivery.
    """
    ensure_pipeline_jobs_schema(conn)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
    claimed_ids: List[str] = []

    with with_db_write():
        begin_immediate(conn)
        try:
            query = "SELECT job_id FROM pipeline_jobs WHERE status='queued' AND kind=?"
            params: list[Any] = [kind]
            if source_id is None:
                query += " AND source_id IS NULL"
            else:
                query += " AND source_id=?"
                params.append(source_id)
            query += " ORDER BY created_at ASC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(query, tuple(params)).fetchall()
            for row in rows:
                updated = conn.execute(
                    """
                    UPDATE pipeline_jobs
                    SET status='running', lease_owner=?, lease_expires_at=?,
                        started_at=COALESCE(started_at, datetime('now')),
                        updated_at=datetime('now')
                    WHERE job_id=? AND status='queued'
                    """,
                    (lease_owner, expires, str(row[0])),
                )
                if int(updated.rowcount or 0):
                    claimed_ids.append(str(row[0]))
            sqlite_retry_busy(conn.commit)
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return [job for job in (get_job(conn, jid) for jid in claimed_ids) if job is not None]


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    ensure_pipeline_jobs_schema(conn)
    with with_db_write():
        conn.execute(
            """
            UPDATE pipeline_jobs
            SET status='done', finished_at=datetime('now'), updated_at=datetime('now'),
                detail_json=?, lease_owner=NULL, lease_expires_at=NULL
            WHERE job_id=?
            """,
            (json.dumps(detail) if detail else None, job_id),
        )
        commit_connection(conn)


def fail_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    error: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    ensure_pipeline_jobs_schema(conn)
    payload = {"error": error, **(detail or {})}
    with with_db_write():
        conn.execute(
            """
            UPDATE pipeline_jobs
            SET status='failed', finished_at=datetime('now'), updated_at=datetime('now'),
                detail_json=?, lease_owner=NULL, lease_expires_at=NULL
            WHERE job_id=?
            """,
            (json.dumps(payload), job_id),
        )
        commit_connection(conn)


def requeue_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Return a claimed job to the queue without recording an attempt.

    For executors that decline to run right now (e.g. a derivation batch is in
    flight). Unlike ``fail_job`` the row goes back to 'queued', so nothing
    downstream reads a deferral as a failure. Guarded on status='running' so a
    job that was settled out-of-band while claimed (``clear_derivation_retry``
    marks the row 'done' when a later batch succeeds) is not resurrected.
    """
    ensure_pipeline_jobs_schema(conn)
    with with_db_write():
        conn.execute(
            """
            UPDATE pipeline_jobs
            SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                updated_at=datetime('now')
            WHERE job_id=? AND status='running'
            """,
            (job_id,),
        )
        commit_connection(conn)


def requeue_failed_jobs(conn: sqlite3.Connection, job_ids: List[str]) -> int:
    """Return specific FAILED jobs to the queue. Returns rows moved.

    Nothing else in this module moves a row out of 'failed': ``requeue_job``
    only releases a claim the worker still holds, and ``recover_stale_jobs``
    only touches 'running'. So a job that failed for a reason that has since
    gone away — a model that was not installed at the time and is now — stayed
    parked forever on its one spent attempt. Callers decide what "gone away"
    means; this only performs the transition, for ids they name.
    """
    ids = [str(j) for j in (job_ids or []) if j]
    if not ids:
        return 0
    ensure_pipeline_jobs_schema(conn)
    placeholders = ",".join("?" for _ in ids)
    with with_db_write():
        cursor = conn.execute(
            f"""
            UPDATE pipeline_jobs
            SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                finished_at=NULL, updated_at=datetime('now')
            WHERE status='failed' AND job_id IN ({placeholders})
            """,
            ids,
        )
        commit_connection(conn)
    return int(cursor.rowcount or 0)


def recover_stale_jobs(conn: sqlite3.Connection) -> int:
    """Reset expired or orphaned running jobs to queued."""
    ensure_pipeline_jobs_schema(conn)
    now = _now()
    with with_db_write():
        cursor = conn.execute(
            """
            UPDATE pipeline_jobs
            SET status='queued', lease_owner=NULL, lease_expires_at=NULL,
                updated_at=datetime('now')
            WHERE status='running'
              AND (lease_expires_at IS NULL OR lease_expires_at < ?)
            """,
            (now,),
        )
        commit_connection(conn)
    return int(cursor.rowcount or 0)


def record_derivation_completion(
    conn: sqlite3.Connection,
    *,
    write_id: str,
    job_id: str,
    source_id: Optional[str] = None,
    sync_batch_id: Optional[str] = None,
) -> None:
    ensure_pipeline_jobs_schema(conn)
    wid = str(write_id or "").strip()
    if not wid:
        return
    with with_db_write():
        conn.execute(
            """
            INSERT INTO pipeline_derivation_completion (
                write_id, job_id, sync_batch_id, source_id, completed_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(write_id) DO UPDATE SET
                job_id=excluded.job_id,
                sync_batch_id=excluded.sync_batch_id,
                source_id=excluded.source_id,
                completed_at=excluded.completed_at
            """,
            (wid, job_id, sync_batch_id, source_id),
        )
        commit_connection(conn)


def is_derivation_complete(conn: sqlite3.Connection, write_id: str) -> bool:
    ensure_pipeline_jobs_schema(conn)
    wid = str(write_id or "").strip()
    if not wid:
        return False
    row = conn.execute(
        "SELECT 1 FROM pipeline_derivation_completion WHERE write_id=?",
        (wid,),
    ).fetchone()
    return row is not None


def _row_to_dict(row: tuple[Any, ...]) -> Dict[str, Any]:
    def _loads(raw: Any) -> Any:
        if not raw:
            return None
        try:
            return json.loads(str(raw))
        except (TypeError, ValueError):
            return None

    return {
        "job_id": row[0],
        "kind": row[1],
        "status": row[2],
        "lease_owner": row[3],
        "lease_expires_at": row[4],
        "payload": _loads(row[5]),
        "progress": _loads(row[6]),
        "detail": _loads(row[7]),
        "sync_batch_id": row[8],
        "source_id": row[9],
        "write_id": row[10],
        "idempotency_key": row[11],
        "started_at": row[12],
        "finished_at": row[13],
        "created_at": row[14],
        "updated_at": row[15],
    }
