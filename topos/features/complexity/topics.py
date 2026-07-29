"""Topic Quality Lens metrics."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from typing import Any, Dict, List, Optional

from topos.features.signal.topic_clustering import compute_cluster_metrics

from .entropy import clamp_score, shannon_entropy_bits


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) > 2]


def _npmi_coherence(conn: sqlite3.Connection, cluster_id: str, label_terms: List[str]) -> Optional[float]:
    if not label_terms or not _table_exists(conn, "topic_cluster_members"):
        return None
    rows = conn.execute(
        """
        SELECT text_preview FROM topic_cluster_members
        WHERE cluster_id=? AND text_preview IS NOT NULL AND text_preview != ''
        LIMIT 200
        """,
        (cluster_id,),
    ).fetchall()
    docs = [_tokenize(str(r["text_preview"])) for r in rows if r["text_preview"]]
    if len(docs) < 2:
        return None
    term_sets = [set(d) for d in docs]
    n_docs = len(term_sets)
    cooc = Counter()
    for terms in label_terms[:10]:
        t = terms.lower()
        doc_freq = sum(1 for s in term_sets if t in s)
        if doc_freq < 1:
            continue
        for other in label_terms[:10]:
            o = other.lower()
            if o <= t:
                continue
            joint = sum(1 for s in term_sets if t in s and o in s)
            if joint <= 0:
                continue
            p_xy = joint / n_docs
            p_x = doc_freq / n_docs
            p_y = sum(1 for s in term_sets if o in s) / n_docs
            if p_x <= 0 or p_y <= 0:
                continue
            pmi = math.log2(p_xy / (p_x * p_y))
            norm = -math.log2(p_xy)
            if p_xy >= 1.0:
                npmi = 1.0
            elif norm <= 0:
                npmi = 0.0
            else:
                npmi = pmi / norm
            cooc[(t, o)] = npmi
    if not cooc:
        return None
    return round(sum(cooc.values()) / len(cooc), 4)


def compute_topic_quality(conn: sqlite3.Connection) -> Dict[str, Any]:
    if not _table_exists(conn, "topic_clusters"):
        return {
            "score": 0.0,
            "interpretation": "No topic clusters yet — connect sources and run enrichment.",
            "clusters": [],
            "sub_metrics": {},
        }

    rows = conn.execute(
        """
        SELECT cluster_id, label, member_count, label_terms_json
        FROM topic_clusters
        ORDER BY member_count DESC
        """
    ).fetchall()
    def _terms(raw: object) -> list:
        try:
            parsed = json.loads(str(raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []

    clusters = [
        {
            "cluster_id": str(r["cluster_id"]),
            "label": str(r["label"]),
            "member_count": int(r["member_count"] or 0),
            "label_terms": _terms(r["label_terms_json"]),
        }
        for r in rows
    ]
    base_metrics = compute_cluster_metrics(clusters, records_loaded=sum(c["member_count"] for c in clusters))
    topic_entropy = shannon_entropy_bits({c["cluster_id"]: c["member_count"] for c in clusters})

    per_cluster: List[Dict[str, Any]] = []
    coherence_scores: List[float] = []
    for cluster in clusters:
        terms = cluster.get("label_terms") if isinstance(cluster.get("label_terms"), list) else []
        npmi = _npmi_coherence(conn, cluster["cluster_id"], [str(t) for t in terms])
        if npmi is not None:
            coherence_scores.append(npmi)
        per_cluster.append(
            {
                "cluster_id": cluster["cluster_id"],
                "label": cluster["label"],
                "member_count": cluster["member_count"],
                "npmi_coherence": npmi,
            }
        )

    mean_coherence = sum(coherence_scores) / len(coherence_scores) if coherence_scores else 0.0
    max_share = float(base_metrics.get("max_cluster_share") or 0.0)
    mega_penalty = max(0.0, (max_share - 0.25) * 120.0) if max_share > 0.25 else 0.0
    cohesion_bonus = float(base_metrics.get("mean_cohesion") or 0.0) * 20.0
    entropy_penalty = min(30.0, topic_entropy * 4.0)
    raw = 70.0 + mean_coherence * 25.0 + cohesion_bonus - mega_penalty - entropy_penalty
    score = clamp_score(raw)

    if max_share > 0.4:
        interpretation = "One topic dominates your map — try re-clustering or limiting sources."
    elif score >= 70:
        interpretation = "Topics look well-separated with coherent labels."
    elif score >= 45:
        interpretation = "Topic structure is usable but some clusters may be noisy."
    else:
        interpretation = "Topic map quality is weak — enrichment may help."

    return {
        "score": round(score, 1),
        "interpretation": interpretation,
        "sub_metrics": {
            **base_metrics,
            "topic_entropy_bits": round(topic_entropy, 4),
            "mean_npmi_coherence": round(mean_coherence, 4),
        },
        "clusters": per_cluster[:50],
    }
