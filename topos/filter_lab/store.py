"""Persistence helpers for Filter Lab."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .schema import ensure_filter_lab_schema


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_iso() -> str:
    """ISO timestamp for run boundaries (public for worker)."""
    return _now()


def ensure_schema(conn: sqlite3.Connection) -> None:
    ensure_filter_lab_schema(conn)


def insert_group(
    conn: sqlite3.Connection,
    *,
    filter_id: str,
    bundle_id: str,
    bundle_version: str,
    baseline_models: List[str],
    models: List[str],
    record_ids: List[str],
    options: Optional[Dict[str, Any]] = None,
) -> str:
    ensure_schema(conn)
    gid = str(uuid.uuid4())
    created = _now()
    conn.execute(
        """
        INSERT INTO filter_lab_job_group (
            id, created_at, filter_id, bundle_id, bundle_version, status,
            baseline_models_json, pulled_models_json, options_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            gid,
            created,
            filter_id,
            bundle_id,
            bundle_version,
            "pending",
            json.dumps(baseline_models),
            json.dumps([]),
            json.dumps(options or {}),
        ),
    )
    for model_tag in models:
        for rid in record_ids:
            run_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO filter_lab_run (
                    id, group_id, model_tag, record_id, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, gid, model_tag, rid, "queued"),
            )
    conn.commit()
    return gid


def get_group(conn: sqlite3.Connection, group_id: str) -> Optional[sqlite3.Row]:
    ensure_schema(conn)
    cur = conn.execute("SELECT * FROM filter_lab_job_group WHERE id = ?", (group_id,))
    return cur.fetchone()


def list_runs(conn: sqlite3.Connection, group_id: str) -> List[sqlite3.Row]:
    ensure_schema(conn)
    cur = conn.execute(
        "SELECT * FROM filter_lab_run WHERE group_id = ? ORDER BY model_tag, record_id",
        (group_id,),
    )
    return list(cur.fetchall())


def update_group_status(conn: sqlite3.Connection, group_id: str, status: str) -> None:
    ensure_schema(conn)
    conn.execute("UPDATE filter_lab_job_group SET status = ? WHERE id = ?", (status, group_id))
    conn.commit()


def set_group_pulled_models(conn: sqlite3.Connection, group_id: str, pulled: List[str]) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE filter_lab_job_group SET pulled_models_json = ? WHERE id = ?",
        (json.dumps(pulled), group_id),
    )
    conn.commit()


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    latency_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    input_hash: Optional[str] = None,
    input_text: Optional[str] = None,
    output_text: Optional[str] = None,
    metrics_json: Optional[str] = None,
) -> None:
    ensure_schema(conn)
    fields: List[str] = []
    vals: List[Any] = []
    if status is not None:
        fields.append("status = ?")
        vals.append(status)
    if started_at is not None:
        fields.append("started_at = ?")
        vals.append(started_at)
    if finished_at is not None:
        fields.append("finished_at = ?")
        vals.append(finished_at)
    if latency_ms is not None:
        fields.append("latency_ms = ?")
        vals.append(latency_ms)
    if error_code is not None:
        fields.append("error_code = ?")
        vals.append(error_code)
    if input_hash is not None:
        fields.append("input_hash = ?")
        vals.append(input_hash)
    if input_text is not None:
        fields.append("input_text = ?")
        vals.append(input_text)
    if output_text is not None:
        fields.append("output_text = ?")
        vals.append(output_text)
    if metrics_json is not None:
        fields.append("metrics_json = ?")
        vals.append(metrics_json)
    if not fields:
        return
    vals.append(run_id)
    conn.execute(f"UPDATE filter_lab_run SET {', '.join(fields)} WHERE id = ?", vals)
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
    row = get_group(conn, group_id)
    if not row:
        return
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
    conn.execute(f"UPDATE filter_lab_job_group SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()


def patch_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    user_quality_score_0_10: Any = None,
    user_liked: Any = None,
    user_note: Any = None,
    rated_at: Any = None,
) -> None:
    ensure_schema(conn)
    fields: List[str] = []
    vals: List[Any] = []
    if user_quality_score_0_10 is not None:
        fields.append("user_quality_score_0_10 = ?")
        vals.append(user_quality_score_0_10)
    if user_liked is not None:
        fields.append("user_liked = ?")
        vals.append(1 if user_liked is True else 0 if user_liked is False else None)
    if user_note is not None:
        fields.append("user_note = ?")
        vals.append(user_note)
    if rated_at is not None:
        fields.append("rated_at = ?")
        vals.append(rated_at)
    if not fields:
        return
    vals.append(run_id)
    conn.execute(f"UPDATE filter_lab_run SET {', '.join(fields)} WHERE id = ?", vals)
    conn.commit()


