"""Owner-node daily counters. Reads never create schema or backfill measurements."""
from datetime import date, datetime, timedelta, timezone
import sqlite3

from ..storage.db.migrations.dashboard_measurements_v1 import INBOUND_TABLES


def forget_erased_fact_measurements(conn, logical_keys):
    """Forget value-bearing receipts during explicit source/record erasure.

    The caller holds its write transaction. A surviving revision or another
    source's fact retains its receipt. Ordinary pruning and replacement writes
    never call this, so arrival deduplication survives those operations.
    """
    keys = sorted({str(key) for key in logical_keys if key is not None})
    if not keys or not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_measurement_items'"
    ).fetchone():
        return 0
    removed = 0
    for start in range(0, len(keys), 128):
        chunk = keys[start:start + 128]
        placeholders = ",".join("?" for _ in chunk)
        cursor = conn.execute(
            "DELETE FROM dashboard_measurement_items "
            "WHERE metric='new_facts' AND family='signal_objects' "
            f"AND item_key IN ({placeholders}) "
            "AND item_key NOT IN (SELECT object_key FROM signal_objects "
            "WHERE object_type='fact' AND object_key IS NOT NULL)",
            chunk,
        )
        removed += max(0, cursor.rowcount)
    return removed


def daily_measurements(conn, start: str, end: str, *, now=None):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 < (last-first).days <= 366:
        raise ValueError("Expected an exclusive end and a range of 1–366 UTC days")
    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if conn is None:
        return {"available": False}
    try:
        # Single read snapshot: coverage and values must refer to the same committed state.
        conn.execute("SAVEPOINT dashboard_read")
        coverage = dict(conn.execute("SELECT family, measured_since FROM dashboard_measurement_coverage"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        rows = conn.execute("""SELECT substr(recorded_at,1,10), metric, source_id, count(*)
            FROM dashboard_measurement_items WHERE recorded_at >= ? AND recorded_at < ?
            AND recorded_at <= ? GROUP BY 1,2,3""", (start, end, as_of)).fetchall()
    except sqlite3.OperationalError:
        return {"available": False}
    finally:
        try:
            conn.execute("RELEASE dashboard_read")
        except sqlite3.OperationalError:
            pass
    metrics = {}
    for metric, families in (("inbound_records", list(INBOUND_TABLES)), ("new_facts", ["signal_objects"])):
        present = [t for t in families if t in tables]
        ready = bool(present) and all(t in coverage and f"dashboard_measure_{t}" in triggers and f"dashboard_measure_{t}_update" in triggers for t in present)
        since = max(coverage[t] for t in present) if ready else None
        buckets = []
        day = first
        while day < last:
            key = day.isoformat()
            measured = bool(since and key >= since[:10] and key <= as_of[:10])
            sources = [{"source_id": r[2], "count": r[3]} for r in rows if r[0] == key and r[1] == metric] if measured else []
            buckets.append({"date": key, "count": sum(s["count"] for s in sources) if measured else None,
                            "coverage": ("partial" if key == since[:10] or key == as_of[:10] else "complete") if measured else "unavailable",
                            "sources": sources})
            day += timedelta(days=1)
        metrics[metric] = {"measured_since": since, "buckets": buckets}
    return {"available": True, "schema_version": 1, "scope": "owner_node", "period_start": start,
            "period_end": end, "as_of": as_of, "metrics": metrics, "inbound_bytes": None}
