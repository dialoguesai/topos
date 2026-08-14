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
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("topos.features.signal.topic_clustering")

MVP_QUERY_SOURCE_IDS = (
    "chatgpt_file_ingestion",
    "chatgpt_ui_conversation",
    "browser_visits",
    "demo_browser_file",
    "demo_messenger_file",
)


def _resolved_topic_cluster_source_ids(source_ids: Optional[Sequence[str]] = None) -> Tuple[str, ...]:
    from ...sources.registry import topic_cluster_source_ids

    configured = topic_cluster_source_ids()
    if source_ids:
        requested = {str(s).strip() for s in source_ids if str(s).strip()}
        filtered = tuple(sid for sid in configured if sid in requested)
        if filtered:
            return filtered
    return configured or MVP_QUERY_SOURCE_IDS

_MIN_CLUSTER_RECORDS = 3
# Legacy fallback caps. The effective cap is cluster_max_k() (TOPOS_CLUSTER_MAX_K,
# default 64): the old hard cap of 6 per facet collapsed a 20k-record corpus
# into six mega-clusters, one holding 55% of all members.
_MAX_CLUSTERS = 12
_MAX_CLUSTERS_PER_FACET = 6
_KMEANS_ITERATIONS = 25
_MIN_MEANINGFUL_CLUSTER_SIZE = 3
_SIMILAR_CLUSTER_COSINE = 0.85
_ENTITY_RANK_BOOST = 2
_SEMANTIC_RANK_WEIGHT = 10.0


def _effective_max_k() -> int:
    from .vector_settings import cluster_max_k

    return cluster_max_k()

_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "about",
        "what",
        "when",
        "where",
        "which",
        "would",
        "could",
        "should",
        "there",
        "their",
        "them",
        "they",
        "your",
        "been",
        "being",
        "into",
        "over",
        "also",
        "just",
        "like",
        "some",
        "more",
        "most",
        "only",
        "other",
        "such",
        "than",
        "then",
        "very",
        "will",
        "task",
        # Web/URL artifacts — dominated live cluster labels ("https / good / here").
        "https",
        "http",
        "href",
        "html",
        "utm",
        # Low-signal conversational fillers over personal corpora.
        "good",
        "here",
        "people",
        "think",
        "know",
        "want",
        "going",
        "really",
        "said",
        "says",
        "yeah",
        "okay",
        "sure",
        "thanks",
        "thank",
        "message",
        "sent",
        "still",
        "much",
        "well",
        "make",
        "made",
        "back",
        "right",
        "need",
        "things",
        "thing",
    }
)


def _normalize_dimension_key(dimension: Optional[str]) -> str:
    raw = str(dimension or "memory").strip().lower()
    aliases = {
        "profile": "profile",
        "time": "time",
        "interests": "interests",
        "interest": "interests",
        "relationships": "relationships",
        "relationship": "relationships",
        "work": "work",
        "memory": "memory",
        "wellbeing": "wellbeing",
        "resources": "resources",
        "places": "places",
        "intentions": "intentions",
        "network_bridge": "network_bridge",
    }
    return aliases.get(raw, raw)


def _dimension_matches_scope(cluster_dimension: str, scope_dimensions: Sequence[str]) -> bool:
    cluster_key = _normalize_dimension_key(cluster_dimension)
    if cluster_key == "network_bridge":
        return True
    for scope_dim in scope_dimensions:
        if _normalize_dimension_key(scope_dim) == cluster_key:
            return True
    return False


def filter_clusters_by_dimensions(
    clusters: List[Dict[str, Any]],
    primary_dimensions: Optional[Sequence[str]],
) -> List[Dict[str, Any]]:
    if not primary_dimensions:
        return clusters
    return [
        cluster
        for cluster in clusters
        if _dimension_matches_scope(
            str(cluster.get("primary_dimension") or cluster.get("dimension") or ""),
            primary_dimensions,
        )
    ]


def _choose_k(n: int, requested: Optional[int] = None, *, max_clusters: int = _MAX_CLUSTERS) -> int:
    if requested is not None and requested > 0:
        return min(requested, n, max_clusters)
    if n < _MIN_CLUSTER_RECORDS:
        return 1
    return max(2, min(max_clusters, int(round(math.sqrt(n)))))


def _normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm <= 0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return -1.0
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _record_type_for_embedding(source_id: str, metadata: Dict[str, Any]) -> str:
    explicit = metadata.get("record_type")
    if explicit:
        return str(explicit)
    sid = str(source_id or "")
    if sid in ("browser_visits", "demo_browser_file"):
        return "activity_event"
    from ...sources.registry import REGISTRY

    defn = REGISTRY.get(sid)
    if defn and str(getattr(defn, "canonical_group_id", "") or "") == "journal":
        return "journal_entry"
    if sid in ("demo_messenger_file", "demo_email_file", "imessage", "signal"):
        return "conversation_message"
    return "ai_chat_message"


