"""Focus Index v2: four readings computed on analytical projections.

Replaces the single saturated score with:
  current_focus        concentration in the selected window (normalized entropy)
  information_breadth  effective topic count, active entities by type, source diversity
  pipeline_confidence  how trustworthy the derivation is
plus a personal baseline: the score is compared against the user's own trailing
windows, not a universal ceiling. structural_clarity lives in graph_metrics.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .canonical import CanonicalIndex
from .entropy import clamp_score, effective_count, entropy_bits, normalized_entropy
from .projection import (
    AnalyticalProjection,
    EvidenceCache,
    GENERIC_RELATIONS,
    ProjectionConfig,
    utc_now,
)
from .topic_merge import SuperTopicIndex

WINDOW_PRESETS = (7, 30, 90, 365)
HEADLINE_WINDOW = 30

# Entity types grouped into the lenses reported to the user.
TYPE_LENSES: Dict[str, Tuple[str, ...]] = {
    "people": ("person",),
    "organizations": ("org",),
    "projects": ("project", "product"),
    "places": ("place",),
    "concepts": ("topic", "work_of_art", "event"),
}

W_TOPIC = 0.6
W_ENTITY = 0.4

# A topic must hold >=1% of window evidence to count as an active theme.
ACTIVE_TOPIC_SHARE = 0.01
# An entity must hold >=1% of its lens's evidence to count as active — without
# this, hundreds of one-mention extraction artifacts flatten per-lens entropy
# toward uniform (live loop-2 finding: 134 "active" organizations in 30 days).
ACTIVE_ENTITY_SHARE = 0.01
MIN_BASELINE_WINDOWS = 4
BASELINE_STEPS = 12
# A trailing window needs this much weighted evidence before its score is
# considered meaningful for the personal baseline.
MIN_WINDOW_EVIDENCE = 3.0


def _projection(
    conn: sqlite3.Connection,
    canonical: CanonicalIndex,
    supertopics: SuperTopicIndex,
    config: ProjectionConfig,
    cache: Optional[EvidenceCache] = None,
) -> AnalyticalProjection:
    return AnalyticalProjection(
        conn, config, canonical=canonical, supertopics=supertopics, cache=cache
    )


# A topic also counts as active with at least this much effective evidence
# even below the share floor (plan: ">=1% share or >=3 effective records").
# "Effective" is the weighted evidence mass: one full-intent recent record
# contributes ~1.0, so three raw passive browser hits (~0.3) do NOT qualify —
# a raw record count here doubled the active set with noise topics (loop 5).
ACTIVE_TOPIC_MIN_EFFECTIVE = 3.0


def _active_topic_weights(projection: AnalyticalProjection) -> Dict[str, float]:
    evidence = projection.topic_evidence()
    total = sum(item.weight for item in evidence.values())
    if total <= 0:
        return {}
    return {
        topic_id: item.weight
        for topic_id, item in evidence.items()
        if item.weight / total >= ACTIVE_TOPIC_SHARE
        or item.weight >= ACTIVE_TOPIC_MIN_EFFECTIVE
    }


def _entity_lens_weights(
    projection: AnalyticalProjection, canonical: CanonicalIndex
) -> Dict[str, Dict[str, float]]:
    lenses: Dict[str, Dict[str, float]] = {lens: {} for lens in TYPE_LENSES}
    type_to_lens = {t: lens for lens, types in TYPE_LENSES.items() for t in types}
    for canonical_id, item in projection.entity_evidence().items():
        entity = canonical.get(canonical_id)
        if entity is None or not entity.attention_eligible:
            continue
        lens = type_to_lens.get(entity.canonical_type)
        if lens is None:
            continue
        lenses[lens][canonical_id] = item.weight
    for lens, weights in lenses.items():
        lens_total = sum(weights.values())
        if lens_total <= 0:
            continue
        lenses[lens] = {
            canonical_id: weight
            for canonical_id, weight in weights.items()
            if weight / lens_total >= ACTIVE_ENTITY_SHARE
        }
    return lenses


def _window_focus(
    projection: AnalyticalProjection, canonical: CanonicalIndex
) -> Dict[str, Any]:
    """Concentration reading for one window."""
    topic_weights = _active_topic_weights(projection)
    h_topic_norm = normalized_entropy(list(topic_weights.values()))
    n_eff = effective_count(list(topic_weights.values()))

    lens_weights = _entity_lens_weights(projection, canonical)
    lens_focus: Dict[str, Dict[str, float]] = {}
    total_entity_evidence = 0.0
    weighted_h = 0.0
    for lens, weights in lens_weights.items():
        lens_total = sum(weights.values())
        if lens_total <= 0 or len(weights) == 0:
            continue
        h_norm = normalized_entropy(list(weights.values()))
        lens_focus[lens] = {
            "focus": round(1.0 - h_norm, 4),
            "active_count": len(weights),
            "evidence_share": lens_total,  # normalized below
        }
        total_entity_evidence += lens_total
        weighted_h += lens_total * h_norm
    h_entity_norm = weighted_h / total_entity_evidence if total_entity_evidence > 0 else 0.0
    for lens in lens_focus:
        lens_focus[lens]["evidence_share"] = round(
            lens_focus[lens]["evidence_share"] / total_entity_evidence, 4
        )

    total_topic_evidence = sum(topic_weights.values())
    has_signal = total_topic_evidence > 0 or total_entity_evidence > 0
    if not has_signal:
        score = 0.0
    elif total_topic_evidence > 0 and total_entity_evidence > 0:
        score = 100.0 * (W_TOPIC * (1.0 - h_topic_norm) + W_ENTITY * (1.0 - h_entity_norm))
    elif total_topic_evidence > 0:
        score = 100.0 * (1.0 - h_topic_norm)
    else:
        score = 100.0 * (1.0 - h_entity_norm)

    topic_labels = projection.supertopics
    top_topics = sorted(topic_weights.items(), key=lambda kv: -kv[1])[:5]
    top_total = sum(topic_weights.values()) or 1.0
    return {
        "score": round(clamp_score(score), 1),
        "effective_topics": round(n_eff, 1),
        "active_topics": len(topic_weights),
        "topic_entropy_normalized": round(h_topic_norm, 4),
        "entity_entropy_normalized": round(h_entity_norm, 4),
        "entity_lenses": lens_focus,
        "evidence_total": round(total_topic_evidence + total_entity_evidence, 2),
        "top_topics": [
            {
                "supertopic_id": topic_id,
                "label": topic_labels.label_of(topic_id),
                "share": round(weight / top_total, 4),
            }
            for topic_id, weight in top_topics
        ],
    }


def compute_focus_baseline(
    conn: sqlite3.Connection,
    canonical: CanonicalIndex,
    supertopics: SuperTopicIndex,
    *,
    window_days: int = HEADLINE_WINDOW,
    half_life_days: Optional[float] = None,
    steps: int = BASELINE_STEPS,
    cache: Optional[EvidenceCache] = None,
) -> List[Dict[str, Any]]:
    """Focus score for `steps` trailing windows stepped back one week each."""
    now = utc_now()
    cache = cache or EvidenceCache(conn)
    history: List[Dict[str, Any]] = []
    for i in range(steps - 1, -1, -1):
        end = now - timedelta(weeks=i)
        config = ProjectionConfig(window_days=window_days, half_life_days=half_life_days, end=end)
        projection = _projection(conn, canonical, supertopics, config, cache)
        reading = _window_focus(projection, canonical)
        history.append(
            {
                "window_end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "score": reading["score"],
                "effective_topics": reading["effective_topics"],
                "evidence_total": reading["evidence_total"],
                "usable": reading["evidence_total"] >= MIN_WINDOW_EVIDENCE,
            }
        )
    return history


def _baseline_summary(history: List[Dict[str, Any]]) -> Dict[str, Any]:
    usable = [h for h in history if h.get("usable")]
    current = history[-1] if history else None
    prior = usable[:-1] if usable and current is usable[-1] else usable
    if current is None or len(prior) < MIN_BASELINE_WINDOWS:
        return {"status": "insufficient_history", "windows_used": len(prior)}
    if not current.get("usable"):
        # A near-empty current window scores as maximally focused (one active
        # topic == zero entropy); comparing it against real history would emit
        # a confident percentile from statistical noise.
        return {"status": "insufficient_evidence", "windows_used": len(prior)}
    scores = [float(h["score"]) for h in prior]
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    std = math.sqrt(variance)
    current_score = float(current["score"])
    below = sum(1 for s in scores if s < current_score)
    equal = sum(1 for s in scores if s == current_score)
    percentile = 100.0 * (below + 0.5 * equal) / len(scores)
    return {
        "status": "ok",
        "windows_used": len(prior),
        "mean": round(mean, 1),
        "std": round(std, 2),
        "z": round((current_score - mean) / std, 2) if std > 0 else 0.0,
        "percentile": round(percentile, 1),
        "score_vs_baseline": round(current_score - mean, 1),
    }


def _trend(history: List[Dict[str, Any]], points: int = 4) -> str:
    usable = [h for h in history if h.get("usable")][-points:]
    if len(usable) < 2:
        return "stable"
    xs = list(range(len(usable)))
    ys = [float(h["score"]) for h in usable]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom else 0.0
    if slope >= 2.0:
        return "focusing"
    if slope <= -2.0:
        return "fragmenting"
    return "stable"


def _embedding_coverage(conn: sqlite3.Connection) -> Optional[float]:
    """Fraction of clustered records with an embedding vector (None = unknown)."""
    if not _has_table(conn, "topic_cluster_members"):
        return None
    total_row = conn.execute(
        "SELECT COUNT(DISTINCT record_id) AS n FROM topic_cluster_members"
    ).fetchone()
    total = int(total_row["n"] or 0)
    if total == 0:
        return None
    if not _has_table(conn, "signal_embeddings"):
        return 0.0
    try:
        covered_row = conn.execute(
            """
            SELECT COUNT(DISTINCT m.record_id) AS n
            FROM topic_cluster_members m
            JOIN signal_embeddings e ON e.record_id = m.record_id
            WHERE e.vector_blob IS NOT NULL
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return 0.0
    return int(covered_row["n"] or 0) / total


