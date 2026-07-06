"""Persistence helpers for the Enrichment Lab."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .schema import ensure_enrichment_lab_schema


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(conn: sqlite3.Connection) -> None:
    ensure_enrichment_lab_schema(conn)


def insert_group(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    dataset_kind: str,
    models: List[str],
    record_inputs: Dict[str, Dict[str, Any]],
    bundle_id: Optional[str] = None,
    bundle_version: Optional[str] = None,
    source_id: Optional[str] = None,
    sample_limit: Optional[int] = None,
    options: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert a group and one run per (model, record). Inputs are materialized
    at creation time so node-sampled dry-runs are stable and reproducible."""
    ensure_schema(conn)
    gid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO enrichment_lab_job_group (
            id, created_at, job_id, dataset_kind, bundle_id, bundle_version,
            source_id, sample_limit, status, models_json, options_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gid,
            utc_now_iso(),
            job_id,
            dataset_kind,
            bundle_id,
            bundle_version,
            source_id,
            sample_limit,
            "pending",
            json.dumps(models),
            json.dumps(options or {}),
        ),
    )
    for model_tag in models:
        for rid, record in record_inputs.items():
            conn.execute(
                """
                INSERT INTO enrichment_lab_run (
                    id, group_id, model_tag, record_id, status, input_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), gid, model_tag, rid, "queued", json.dumps(record)),
            )
    conn.commit()
    return gid


def get_group(conn: sqlite3.Connection, group_id: str) -> Optional[sqlite3.Row]:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM enrichment_lab_job_group WHERE id = ?", (group_id,)
    ).fetchone()


def list_runs(conn: sqlite3.Connection, group_id: str) -> List[sqlite3.Row]:
    ensure_schema(conn)
    return list(
        conn.execute(
            "SELECT * FROM enrichment_lab_run WHERE group_id = ? ORDER BY model_tag, record_id",
            (group_id,),
        ).fetchall()
    )


def update_group_status(conn: sqlite3.Connection, group_id: str, status: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE enrichment_lab_job_group SET status = ? WHERE id = ?", (status, group_id)
    )
    conn.commit()


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    **fields: Any,
) -> None:
    allowed = {
        "status",
        "started_at",
        "finished_at",
        "latency_ms",
        "error_code",
        "output_json",
        "metrics_json",
    }
    sets: List[str] = []
    vals: List[Any] = []
    for key, value in fields.items():
        if key in allowed and value is not None:
            sets.append(f"{key} = ?")
            vals.append(value)
    if not sets:
        return
    vals.append(run_id)
    conn.execute(f"UPDATE enrichment_lab_run SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()


def patch_group(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    preferred_model_tag: Any = None,
    group_notes: Any = None,
    notes: Any = None,
) -> None:
    ensure_schema(conn)
    fields: List[str] = []
    vals: List[Any] = []
    if preferred_model_tag is not None:
        fields.append("preferred_model_tag = ?")
        vals.append(preferred_model_tag)
    if group_notes is not None:
        fields.append("group_notes = ?")
        vals.append(group_notes)
    if notes is not None:
        fields.append("notes = ?")
        vals.append(notes)
    if not fields:
        return
    vals.append(group_id)
    conn.execute(
        f"UPDATE enrichment_lab_job_group SET {', '.join(fields)} WHERE id = ?", vals
    )
    conn.commit()


def patch_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    user_quality_score_0_10: Any = None,
    user_liked: Any = None,
    user_note: Any = None,
) -> None:
    ensure_schema(conn)
    fields: List[str] = []
    vals: List[Any] = []
    if user_quality_score_0_10 is not None:
        fields.append("user_quality_score_0_10 = ?")
        vals.append(user_quality_score_0_10)
    if user_liked is not None:
        fields.append("user_liked = ?")
        vals.append(1 if user_liked is True else 0)
    if user_note is not None:
        fields.append("user_note = ?")
        vals.append(user_note)
    if not fields:
        return
    fields.append("rated_at = ?")
    vals.append(utc_now_iso())
    vals.append(run_id)
    conn.execute(f"UPDATE enrichment_lab_run SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()


def list_groups(
    conn: sqlite3.Connection,
    *,
    job_id: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> List[sqlite3.Row]:
    ensure_schema(conn)
    if job_id:
        return list(
            conn.execute(
                """
                SELECT * FROM enrichment_lab_job_group WHERE job_id = ?
                ORDER BY created_at DESC LIMIT ? OFFSET ?
                """,
                (job_id, limit, offset),
            ).fetchall()
        )
    return list(
        conn.execute(
            "SELECT * FROM enrichment_lab_job_group ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    )


def history_summaries_for_group_ids(
    conn: sqlite3.Connection, group_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    ensure_schema(conn)
    if not group_ids:
        return {}
    placeholders = ",".join("?" * len(group_ids))
    rows = conn.execute(
        f"SELECT * FROM enrichment_lab_run WHERE group_id IN ({placeholders})",
        group_ids,
    ).fetchall()
    by_gid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        d = dict(row)
        by_gid[str(d.get("group_id") or "")].append(d)
    out: Dict[str, Dict[str, Any]] = {}
    for gid in group_ids:
        runs = by_gid.get(str(gid), [])
        models = sorted({str(r.get("model_tag") or "") for r in runs if r.get("model_tag")})
        latencies = [
            int(r["latency_ms"])
            for r in runs
            if r.get("status") == "succeeded" and r.get("latency_ms") is not None
        ]
        out[str(gid)] = {
            "models": ", ".join(models),
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "any_liked": any(r.get("user_liked") == 1 for r in runs),
            "run_count": len(runs),
            "succeeded": sum(1 for r in runs if r.get("status") == "succeeded"),
            "failed": sum(1 for r in runs if r.get("status") == "failed"),
        }
    return out


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    ensure_schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM enrichment_lab_run WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM enrichment_lab_job_group WHERE id = ?", (group_id,))
    conn.commit()


def prune_old_groups(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    ensure_schema(conn)
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM enrichment_lab_job_group WHERE created_at < ?", (cutoff_iso,)
        ).fetchall()
    ]
    for gid in ids:
        delete_group(conn, gid)
    return len(ids)
