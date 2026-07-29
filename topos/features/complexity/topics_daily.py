"""Per-day supertopic evidence shares (PLAN_TIMELINE_UNIFIED.md E2/G1).

The braid's real substrate: what share of each day's clustered evidence each
self-organized supertopic carried. Clusters resolve to supertopics via the
same merge index the complexity summary uses, so labels and ids line up with
influence threads. Topics outside the top-K collapse into "other" — the braid
wants the protagonists, not 48 ribbons.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .topic_merge import build_supertopics

OTHER_ID = "other"


def topics_daily(
    conn: sqlite3.Connection,
    *,
    days: int = 90,
    top: int = 10,
    end_day: Optional[str] = None,
) -> Dict[str, Any]:
    end = date.fromisoformat(end_day) if end_day else date.today()
    day_keys = [(end - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    cutoff = day_keys[0]
    upper = (end + timedelta(days=1)).isoformat()

    index = build_supertopics(conn)

    counts: Dict[str, Dict[str, int]] = {key: {} for key in day_keys}
    totals: Dict[str, int] = {}
    try:
        rows = conn.execute(
            """
            SELECT substr(t.event_at, 1, 10) AS day, m.cluster_id, COUNT(*) AS n
            FROM topic_cluster_members m
            JOIN timeline t ON t.record_id = m.record_id
            WHERE t.event_at >= ? AND t.event_at < ?
            GROUP BY day, m.cluster_id
            """,
            (cutoff, upper),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    for row in rows:
        day = str(row[0])
        if day not in counts:
            continue
        cluster_id = str(row[1])
        super_id = index.resolve(cluster_id) or cluster_id
        n = int(row[2] or 0)
        counts[day][super_id] = counts[day].get(super_id, 0) + n
        totals[super_id] = totals.get(super_id, 0) + n

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    keep = [topic_id for topic_id, _ in ranked[: max(1, top)]]
    keep_set = set(keep)

    topic_meta: Dict[str, Dict[str, Any]] = {}
    day_shares: List[Dict[str, Any]] = []
    for key in day_keys:
        day_counts = counts[key]
        total = sum(day_counts.values())
        shares: Dict[str, float] = {}
        for topic_id, n in day_counts.items():
            bucket = topic_id if topic_id in keep_set else OTHER_ID
            shares[bucket] = shares.get(bucket, 0.0) + (n / total if total else 0.0)
            meta = topic_meta.setdefault(
                bucket,
                {"id": bucket, "first_day": key, "last_day": key, "total": 0},
            )
            meta["last_day"] = key
            meta["total"] = int(meta["total"]) + n
        day_shares.append(
            {"day": key, "total": total, "shares": {k: round(v, 4) for k, v in shares.items()}}
        )

    topics: List[Dict[str, Any]] = []
    for topic_id in [*keep, OTHER_ID]:
        meta = topic_meta.get(topic_id)
        if not meta:
            continue
        topics.append(
            {
                "id": topic_id,
                "label": "Other topics" if topic_id == OTHER_ID else index.label_of(topic_id),
                "first_day": meta["first_day"],
                "last_day": meta["last_day"],
                "total": meta["total"],
            }
        )

    return {
        "start": day_keys[0],
        "end": day_keys[-1],
        "days": day_shares,
        "topics": topics,
        "raw_clusters": index.raw_cluster_count,
        "supertopics": len(index.supertopics),
    }