def compute_pipeline_confidence(
    conn: sqlite3.Connection,
    canonical: CanonicalIndex,
    supertopics: SuperTopicIndex,
    projection: AnalyticalProjection,
) -> Dict[str, Any]:
    flags: List[str] = []

    edges = projection.edges()
    edge_mass = sum(e.score for e in edges)
    generic_mass = sum(e.score for e in edges if e.is_generic)
    specific_share = 1.0 - (generic_mass / edge_mass) if edge_mass > 0 else 0.0
    if edge_mass > 0 and specific_share < 0.35:
        flags.append("generic_relations_dominate_graph")

    entity_merge_rate = canonical.merge_rate
    topic_merge_rate = supertopics.merge_rate
    if topic_merge_rate > 0.4:
        flags.append("many_near_duplicate_topic_clusters")

    mention_row = conn.execute(
        "SELECT AVG(confidence) AS avg_conf, COUNT(*) AS n FROM entity_mentions"
    ).fetchone() if _has_table(conn, "entity_mentions") else None
    mention_confidence = float(mention_row["avg_conf"] or 0.0) if mention_row and mention_row["n"] else 0.0
    if mention_row is None or not mention_row["n"]:
        flags.append("no_entity_mention_evidence")

    ts_health = 1.0
    if _has_table(conn, "timeline"):
        row = conn.execute(
            "SELECT SUM(CASE WHEN event_at < '2000-01-01' THEN 1 ELSE 0 END) AS bad, COUNT(*) AS n FROM timeline"
        ).fetchone()
        n = int(row["n"] or 0)
        if n:
            ts_health = 1.0 - (int(row["bad"] or 0) / n)
        cluster_row = conn.execute(
            """
            SELECT COUNT(DISTINCT t.record_id) AS clustered
            FROM timeline t JOIN topic_cluster_members m ON m.record_id = t.record_id
            """
        ).fetchone() if _has_table(conn, "topic_cluster_members") else None
        total_records = conn.execute(
            "SELECT COUNT(DISTINCT record_id) AS n FROM timeline"
        ).fetchone()
        cluster_coverage = (
            int(cluster_row["clustered"] or 0) / int(total_records["n"])
            if cluster_row and int(total_records["n"] or 0) > 0
            else 0.0
        )
    else:
        cluster_coverage = 0.0
        flags.append("no_timeline")
    if cluster_coverage < 0.5:
        flags.append("low_topic_coverage_of_timeline")

    embedding_coverage = _embedding_coverage(conn)
    if embedding_coverage is not None and embedding_coverage < 0.5:
        flags.append("low_embedding_coverage")

    score = clamp_score(
        100.0
        * (
            0.22 * specific_share
            + 0.18 * mention_confidence
            + 0.18 * cluster_coverage
            + 0.12 * ts_health
            + 0.10 * (1.0 - min(1.0, topic_merge_rate / 0.6))
            + 0.10 * min(1.0, entity_merge_rate / 0.05 if entity_merge_rate else 0.0)
            + 0.10 * (embedding_coverage if embedding_coverage is not None else 0.5)
        )
    )
    # entity merges found = duplicates are being handled → confidence up;
    # zero merges on a big graph usually means resolution has not run at all.

    if score >= 70:
        interpretation = "Graph derivation looks trustworthy — readings can be taken at face value."
    elif score >= 45:
        interpretation = "Readings are usable but carry derivation noise — see flags."
    else:
        interpretation = "Low pipeline confidence — treat focus/influence readings as rough."

    return {
        "score": round(score, 1),
        "interpretation": interpretation,
        "flags": flags,
        "sub_metrics": {
            "specific_edge_mass_share": round(specific_share, 4),
            "entity_merge_rate": round(entity_merge_rate, 4),
            "entity_groups_merged": sum(1 for e in canonical.entities.values() if e.merged),
            "topic_merge_rate": round(topic_merge_rate, 4),
            "supertopics": len(supertopics.supertopics),
            "raw_clusters": supertopics.raw_cluster_count,
            "mention_confidence_mean": round(mention_confidence, 4),
            "timeline_timestamp_health": round(ts_health, 4),
            "topic_coverage_of_timeline": round(cluster_coverage, 4),
            "embedding_coverage": (
                round(embedding_coverage, 4) if embedding_coverage is not None else None
            ),
        },
    }


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
        ).fetchone()
        is not None
    )


