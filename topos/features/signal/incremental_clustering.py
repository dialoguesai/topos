"""Incremental topic clustering: assign-first, debounced consolidation, stable IDs.

Per-ingest cost is O(batch × k) — each new embedding either joins the nearest
existing cluster (cosine >= ASSIGN_THRESHOLD, centroid nudged incrementally)
or lands in a candidate pool. A full recompute runs only when the pool is
large or the snapshot is stale, and cluster IDs survive it via greedy
centroid matching, so `top_topics` facts, UI links, and
signal_embeddings.cluster_id stop churning.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .vector_codec import decode_vector, encode_f32
from .vector_math import cosine_similarity
from .topic_clustering import _normalize

logger = logging.getLogger("topos.features.signal.incremental_clustering")

ASSIGN_THRESHOLD = 0.60
STABLE_MATCH_THRESHOLD = 0.70
CONSOLIDATE_POOL_SIZE = 100
CONSOLIDATE_STALE_HOURS = 24


def load_cluster_centroids(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT cluster_id, label, dimension, member_count, centroid_vector, metadata_json
        FROM topic_clusters
        """
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for cluster_id, label, dimension, member_count, centroid_blob, metadata_json in rows:
        centroid: Optional[List[float]] = None
        if centroid_blob:
            try:
                centroid = decode_vector(centroid_blob, "f32")
            except Exception:
                centroid = None
        if centroid is None and metadata_json:
            try:
                meta = json.loads(metadata_json)
                if isinstance(meta.get("centroid_vector"), list):
                    centroid = [float(x) for x in meta["centroid_vector"]]
            except json.JSONDecodeError:
                pass
        if centroid:
            out.append(
                {
                    "cluster_id": str(cluster_id),
                    "label": label,
                    "dimension": dimension,
                    "member_count": int(member_count or 0),
                    "centroid": _normalize(centroid),
                }
            )
    return out


def _nudge_centroid(centroid: Sequence[float], vector: Sequence[float], count: int) -> List[float]:
    """Incremental mean update, renormalized (count = members before this one)."""
    n = max(1, count)
    merged = [(c * n + v) / (n + 1) for c, v in zip(centroid, vector)]
    return _normalize(merged)


def assign_embeddings(
    conn: sqlite3.Connection,
    embedding_rows: List[Dict[str, Any]],
    *,
    threshold: float = ASSIGN_THRESHOLD,
) -> Dict[str, int]:
    """Assign new embeddings to existing clusters or the candidate pool.

    embedding_rows: [{embedding_id, record_id, source_id, vector, text_preview,
                      record_type}]
    """
    clusters = load_cluster_centroids(conn)
    assigned = 0
    pooled = 0
    for row in embedding_rows:
        vector = row.get("vector")
        if not isinstance(vector, list) or not vector:
            continue
        vector = _normalize([float(x) for x in vector])
        best_cluster, best_sim = None, 0.0
        for cluster in clusters:
            if len(cluster["centroid"]) != len(vector):
                continue
            sim = cosine_similarity(vector, cluster["centroid"])
            if sim > best_sim:
                best_cluster, best_sim = cluster, sim
        if best_cluster is not None and best_sim >= threshold:
            conn.execute(
                """
                INSERT OR REPLACE INTO topic_cluster_members (
                    member_id, cluster_id, record_id, source_id, record_type,
                    text_preview, weight, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
                """,
                (
                    f"tcm_{uuid.uuid4().hex[:16]}",
                    best_cluster["cluster_id"],
                    row.get("record_id"),
                    row.get("source_id"),
                    row.get("record_type") or "unknown",
                    row.get("text_preview"),
                    round(float(best_sim), 4),
                ),
            )
            new_centroid = _nudge_centroid(
                best_cluster["centroid"], vector, best_cluster["member_count"]
            )
            best_cluster["centroid"] = new_centroid
            best_cluster["member_count"] += 1
            conn.execute(
                """
                UPDATE topic_clusters
                SET member_count = member_count + 1,
                    centroid_vector = ?,
                    updated_at = datetime('now')
                WHERE cluster_id = ?
                """,
                (encode_f32(new_centroid), best_cluster["cluster_id"]),
            )
            _set_embedding_cluster(conn, row.get("embedding_id"), best_cluster["cluster_id"])
            assigned += 1
        else:
            conn.execute(
                "INSERT OR IGNORE INTO cluster_candidates (embedding_id, record_id, source_id) VALUES (?, ?, ?)",
                (row.get("embedding_id"), row.get("record_id"), row.get("source_id")),
            )
            pooled += 1
    conn.commit()
    return {"assigned": assigned, "pooled": pooled}