def list_groups_for_filter(
    conn: sqlite3.Connection, filter_id: str, *, limit: int = 20, offset: int = 0
) -> List[sqlite3.Row]:
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT * FROM filter_lab_job_group
        WHERE filter_id = ?
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (filter_id, limit, offset),
    )
    return list(cur.fetchall())


def list_all_job_groups(
    conn: sqlite3.Connection, *, limit: int = 50, offset: int = 0
) -> List[sqlite3.Row]:
    """Recent job groups across all transforms (newest first)."""
    ensure_schema(conn)
    cur = conn.execute(
        """
        SELECT * FROM filter_lab_job_group
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return list(cur.fetchall())


def _run_row_liked(val: Any) -> bool:
    return val is True or val == 1


def empty_history_summary() -> Dict[str, Any]:
    """Default summary when a group has no runs (should be rare)."""
    return {
        "models": "",
        "avg_latency_ms": None,
        "any_liked": False,
        "rating_text": None,
    }


def _history_summary_from_runs(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate run rows for list/history UI (models, latency, liked, ratings)."""
    if not runs:
        return empty_history_summary()
    models = sorted({str(r.get("model_tag") or "") for r in runs if str(r.get("model_tag") or "").strip()})
    latencies: List[int] = []
    for r in runs:
        if str(r.get("status") or "") != "succeeded":
            continue
        lm = r.get("latency_ms")
        if lm is None:
            continue
        try:
            latencies.append(int(lm))
        except (TypeError, ValueError):
            continue
    avg_lat = round(sum(latencies) / len(latencies)) if latencies else None
    any_liked = any(_run_row_liked(r.get("user_liked")) for r in runs)
    by_model: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in runs:
        mt = str(r.get("model_tag") or "")
        if mt:
            by_model[mt].append(r)
    rating_parts: List[str] = []
    for m in sorted(by_model.keys()):
        scores = [
            x.get("user_quality_score_0_10")
            for x in by_model[m]
            if x.get("user_quality_score_0_10") is not None
        ]
        if not scores:
            continue
        try:
            best = max(int(s) for s in scores if isinstance(s, (int, float)))
        except ValueError:
            continue
        rating_parts.append(f"{m}: {best}/10")
    if not rating_parts:
        rating_text = None
    elif len(models) == 1 and len(rating_parts) == 1:
        rating_text = rating_parts[0].split(": ", 1)[-1]
    else:
        rating_text = " · ".join(rating_parts)
    return {
        "models": ", ".join(models),
        "avg_latency_ms": avg_lat,
        "any_liked": any_liked,
        "rating_text": rating_text,
    }


def history_summaries_for_group_ids(
    conn: sqlite3.Connection, group_ids: List[str]
) -> Dict[str, Dict[str, Any]]:
    """One query; map group_id -> summary dict for list endpoints."""
    ensure_schema(conn)
    if not group_ids:
        return {}
    placeholders = ",".join("?" * len(group_ids))
    cur = conn.execute(
        f"""
        SELECT * FROM filter_lab_run
        WHERE group_id IN ({placeholders})
        ORDER BY group_id, model_tag, record_id
        """,
        group_ids,
    )
    by_gid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in cur.fetchall():
        d = dict(row)
        gid = str(d.get("group_id") or "")
        if gid:
            by_gid[gid].append(d)
    out: Dict[str, Dict[str, Any]] = {}
    for gid in group_ids:
        out[str(gid)] = _history_summary_from_runs(by_gid.get(str(gid), []))
    return out


def insert_model_event(conn: sqlite3.Connection, group_id: str, event_type: str, model_tag: str) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO filter_lab_model_event (group_id, event_type, model_tag, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (group_id, event_type, model_tag, _now()),
    )
    conn.commit()


def delete_group(conn: sqlite3.Connection, group_id: str) -> None:
    ensure_schema(conn)
    conn.execute("DELETE FROM filter_lab_job_group WHERE id = ?", (group_id,))
    conn.commit()


def prune_old_groups(conn: sqlite3.Connection, *, max_age_days: int = 30) -> int:
    """Delete job groups (and runs) older than max_age_days. Returns deleted group count."""
    ensure_schema(conn)
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    # created_at is ISO string — compare lexicographically works for ISO8601 UTC
    from datetime import datetime as dt

    cutoff_iso = dt.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    cur = conn.execute("SELECT id FROM filter_lab_job_group WHERE created_at < ?", (cutoff_iso,))
    ids = [r[0] for r in cur.fetchall()]
    for gid in ids:
        conn.execute("DELETE FROM filter_lab_job_group WHERE id = ?", (gid,))
    conn.commit()
    return len(ids)
