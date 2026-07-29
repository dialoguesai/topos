"""Shared week binning and small evidence-table loaders."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List


def week_starts(*, weeks: int = 12, end: datetime | None = None) -> List[str]:
    end_dt = end or datetime.now(timezone.utc)
    # Align to Monday UTC
    weekday = end_dt.weekday()
    end_monday = (end_dt - timedelta(days=weekday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    starts: List[str] = []
    for i in range(weeks - 1, -1, -1):
        start = end_monday - timedelta(weeks=i)
        starts.append(start.strftime("%Y-%m-%d"))
    return starts


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def load_new_entity_rate(
    conn: sqlite3.Connection,
    *,
    week_start: str,
    week_end: str,
) -> float:
    """Share of entities whose FIRST OBSERVED MENTION falls in the week.

    entities.first_seen is extraction/backfill time on live nodes (and often
    NULL), so a backfill run would spike every historical week; the earliest
    mention event time is the evidence-faithful signal. Falls back to
    first_seen when mention evidence is absent.
    """
    if _table_exists(conn, "entity_mentions"):
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN first_mention >= ? AND first_mention < ? THEN 1 ELSE 0 END) AS new_n,
                COUNT(*) AS total_n
            FROM (
                SELECT entity_id, MIN(event_at) AS first_mention
                FROM entity_mentions
                WHERE event_at >= '2000-01-01'
                GROUP BY entity_id
            )
            """,
            (week_start, week_end),
        ).fetchone()
        total_n = int(row["total_n"] or 0)
        if total_n > 0:
            return round(int(row["new_n"] or 0) / total_n, 4)
    if not _table_exists(conn, "entities"):
        return 0.0
    total = conn.execute("SELECT COUNT(*) AS cnt FROM entities").fetchone()
    new = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM entities
        WHERE first_seen >= ? AND first_seen < ?
        """,
        (week_start, week_end),
    ).fetchone()
    total_n = int(total["cnt"]) if total else 0
    new_n = int(new["cnt"]) if new else 0
    if total_n <= 0:
        return 0.0
    return round(new_n / total_n, 4)
