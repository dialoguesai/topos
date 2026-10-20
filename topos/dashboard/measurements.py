"""Owner-node daily counters. Reads never create schema or backfill measurements."""
from datetime import date, datetime, timedelta, timezone
import math
import re
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


def _parse_utc(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _request_buckets(first, last):
    buckets, day = [], first
    while day < last:
        buckets.append({"date": day.isoformat(), "count": None, "coverage": "unavailable", "blocks": None})
        day += timedelta(days=1)
    return {"population": "mcp_tool_calls", "measured_since": None, "buckets": buckets}


def mcp_request_activity(earliest, timestamps, start: str, end: str, *, now=None):
    """Recorded MCP tool calls only. Days outside the retained log stay unavailable.

    A retained day with no rows is zero tool calls, not proof that TOPOS was unused.
    Home chat, ingestion writes, model calls, and outcomes are not this population.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 < (last - first).days <= 366:
        raise ValueError("Expected an exclusive end and a range of 1–366 UTC days")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unavailable = _request_buckets(first, last)
    since = _parse_utc(earliest) if earliest else None
    if earliest and since is None:
        return unavailable
    parsed = []
    for raw in timestamps:
        stamp = _parse_utc(raw)
        if stamp is None:
            return unavailable
        if stamp <= now:
            parsed.append(stamp)
    if since is None:
        return unavailable
    counted = {}
    for stamp in parsed:
        blocks = counted.setdefault(stamp.date(), [0, 0, 0, 0, 0, 0])
        blocks[stamp.hour // 4] += 1
    since_day, as_of_day = since.date(), now.date()
    buckets = []
    day = first
    while day < last:
        measured = since_day <= day <= as_of_day
        blocks = counted.get(day, [0, 0, 0, 0, 0, 0]) if measured else None
        buckets.append({
            "date": day.isoformat(),
            "count": sum(blocks) if measured else None,
            "coverage": ("partial" if day in (since_day, as_of_day) else "complete") if measured else "unavailable",
            "blocks": blocks,
        })
        day += timedelta(days=1)
    return {"population": "mcp_tool_calls", "measured_since": since.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "buckets": buckets}


_LABEL = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_RUN_STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled"})


def _label(value):
    text = str(value or "").strip()
    return text if _LABEL.fullmatch(text) else "unknown"


def _status(value):
    text = str(value or "").strip().lower()
    return text if text in _RUN_STATUSES else "unknown"


def _iso(stamp):
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dated(first, last, blank):
    buckets, day = [], first
    while day < last:
        buckets.append({"date": day.isoformat(), **blank})
        day += timedelta(days=1)
    return buckets


def _window(start, end, now):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 < (last - first).days <= 366:
        raise ValueError("Expected an exclusive end and a range of 1–366 UTC days")
    return first, last, (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def mcp_caller_activity(earliest, rows, start: str, end: str, *, now=None):
    """Who made the recorded MCP tool calls. Same population as the request chart."""
    first, last, now = _window(start, end, now)
    blank = {"count": None, "coverage": "unavailable", "callers": None, "calls": None}
    since = _parse_utc(earliest) if earliest else None
    if (earliest and since is None) or since is None and rows:
        return {"population": "mcp_tool_calls", "measured_since": None, "buckets": _dated(first, last, blank)}
    parsed = []
    for raw_at, tool, source, context in rows:
        stamp = _parse_utc(raw_at)
        if stamp is None:
            return {"population": "mcp_tool_calls", "measured_since": None, "buckets": _dated(first, last, blank)}
        if stamp <= now:
            parsed.append((stamp, _label(tool), _label(source), _label(context)))
    if since is None:
        return {"population": "mcp_tool_calls", "measured_since": None, "buckets": _dated(first, last, blank)}
    by_day = {}
    for item in parsed:
        by_day.setdefault(item[0].date(), []).append(item)
    since_day, as_of_day = since.date(), now.date()
    buckets = []
    day = first
    while day < last:
        measured = since_day <= day <= as_of_day
        if not measured:
            buckets.append({"date": day.isoformat(), **blank})
        else:
            grouped, calls = {}, []
            for stamp, tool, source, context in sorted(by_day.get(day, [])):
                grouped[(source, context)] = grouped.get((source, context), 0) + 1
                calls.append({"at": _iso(stamp), "tool": tool, "source": source, "access_context": context})
            callers = [{"source": source, "access_context": context, "count": count}
                       for (source, context), count in sorted(grouped.items(), key=lambda item: (-item[1], item[0]))]
            buckets.append({"date": day.isoformat(), "count": len(calls),
                            "coverage": "partial" if day in (since_day, as_of_day) else "complete",
                            "callers": callers, "calls": calls})
        day += timedelta(days=1)
    return {"population": "mcp_tool_calls", "measured_since": _iso(since), "buckets": buckets}


def routine_run_activity(earliest, rows, start: str, end: str, *, now=None):
    """Routine runs by the time the run row was last updated. Not UMA routine-access counts."""
    first, last, now = _window(start, end, now)
    blank = {"count": None, "coverage": "unavailable", "statuses": None}
    since = _parse_utc(earliest) if earliest else None
    if (earliest and since is None) or since is None and rows:
        return {"population": "routine_runs", "measured_since": None, "buckets": _dated(first, last, blank)}
    parsed = []
    for raw_at, status in rows:
        stamp = _parse_utc(raw_at)
        if stamp is None:
            return {"population": "routine_runs", "measured_since": None, "buckets": _dated(first, last, blank)}
        if stamp <= now:
            parsed.append((stamp, _status(status)))
    if since is None:
        return {"population": "routine_runs", "measured_since": None, "buckets": _dated(first, last, blank)}
    by_day = {}
    for stamp, status in parsed:
        by_day.setdefault(stamp.date(), []).append(status)
    since_day, as_of_day = since.date(), now.date()
    buckets = []
    day = first
    while day < last:
        measured = since_day <= day <= as_of_day
        if not measured:
            buckets.append({"date": day.isoformat(), **blank})
        else:
            grouped = {}
            for status in by_day.get(day, []):
                grouped[status] = grouped.get(status, 0) + 1
            statuses = [{"status": status, "count": count} for status, count in sorted(grouped.items())]
            buckets.append({"date": day.isoformat(), "count": sum(item["count"] for item in statuses),
                            "coverage": "partial" if day in (since_day, as_of_day) else "complete",
                            "statuses": statuses})
        day += timedelta(days=1)
    return {"population": "routine_runs", "measured_since": _iso(since), "buckets": buckets}


def _p50(values):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.5 * len(ordered)))
    return ordered[rank - 1]


def serve_activity(earliest, rows, start: str, end: str, *, now=None):
    """Query serve durations. Days before the first span stay unavailable.

    A covered day with no spans of a kind is a measured empty set: count zero and a null percentile.
    Time to first token is summarized only from LLM spans that recorded one.
    """
    first, last, now = _window(start, end, now)
    blank = {
        "coverage": "unavailable",
        "request_p50_ms": None, "db_p50_ms": None, "llm_p50_ms": None, "ttfb_p50_ms": None,
        "request_n": None, "db_n": None, "llm_n": None,
    }
    closed = {"population": "query_serve", "measured_since": None, "buckets": _dated(first, last, blank)}
    since = _parse_utc(earliest) if earliest else None
    if (earliest and since is None) or (since is None and rows):
        return closed
    parsed = []
    for raw_at, kind, duration_ms, ttfb_ms in rows:
        stamp = _parse_utc(raw_at)
        if stamp is None:
            return closed
        if stamp <= now and kind in ("request", "db", "llm"):
            parsed.append((stamp, kind, duration_ms, ttfb_ms))
    if since is None:
        return closed
    by_day = {}
    for item in parsed:
        by_day.setdefault(item[0].date(), []).append(item)
    since_day, as_of_day = since.date(), now.date()
    buckets = []
    day = first
    while day < last:
        if not (since_day <= day <= as_of_day):
            buckets.append({"date": day.isoformat(), **blank})
        else:
            grouped = {"request": [], "db": [], "llm": [], "ttfb": []}
            for _stamp, kind, duration_ms, ttfb_ms in by_day.get(day, []):
                grouped[kind].append(duration_ms)
                if kind == "llm" and ttfb_ms is not None:
                    grouped["ttfb"].append(ttfb_ms)
            buckets.append({
                "date": day.isoformat(),
                "coverage": "partial" if day in (since_day, as_of_day) else "complete",
                "request_p50_ms": _p50(grouped["request"]),
                "db_p50_ms": _p50(grouped["db"]),
                "llm_p50_ms": _p50(grouped["llm"]),
                "ttfb_p50_ms": _p50(grouped["ttfb"]),
                "request_n": len(grouped["request"]),
                "db_n": len(grouped["db"]),
                "llm_n": len(grouped["llm"]),
            })
        day += timedelta(days=1)
    return {"population": "query_serve", "measured_since": _iso(since), "buckets": buckets}


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


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
        earliest, request_rows, call_rows = None, [], []
        routine_earliest, routine_rows = None, []
        serve_earliest, serve_rows = None, []
        if "mcp_request_log" in tables and "requested_at" in _columns(conn, "mcp_request_log"):
            mcp_columns = _columns(conn, "mcp_request_log")
            selected = ["requested_at"] + [name for name in ("tool_name", "source", "access_context") if name in mcp_columns]
            earliest = conn.execute("SELECT MIN(requested_at) FROM mcp_request_log").fetchone()[0]
            for record in conn.execute(
                f"SELECT {', '.join(selected)} FROM mcp_request_log WHERE requested_at >= ? AND requested_at < ?",
                (start, end)):
                values = dict(zip(selected, record))
                request_rows.append(values["requested_at"])
                call_rows.append((values["requested_at"], values.get("tool_name"), values.get("source"), values.get("access_context")))
        if "routine_runs" in tables and {"updated_at", "status"} <= _columns(conn, "routine_runs"):
            routine_earliest = conn.execute("SELECT MIN(updated_at) FROM routine_runs").fetchone()[0]
            routine_rows = conn.execute(
                "SELECT updated_at, status FROM routine_runs WHERE updated_at >= ? AND updated_at < ?",
                (start, end)).fetchall()
        if "serve_spans" in tables and {"recorded_at", "kind", "duration_ms", "ttfb_ms"} <= _columns(conn, "serve_spans"):
            serve_earliest = conn.execute("SELECT MIN(recorded_at) FROM serve_spans").fetchone()[0]
            serve_rows = conn.execute(
                "SELECT recorded_at, kind, duration_ms, ttfb_ms FROM serve_spans WHERE recorded_at >= ? AND recorded_at < ?",
                (start, end)).fetchall()
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
    requests = mcp_request_activity(earliest, request_rows, start, end, now=now)
    callers = mcp_caller_activity(earliest, call_rows, start, end, now=now)
    routines = routine_run_activity(routine_earliest, routine_rows, start, end, now=now)
    serve = serve_activity(serve_earliest, serve_rows, start, end, now=now)
    return {"available": True, "schema_version": 1, "scope": "owner_node", "period_start": start,
            "period_end": end, "as_of": as_of, "metrics": metrics, "requests": requests,
            "callers": callers, "routine_runs": routines, "serve": serve, "inbound_bytes": None}
