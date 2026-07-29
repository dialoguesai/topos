"""Collapse near-duplicate topic clusters into supertopics before any entropy.

The live node clusters the same corpus separately per signal dimension
(relationships / memory / network_bridge / wellbeing / interests), so the same
attention theme appears up to 8 times ("Personal Projects") and 612 records sit
in two dimensions. Entropy over the raw 125 clusters therefore measures the
pipeline's fan-out, not the person's breadth.

Two merge rules (live finding, loop 2): the per-dimension clusterings partition
DIFFERENT record subsets, so same-theme clusters usually share zero records.

  Rule A (content duplicates): combined similarity of centroid cosine
  (centroids computed from member record embeddings in signal_embeddings —
  topic_clusters.centroid_vector is NULL on live), member-record overlap,
  label-term overlap, and weekly co-activation >= threshold.

  Rule B (same theme, different records): identical normalized labels merge
  when weakly corroborated semantically (centroid cos >= 0.2 or co-activation
  >= 0.2; auto-pass when centroids are unavailable).

Merging is non-destructive: supertopics keep merged_from and dimension mix.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from .projection import MIN_SANE_TS, parse_ts

MERGE_THRESHOLD = 0.60
PARENT_THRESHOLD = 0.45
# Pair similarities are retained down to this floor so downstream consumers
# (influence topic-topic edges filter at >= 0.30) actually see them.
SIMILARITY_KEEP = 0.30
LABEL_MERGE_MIN_COS = 0.2
LABEL_MERGE_MIN_COACTIVATION = 0.2
LABEL_MERGE_CONFIDENCE = 0.8
# Candidate blocking: a pair gets a full similarity computation only when it
# shares members/terms/label or clears this centroid-cosine gate. With zero
# member and term overlap, sim = .45*cos + .15*coact, so cos >= 0.33 is the
# lowest cosine that can still reach SIMILARITY_KEEP.
BLOCK_MIN_COS = 0.33

W_EMBEDDING = 0.45
W_MEMBERS = 0.25
W_TERMS = 0.15
W_COACTIVATION = 0.15


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


@dataclass
class SuperTopic:
    supertopic_id: str
    label: str
    merged_from: List[str] = field(default_factory=list)
    labels_merged: List[str] = field(default_factory=list)
    dimensions: Dict[str, int] = field(default_factory=dict)
    record_ids: Set[str] = field(default_factory=set)
    label_terms: List[str] = field(default_factory=list)
    merge_confidence: float = 1.0
    parent_id: Optional[str] = None

    @property
    def member_count(self) -> int:
        return len(self.record_ids)

    @property
    def merged(self) -> bool:
        return len(self.merged_from) > 1


@dataclass
class SuperTopicIndex:
    supertopics: Dict[str, SuperTopic] = field(default_factory=dict)
    to_supertopic: Dict[str, str] = field(default_factory=dict)
    parents: Dict[str, List[str]] = field(default_factory=dict)
    raw_cluster_count: int = 0
    similarity: Dict[Tuple[str, str], float] = field(default_factory=dict)

    def resolve(self, cluster_id: str) -> Optional[str]:
        return self.to_supertopic.get(cluster_id)

    def label_of(self, supertopic_id: str) -> str:
        st = self.supertopics.get(supertopic_id)
        return st.label if st else supertopic_id

    @property
    def merge_rate(self) -> float:
        if self.raw_cluster_count == 0:
            return 0.0
        return 1.0 - (len(self.supertopics) / self.raw_cluster_count)

    def merged_groups(self, limit: int = 20) -> List[Dict[str, object]]:
        groups = [st for st in self.supertopics.values() if st.merged]
        groups.sort(key=lambda st: len(st.merged_from), reverse=True)
        return [
            {
                "supertopic_id": st.supertopic_id,
                "label": st.label,
                "merged_cluster_count": len(st.merged_from),
                "labels_merged": st.labels_merged[:8],
                "dimensions": st.dimensions,
                "record_count": st.member_count,
                "merge_confidence": st.merge_confidence,
            }
            for st in groups[:limit]
        ]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


def _cosine(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    if a is None or b is None:
        return None
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0:
        return None
    return float(np.dot(a, b) / denom)


def _series_cosine(a: List[int], b: List[int]) -> float:
    va, vb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom <= 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _load_record_vectors(conn: sqlite3.Connection, record_ids: Set[str]) -> Dict[str, np.ndarray]:
    if not record_ids or not _table_exists(conn, "signal_embeddings"):
        return {}
    from topos.features.signal.vector_codec import decode_vector

    out: Dict[str, np.ndarray] = {}
    try:
        # Iterate the cursor (no fetchall) and skip non-members before decode,
        # so peak memory stays bounded by the clustered records, not the table.
        cursor = conn.execute(
            """
            SELECT record_id, vector_blob, vector_format
            FROM signal_embeddings
            WHERE record_id IS NOT NULL AND vector_blob IS NOT NULL AND chunk_index = 0
            """
        )
    except sqlite3.OperationalError:
        # Pre-migration nodes lack vector_format/chunk_index columns; degrade
        # to no-embedding merging instead of failing every complexity handler.
        return {}
    for row in cursor:
        record_id = str(row["record_id"])
        if record_id not in record_ids or record_id in out:
            continue
        try:
            vec = np.asarray(
                decode_vector(row["vector_blob"], str(row["vector_format"] or "f32")),
                dtype=np.float32,
            )
        except Exception:
            continue
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            out[record_id] = vec / norm
    return out


def _weekly_bins(times: List[datetime], n_weeks: int = 26, end: Optional[datetime] = None) -> List[int]:
    end_dt = end or datetime.now(timezone.utc)
    series = [0] * n_weeks
    for dt in times:
        weeks_ago = int((end_dt - dt).total_seconds() // (7 * 86400))
        if 0 <= weeks_ago < n_weeks:
            series[n_weeks - 1 - weeks_ago] += 1
    return series


def pair_similarity(
    centroid_a: Optional[np.ndarray],
    centroid_b: Optional[np.ndarray],
    records_a: Set[str],
    records_b: Set[str],
    terms_a: Set[str],
    terms_b: Set[str],
    series_a: List[int],
    series_b: List[int],
) -> float:
    cos = _cosine(centroid_a, centroid_b)
    member_j = _jaccard(records_a, records_b)
    term_j = _jaccard(terms_a, terms_b)
    coact = _series_cosine(series_a, series_b)
    if cos is None:
        # redistribute the embedding weight over the remaining evidence
        rest = W_MEMBERS + W_TERMS + W_COACTIVATION
        return (W_MEMBERS * member_j + W_TERMS * term_j + W_COACTIVATION * coact) / rest
    return W_EMBEDDING * cos + W_MEMBERS * member_j + W_TERMS * term_j + W_COACTIVATION * coact


def build_supertopics(
    conn: sqlite3.Connection,
    *,
    threshold: float = MERGE_THRESHOLD,
    parent_threshold: float = PARENT_THRESHOLD,
    end: Optional[datetime] = None,
) -> SuperTopicIndex:
    index = SuperTopicIndex()
    if not _table_exists(conn, "topic_clusters"):
        return index

    clusters: Dict[str, Dict[str, object]] = {}
    for row in conn.execute(
        "SELECT cluster_id, label, dimension, member_count, label_terms_json FROM topic_clusters"
    ).fetchall():
        cluster_id = str(row["cluster_id"])
        try:
            terms = {str(t).lower() for t in json.loads(str(row["label_terms_json"] or "[]"))}
        except (json.JSONDecodeError, TypeError):
            terms = set()
        clusters[cluster_id] = {
            "label": str(row["label"] or cluster_id),
            "dimension": str(row["dimension"] or "unknown"),
            "member_count": int(row["member_count"] or 0),
            "terms": terms,
            "records": set(),
        }
    index.raw_cluster_count = len(clusters)
    if not clusters:
        return index

    all_records: Set[str] = set()
    if _table_exists(conn, "topic_cluster_members"):
        for row in conn.execute(
            "SELECT cluster_id, record_id FROM topic_cluster_members"
        ).fetchall():
            cluster_id = str(row["cluster_id"])
            if cluster_id in clusters:
                record_id = str(row["record_id"])
                clusters[cluster_id]["records"].add(record_id)
                all_records.add(record_id)

    record_times: Dict[str, datetime] = {}
    if _table_exists(conn, "timeline"):
        for row in conn.execute(
            """
            SELECT record_id, MAX(event_at) AS event_at
            FROM timeline WHERE event_at >= ? GROUP BY record_id
            """,
            (MIN_SANE_TS,),
        ).fetchall():
            dt = parse_ts(row["event_at"])
            if dt is not None:
                record_times[str(row["record_id"])] = dt

    vectors = _load_record_vectors(conn, all_records)
    centroids: Dict[str, Optional[np.ndarray]] = {}
    weekly: Dict[str, List[int]] = {}
    for cluster_id, cluster in clusters.items():
        records: Set[str] = cluster["records"]  # type: ignore[assignment]
        member_vecs = [vectors[r] for r in records if r in vectors]
        if member_vecs:
            centroid = np.mean(np.stack(member_vecs), axis=0)
            norm = float(np.linalg.norm(centroid))
            centroids[cluster_id] = centroid / norm if norm > 0 else None
        else:
            centroids[cluster_id] = None
        times = [record_times[r] for r in records if r in record_times]
        weekly[cluster_id] = _weekly_bins(times, end=end)

    def _normalize_label(label: str) -> str:
        import re

        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", label.lower())).strip()

    norm_labels = {cid: _normalize_label(str(clusters[cid]["label"])) for cid in clusters}

    ids = sorted(clusters.keys())

    # All-pairs centroid cosines in one vectorized pass; used both as a
    # similarity component and as the candidate-blocking gate (the plan's
    # blocking requirement — O(K^2) full scoring does not survive 10x growth).
    cos_matrix: Optional[np.ndarray] = None
    id_index = {cid: i for i, cid in enumerate(ids)}
    dense = [cid for cid in ids if centroids.get(cid) is not None]
    if dense:
        stacked = np.stack([centroids[cid] for cid in dense])
        dense_cos = stacked @ stacked.T
        cos_matrix = np.full((len(ids), len(ids)), np.nan, dtype=np.float32)
        for x, ca in enumerate(dense):
            for y, cb in enumerate(dense):
                cos_matrix[id_index[ca], id_index[cb]] = dense_cos[x, y]

    def pair_cos(a: str, b: str) -> Optional[float]:
        if cos_matrix is None:
            return None
        value = cos_matrix[id_index[a], id_index[b]]
        return None if np.isnan(value) else float(value)

    def blocked(a: str, b: str) -> bool:
        """True when the pair can be skipped without a full similarity pass."""
        if norm_labels[a] and norm_labels[a] == norm_labels[b]:
            return False
        if clusters[a]["records"] & clusters[b]["records"]:  # type: ignore[operator]
            return False
        if clusters[a]["terms"] & clusters[b]["terms"]:  # type: ignore[operator]
            return False
        cos = pair_cos(a, b)
        if cos is None:
            return False  # no embedding signal: cannot rule the pair out
        return cos < BLOCK_MIN_COS

    uf = _UnionFind()
    similarities: Dict[Tuple[str, str], float] = {}
    merge_confidences: Dict[Tuple[str, str], float] = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if blocked(a, b):
                continue
            sim = pair_similarity(
                centroids[a],
                centroids[b],
                clusters[a]["records"],  # type: ignore[arg-type]
                clusters[b]["records"],  # type: ignore[arg-type]
                clusters[a]["terms"],  # type: ignore[arg-type]
                clusters[b]["terms"],  # type: ignore[arg-type]
                weekly[a],
                weekly[b],
            )
            if sim >= SIMILARITY_KEEP:
                similarities[(a, b)] = sim
            if sim >= threshold:
                uf.union(a, b)
                merge_confidences[(a, b)] = sim
                continue
            # Rule B: same normalized label, weak semantic corroboration.
            if norm_labels[a] and norm_labels[a] == norm_labels[b]:
                cos = pair_cos(a, b)
                coact = _series_cosine(weekly[a], weekly[b])
                if (
                    cos is None
                    or cos >= LABEL_MERGE_MIN_COS
                    or coact >= LABEL_MERGE_MIN_COACTIVATION
                ):
                    uf.union(a, b)
                    merge_confidences[(a, b)] = max(sim, LABEL_MERGE_CONFIDENCE)
                    similarities.setdefault((a, b), max(sim, parent_threshold))

    groups: Dict[str, List[str]] = {}
    for cluster_id in ids:
        groups.setdefault(uf.find(cluster_id), []).append(cluster_id)

    for members in groups.values():
        members.sort(key=lambda c: (-int(clusters[c]["member_count"]), c))
        rep = members[0]
        supertopic = SuperTopic(
            supertopic_id=f"st_{rep}",
            label=str(clusters[rep]["label"]),
            merged_from=list(members),
            labels_merged=sorted({str(clusters[c]["label"]) for c in members}),
        )
        min_sim = 1.0
        for c in members:
            supertopic.record_ids |= clusters[c]["records"]  # type: ignore[arg-type]
            supertopic.label_terms = sorted(
                set(supertopic.label_terms) | set(clusters[c]["terms"])  # type: ignore[arg-type]
            )
            dim = str(clusters[c]["dimension"])
            supertopic.dimensions[dim] = supertopic.dimensions.get(dim, 0) + 1
        if len(members) > 1:
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    conf = merge_confidences.get((min(a, b), max(a, b)))
                    if conf is not None:
                        min_sim = min(min_sim, conf)
            supertopic.merge_confidence = round(min_sim, 4)
        index.supertopics[supertopic.supertopic_id] = supertopic
        for c in members:
            index.to_supertopic[c] = supertopic.supertopic_id

    # Parent themes: looser second pass over supertopics for hierarchy/breadth.
    parent_uf = _UnionFind()
    st_ids = sorted(index.supertopics.keys())
    for i, a in enumerate(st_ids):
        for b in st_ids[i + 1 :]:
            best = 0.0
            for ca in index.supertopics[a].merged_from:
                for cb in index.supertopics[b].merged_from:
                    sim = similarities.get((min(ca, cb), max(ca, cb)), 0.0)
                    best = max(best, sim)
            if best >= parent_threshold:
                parent_uf.union(a, b)
    parent_groups: Dict[str, List[str]] = {}
    for st_id in st_ids:
        parent_groups.setdefault(parent_uf.find(st_id), []).append(st_id)
    for members in parent_groups.values():
        members.sort(key=lambda s: -index.supertopics[s].member_count)
        parent_id = f"pt_{members[0]}"
        index.parents[parent_id] = members
        for st_id in members:
            index.supertopics[st_id].parent_id = parent_id

    index.similarity = similarities
    return index
