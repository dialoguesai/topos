"""Per-day rollup of the unified timeline (PLAN_TIMELINE_UNIFIED.md E1/G2).

One cheap grouped scan per source instead of paging raw records: daily
evidence counts per canonical lane, plus the metabolism counters — entity
births (event-time first mention), edge births (validity open), fact births
(belief time), and ingest episodes. Junk epoch dates (timestamp-health tails)
fall out via the cutoff comparison.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Dict, List, Optional


def _day_keys(end: date, days: int) -> List[str]:
    return [(end - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]


def _grouped(
    conn: sqlite3.Connection, sql: str, params: tuple
) -> Dict[str, Any]:
    try:
        return {str(row[0]): row for row in conn.execute(sql, params)}
    except sqlite3.OperationalError:
        # Pre-migration nodes may lack a table — degrade to zeros.
        return {}


def timeline_daily_rollup(
    conn: sqlite3.Connection,
    *,
    days: int = 90,
    end_day: Optional[str] = None,
) -> Dict[str, Any]:
    end = date.fromisoformat(end_day) if end_day else date.today()
    day_keys = _day_keys(end, days)
    cutoff = day_keys[0]
    upper = (end + timedelta(days=1)).isoformat()

    lanes_by_day: Dict[str, Dict[str, int]] = {key: {} for key in day_keys}
    try:
        for row in conn.execute(
            """
            SELECT substr(event_at, 1, 10) AS day, canonical_table, COUNT(*) AS n
            FROM timeline
            WHERE event_at >= ? AND event_at < ?
            GROUP BY day, canonical_table
            """,
            (cutoff, upper),
        ):
            day = str(row[0])
            if day in lanes_by_day and row[1]:
                lanes_by_day[day][str(row[1])] = int(row[2])
    except sqlite3.OperationalError:
        pass

    entity_births = _grouped(
        conn,
        """
        SELECT first_day, COUNT(*) FROM (
            SELECT entity_id, MIN(substr(event_at, 1, 10)) AS first_day
            FROM entity_mentions
            WHERE event_at IS NOT NULL AND event_at > '2000-01-01'
            GROUP BY entity_id
        )
        WHERE first_day >= ? AND first_day < ?
        GROUP BY first_day
        """,
        (cutoff, upper),
    )
    edge_births = _grouped(
        conn,
        """
        SELECT substr(valid_from, 1, 10) AS day, COUNT(*)
        FROM entity_edges
        WHERE valid_from >= ? AND valid_from < ?
        GROUP BY day
        """,
        (cutoff, upper),
    )
    fact_births = _grouped(
        conn,
        """
        SELECT substr(valid_from, 1, 10) AS day, COUNT(*)
        FROM signal_objects
        WHERE object_type = 'fact' AND valid_from >= ? AND valid_from < ?
        GROUP BY day
        """,
        (cutoff, upper),
    )
    episodes = _grouped(
        conn,
        """
        SELECT substr(started_at, 1, 10) AS day, COUNT(*), COALESCE(SUM(n_records), 0)
        FROM episodes
        WHERE started_at >= ? AND started_at < ?
        GROUP BY day
        """,
        (cutoff, upper),
    )

    out_days: List[Dict[str, Any]] = []
    for key in day_keys:
        episode_row = episodes.get(key)
        out_days.append(
            {
                "day": key,
                "lanes": lanes_by_day.get(key, {}),
                "births": {
                    "entities": int(entity_births.get(key, (key, 0))[1] or 0),
                    "edges": int(edge_births.get(key, (key, 0))[1] or 0),
                    "facts": int(fact_births.get(key, (key, 0))[1] or 0),
                },
                "episodes": int(episode_row[1]) if episode_row else 0,
                "episode_records": int(episode_row[2]) if episode_row else 0,
            }
        )

    return {"start": day_keys[0], "end": day_keys[-1], "days": out_days}
