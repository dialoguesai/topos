"""Newsletter readiness gate (PLAN_NEWSLETTER_UNLOCK.md).

The default digest newsletter only activates once the node has enough real
signal to triage — below that, a daily digest is added noise. The same
numbers double as the unlock-goal card: progress toward connectors, a
recency streak, and total flux. Deterministic, cheap, recomputed on demand.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Dict

MIN_CONNECTORS = 3
MIN_STREAK_DAYS = 7
MIN_TOTAL_ITEMS = 200
MIN_ITEMS_PER_DAY = 3

_EXCLUDED_SOURCE_PREFIXES = ("demo_", "e2e", "sim_runs", "calendar_stub")


def _real_source(source_id: str) -> bool:
    s = str(source_id or "")
    return bool(s) and not any(s.startswith(p) for p in _EXCLUDED_SOURCE_PREFIXES)


def newsletter_readiness(conn: sqlite3.Connection, *, asof: date | None = None) -> Dict[str, Any]:
    """Gate + goal-card data: {ready, connectors, streak_days, total_items, missing}."""
    asof = asof or date.today()
    sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source_id FROM timeline WHERE source_id IS NOT NULL")]
    connectors = sorted({s for s in sources if _real_source(s)})

    total = conn.execute(
        "SELECT COUNT(*) FROM timeline WHERE source_id NOT LIKE 'demo_%'").fetchone()[0]

    # recency streak: consecutive days ending yesterday-or-today with enough flux
    counts = dict(conn.execute(
        "SELECT substr(event_at,1,10), COUNT(*) FROM timeline "
        "WHERE event_at >= ? GROUP BY substr(event_at,1,10)",
        ((asof - timedelta(days=MIN_STREAK_DAYS + 2)).isoformat(),)))
    streak, day = 0, asof
    if counts.get(day.isoformat(), 0) < MIN_ITEMS_PER_DAY:
        day = day - timedelta(days=1)  # today may simply not be synced yet
    while counts.get(day.isoformat(), 0) >= MIN_ITEMS_PER_DAY:
        streak += 1
        day = day - timedelta(days=1)

    checks = {
        "connectors": {"have": len(connectors), "need": MIN_CONNECTORS,
                       "sources": connectors[:10]},
        "streak_days": {"have": streak, "need": MIN_STREAK_DAYS,
                        "min_items_per_day": MIN_ITEMS_PER_DAY},
        "total_items": {"have": total, "need": MIN_TOTAL_ITEMS},
    }
    missing = [k for k, v in checks.items() if v["have"] < v["need"]]
    return {"ready": not missing, "missing": missing, "asof": asof.isoformat(), **checks}
