"""Orchestrate complexity metric computation and caching (v2).

Canonical entity resolution and topic merging are computed once per snapshot
and shared by every reading, so all metrics see the same analytical views.
The summary keeps the v1 top-level keys (focus_index, structure_clarity,
topic_quality, shift_detector, influence_threads) and adds `readings` with the
four v2 outputs.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .canonical import build_canonical_index
from .focus import HEADLINE_WINDOW, compute_focus_readings
from .graph_metrics import compute_structure_clarity
from .influence import compute_influence_threads
from .projection import EvidenceCache
from .store import load_latest_summary, upsert_snapshot
from .topic_merge import build_supertopics
from .topics import compute_topic_quality
from .volatility import compute_shift_timeline, summarize_shift

from topos.storage.db.write_gate import batched_writes


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_complexity_snapshot(
    conn: sqlite3.Connection,
    *,
    weeks: int = 12,
    window_days: int = HEADLINE_WINDOW,
    half_life_days: Optional[float] = None,
    top_k: int = 5,
) -> Dict[str, Any]:
    canonical = build_canonical_index(conn)
    supertopics = build_supertopics(conn)
    cache = EvidenceCache(conn)

    focus_readings = compute_focus_readings(
        conn,
        canonical,
        supertopics,
        window_days=window_days,
        half_life_days=half_life_days,
        cache=cache,
    )
    current_focus = focus_readings["current_focus"]
    structure_clarity = compute_structure_clarity(
        conn, canonical=canonical, supertopics=supertopics, cache=cache
    )
    topic_quality = compute_topic_quality(conn)
    topic_quality["supertopics"] = {
        "count": len(supertopics.supertopics),
        "raw_clusters": supertopics.raw_cluster_count,
        "merge_rate": round(supertopics.merge_rate, 4),
        "top_merges": supertopics.merged_groups(limit=10),
    }
    shift_timeline = compute_shift_timeline(
        conn, weeks=weeks, canonical=canonical, supertopics=supertopics, cache=cache
    )
    shift_summary = summarize_shift(shift_timeline)
    influence_threads = compute_influence_threads(
        conn,
        weeks=weeks,
        top_k=top_k,
        canonical=canonical,
        supertopics=supertopics,
        cache=cache,
    )

    # v1-compatible focus_index block (score/interpretation/trend/sparkline).
    baseline_history = current_focus.get("baseline_history") or []
    sparkline = [float(h["score"]) for h in baseline_history]
    focus_index = {
        "score": current_focus["score"],
        "interpretation": current_focus["interpretation"],
        "trend": current_focus["trend"],
        "sparkline": sparkline,
        "effective_topics": current_focus["effective_topics"],
        "windows": current_focus["windows"],
        "baseline": current_focus["baseline"],
        "top_topics": current_focus["top_topics"],
        "entity_lenses": current_focus["entity_lenses"],
        "sub_metrics": current_focus["sub_metrics"],
    }

    summary = {
        "computed_at": _now_iso(),
        "engine_version": 2,
        "params": {
            "weeks": weeks,
            "window_days": window_days,
            "half_life_days": half_life_days,
            "top_k": top_k,
        },
        "focus_index": focus_index,
        "structure_clarity": structure_clarity,
        "topic_quality": topic_quality,
        "shift_detector": {
            "score": shift_summary.get("score"),
            "interpretation": shift_summary.get("interpretation"),
            "current_week": shift_summary.get("current_week"),
            "timeline": shift_timeline,
        },
        "influence_threads": influence_threads,
        "readings": {
            "current_focus": current_focus,
            "structural_clarity": structure_clarity,
            "information_breadth": focus_readings["information_breadth"],
            "pipeline_confidence": focus_readings["pipeline_confidence"],
        },
        "canonicalization": {
            "raw_entities": len(canonical.to_canonical),
            "canonical_entities": len(canonical.entities),
            "merge_rate": round(canonical.merge_rate, 4),
            "top_merges": canonical.merged_groups(limit=10),
        },
    }

    # ~16 rows per snapshot: hold the write gate once and commit once.
    with batched_writes(conn):
        upsert_snapshot(
            conn,
            snapshot_id="summary_latest",
            metric_set="summary",
            metrics=summary,
            commit=False,
        )
        for row in shift_timeline:
            week = str(row.get("week_start") or "")
            upsert_snapshot(
                conn,
                snapshot_id=f"shift_{week}",
                metric_set="shift_week",
                week_start=week,
                metrics=row,
                commit=False,
            )
        upsert_snapshot(
            conn,
            snapshot_id=f"shift_timeline_{uuid.uuid4().hex[:12]}",
            metric_set="shift_timeline",
            metrics={"weeks": shift_timeline},
            commit=False,
        )
        upsert_snapshot(
            conn,
            snapshot_id=f"influence_{uuid.uuid4().hex[:12]}",
            metric_set="influence",
            metrics={"threads": influence_threads},
            commit=False,
        )
        # Append-only personal-baseline history so future runs can compare
        # against genuinely historical readings, not just recomputed windows.
        upsert_snapshot(
            conn,
            snapshot_id=f"focus_baseline_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            metric_set="focus_baseline",
            metrics={
                "score": current_focus["score"],
                "effective_topics": current_focus["effective_topics"],
                "window_days": window_days,
            },
            commit=False,
        )
    return summary


def get_complexity_summary(
    conn: sqlite3.Connection,
    *,
    recompute: bool = False,
    weeks: int = 12,
    window_days: int = HEADLINE_WINDOW,
    half_life_days: Optional[float] = None,
) -> Dict[str, Any]:
    if not recompute:
        cached = load_latest_summary(conn)
        if cached and int(cached.get("engine_version") or 0) >= 2:
            cached_params = cached.get("params") or {}
            requested = {
                "weeks": weeks,
                "window_days": window_days,
                "half_life_days": half_life_days,
            }
            # A cached summary computed with different parameters must not be
            # served as if it answered this request.
            if all(cached_params.get(k) == v for k, v in requested.items()):
                return cached
    return compute_complexity_snapshot(
        conn, weeks=weeks, window_days=window_days, half_life_days=half_life_days
    )


def get_shift_timeline(
    conn: sqlite3.Connection,
    *,
    recompute: bool = False,
    weeks: int = 12,
) -> Dict[str, Any]:
    if not recompute:
        summary = load_latest_summary(conn)
        if summary and int(summary.get("engine_version") or 0) >= 2:
            timeline = (summary.get("shift_detector") or {}).get("timeline")
            if isinstance(timeline, list) and timeline:
                return summarize_shift(timeline)
    timeline = compute_shift_timeline(conn, weeks=weeks)
    return summarize_shift(timeline)


def get_influence_threads(
    conn: sqlite3.Connection,
    *,
    recompute: bool = False,
    weeks: int = 12,
    top_k: int = 5,
    window_days: int = 90,
    target: Optional[str] = None,
) -> Dict[str, Any]:
    if target is not None or recompute:
        cache = EvidenceCache(conn)
        threads = compute_influence_threads(
            conn,
            weeks=weeks,
            top_k=top_k,
            window_days=window_days,
            target=target,
            cache=cache,
        )
        return {"threads": threads, "computed_at": _now_iso(), "target": target}
    summary = load_latest_summary(conn)
    if summary and int(summary.get("engine_version") or 0) >= 2:
        threads = summary.get("influence_threads")
        if isinstance(threads, list):
            return {
                "threads": threads[:top_k],
                "computed_at": summary.get("computed_at"),
            }
    threads = compute_influence_threads(conn, weeks=weeks, top_k=top_k, window_days=window_days)
    return {"threads": threads, "computed_at": _now_iso()}