def compute_focus_readings(
    conn: sqlite3.Connection,
    canonical: CanonicalIndex,
    supertopics: SuperTopicIndex,
    *,
    window_days: int = HEADLINE_WINDOW,
    half_life_days: Optional[float] = None,
    cache: Optional[EvidenceCache] = None,
) -> Dict[str, Any]:
    """current_focus + information_breadth + pipeline_confidence + baseline."""
    cache = cache or EvidenceCache(conn)
    windows: Dict[str, Dict[str, Any]] = {}
    projections: Dict[int, AnalyticalProjection] = {}
    for days in sorted(set(WINDOW_PRESETS) | {window_days}):
        config = ProjectionConfig(window_days=days, half_life_days=half_life_days)
        projection = _projection(conn, canonical, supertopics, config, cache)
        projections[days] = projection
        windows[f"{days}d"] = _window_focus(projection, canonical)

    headline = windows[f"{window_days}d"]
    baseline_history = compute_focus_baseline(
        conn,
        canonical,
        supertopics,
        window_days=window_days,
        half_life_days=half_life_days,
        cache=cache,
    )
    baseline = _baseline_summary(baseline_history)
    trend = _trend(baseline_history)

    n_eff = headline["effective_topics"]
    if headline["evidence_total"] <= 0:
        interpretation = "No recent evidence in this window — connect or sync sources."
    elif baseline.get("status") == "ok":
        # The personal baseline is the faithful reading; the absolute score is
        # only meaningful against the user's own history.
        delta = float(baseline["score_vs_baseline"])
        if delta >= 1.0:
            comparison = f"more focused than {baseline['percentile']:.0f}% of your own recent {window_days}-day windows"
        elif delta <= -1.0:
            comparison = f"more scattered than usual (percentile {baseline['percentile']:.0f} of your recent {window_days}-day windows)"
        else:
            comparison = f"in line with your typical {window_days}-day window"
        interpretation = (
            f"You're currently {comparison}. Your last {window_days} days behave like"
            f" ~{n_eff:g} equally weighted themes."
        )
    elif baseline.get("status") == "insufficient_evidence":
        interpretation = (
            f"Very little evidence in the last {window_days} days — too quiet a window"
            " for a reliable focus reading."
        )
    else:
        interpretation = (
            f"Your last {window_days} days behave like ~{n_eff:g} equally weighted themes."
            " (Not enough history yet for a personal baseline.)"
        )

    headline_projection = projections[window_days]
    year_projection = projections.get(365)
    source_weights: Dict[str, float] = {}
    for item in headline_projection.topic_evidence().values():
        for source_id, value in item.contributions:
            source_weights[source_id] = source_weights.get(source_id, 0.0) + value
    active_entities = {
        lens: reading["active_count"]
        for lens, reading in headline["entity_lenses"].items()
    }
    breadth = {
        "score": round(clamp_score(100.0 * headline["topic_entropy_normalized"]), 1),
        "interpretation": (
            f"~{headline['effective_topics']:g} active themes now,"
            f" ~{windows['365d']['effective_topics']:g} over the last year,"
            f" across {len(supertopics.parents)} parent theme groups."
        ),
        "sub_metrics": {
            "effective_topics_now": headline["effective_topics"],
            "effective_topics_year": windows["365d"]["effective_topics"],
            "parent_theme_groups": len(supertopics.parents),
            "active_entities_by_lens": active_entities,
            "source_diversity_bits": round(entropy_bits(list(source_weights.values())), 3),
            "active_sources": len(source_weights),
        },
    }

    current_focus = {
        "score": headline["score"],
        "interpretation": interpretation,
        "trend": trend,
        "effective_topics": headline["effective_topics"],
        "windows": windows,
        "baseline": baseline,
        "baseline_history": baseline_history,
        "top_topics": headline["top_topics"],
        "entity_lenses": headline["entity_lenses"],
        "sub_metrics": {
            "topic_entropy_normalized": headline["topic_entropy_normalized"],
            "entity_entropy_normalized": headline["entity_entropy_normalized"],
            "active_topics": headline["active_topics"],
            "window_days": window_days,
            "evidence_total": headline["evidence_total"],
        },
    }

    pipeline = compute_pipeline_confidence(
        conn, canonical, supertopics, year_projection or headline_projection
    )

    return {
        "current_focus": current_focus,
        "information_breadth": breadth,
        "pipeline_confidence": pipeline,
    }
