"""Promote enrichment tables and rollups into scope-named signal_objects for owner UI."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List

from ..signal_object_store import SignalObjectStore
from .aggregates import recompute_all_gate_aggregates
from .commitments import recompute_commitments
from .rhythm import recompute_rhythm


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _source_ref(table: str, record_id: str) -> Dict[str, str]:
    return {"table": table, "id": str(record_id)}


def _materialize_event_counts(store: SignalObjectStore, conn: sqlite3.Connection) -> int:
    by_day: Dict[str, Dict[str, int]] = defaultdict(lambda: {"busy": 0, "free": 0, "total": 0})
    busy_count = 0
    free_count = 0
    refs: List[Dict[str, str]] = []
    total = 0

    if _table_exists(conn, "calendar_events"):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()}
        select_cols = ["event_id", "starts_at"]
        if "is_busy" in cols:
            select_cols.append("is_busy")
        if "metadata_json" in cols:
            select_cols.append("metadata_json")
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM calendar_events ORDER BY starts_at"
        ).fetchall()
        for row in rows:
            data = dict(zip(select_cols, row))
            event_id = data["event_id"]
            starts_at = data.get("starts_at")
            busy = bool(data.get("is_busy"))
            if not busy and data.get("metadata_json"):
                try:
                    meta = json.loads(str(data["metadata_json"]))
                    if isinstance(meta, dict) and "is_busy" in meta:
                        busy = bool(meta.get("is_busy"))
                except json.JSONDecodeError:
                    pass
            day = str(starts_at or "")[:10] or "unknown"
            by_day[day]["total"] += 1
            if busy:
                by_day[day]["busy"] += 1
                busy_count += 1
            else:
                by_day[day]["free"] += 1
                free_count += 1
            refs.append(_source_ref("calendar_events", event_id))
            total += 1

    if _table_exists(conn, "journal_entries"):
        journal_rows = conn.execute(
            "SELECT entry_id, entry_at, starts_at, ends_at, metadata_json FROM journal_entries ORDER BY entry_at"
        ).fetchall()
        for entry_id, entry_at, starts_at, ends_at, metadata_json in journal_rows:
            meta: Dict[str, Any] = {}
            if metadata_json:
                try:
                    parsed = json.loads(str(metadata_json))
                    if isinstance(parsed, dict):
                        meta = parsed
                except json.JSONDecodeError:
                    meta = {}
            end_value = str(ends_at or meta.get("ends_at") or "").strip()
            if not end_value:
                continue
            day = str(starts_at or entry_at or "")[:10] or "unknown"
            by_day[day]["total"] += 1
            by_day[day]["busy"] += 1
            busy_count += 1
            refs.append(_source_ref("journal_entries", str(entry_id)))
            total += 1

    if total == 0:
        return 0
    store.upsert_object(
        "time",
        "event_counts",
        "rolling",
        {
            "total_events": total,
            "busy_count": busy_count,
            "free_count": free_count,
            "by_day": dict(by_day),
        },
        source_refs=refs[:40],
        confidence=0.92,
        extractor_version="scope_materializer_v1",
    )
    return 1


def _materialize_table_rows(
    store: SignalObjectStore,
    conn: sqlite3.Connection,
    *,
    table: str,
    object_type: str,
    dimension: str,
    id_col: str,
    payload_cols: List[str],
    ref_table: str | None = None,
    ref_id_col: str = "record_id",
    limit: int = 200,
) -> int:
    if not _table_exists(conn, table):
        return 0
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    available = [c for c in payload_cols if c in cols]
    if id_col not in cols:
        return 0
    select_cols = [id_col] + available
    if ref_id_col in cols and ref_id_col not in select_cols:
        select_cols.append(ref_id_col)
    if "source_id" in cols and "source_id" not in select_cols:
        select_cols.append("source_id")
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} FROM {table} LIMIT ?",
        (limit,),
    ).fetchall()
    created = 0
    for row in rows:
        data = dict(zip(select_cols, row))
        object_key = str(data.get(id_col) or "")
        if not object_key:
            continue
        payload = {col: data.get(col) for col in available if data.get(col) not in (None, "")}
        ref_id = str(data.get(ref_id_col) or object_key)
        store.upsert_object(
            dimension,
            object_type,
            object_key,
            payload,
            source_refs=[_source_ref(ref_table or table, ref_id)],
            confidence=0.8,
            extractor_version="scope_materializer_v1",
        )
        created += 1
    return created


def _materialize_activity_tags(store: SignalObjectStore, conn: sqlite3.Connection) -> int:
    created = 0
    if not _table_exists(conn, "activity_events"):
        return 0
    tag_rules = (
        ("edtech", ("edtech", "learning", "school")),
        ("ai_research", ("arxiv", "retrieval", "memory systems", "ai agents")),
        ("infrastructure", ("github", "control-plane", "topos")),
        ("outdoors", ("alltrails", "hiking", "mission peak")),
        ("privacy", ("uma", "permissions", "privacy", "hn")),
    )
    rows = conn.execute(
        "SELECT event_id, url, title FROM activity_events LIMIT 200"
    ).fetchall()
    for event_id, url, title in rows:
        haystack = f"{url or ''} {title or ''}".lower()
        for tag, keywords in tag_rules:
            if any(k in haystack for k in keywords):
                store.upsert_object(
                    "interests",
                    "activity_tags",
                    f"{event_id}:{tag}",
                    {"tag": tag, "url": url, "title": title},
                    source_refs=[_source_ref("activity_events", event_id)],
                    confidence=0.75,
                    extractor_version="scope_materializer_v1",
                )
                created += 1
                break
    return created


def _materialize_top_topics(store: SignalObjectStore, conn: sqlite3.Connection) -> int:
    if _table_exists(conn, "signal_facts"):
        fact_cols = {row[1] for row in conn.execute("PRAGMA table_info(signal_facts)").fetchall()}
        if "object_type" in fact_cols:
            rows = conn.execute(
                """
                SELECT fact_id, record_id, payload_json
                FROM signal_facts
                WHERE object_type='top_topics' OR fact_id LIKE 'top_topics:%'
                LIMIT 50
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT fact_id, record_id, payload_json
                FROM signal_facts
                WHERE fact_id LIKE 'top_topics:%'
                LIMIT 50
                """
            ).fetchall()
        created = 0
        for fact_id, record_id, payload_json in rows:
            payload = json.loads(payload_json or "{}") if payload_json else {}
            cluster_id = str(record_id or fact_id or "")
            if not cluster_id:
                continue
            store.upsert_object(
                "interests",
                "top_topics",
                cluster_id,
                payload if isinstance(payload, dict) else {"summary": payload},
                source_refs=[_source_ref("topic_clusters", cluster_id)],
                confidence=0.82,
                extractor_version="scope_materializer_v1",
            )
            created += 1
        if created:
            return created
    if not _table_exists(conn, "topic_clusters"):
        return 0
    cluster_cols = {row[1] for row in conn.execute("PRAGMA table_info(topic_clusters)").fetchall()}
    select_cols = [c for c in ("cluster_id", "label", "member_count", "source_ids_json") if c in cluster_cols]
    if "cluster_id" not in select_cols:
        return 0
    clusters = conn.execute(
        f"""
        SELECT {', '.join(select_cols)}
        FROM topic_clusters
        ORDER BY member_count DESC
        LIMIT 20
        """
    ).fetchall()
    created = 0
    for row in clusters:
        data = dict(zip(select_cols, row))
        cluster_id = str(data.get("cluster_id") or "")
        if not cluster_id:
            continue
        payload = {
            "label": data.get("label"),
            "member_count": data.get("member_count"),
        }
        if data.get("source_ids_json"):
            try:
                payload["source_ids"] = json.loads(str(data["source_ids_json"]))
            except json.JSONDecodeError:
                payload["source_ids"] = []
        store.upsert_object(
            "interests",
            "top_topics",
            cluster_id,
            payload,
            source_refs=[_source_ref("topic_clusters", cluster_id)],
            confidence=0.82,
            extractor_version="scope_materializer_v1",
        )
        created += 1
    return created


def materialize_scope_signal_objects(conn: sqlite3.Connection) -> Dict[str, int]:
    """Mirror enrichment / rollups into signal_objects so owner UI counts match scope registry."""
    store = SignalObjectStore(conn)
    counts: Dict[str, int] = {
        "event_counts": _materialize_event_counts(store, conn),
        "message_entities": _materialize_table_rows(
            store,
            conn,
            table="message_entities",
            object_type="message_entities",
            dimension="memory",
            id_col="entity_id",
            payload_cols=["entity_text", "record_id", "source_id", "message_id"],
            ref_table="conversation_messages",
        ),
        "message_emotions": _materialize_table_rows(
            store,
            conn,
            table="message_emotions",
            object_type="message_emotions",
            dimension="memory",
            id_col="emotion_id" if _table_has_column(conn, "message_emotions", "emotion_id") else "record_id",
            payload_cols=["emotion_label", "confidence", "source_id", "message_id", "record_id"],
            ref_table="conversation_messages",
            ref_id_col="message_id" if _table_has_column(conn, "message_emotions", "message_id") else "record_id",
        ),
        "message_topics": _materialize_table_rows(
            store,
            conn,
            table="message_topics",
            object_type="message_topics",
            dimension="memory",
            id_col="topic_id",
            payload_cols=["topic", "record_id", "source_id", "message_id"],
            ref_table="ai_chat_messages",
        ),
        "user_goals_memory": _materialize_table_rows(
            store,
            conn,
            table="user_goals",
            object_type="user_goals",
            dimension="memory",
            id_col="goal_id",
            payload_cols=["goal_text", "source_id", "record_id"],
        ),
        "user_goals_work": _materialize_table_rows(
            store,
            conn,
            table="user_goals",
            object_type="user_goals",
            dimension="work",
            id_col="goal_id",
            payload_cols=["goal_text", "source_id", "record_id"],
        ),
        "user_goals_intentions": _materialize_table_rows(
            store,
            conn,
            table="user_goals",
            object_type="user_goals",
            dimension="intentions",
            id_col="goal_id",
            payload_cols=["goal_text", "source_id", "record_id"],
        ),
        "activity_tags": _materialize_activity_tags(store, conn),
        "top_topics": _materialize_top_topics(store, conn),
    }
    # Steady-state reconciliation: before cluster IDs became stable, every
    # recompute minted new ids and this materializer accumulated a fresh
    # top_topics object per id, forever (11k stale rows on the live node).
    try:
        from ...lifecycle.gc import reconcile_top_topics_objects

        counts["top_topics_pruned"] = reconcile_top_topics_objects(conn)
    except Exception:  # noqa: BLE001 — reconciliation must never block materialization
        counts["top_topics_pruned"] = 0
    # Rhythm + commitments before gate aggregates: availability_summary (a time
    # aggregate) folds in the routine_confidence this pass just wrote.
    counts["rhythm"] = recompute_rhythm(store, conn)
    counts["commitments"] = recompute_commitments(store, conn)
    aggregates = recompute_all_gate_aggregates(store)
    counts["aggregates"] = sum(int(v or 0) for v in aggregates.values())
    return counts
