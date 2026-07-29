"""Shift Detector v2 — temporal volatility on real evidence joins.

v1 read `timeline.cluster_id` and windowed `entity_edges` rows, both of which
are empty/degenerate on live nodes. v2 measures week-over-week change as
Jensen–Shannon divergence between consecutive weekly evidence distributions
(entities and supertopics). Set-Jaccard on sparse weekly mention sets was
saturated at ~0.9 every week (loop-3 finding) — distributions weight the
entities by activity instead of treating every mention set as all-or-nothing.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .canonical import CanonicalIndex, build_canonical_index
from .entropy import clamp_score, js_divergence_bits
from .projection import AnalyticalProjection, EvidenceCache, ProjectionConfig
from .snapshots import load_new_entity_rate, week_starts
from .topic_merge import SuperTopicIndex, build_supertopics


def _distribution_shift(
    prev: Optional[Dict[str, float]], cur: Dict[str, float]
) -> float:
    """JS divergence in bits, with empty-vs-nonempty transitions = max shift.

    js_divergence_bits returns 0 when either side is empty, which would score
    a week where all activity stops (or restarts) as zero volatility.
    """
    if prev is None:
        return 0.0
    if bool(prev) != bool(cur):
        return 1.0
    return js_divergence_bits(prev, cur)


def compute_shift_timeline(
    conn: sqlite3.Connection,
    *,
    weeks: int = 12,
    canonical: Optional[CanonicalIndex] = None,
    supertopics: Optional[SuperTopicIndex] = None,
    cache: Optional[EvidenceCache] = None,
) -> List[Dict[str, Any]]:
    canonical = canonical or build_canonical_index(conn)
    supertopics = supertopics or build_supertopics(conn)
    starts = week_starts(weeks=weeks)

    projection = AnalyticalProjection(
        conn,
        ProjectionConfig(window_days=None),
        canonical=canonical,
        supertopics=supertopics,
        cache=cache,
    )
    entity_series, topic_series = projection.weekly_series(starts)

    timeline: List[Dict[str, Any]] = []
    raw_scores: List[float] = []
    prev_topics: Optional[Dict[str, float]] = None

    prev_entity_dist: Optional[Dict[str, float]] = None
    for i, start in enumerate(starts):
        entity_dist = {
            entity_id: float(series[i])
            for entity_id, series in entity_series.items()
            if series[i] > 0
        }
        entity_volatility = _distribution_shift(prev_entity_dist, entity_dist)

        topics = {
            topic_id: float(series[i])
            for topic_id, series in topic_series.items()
            if series[i] > 0
        }
        topic_drift = _distribution_shift(prev_topics, topics)

        from datetime import datetime, timedelta, timezone

        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        week_start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        week_end_iso = (start_dt + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_entity_rate = load_new_entity_rate(
            conn, week_start=week_start_iso, week_end=week_end_iso
        )

        raw = (
            40.0 * entity_volatility
            + 35.0 * min(1.0, topic_drift / 1.0)
            + 25.0 * min(1.0, new_entity_rate * 5.0)
        )
        raw_scores.append(raw)
        timeline.append(
            {
                "week_start": start,
                "shift_score": round(clamp_score(raw), 1),
                "entity_volatility": round(entity_volatility, 4),
                "topic_drift_bits": round(topic_drift, 4),
                "new_entity_rate": new_entity_rate,
                "active_entities": len(entity_dist),
                "active_topics": len(topics),
                "spike": False,
            }
        )
        prev_entity_dist = entity_dist
        prev_topics = topics

    if len(raw_scores) >= 3:
        mean = sum(raw_scores) / len(raw_scores)
        variance = sum((x - mean) ** 2 for x in raw_scores) / len(raw_scores)
        std = variance**0.5
        threshold = mean + 2 * std
        for i, row in enumerate(timeline):
            row["spike"] = raw_scores[i] > threshold if std > 0 else False

    return timeline


def summarize_shift(timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not timeline:
        return {"score": 0.0, "interpretation": "No timeline data yet.", "current_week": None}
    current = timeline[-1]
    score = float(current.get("shift_score") or 0.0)
    if current.get("spike"):
        interpretation = (
            f"Week of {current['week_start']}: notable shift in your information environment."
        )
    elif score >= 55:
        interpretation = "Recent weeks show elevated change — review new connectors or sources."
    elif score <= 20:
        interpretation = "Stable information environment — no major structural shifts."
    else:
        interpretation = "Moderate evolution in topics and connections."
    return {
        "score": round(score, 1),
        "interpretation": interpretation,
        "current_week": current,
        "timeline": timeline,
    }
