"""Owner-facing read queries over the fact store (Facts tab backend)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .store import FactStore

if TYPE_CHECKING:  # import cost only, never at runtime
    from ..lifecycle.blackhole_guard import BlackholeGuard


def _subject_names(conn: sqlite3.Connection, subject_ids: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    ids = [s for s in dict.fromkeys(subject_ids) if s]
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        placeholders = ",".join("?" for _ in chunk)
        for entity_id, name, is_self in conn.execute(
            f"SELECT entity_id, canonical_name, is_self FROM entities WHERE entity_id IN ({placeholders})",
            chunk,
        ).fetchall():
            names[str(entity_id)] = "you" if is_self else str(name)
    return names


def list_facts(
    conn: sqlite3.Connection,
    *,
    guard: "BlackholeGuard",
    predicate: Optional[str] = None,
    dimension: Optional[str] = None,
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = ["object_type = 'fact'"]
    params: List[Any] = []
    if not include_closed:
        where.append("valid_to IS NULL")
    if dimension and str(dimension).strip():
        where.append("signal_dimension = ?")
        params.append(str(dimension).strip().lower())
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT object_id, signal_dimension, object_key, payload_json, confidence,
               source_refs_json, valid_from, valid_to, updated_at
        FROM signal_objects
        WHERE {where_sql}
        ORDER BY (valid_to IS NULL) DESC, valid_from DESC
        """,
        params,
    ).fetchall()

    parsed: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row[3] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if predicate and str(payload.get("predicate") or "") != str(predicate).strip().lower():
            continue
        parsed.append(
            {
                "object_id": row[0],
                "dimension": row[1],
                "object_key": row[2],
                "payload": payload,
                # Lifted for the black-hole filter below. Both are internal:
                # `items` is rebuilt field by field, so neither reaches a caller.
                "subject_entity_id": str(payload.get("subject_entity_id") or ""),
                "_scan_blob": json.dumps(payload, default=str),
                "confidence": row[4],
                "source_refs": json.loads(row[5] or "[]"),
                "valid_from": row[6],
                "valid_to": row[7],
                "updated_at": row[8],
            }
        )

    # Filtered here, ahead of total/page/predicate_counts, so all three agree.
    # A `total` computed before the filter would shift when an entity is
    # protected and confirm its existence -- the count side channel D5 removes.
    # Both halves are needed: the id-join is exact but misses a fact whose
    # subject was never bound, and the payload scan catches the name in prose
    # (object_value, a rendered claim) where no id exists at all.
    parsed = guard.filter_rows(parsed, id_keys=("subject_entity_id",))
    parsed = guard.filter_name_string_artifacts(parsed, text_keys=("_scan_blob",))

    total = len(parsed)
    page = parsed[offset : offset + limit]
    subject_names = _subject_names(
        conn, [str(f["payload"].get("subject_entity_id") or "") for f in page]
    )

    predicate_counts: Dict[str, int] = {}
    for fact in parsed:
        if fact["valid_to"] is None:
            key = str(fact["payload"].get("predicate") or "unknown")
            predicate_counts[key] = predicate_counts.get(key, 0) + 1

    items = []
    for fact in page:
        payload = fact["payload"]
        subject_id = str(payload.get("subject_entity_id") or "")
        subject_name = subject_names.get(subject_id, "owner")
        items.append(
            {
                "object_id": fact["object_id"],
                "dimension": fact["dimension"],
                "subject_entity_id": subject_id,
                "subject_name": subject_name,
                "predicate": payload.get("predicate"),
                "object_value": payload.get("object_value"),
                "period_start": payload.get("period_start"),
                "period_end": payload.get("period_end"),
                "confidence": payload.get("confidence", fact["confidence"]),
                "disclosure": payload.get("disclosure", "scoped"),
                # Attribution: who asserted this claim (owner | contact:<id> |
                # assistant | page-author | …). Powers the person→ambient badge.
                "asserted_by": payload.get("asserted_by", "owner"),
                "excluded_by_owner": bool(payload.get("excluded_by_owner")),
                "verified_by_owner": bool(payload.get("verified_by_owner")),
                "rendered": FactStore.render(fact, subject_name=subject_name),
                "source_refs": fact["source_refs"],
                "valid_from": fact["valid_from"],
                "valid_to": fact["valid_to"],
                "is_active": fact["valid_to"] is None,
            }
        )

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "predicate_counts": predicate_counts,
    }


def list_stat_insights(
    conn: sqlite3.Connection,
    *,
    guard: "BlackholeGuard",
    dimension: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Promoted stat_insight facts — the human-readable statistics layer."""
    limit = max(1, min(int(limit), 500))
    where = ["fact_id LIKE 'stat:%'"]
    params: List[Any] = []
    if dimension and str(dimension).strip():
        where.append("dimension = ?")
        params.append(str(dimension).strip().lower())
    rows = conn.execute(
        f"""
        SELECT fact_id, dimension, payload_json, created_at
        FROM signal_facts
        WHERE {" AND ".join(where)}
        ORDER BY dimension, fact_id
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    items: List[Dict[str, Any]] = []
    dimension_counts: Dict[str, int] = {}
    for fact_id, dim, payload_json, created_at in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        text = str(payload.get("tag") or payload.get("summary_text") or "").strip()
        if not text:
            continue
        dim_key = str(dim or "memory")
        dimension_counts[dim_key] = dimension_counts.get(dim_key, 0) + 1
        items.append(
            {
                "fact_id": fact_id,
                "dimension": dim_key,
                "text": text,
                "stat_id": payload.get("record_id"),
                "group_key": payload.get("group_key") or "",
                "confidence": payload.get("confidence"),
                "disclosure": payload.get("disclosure", "owner_only"),
                "created_at": created_at,
            }
        )
    # A stat insight carries the entity name in its group_key and its rendered
    # text and has no id to join on, which is why the feasibility report listed
    # stat group_key as a name-string leak surface in its own right.
    items = guard.filter_name_string_artifacts(items, text_keys=("text", "group_key"))
    dimension_counts = {}
    for item in items:
        key = str(item.get("dimension") or "memory")
        dimension_counts[key] = dimension_counts.get(key, 0) + 1
    return {"items": items, "total": len(items), "dimension_counts": dimension_counts}


def list_timeline(
    conn: sqlite3.Connection,
    *,
    canonical_table: Optional[str] = None,
    source_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = ["1=1"]
    params: List[Any] = []
    if canonical_table and str(canonical_table).strip():
        where.append("canonical_table = ?")
        params.append(str(canonical_table).strip())
    if source_id and str(source_id).strip():
        where.append("source_id = ?")
        params.append(str(source_id).strip())
    if date_from and str(date_from).strip():
        where.append("event_at >= ?")
        params.append(str(date_from).strip())
    if date_to and str(date_to).strip():
        where.append("event_at <= ?")
        params.append(str(date_to).strip() + ("T23:59:59Z" if len(str(date_to).strip()) == 10 else ""))
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM timeline WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT event_at, record_id, source_id, canonical_table, record_type
        FROM timeline WHERE {where_sql}
        ORDER BY event_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    table_rows = conn.execute(
        "SELECT canonical_table, COUNT(*) FROM timeline GROUP BY canonical_table"
    ).fetchall()

    return {
        "items": [
            {
                "event_at": r[0],
                "record_id": r[1],
                "source_id": r[2],
                "canonical_table": r[3],
                "record_type": r[4],
            }
            for r in rows
        ],
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "table_counts": {str(t or "unknown"): int(n) for t, n in table_rows},
    }
