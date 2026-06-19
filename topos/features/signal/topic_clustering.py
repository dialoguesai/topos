"""Topic clustering over canonical embeddings (ChatGPT + browser activity).

Implements wiki ``memory_topic_map`` / ``top_topics`` rollups for cross-source query.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("topos.features.signal.topic_clustering")

MVP_QUERY_SOURCE_IDS = (
    "chatgpt_file_ingestion",
    "chatgpt_ui_conversation",
    "browser_visits",
)

_MIN_CLUSTER_RECORDS = 3
_MAX_CLUSTERS = 12
_KMEANS_ITERATIONS = 25


def _normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return -1.0
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _choose_k(n: int, requested: Optional[int] = None) -> int:
    if requested is not None and requested > 0:
        return min(requested, n)
    if n < _MIN_CLUSTER_RECORDS:
        return 1
    return max(2, min(_MAX_CLUSTERS, int(round(math.sqrt(n)))))


def load_embedding_records(
    conn,
    *,
    source_ids: Optional[Sequence[str]] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """Load embeddable rows from signal_embeddings for MVP query sources."""
    if conn is None:
        return []
    ids = list(source_ids or MVP_QUERY_SOURCE_IDS)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT embedding_id, record_id, source_id, signal_dimension, text_preview,
               vector_blob, provenance_json
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
          AND source_id IN ({placeholders})
        ORDER BY embedding_id
        LIMIT ?
        """,
        (*ids, limit),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        blob = row[5]
        if not blob:
            continue
        try:
            vector = json.loads(blob.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if not isinstance(vector, list) or len(vector) < 2:
            continue
        metadata: Dict[str, Any] = {}
        try:
            if row[6]:
                parsed = json.loads(row[6])
                if isinstance(parsed, dict):
                    metadata = parsed
        except json.JSONDecodeError:
            pass
        record_type = "activity_event" if str(row[2]).startswith("browser_") else "ai_chat_message"
        out.append(
            {
                "embedding_id": row[0],
                "record_id": row[1],
                "source_id": row[2],
                "signal_dimension": row[3],
                "text_preview": row[4] or "",
                "vector": _normalize(vector),
                "record_type": record_type,
                "metadata": metadata,
            }
        )
    return out


def _init_centroids(vectors: List[List[float]], k: int) -> List[List[float]]:
    if k >= len(vectors):
        return [list(v) for v in vectors]
    rng = random.Random(42)
    indices = rng.sample(range(len(vectors)), k)
    return [list(vectors[i]) for i in indices]


def cluster_embedding_records(
    records: List[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """K-means (cosine) over normalized embedding vectors."""
    if not records:
        return []
    vectors = [rec["vector"] for rec in records]
    n = len(vectors)
    cluster_k = _choose_k(n, k)
    if cluster_k <= 1:
        return [_build_cluster("tc_single", records, cluster_index=0)]

    centroids = _init_centroids(vectors, cluster_k)
    assignments = [0] * n

    for _ in range(_KMEANS_ITERATIONS):
        changed = False
        for idx, vec in enumerate(vectors):
            best = max(range(cluster_k), key=lambda ci: _cosine(vec, centroids[ci]))
            if assignments[idx] != best:
                assignments[idx] = best
                changed = True
        if not changed:
            break
        for ci in range(cluster_k):
            members = [vectors[i] for i, a in enumerate(assignments) if a == ci]
            if not members:
                centroids[ci] = list(vectors[assignments[ci % n]])
                continue
            dim = len(members[0])
            mean = [sum(m[d] for m in members) / len(members) for d in range(dim)]
            centroids[ci] = _normalize(mean)

    buckets: Dict[int, List[Dict[str, Any]]] = {i: [] for i in range(cluster_k)}
    for rec, assign in zip(records, assignments):
        buckets[assign].append(rec)

    clusters: List[Dict[str, Any]] = []
    for ci, members in buckets.items():
        if not members:
            continue
        cluster_id = f"tc_{uuid.uuid4().hex[:16]}"
        clusters.append(_build_cluster(cluster_id, members, cluster_index=ci))
    return clusters


def _build_cluster(cluster_id: str, members: List[Dict[str, Any]], *, cluster_index: int) -> Dict[str, Any]:
    source_mix = Counter(str(m.get("source_id") or "") for m in members)
    dimension = "interests" if source_mix.get("browser_visits", 0) > len(members) / 2 else "memory"
    label = label_cluster(members)
    previews = [str(m.get("text_preview") or "") for m in members if m.get("text_preview")]
    centroid_preview = previews[0][:120] if previews else label
    return {
        "cluster_id": cluster_id,
        "label": label,
        "dimension": dimension,
        "member_count": len(members),
        "source_mix": dict(source_mix),
        "label_terms": _extract_terms(members),
        "centroid_preview": centroid_preview,
        "members": [
            {
                "record_id": m.get("record_id"),
                "source_id": m.get("source_id"),
                "record_type": m.get("record_type") or "unknown",
                "text_preview": m.get("text_preview"),
                "weight": 1.0,
                "metadata": m.get("metadata") or {},
            }
            for m in members
        ],
        "cluster_index": cluster_index,
    }


def _extract_terms(members: List[Dict[str, Any]]) -> List[str]:
    counter: Counter[str] = Counter()
    for member in members:
        cat = (member.get("metadata") or {}).get("url_category")
        if isinstance(cat, str) and cat.strip():
            counter[cat.strip().lower()] += 3
        text = str(member.get("text_preview") or "").lower()
        for token in re.findall(r"[a-z]{4,}", text):
            if token not in {"that", "this", "with", "from", "have", "about"}:
                counter[token] += 1
    return [term for term, _ in counter.most_common(5)]


def label_cluster(members: List[Dict[str, Any]]) -> str:
    terms = _extract_terms(members)
    if terms:
        return " / ".join(terms[:3])
    previews = [str(m.get("text_preview") or "").strip() for m in members if m.get("text_preview")]
    if previews:
        return previews[0][:64]
    return "topic cluster"


def persist_topic_clusters(
    conn,
    clusters: List[Dict[str, Any]],
    *,
    sync_batch_id: Optional[str] = None,
    model: str = "kmeans_cosine_v1",
    provider: str = "topos",
) -> Dict[str, int]:
    if conn is None or not clusters:
        return {"clusters_written": 0, "members_written": 0}

    cluster_ids = [c["cluster_id"] for c in clusters if c.get("cluster_id")]
    if cluster_ids:
        placeholders = ",".join("?" for _ in cluster_ids)
        conn.execute(
            f"DELETE FROM topic_cluster_members WHERE cluster_id IN ({placeholders})",
            cluster_ids,
        )
        conn.execute(
            f"DELETE FROM topic_clusters WHERE cluster_id IN ({placeholders})",
            cluster_ids,
        )

    clusters_written = 0
    members_written = 0
    for cluster in clusters:
        cid = cluster["cluster_id"]
        conn.execute(
            """
            INSERT INTO topic_clusters (
                cluster_id, label, dimension, member_count, source_mix_json,
                label_terms_json, centroid_preview, model, provider, sync_batch_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                cid,
                cluster.get("label") or "topic cluster",
                cluster.get("dimension") or "memory",
                int(cluster.get("member_count") or 0),
                json.dumps(cluster.get("source_mix") or {}),
                json.dumps(cluster.get("label_terms") or []),
                cluster.get("centroid_preview"),
                model,
                provider,
                sync_batch_id,
            ),
        )
        clusters_written += 1
        for member in cluster.get("members") or []:
            if not member.get("record_id"):
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO topic_cluster_members (
                    member_id, cluster_id, record_id, source_id, record_type,
                    text_preview, weight, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"tcm_{uuid.uuid4().hex[:16]}",
                    cid,
                    member.get("record_id"),
                    member.get("source_id"),
                    member.get("record_type") or "unknown",
                    member.get("text_preview"),
                    float(member.get("weight") or 1.0),
                    json.dumps(member.get("metadata") or {}),
                ),
            )
            members_written += 1
    conn.commit()
    return {"clusters_written": clusters_written, "members_written": members_written}


def write_top_topics_signal_facts(
    adapters,
    conn,
    *,
    limit: int = 20,
) -> int:
    """Promote cluster rollups into signal_facts as wiki ``top_topics`` objects."""
    if conn is None:
        return 0
    rows = conn.execute(
        """
        SELECT cluster_id, label, dimension, member_count, source_mix_json, label_terms_json
        FROM topic_clusters
        ORDER BY member_count DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    written = 0
    for row in rows:
        source_mix = json.loads(row[4] or "{}")
        primary_source = max(source_mix, key=source_mix.get) if source_mix else "cross_source"
        adapters.signal.put_fact(
            {
                "dimension": row[2] or "memory",
                "source_id": primary_source,
                "record_id": row[0],
                "tag": row[1],
                "confidence": min(1.0, float(row[3] or 1) / 10.0),
                "object_type": "top_topics",
                "member_count": row[3],
                "label_terms": json.loads(row[5] or "[]"),
                "source_mix": source_mix,
            }
        )
        written += 1
    return written


def recompute_topic_clusters(
    conn,
    *,
    source_ids: Optional[Sequence[str]] = None,
    sync_batch_id: Optional[str] = None,
    min_records: int = _MIN_CLUSTER_RECORDS,
    k: Optional[int] = None,
) -> Dict[str, Any]:
    records = load_embedding_records(conn, source_ids=source_ids)
    if len(records) < min_records:
        return {
            "status": "skipped",
            "reason": "insufficient_embeddings",
            "records_loaded": len(records),
            "clusters_written": 0,
            "members_written": 0,
        }
    clusters = cluster_embedding_records(records, k=k)
    persist_result = persist_topic_clusters(conn, clusters, sync_batch_id=sync_batch_id)
    return {
        "status": "completed",
        "records_loaded": len(records),
        **persist_result,
        "cluster_labels": [c.get("label") for c in clusters],
    }


def load_topic_clusters_for_query(
    conn,
    *,
    limit: int = 20,
    dimension: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    params: List[Any] = []
    query = """
        SELECT cluster_id, label, dimension, member_count, source_mix_json,
               label_terms_json, centroid_preview
        FROM topic_clusters
    """
    if dimension:
        query += " WHERE dimension=?"
        params.append(dimension)
    query += " ORDER BY member_count DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "cluster_id": row[0],
                "label": row[1],
                "dimension": row[2],
                "member_count": row[3],
                "source_mix": json.loads(row[4] or "{}"),
                "label_terms": json.loads(row[5] or "[]"),
                "centroid_preview": row[6],
                "object_type": "top_topics",
            }
        )
    return out


def _query_tokens(query_text: str) -> List[str]:
    import re

    return list(dict.fromkeys(re.findall(r"[a-z0-9]{3,}", (query_text or "").lower())))


def rank_topic_clusters_for_query(
    clusters: List[Dict[str, Any]],
    query_text: str,
    *,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Rank clusters by query term overlap with label + label_terms; tie-break member_count."""
    tokens = _query_tokens(query_text)
    if not clusters:
        return []

    def cluster_key(cluster: Dict[str, Any]) -> tuple[int, int]:
        label = str(cluster.get("label") or "").lower()
        terms = [str(term).lower() for term in (cluster.get("label_terms") or [])]
        blob = f"{label} {' '.join(terms)}"
        overlap = sum(1 for token in tokens if token in blob) if tokens else 0
        return (overlap, int(cluster.get("member_count") or 0))

    ranked = sorted(clusters, key=cluster_key, reverse=True)
    token_count = len(tokens) or 1
    out: List[Dict[str, Any]] = []
    for cluster in ranked[: max(1, limit)]:
        overlap, _ = cluster_key(cluster)
        relevance = 1.0 if not tokens else min(1.0, overlap / token_count)
        out.append({**cluster, "relevance_score": round(relevance, 4)})
    return out


def load_topic_cluster_members(
    conn,
    cluster_id: str,
    *,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if conn is None or not cluster_id:
        return []
    rows = conn.execute(
        """
        SELECT member_id, record_id, source_id, record_type, text_preview, weight, metadata_json
        FROM topic_cluster_members
        WHERE cluster_id=?
        ORDER BY weight DESC, record_id ASC
        LIMIT ?
        """,
        (cluster_id, max(1, min(int(limit), 500))),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        metadata: Dict[str, Any] = {}
        try:
            if row[6]:
                parsed = json.loads(row[6])
                if isinstance(parsed, dict):
                    metadata = parsed
        except json.JSONDecodeError:
            pass
        out.append(
            {
                "member_id": row[0],
                "record_id": row[1],
                "source_id": row[2],
                "record_type": row[3],
                "text_preview": row[4],
                "weight": row[5],
                "metadata": metadata,
            }
        )
    return out