def _set_embedding_cluster(conn: sqlite3.Connection, embedding_id: Optional[str], cluster_id: str) -> None:
    if not embedding_id:
        return
    try:
        conn.execute(
            "UPDATE signal_embeddings SET cluster_id=? WHERE embedding_id=?",
            (cluster_id, embedding_id),
        )
    except sqlite3.OperationalError:
        pass


def candidate_pool_size(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute("SELECT COUNT(*) FROM cluster_candidates").fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def consolidation_due(conn: sqlite3.Connection) -> bool:
    if candidate_pool_size(conn) >= CONSOLIDATE_POOL_SIZE:
        return True
    try:
        row = conn.execute(
            "SELECT 1 FROM cluster_recompute_log WHERE kind='full' AND created_at >= datetime('now', ?) LIMIT 1",
            (f"-{CONSOLIDATE_STALE_HOURS} hours",),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is None


def match_stable_cluster_ids(
    old_clusters: List[Dict[str, Any]],
    new_clusters: List[Dict[str, Any]],
    *,
    threshold: float = STABLE_MATCH_THRESHOLD,
    old_members: Optional[Dict[str, set]] = None,
    member_overlap_threshold: float = 0.5,
) -> int:
    """Rename new clusters to matched old IDs (greedy best-similarity-first).

    Two passes: centroid cosine first, then member-record overlap for
    unmatched clusters (robust to k-means splits, where the centroid moves
    but the members largely don't). Mutates new_clusters in place; returns
    count of preserved IDs.
    """
    pairs: List[Tuple[float, int, int]] = []
    for oi, old in enumerate(old_clusters):
        old_centroid = old.get("centroid") or old.get("centroid_vector")
        if not old_centroid:
            continue
        for ni, new in enumerate(new_clusters):
            new_centroid = new.get("centroid_vector")
            if not new_centroid or len(new_centroid) != len(old_centroid):
                continue
            sim = cosine_similarity(old_centroid, new_centroid)
            if sim >= threshold:
                pairs.append((sim, oi, ni))
    pairs.sort(reverse=True)
    used_old: set = set()
    used_new: set = set()
    preserved = 0
    for sim, oi, ni in pairs:
        if oi in used_old or ni in used_new:
            continue
        used_old.add(oi)
        used_new.add(ni)
        new_clusters[ni]["cluster_id"] = old_clusters[oi]["cluster_id"]
        preserved += 1

    if old_members:
        overlap_pairs: List[Tuple[float, int, int]] = []
        for ni, new in enumerate(new_clusters):
            if ni in used_new:
                continue
            new_ids = {
                str(m.get("record_id"))
                for m in (new.get("members") or [])
                if m.get("record_id")
            }
            if not new_ids:
                continue
            for oi, old in enumerate(old_clusters):
                if oi in used_old:
                    continue
                members = old_members.get(str(old.get("cluster_id"))) or set()
                if not members:
                    continue
                overlap = len(new_ids & members) / len(new_ids)
                if overlap >= member_overlap_threshold:
                    overlap_pairs.append((overlap, oi, ni))
        overlap_pairs.sort(reverse=True)
        for overlap, oi, ni in overlap_pairs:
            if oi in used_old or ni in used_new:
                continue
            used_old.add(oi)
            used_new.add(ni)
            new_clusters[ni]["cluster_id"] = old_clusters[oi]["cluster_id"]
            preserved += 1
    return preserved


def load_cluster_member_map(conn: sqlite3.Connection) -> Dict[str, set]:
    """cluster_id -> set(record_id) for the current snapshot."""
    try:
        rows = conn.execute(
            "SELECT cluster_id, record_id FROM topic_cluster_members WHERE record_id IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: Dict[str, set] = {}
    for cluster_id, record_id in rows:
        out.setdefault(str(cluster_id), set()).add(str(record_id))
    return out


def clear_candidate_pool(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DELETE FROM cluster_candidates")
    except sqlite3.OperationalError:
        pass


def log_recompute(
    conn: sqlite3.Connection,
    *,
    kind: str,
    records_processed: int,
    clusters_written: int,
    ids_preserved: int,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO cluster_recompute_log (kind, records_processed, clusters_written, ids_preserved)
            VALUES (?, ?, ?, ?)
            """,
            (kind, records_processed, clusters_written, ids_preserved),
        )
    except sqlite3.OperationalError:
        pass


def sync_member_cluster_ids(conn: sqlite3.Connection) -> int:
    """Backfill signal_embeddings.cluster_id from topic_cluster_members."""
    try:
        cursor = conn.execute(
            """
            UPDATE signal_embeddings
            SET cluster_id = (
                SELECT m.cluster_id FROM topic_cluster_members m
                WHERE m.record_id = signal_embeddings.record_id
                LIMIT 1
            )
            WHERE record_id IN (SELECT record_id FROM topic_cluster_members)
            """
        )
        return int(cursor.rowcount or 0)
    except sqlite3.OperationalError:
        return 0