def load_embedding_records(
    conn,
    *,
    source_ids: Optional[Sequence[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Load embeddable rows from signal_embeddings for MVP query sources."""
    if conn is None:
        return []
    if limit is None:
        from .vector_settings import cluster_max_records

        limit = cluster_max_records()
    ids = list(_resolved_topic_cluster_source_ids(source_ids))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT embedding_id, record_id, source_id, signal_dimension, text_preview,
               vector_blob, provenance_json, vector_format, chunk_index
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
          AND source_id IN ({placeholders})
        ORDER BY record_id, chunk_index
        LIMIT ?
        """,
        (*ids, limit),
    ).fetchall()
    from .embed_context import is_derivable_content
    from .vector_codec import decode_vector, is_normalized
    from .vector_settings import cluster_collapse_chunks_enabled

    out: List[Dict[str, Any]] = []
    collapsed: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        blob = row[5]
        if not blob:
            continue
        # Serialization garbage (NSKeyedArchiver / streamtyped blobs) must not
        # cluster: on the live node it formed a 2k-member junk cluster.
        preview_text = str(row[4] or "")
        if preview_text and not is_derivable_content(preview_text):
            continue
        try:
            vector = decode_vector(blob, row[7] or "json")
        except Exception:
            continue
        if not isinstance(vector, list) or len(vector) < 2:
            continue
        if row[7] == "f32" and is_normalized(vector):
            normalized = vector
        else:
            normalized = _normalize(vector)
        metadata: Dict[str, Any] = {}
        try:
            if row[6]:
                parsed = json.loads(row[6])
                if isinstance(parsed, dict):
                    metadata = parsed
        except json.JSONDecodeError:
            pass
        record_type = _record_type_for_embedding(str(row[2]), metadata)
        item = {
            "embedding_id": row[0],
            "record_id": row[1],
            "source_id": row[2],
            "signal_dimension": row[3],
            "text_preview": row[4] or "",
            "vector": normalized,
            "record_type": record_type,
            "metadata": metadata,
            "chunk_index": int(row[8] or 0),
        }
        if cluster_collapse_chunks_enabled():
            record_key = str(row[1])
            existing = collapsed.get(record_key)
            if existing is None or item["chunk_index"] < int(existing.get("chunk_index") or 0):
                collapsed[record_key] = item
        else:
            out.append(item)
    return list(collapsed.values()) if cluster_collapse_chunks_enabled() else out


def _init_centroids(vectors: List[List[float]], k: int) -> List[List[float]]:
    if k >= len(vectors):
        return [list(v) for v in vectors]
    rng = random.Random(42)
    indices = rng.sample(range(len(vectors)), k)
    return [list(vectors[i]) for i in indices]


def _kmeans_assign_numpy(vectors: List[List[float]], centroids: List[List[float]], cluster_k: int) -> Optional[List[int]]:
    """Vectorized k-means (cosine via dot on normalized vectors); None if numpy absent."""
    try:
        import numpy as np
    except ImportError:
        return None
    X = np.asarray(vectors, dtype=np.float32)
    C = np.asarray(centroids, dtype=np.float32)
    assignments = np.zeros(len(vectors), dtype=np.int64)
    for _ in range(_KMEANS_ITERATIONS):
        new_assignments = np.argmax(X @ C.T, axis=1)
        if np.array_equal(new_assignments, assignments):
            break
        assignments = new_assignments
        for ci in range(cluster_k):
            members = X[assignments == ci]
            if len(members) == 0:
                C[ci] = X[ci % len(X)]
                continue
            mean = members.mean(axis=0)
            norm = np.linalg.norm(mean)
            C[ci] = mean / norm if norm > 0 else mean
    return [int(a) for a in assignments]


def cluster_embedding_records(
    records: List[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """K-means (cosine) over normalized embedding vectors (numpy when available)."""
    if not records:
        return []
    vectors = [rec["vector"] for rec in records]
    n = len(vectors)
    cluster_k = _choose_k(n, k, max_clusters=_effective_max_k())
    if cluster_k <= 1:
        return [_build_cluster("tc_single", records, cluster_index=0)]

    centroids = _init_centroids(vectors, cluster_k)

    assignments = _kmeans_assign_numpy(vectors, centroids, cluster_k)
    if assignments is None:
        # Pure-Python fallback (no numpy): fine for small corpora.
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


def _member_term_counts(members: List[Dict[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for member in members:
        cat = (member.get("metadata") or {}).get("url_category")
        if isinstance(cat, str) and cat.strip():
            counter[cat.strip().lower()] += 3
        text = str(member.get("text_preview") or "").lower()
        for token in re.findall(r"[a-z]{4,}", text):
            if token not in _STOPWORDS:
                counter[token] += 1
    return counter


def _extract_terms(members: List[Dict[str, Any]]) -> List[str]:
    return [term for term, _ in _member_term_counts(members).most_common(5)]


def _global_term_document_frequency(clusters: List[Dict[str, Any]]) -> Counter[str]:
    df: Counter[str] = Counter()
    for cluster in clusters:
        for term in _member_term_counts(cluster.get("members") or []):
            df[term] += 1
    return df


def _extract_distinctive_terms(
    members: List[Dict[str, Any]],
    global_df: Counter[str],
    n_clusters: int,
    *,
    top_n: int = 5,
) -> List[str]:
    counter = _member_term_counts(members)
    if not counter:
        return []
    denom = max(n_clusters, 1)
    scored: List[tuple[str, float]] = []
    for term, tf in counter.items():
        idf = math.log((denom + 1) / (global_df.get(term, 0) + 1)) + 1.0
        scored.append((term, tf * idf))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [term for term, _ in scored[:top_n]]


def _label_from_terms(terms: List[str]) -> str:
    if not terms:
        return "topic cluster"
    return " / ".join(terms[:3])


def label_cluster(members: List[Dict[str, Any]]) -> str:
    terms = _extract_terms(members)
    return _label_from_terms(terms) if terms else _fallback_label(members)


def _fallback_label(members: List[Dict[str, Any]]) -> str:
    previews = [str(m.get("text_preview") or "").strip() for m in members if m.get("text_preview")]
    if previews:
        return previews[0][:64]
    return "topic cluster"


def _cluster_centroid(members: List[Dict[str, Any]]) -> List[float]:
    vectors = [m.get("vector") for m in members if isinstance(m.get("vector"), list)]
    if not vectors:
        return []
    dim = len(vectors[0])
    mean = [sum(float(vec[i]) for vec in vectors) / len(vectors) for i in range(dim)]
    return _normalize(mean)


def _centroid_preview(members: List[Dict[str, Any]], centroid: Sequence[float]) -> str:
    if not members:
        return "topic cluster"
    if not centroid:
        previews = [str(m.get("text_preview") or "") for m in members if m.get("text_preview")]
        return previews[0][:120] if previews else "topic cluster"
    best_member = max(members, key=lambda m: _cosine(m.get("vector") or [], centroid))
    preview = str(best_member.get("text_preview") or "").strip()
    return preview[:120] if preview else _fallback_label(members)


def _facet_dimension(members: List[Dict[str, Any]]) -> str:
    dims = [
        _normalize_dimension_key(str(m.get("signal_dimension") or ""))
        for m in members
        if m.get("signal_dimension")
    ]
    if dims:
        return Counter(dims).most_common(1)[0][0]
    source_mix = Counter(str(m.get("source_id") or "") for m in members)
    if source_mix.get("browser_visits", 0) + source_mix.get("demo_browser_file", 0) > len(members) / 2:
        return "interests"
    return "memory"


def _apply_distinctive_labels(clusters: List[Dict[str, Any]]) -> None:
    global_df = _global_term_document_frequency(clusters)
    n_clusters = max(len(clusters), 1)
    for cluster in clusters:
        members = cluster.get("members") or []
        distinctive = _extract_distinctive_terms(members, global_df, n_clusters, top_n=5)
        if not distinctive:
            distinctive = _extract_terms(members)
        label_terms = distinctive[:3]
        extra_terms = [term for term in distinctive if term not in label_terms][:5]
        cluster["label"] = _label_from_terms(label_terms)
        cluster["label_terms"] = extra_terms if extra_terms else distinctive[3:]


def _disambiguate_labels(clusters: List[Dict[str, Any]]) -> None:
    """Make repeated term labels distinct — checking the result, not just the input.

    The member_count suffix is not unique: two 7-member clusters that both came
    out "hello" both became "hello (7)", and a live node carried exactly that
    pair. Every candidate is now checked against the labels already handed out,
    with the occurrence index as the suffix that cannot collide.
    """
    seen: Dict[str, int] = Counter()
    taken = {
        str(c.get("label") or "").lower()
        for c in clusters
        if int(c.get("member_count") or 0) >= _MIN_MEANINGFUL_CLUSTER_SIZE
    }
    for cluster in clusters:
        if int(cluster.get("member_count") or 0) < _MIN_MEANINGFUL_CLUSTER_SIZE:
            continue
        label = str(cluster.get("label") or "")
        seen[label] += 1
        if seen[label] > 1:
            suffix_terms = [
                term
                for term in (cluster.get("label_terms") or [])
                if term.lower() not in label.lower()
            ]
            candidates = [f"{label} ({term})" for term in suffix_terms[:1]]
            candidates.append(f"{label} ({cluster.get('member_count')})")
            candidates.append(f"{label} ({seen[label]})")
            for candidate in candidates:
                if candidate.lower() not in taken:
                    cluster["label"] = candidate
                    taken.add(candidate.lower())
                    break


def _merge_cluster_into(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    target_members = list(target.get("members") or [])
    seen = {(m.get("record_id"), m.get("source_id")) for m in target_members}
    for member in source.get("members") or []:
        key = (member.get("record_id"), member.get("source_id"))
        if key not in seen:
            target_members.append(member)
            seen.add(key)
    target["members"] = target_members
    target["member_count"] = len(target_members)
    target["source_mix"] = dict(
        Counter(str(m.get("source_id") or "") for m in target_members)
    )
    centroid = _cluster_centroid(target_members)
    target["centroid_vector"] = centroid
    target["centroid_preview"] = _centroid_preview(target_members, centroid)


def _merge_small_clusters(clusters: List[Dict[str, Any]], *, min_size: int = _MIN_MEANINGFUL_CLUSTER_SIZE) -> List[Dict[str, Any]]:
    if len(clusters) <= 1:
        return clusters
    large = [c for c in clusters if int(c.get("member_count") or 0) >= min_size]
    small = [c for c in clusters if int(c.get("member_count") or 0) < min_size]
    if not small:
        return clusters
    if not large:
        large = [small[0]]
        small = small[1:]
    for cluster in small:
        centroid = cluster.get("centroid_vector") or _cluster_centroid(cluster.get("members") or [])
        target = max(
            large,
            key=lambda candidate: _cosine(centroid, candidate.get("centroid_vector") or []),
        )
        _merge_cluster_into(target, cluster)
    return large


def _shared_label_terms(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    terms_a = {str(t).lower() for t in (a.get("label_terms") or []) + str(a.get("label") or "").split(" / ")}
    terms_b = {str(t).lower() for t in (b.get("label_terms") or []) + str(b.get("label") or "").split(" / ")}
    return len(terms_a & terms_b)


def _merge_similar_clusters(clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(clusters) <= 1:
        return clusters
    pool = list(clusters)
    changed = True
    while changed:
        changed = False
        next_pool: List[Dict[str, Any]] = []
        used: set[int] = set()
        for i, cluster in enumerate(pool):
            if i in used:
                continue
            current = cluster
            for j in range(i + 1, len(pool)):
                if j in used:
                    continue
                other = pool[j]
                sim = _cosine(
                    current.get("centroid_vector") or [],
                    other.get("centroid_vector") or [],
                )
                if sim >= _SIMILAR_CLUSTER_COSINE and _shared_label_terms(current, other) >= 2:
                    _merge_cluster_into(current, other)
                    used.add(j)
                    changed = True
            next_pool.append(current)
        pool = next_pool
    return pool


def _post_process_facet_clusters(
    clusters: List[Dict[str, Any]],
    *,
    merge_small: bool = True,
) -> List[Dict[str, Any]]:
    if not clusters:
        return []
    for cluster in clusters:
        members = cluster.get("members") or []
        centroid = _cluster_centroid(members)
        cluster["centroid_vector"] = centroid
        cluster["centroid_preview"] = _centroid_preview(members, centroid)
    if merge_small:
        clusters = _merge_small_clusters(clusters)
    return _merge_similar_clusters(clusters)


def _finalize_cluster_labels(clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not clusters:
        return []
    _apply_distinctive_labels(clusters)
    _disambiguate_labels(clusters)
    return clusters


def cluster_records_by_facet(
    records: List[Dict[str, Any]],
    *,
    k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Cluster embeddings within each signal dimension facet."""
    if not records:
        return []
    by_facet: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        facet = _facet_dimension([record])
        by_facet.setdefault(facet, []).append(record)

    explicit_k = k is not None and k > 0
    all_clusters: List[Dict[str, Any]] = []
    for facet, facet_records in sorted(by_facet.items()):
        if len(facet_records) < _MIN_CLUSTER_RECORDS and len(by_facet) > 1:
            continue
        max_k = _effective_max_k()
        facet_k = k
        if facet_k is None:
            facet_k = _choose_k(len(facet_records), max_clusters=max_k)
        else:
            facet_k = min(facet_k, max_k, len(facet_records))
        facet_clusters = cluster_embedding_records(facet_records, k=facet_k)
        for cluster in facet_clusters:
            cluster["dimension"] = facet
            cluster["primary_dimension"] = facet
        facet_clusters = _post_process_facet_clusters(facet_clusters, merge_small=not explicit_k)
        all_clusters.extend(facet_clusters)

    if not all_clusters:
        all_clusters = cluster_embedding_records(records, k=k)
        for cluster in all_clusters:
            facet = _facet_dimension(cluster.get("members") or [])
            cluster["dimension"] = facet
            cluster["primary_dimension"] = facet
        all_clusters = _post_process_facet_clusters(all_clusters, merge_small=not explicit_k)

    return _finalize_cluster_labels(all_clusters)


def _build_cluster(cluster_id: str, members: List[Dict[str, Any]], *, cluster_index: int) -> Dict[str, Any]:
    source_mix = Counter(str(m.get("source_id") or "") for m in members)
    dimension = _facet_dimension(members)
    centroid = _cluster_centroid(members)
    label = label_cluster(members)
    return {
        "cluster_id": cluster_id,
        "label": label,
        "dimension": dimension,
        "primary_dimension": dimension,
        "member_count": len(members),
        "source_mix": dict(source_mix),
        "label_terms": _extract_terms(members),
        "centroid_preview": _centroid_preview(members, centroid),
        "centroid_vector": centroid,
        "members": [
            {
                "record_id": m.get("record_id"),
                "source_id": m.get("source_id"),
                "record_type": m.get("record_type") or "unknown",
                "text_preview": m.get("text_preview"),
                "weight": 1.0,
                "metadata": m.get("metadata") or {},
                "vector": m.get("vector"),
                "signal_dimension": m.get("signal_dimension"),
            }
            for m in members
        ],
        "cluster_index": cluster_index,
        "metadata": {},
    }


def _load_related_entities(conn, members: List[Dict[str, Any]]) -> List[str]:
    if conn is None or not members:
        return []
    entity_counter: Counter[str] = Counter()
    for member in members:
        record_id = member.get("record_id")
        source_id = member.get("source_id")
        if not record_id:
            continue
        rows = conn.execute(
            """
            SELECT entity_text FROM message_entities
            WHERE record_id=? AND (source_id=? OR source_id IS NULL OR ? IS NULL)
            """,
            (record_id, source_id, source_id),
        ).fetchall()
        for row in rows:
            text = str(row[0] or "").strip()
            if text:
                entity_counter[text] += 1
    return [name for name, _ in entity_counter.most_common(10)]


def _load_related_goals(conn, distinctive_terms: Sequence[str]) -> List[str]:
    if conn is None or not distinctive_terms:
        return []
    term_set = {str(term).lower() for term in distinctive_terms}
    rows = conn.execute(
        "SELECT goal_text FROM user_goals WHERE goal_text IS NOT NULL LIMIT 200"
    ).fetchall()
    matches: List[str] = []
    for row in rows:
        goal = str(row[0] or "").strip()
        if not goal:
            continue
        lowered = goal.lower()
        if any(term in lowered for term in term_set):
            matches.append(goal)
        if len(matches) >= 5:
            break
    return matches


def _classify_opportunity_type(
    cluster: Dict[str, Any],
    related_entities: Sequence[str],
    related_goals: Sequence[str],
) -> str:
    if str(cluster.get("primary_dimension") or cluster.get("dimension")) == "network_bridge":
        return "network_bridge"
    if related_entities and len(related_entities) >= 2:
        return "network_bridge"
    if related_goals:
        return "intent"
    dimension = _normalize_dimension_key(str(cluster.get("primary_dimension") or cluster.get("dimension")))
    if dimension in {"work", "memory"}:
        return "expertise"
    if dimension == "interests":
        return "interest"
    return "expertise"


def _enrich_cluster_metadata(conn, cluster: Dict[str, Any]) -> Dict[str, Any]:
    members = cluster.get("members") or []
    distinctive = list(cluster.get("label_terms") or []) + str(cluster.get("label") or "").split(" / ")
    related_entities = _load_related_entities(conn, members)
    related_goals = _load_related_goals(conn, distinctive)
    opportunity_type = _classify_opportunity_type(cluster, related_entities, related_goals)
    query_aliases = list(
        dict.fromkeys(
            [str(term).lower() for term in distinctive if term]
            + [entity.lower() for entity in related_entities]
        )
    )[:15]
    metadata = {
        "related_entities": related_entities,
        "related_goals": related_goals,
        "opportunity_type": opportunity_type,
        "query_aliases": query_aliases,
        "primary_dimension": cluster.get("primary_dimension") or cluster.get("dimension"),
    }
    centroid = cluster.get("centroid_vector")
    if isinstance(centroid, list):
        metadata["centroid_vector"] = centroid
    cluster["metadata"] = metadata
    return metadata


def _detect_bridge_clusters(clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    facet_clusters = [c for c in clusters if c.get("primary_dimension") != "network_bridge"]
    term_map: Dict[str, List[Dict[str, Any]]] = {}
    for cluster in facet_clusters:
        for term in (cluster.get("label_terms") or [])[:3]:
            term_map.setdefault(str(term).lower(), []).append(cluster)

    bridges: List[Dict[str, Any]] = []
    for term, group in term_map.items():
        facets = {_normalize_dimension_key(str(c.get("primary_dimension") or "")) for c in group}
        if len(facets) < 2:
            continue
        entities: Counter[str] = Counter()
        members: List[Dict[str, Any]] = []
        seen_members: set[tuple[Any, Any]] = set()
        for cluster in group:
            for entity in (cluster.get("metadata") or {}).get("related_entities") or []:
                entities[entity] += 1
            for member in cluster.get("members") or []:
                key = (member.get("record_id"), member.get("source_id"))
                if key not in seen_members:
                    members.append(member)
                    seen_members.add(key)
        if not entities and len(group) < 2:
            continue
        # Deterministic from the bridge term so the ID survives recomputes.
        import hashlib

        bridge_id = f"tc_bridge_{hashlib.sha1(term.encode('utf-8')).hexdigest()[:12]}"
        bridge = _build_cluster(bridge_id, members, cluster_index=len(clusters) + len(bridges))
        bridge["dimension"] = "network_bridge"
        bridge["primary_dimension"] = "network_bridge"
        bridge["label"] = f"{term} / bridge"
        bridge["label_terms"] = [term] + [e for e, _ in entities.most_common(4)]
        bridge["metadata"] = {
            "related_entities": [e for e, _ in entities.most_common(10)],
            "related_goals": [],
            "opportunity_type": "network_bridge",
            "query_aliases": [term] + [e.lower() for e in entities.keys()],
            "primary_dimension": "network_bridge",
            "bridge_term": term,
            "source_cluster_ids": [c.get("cluster_id") for c in group],
        }
        bridges.append(bridge)
    return facet_clusters + bridges


def _enrich_all_clusters(conn, clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for cluster in clusters:
        _enrich_cluster_metadata(conn, cluster)
    return _detect_bridge_clusters(clusters)


_TOP_TOPICS_FACT_PREFIX = "top_topics:"


def _clear_all_topic_clusters(conn) -> None:
    """Remove every topic cluster snapshot row (full replace before recompute)."""
    if conn is None:
        return
    conn.execute("DELETE FROM topic_cluster_members")
    conn.execute("DELETE FROM topic_clusters")


def _top_topics_fact_id(cluster_id: str) -> str:
    return f"{_TOP_TOPICS_FACT_PREFIX}{cluster_id}"


def _prune_stale_top_topics_facts(conn, active_cluster_ids: Sequence[str]) -> int:
    """Drop top_topics signal facts whose cluster_id is no longer in the active set."""
    if conn is None:
        return 0
    active = {str(cid) for cid in active_cluster_ids if cid}
    rows = conn.execute(
        "SELECT fact_id, record_id FROM signal_facts WHERE fact_id LIKE ?",
        (f"{_TOP_TOPICS_FACT_PREFIX}%",),
    ).fetchall()
    pruned = 0
    for fact_id, record_id in rows:
        if str(record_id or "") not in active:
            conn.execute("DELETE FROM signal_facts WHERE fact_id=?", (fact_id,))
            pruned += 1
    return pruned


def persist_topic_clusters(
    conn,
    clusters: List[Dict[str, Any]],
    *,
    sync_batch_id: Optional[str] = None,
    model: str = "kmeans_cosine_v1",
    provider: str = "topos",
    commit: bool = True,
) -> Dict[str, int]:
    if conn is None or not clusters:
        return {"clusters_written": 0, "members_written": 0}

    from ...storage.db.write_gate import commit_connection, with_db_write

    # Reentrant: the full-recompute caller already holds the gate around this
    # whole swap (commit=False); a commit=True caller gets the same discipline.
    with with_db_write():
        result = _persist_topic_clusters_gated(
            conn,
            clusters,
            sync_batch_id=sync_batch_id,
            model=model,
            provider=provider,
        )
        if commit:
            commit_connection(conn)
    return result


def _persist_topic_clusters_gated(
    conn,
    clusters: List[Dict[str, Any]],
    *,
    sync_batch_id: Optional[str],
    model: str,
    provider: str,
) -> Dict[str, int]:
    """Write phase of persist_topic_clusters. Caller holds the write gate."""
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
    has_metadata_col = _table_has_column(conn, "topic_clusters", "metadata_json")
    has_centroid_col = _table_has_column(conn, "topic_clusters", "centroid_vector")
    for cluster in clusters:
        cid = cluster["cluster_id"]
        metadata = cluster.get("metadata") or {}
        if cluster.get("centroid_vector") and "centroid_vector" not in metadata:
            metadata = {**metadata, "centroid_vector": cluster.get("centroid_vector")}
        centroid_blob = None
        if has_centroid_col and isinstance(cluster.get("centroid_vector"), list):
            from .vector_codec import encode_f32

            centroid_blob = encode_f32(cluster["centroid_vector"])
        if has_metadata_col:
            if has_centroid_col:
                conn.execute(
                    """
                    INSERT INTO topic_clusters (
                        cluster_id, label, dimension, member_count, source_mix_json,
                        label_terms_json, centroid_preview, model, provider, sync_batch_id,
                        metadata_json, centroid_vector, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        cid,
                        cluster.get("label") or "topic cluster",
                        cluster.get("primary_dimension") or cluster.get("dimension") or "memory",
                        int(cluster.get("member_count") or 0),
                        json.dumps(cluster.get("source_mix") or {}),
                        json.dumps(cluster.get("label_terms") or []),
                        cluster.get("centroid_preview"),
                        model,
                        provider,
                        sync_batch_id,
                        json.dumps(metadata),
                        centroid_blob,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO topic_clusters (
                        cluster_id, label, dimension, member_count, source_mix_json,
                        label_terms_json, centroid_preview, model, provider, sync_batch_id,
                        metadata_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        cid,
                        cluster.get("label") or "topic cluster",
                        cluster.get("primary_dimension") or cluster.get("dimension") or "memory",
                        int(cluster.get("member_count") or 0),
                        json.dumps(cluster.get("source_mix") or {}),
                        json.dumps(cluster.get("label_terms") or []),
                        cluster.get("centroid_preview"),
                        model,
                        provider,
                        sync_batch_id,
                        json.dumps(metadata),
                    ),
                )
        elif has_centroid_col:
            conn.execute(
                """
                INSERT INTO topic_clusters (
                    cluster_id, label, dimension, member_count, source_mix_json,
                    label_terms_json, centroid_preview, model, provider, sync_batch_id,
                    centroid_vector, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    cid,
                    cluster.get("label") or "topic cluster",
                    cluster.get("primary_dimension") or cluster.get("dimension") or "memory",
                    int(cluster.get("member_count") or 0),
                    json.dumps(cluster.get("source_mix") or {}),
                    json.dumps(cluster.get("label_terms") or []),
                    cluster.get("centroid_preview"),
                    model,
                    provider,
                    sync_batch_id,
                    centroid_blob,
                ),
            )
        else:
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
                    cluster.get("primary_dimension") or cluster.get("dimension") or "memory",
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
    return {"clusters_written": clusters_written, "members_written": members_written}


def _table_has_column(conn, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return False
    return any(str(row[1]) == column for row in rows)


def _strip_member_vectors(clusters: List[Dict[str, Any]]) -> None:
    for cluster in clusters:
        cluster.pop("centroid_vector", None)
        for member in cluster.get("members") or []:
            member.pop("vector", None)


def write_top_topics_signal_facts(
    adapters,
    conn,
    *,
    limit: int = 20,
) -> int:
    """Promote cluster rollups into signal_facts as wiki ``top_topics`` objects."""
    if conn is None:
        return 0
    has_metadata_col = _table_has_column(conn, "topic_clusters", "metadata_json")
    if has_metadata_col:
        rows = conn.execute(
            """
            SELECT cluster_id, label, dimension, member_count, source_mix_json,
                   label_terms_json, metadata_json
            FROM topic_clusters
            ORDER BY member_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT cluster_id, label, dimension, member_count, source_mix_json, label_terms_json
            FROM topic_clusters
            ORDER BY member_count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    active_cluster_ids = [str(row[0]) for row in rows]
    _prune_stale_top_topics_facts(conn, active_cluster_ids)

    from ..entities.fact_materializer import _cluster_activity_window

    written = 0
    for row in rows:
        cluster_id = str(row[0])
        source_mix = json.loads(row[4] or "{}")
        primary_source = max(source_mix, key=source_mix.get) if source_mix else "cross_source"
        metadata: Dict[str, Any] = {}
        if has_metadata_col and len(row) > 6 and row[6]:
            try:
                parsed = json.loads(row[6])
                if isinstance(parsed, dict):
                    metadata = parsed
            except json.JSONDecodeError:
                pass
        # Member activity window (same clock as discusses edges / goals) so
        # signal_objects period columns stay retrieval-aligned with the graph.
        period_start, period_end = _cluster_activity_window(conn, cluster_id)
        fact: Dict[str, Any] = {
            "fact_id": _top_topics_fact_id(cluster_id),
            "dimension": row[2] or "memory",
            "source_id": primary_source,
            "record_id": cluster_id,
            "tag": row[1],
            "confidence": min(1.0, float(row[3] or 1) / 10.0),
            "object_type": "top_topics",
            "member_count": row[3],
            "label_terms": json.loads(row[5] or "[]"),
            "source_mix": source_mix,
            "opportunity_type": metadata.get("opportunity_type"),
            "related_entities": metadata.get("related_entities") or [],
            "related_goals": metadata.get("related_goals") or [],
        }
        if period_start:
            fact["period_start"] = period_start
        if period_end:
            fact["period_end"] = period_end
        adapters.signal.put_fact(fact)
        written += 1
    return written


_MAX_SHARE_ALERT = 0.25


def compute_cluster_metrics(
    clusters: List[Dict[str, Any]],
    *,
    records_loaded: int,
) -> Dict[str, Any]:
    """Snapshot quality metrics logged with every recompute.

    The live mega-cluster collapse (one cluster holding 55% of members) was
    only visible by manual inspection; max_cluster_share in the recompute log
    makes it an alert instead.
    """
    sizes = sorted((int(c.get("member_count") or 0) for c in clusters), reverse=True)
    total = sum(sizes)
    metrics: Dict[str, Any] = {
        "clusters": len(clusters),
        "records_loaded": records_loaded,
        "max_cluster_share": round(sizes[0] / total, 4) if total else 0.0,
        "median_cluster_size": sizes[len(sizes) // 2] if sizes else 0,
    }
    cohesion = _weighted_cohesion(clusters)
    if cohesion is not None:
        metrics["mean_cohesion"] = round(cohesion, 4)
    if metrics["max_cluster_share"] > _MAX_SHARE_ALERT and len(clusters) > 2:
        logger.warning(
            "Cluster quality alert: one cluster holds %.0f%% of %d members",
            metrics["max_cluster_share"] * 100,
            total,
        )
    return metrics


def _weighted_cohesion(clusters: List[Dict[str, Any]]) -> Optional[float]:
    """Member-weighted mean cosine to own centroid (needs member vectors)."""
    try:
        import numpy as np
    except ImportError:
        return None
    weighted_sum = 0.0
    weighted_n = 0
    for cluster in clusters:
        centroid = cluster.get("centroid_vector")
        members = cluster.get("members") or []
        vectors = [m.get("vector") for m in members if isinstance(m.get("vector"), list)]
        if not centroid or not vectors:
            continue
        X = np.asarray(vectors, dtype=np.float32)
        c = np.asarray(centroid, dtype=np.float32)
        weighted_sum += float((X @ c).sum())
        weighted_n += len(vectors)
    if not weighted_n:
        return None
    return weighted_sum / weighted_n


def _rebuild_context_centroids(conn) -> Dict[str, Any]:
    """Refresh person mention-context centroids on the cluster rebuild's cadence.

    PLAN_GRAPH_QUERY_AND_LATENT_EDGES D2: nightly, alongside this rebuild, not
    per enrichment pass — a centroid is a whole-corpus average, so recomputing
    it per batch is cost without movement. It shares the cadence only; it does
    not depend on the clusters, which is why it also runs when clustering is
    skipped for want of records. Non-fatal: the clusters are the job.
    """
    try:
        from ..entities.context_vectors import rebuild_entity_context_vectors

        return rebuild_entity_context_vectors(conn)
    except Exception as exc:  # noqa: BLE001 — a derived rider must not fail the rebuild
        logger.warning("entity context centroid rebuild skipped: %s", exc)
        return {"status": "error", "error": str(exc)}


def _rebuild_affinity_edges(conn) -> Dict[str, Any]:
    """Recompute latent affinity edges from the centroids just rebuilt.

    Same nightly cadence (D2) and same non-fatal contract as the centroids, but
    strictly after them: an affinity edge is a snapshot of the current centroid
    set, so running it against yesterday's centroids would write a set that
    matches neither night.
    """
    try:
        from ..entities.affinity import rebuild_affinity_edges

        return rebuild_affinity_edges(conn)
    except Exception as exc:  # noqa: BLE001 — a derived rider must not fail the rebuild
        logger.warning("semantic affinity edge rebuild skipped: %s", exc)
        return {"status": "error", "error": str(exc)}


def _rebuild_affinity_lane(conn) -> Dict[str, Any]:
    """The centroid build and the affinity rebuild that reads it, in order."""
    centroids = _rebuild_context_centroids(conn)
    return {"context_centroids": centroids, "affinity_edges": _rebuild_affinity_edges(conn)}


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
            **_rebuild_affinity_lane(conn),
        }
    clusters = cluster_records_by_facet(records, k=k)
    clusters = _enrich_all_clusters(conn, clusters)

    metrics = compute_cluster_metrics(clusters, records_loaded=len(records))

    from .cluster_labels import apply_llm_cluster_labels

    metrics["llm_labels"] = apply_llm_cluster_labels(clusters)

    # Stable identities: rename new clusters to matched old IDs (centroid
    # cosine, greedy) so top_topics facts / UI links / embedding cluster_id
    # columns survive the recompute instead of churning every batch.
    from .incremental_clustering import (
        clear_candidate_pool,
        load_cluster_centroids,
        load_cluster_member_map,
        log_recompute,
        match_stable_cluster_ids,
        sync_member_cluster_ids,
    )

    old_clusters = load_cluster_centroids(conn)
    ids_preserved = match_stable_cluster_ids(
        old_clusters, clusters, old_members=load_cluster_member_map(conn)
    )

    _strip_member_vectors(clusters)
    from ...storage.db.write_gate import begin_immediate, sqlite_retry_busy, with_db_write

    with with_db_write():
        try:
            begin_immediate(conn)
            _clear_all_topic_clusters(conn)
            persist_result = persist_topic_clusters(
                conn,
                clusters,
                sync_batch_id=sync_batch_id,
                commit=False,
            )
            clear_candidate_pool(conn)
            log_recompute(
                conn,
                kind="full",
                records_processed=len(records),
                clusters_written=int(persist_result.get("clusters_written") or 0),
                ids_preserved=ids_preserved,
                metrics=metrics,
            )
            sync_member_cluster_ids(conn)
            sqlite_retry_busy(conn.commit)
        except Exception:
            conn.rollback()
            raise
    return {
        "status": "completed",
        "records_loaded": len(records),
        **persist_result,
        "ids_preserved": ids_preserved,
        "cluster_labels": [c.get("label") for c in clusters],
        "metrics": metrics,
        **_rebuild_affinity_lane(conn),
    }


def _cluster_row_to_dict(row: Sequence[Any], *, has_metadata: bool) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    if has_metadata and len(row) > 7 and row[7]:
        try:
            parsed = json.loads(row[7])
            if isinstance(parsed, dict):
                metadata = parsed
        except json.JSONDecodeError:
            pass
    dimension = row[2]
    return {
        "cluster_id": row[0],
        "label": row[1],
        "dimension": dimension,
        "primary_dimension": metadata.get("primary_dimension") or dimension,
        "member_count": row[3],
        "source_mix": json.loads(row[4] or "{}"),
        "label_terms": json.loads(row[5] or "[]"),
        "centroid_preview": row[6],
        "metadata": metadata,
        "related_entities": metadata.get("related_entities") or [],
        "related_goals": metadata.get("related_goals") or [],
        "opportunity_type": metadata.get("opportunity_type"),
        "query_aliases": metadata.get("query_aliases") or [],
        "centroid_vector": metadata.get("centroid_vector"),
        "object_type": "top_topics",
    }


def load_clusters_with_members(
    conn,
    *,
    limit: Optional[int] = None,
    dimensions: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Existing clusters plus their member rows — no clustering, no vectors.

    The relabel path (and the labeling evaluation) needs member text and
    centroids for clusters that already exist. Re-running k-means to get them
    would hand back a different partition (seed-to-seed ARI ~0.52 on the live
    corpus), which is precisely what must NOT move when only the prompt does.
    Ordered by member_count then cluster_id so a relabel pass is repeatable.
    """
    if conn is None:
        return []
    has_metadata_col = _table_has_column(conn, "topic_clusters", "metadata_json")
    columns = (
        "cluster_id, label, dimension, member_count, source_mix_json, "
        "label_terms_json, centroid_preview"
    )
    if has_metadata_col:
        columns += ", metadata_json"
    query = f"SELECT {columns} FROM topic_clusters"  # noqa: S608 — fixed column list
    params: List[Any] = []
    wanted = [_normalize_dimension_key(d) for d in (dimensions or []) if str(d or "").strip()]
    if wanted:
        query += f" WHERE dimension IN ({','.join('?' for _ in wanted)})"
        params.extend(wanted)
    query += " ORDER BY member_count DESC, cluster_id ASC"
    if limit and int(limit) > 0:
        query += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(query, tuple(params)).fetchall()
    clusters = [_cluster_row_to_dict(row, has_metadata=has_metadata_col) for row in rows]
    by_id = {str(c["cluster_id"]): c for c in clusters}
    for cluster in clusters:
        cluster["members"] = []
    if not by_id:
        return clusters

    ids = list(by_id)
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        member_rows = conn.execute(
            f"""
            SELECT cluster_id, record_id, source_id, record_type, text_preview, metadata_json
            FROM topic_cluster_members
            WHERE cluster_id IN ({','.join('?' for _ in chunk)})
            ORDER BY cluster_id, member_id
            """,  # noqa: S608 — placeholders only
            tuple(chunk),
        ).fetchall()
        for cluster_id, record_id, source_id, record_type, text_preview, metadata_json in member_rows:
            cluster = by_id.get(str(cluster_id))
            if cluster is None:
                continue
            try:
                metadata = json.loads(metadata_json or "{}")
            except json.JSONDecodeError:
                metadata = {}
            cluster["members"].append(
                {
                    "record_id": record_id,
                    "source_id": source_id,
                    "record_type": record_type or "unknown",
                    "text_preview": text_preview,
                    "metadata": metadata if isinstance(metadata, dict) else {},
                }
            )
    return clusters


def relabel_existing_clusters(
    conn,
    *,
    complete=None,
    mode: Optional[str] = None,
    contrastive: Optional[bool] = None,
    limit: Optional[int] = None,
    dimensions: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Re-run the labeler over the clusters already on disk, in place.

    Cluster membership, ids and centroids are untouched — only ``label`` and
    the label metadata move, so top_topics facts, UI links and embedding
    cluster_id columns all survive. This is what a released prompt change owes
    existing nodes: a full recompute would relabel too, but it would also
    reshuffle every cluster in the process.
    """
    clusters = load_clusters_with_members(conn, limit=limit, dimensions=dimensions)
    if not clusters:
        return {"status": "skipped", "reason": "no_clusters", "clusters": 0, "relabeled": 0}
    before = [str(c.get("label") or "") for c in clusters]

    from .cluster_labels import apply_llm_cluster_labels

    relabeled = apply_llm_cluster_labels(
        clusters, complete=complete, mode=mode, contrastive=contrastive
    )
    changed = [
        {"cluster_id": c["cluster_id"], "before": old, "after": str(c.get("label") or "")}
        for c, old in zip(clusters, before)
        if str(c.get("label") or "") != old
    ]
    if dry_run or not changed:
        return {
            "status": "completed",
            "clusters": len(clusters),
            "relabeled": relabeled,
            "changed": len(changed),
            "dry_run": bool(dry_run),
        }

    from ...storage.db.write_gate import begin_immediate, sqlite_retry_busy, with_db_write

    changed_ids = {entry["cluster_id"] for entry in changed}
    with with_db_write():
        try:
            begin_immediate(conn)
            for cluster in clusters:
                if cluster.get("cluster_id") not in changed_ids:
                    continue
                conn.execute(
                    """
                    UPDATE topic_clusters
                       SET label=?, metadata_json=?, updated_at=datetime('now')
                     WHERE cluster_id=?
                    """,
                    (
                        str(cluster.get("label") or ""),
                        json.dumps(cluster.get("metadata") or {}),
                        str(cluster.get("cluster_id")),
                    ),
                )
            sqlite_retry_busy(conn.commit)
        except Exception:
            conn.rollback()
            raise
    return {
        "status": "completed",
        "clusters": len(clusters),
        "relabeled": relabeled,
        "changed": len(changed),
        "dry_run": False,
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
    has_metadata_col = _table_has_column(conn, "topic_clusters", "metadata_json")
    if has_metadata_col:
        query = """
            SELECT cluster_id, label, dimension, member_count, source_mix_json,
                   label_terms_json, centroid_preview, metadata_json
            FROM topic_clusters
        """
    else:
        query = """
            SELECT cluster_id, label, dimension, member_count, source_mix_json,
                   label_terms_json, centroid_preview
            FROM topic_clusters
        """
    if dimension:
        query += " WHERE dimension=?"
        params.append(_normalize_dimension_key(dimension))
    query += " ORDER BY member_count DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, tuple(params)).fetchall()
    return [_cluster_row_to_dict(row, has_metadata=has_metadata_col) for row in rows]


def embed_query_text_for_ranking(query_text: str) -> Optional[List[float]]:
    """Embed query text for cluster centroid similarity (falls back gracefully)."""
    q = str(query_text or "").strip()
    if not q:
        return None
    try:
        from ...engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model
        from .query_embed_cache import get_cached_query_embedding, set_cached_query_embedding

        model = active_embedding_model()
        cached = get_cached_query_embedding(q, model=model)
        if cached is not None:
            return _normalize(cached)
        hf = HuggingFaceAdapter()
        emb = hf.run_inference(
            {"text": q}, {"subtype": "embedding", "model": model, "input_role": "query"}
        )
        vectors = emb.get("vectors") or []
        if not vectors:
            return None
        vector = _normalize([float(x) for x in vectors[0]])
        set_cached_query_embedding(q, model=model, vector=vector)
        return vector
    except Exception as exc:
        logger.debug("query embedding for cluster rank skipped: %s", exc)
        return None


def supplemental_label_terms(cluster: Dict[str, Any]) -> List[str]:
    """Terms in label_terms that do not already appear in the cluster label."""
    label = str(cluster.get("label") or "").lower()
    return [
        term
        for term in (cluster.get("label_terms") or [])
        if str(term).lower() not in label
    ]


def _query_tokens(query_text: str) -> List[str]:
    return list(dict.fromkeys(re.findall(r"[a-z0-9]{3,}", (query_text or "").lower())))


def rank_topic_clusters_for_query(
    clusters: List[Dict[str, Any]],
    query_text: str,
    *,
    limit: int = 5,
    query_vector: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    """Rank clusters by query overlap, optional semantic similarity, and entity boost."""
    tokens = _query_tokens(query_text)
    if not clusters:
        return []

    def cluster_score(cluster: Dict[str, Any]) -> tuple[float, int]:
        metadata = cluster.get("metadata") or {}
        aliases = metadata.get("query_aliases") or cluster.get("label_terms") or []
        label = str(cluster.get("label") or "").lower()
        terms = [str(term).lower() for term in aliases]
        blob = f"{label} {' '.join(terms)}"
        term_overlap = sum(1 for token in tokens if token in blob) if tokens else 0
        entity_overlap = 0
        for entity in metadata.get("related_entities") or cluster.get("related_entities") or []:
            entity_lower = str(entity).lower()
            if any(token in entity_lower or entity_lower in token for token in tokens):
                entity_overlap += _ENTITY_RANK_BOOST
        semantic_score = 0.0
        if query_vector is not None:
            centroid = cluster.get("centroid_vector")
            if not centroid:
                centroid = (metadata or {}).get("centroid_vector")
            if isinstance(centroid, list):
                semantic_score = max(0.0, _cosine(query_vector, centroid)) * _SEMANTIC_RANK_WEIGHT
        score = float(term_overlap + entity_overlap) + semantic_score
        return (score, int(cluster.get("member_count") or 0))

    ranked = sorted(clusters, key=cluster_score, reverse=True)
    token_count = len(tokens) or 1
    out: List[Dict[str, Any]] = []
    for cluster in ranked[: max(1, limit)]:
        score, _ = cluster_score(cluster)
        term_component = min(1.0, score / max(token_count + _ENTITY_RANK_BOOST, 1))
        out.append(
            {
                **cluster,
                "relevance_score": round(term_component, 4),
                "cluster_rank_strategy": "hybrid" if query_vector is not None else "term_entity",
            }
        )
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
