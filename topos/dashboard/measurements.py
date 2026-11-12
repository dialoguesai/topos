"""Owner-node daily counters. Reads never create schema or backfill measurements."""
from datetime import date, datetime, timedelta, timezone
import math
import re
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


_ZONE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


def resolve_zone(name):
    """The zone days are cut in, and the name the response reports for it.

    Days end at the owner's local midnight when the caller names an IANA zone this
    node can load (DST days are 23 or 25 hours). Anything else keeps UTC days and
    says so, so a client never labels UTC buckets as local ones.
    """
    if isinstance(name, str) and name != "UTC" and len(name) <= 64 and _ZONE_NAME.fullmatch(name):
        try:
            return ZoneInfo(name), name
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return timezone.utc, "UTC"


def _day(stamp, zone):
    return stamp.astimezone(zone).date()


def _day_key(text, zone):
    """Calendar day of a stored UTC instant. UTC days keep the stored date prefix."""
    if zone is timezone.utc:
        return text[:10]
    stamp = _parse_utc(text)
    return _day(stamp, zone).isoformat() if stamp else None


def _request_buckets(first, last):
    buckets, day = [], first
    while day < last:
        buckets.append({"date": day.isoformat(), "count": None, "coverage": "unavailable", "blocks": None})
        day += timedelta(days=1)
    return {"population": "mcp_tool_calls", "measured_since": None, "buckets": buckets}


def mcp_request_activity(earliest, timestamps, start: str, end: str, *, now=None, zone=timezone.utc):
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
        local = stamp.astimezone(zone)
        blocks = counted.setdefault(local.date(), [0, 0, 0, 0, 0, 0])
        blocks[local.hour // 4] += 1
    since_day, as_of_day = _day(since, zone), _day(now, zone)
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


def mcp_caller_activity(earliest, rows, start: str, end: str, *, now=None, zone=timezone.utc):
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
        by_day.setdefault(_day(item[0], zone), []).append(item)
    since_day, as_of_day = _day(since, zone), _day(now, zone)
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


def routine_run_activity(earliest, rows, start: str, end: str, *, now=None, zone=timezone.utc):
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
        by_day.setdefault(_day(stamp, zone), []).append(status)
    since_day, as_of_day = _day(since, zone), _day(now, zone)
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


def serve_activity(earliest, rows, start: str, end: str, *, now=None, zone=timezone.utc):
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
        by_day.setdefault(_day(item[0], zone), []).append(item)
    since_day, as_of_day = _day(since, zone), _day(now, zone)
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


def verification_activity(earliest, rows, start, end, *, now=None, zone=timezone.utc):
    """Answered verification calls, with unknown legacy outcomes separate from abstentions."""
    from .verification import OUTCOMES

    activity = mcp_request_activity(earliest, [at for at, _ in rows], start, end, now=now, zone=zone)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    grouped = {}
    for at, outcome in rows:
        stamp = _parse_utc(at)
        if stamp is not None and stamp <= now:
            counts = grouped.setdefault(_day(stamp, zone).isoformat(), dict.fromkeys((*OUTCOMES, "unavailable"), 0))
            counts[outcome if outcome in OUTCOMES else "unavailable"] += 1
    return {"population": "answered_verify_claim_calls", "measured_since": activity["measured_since"],
            "buckets": [{"date": b["date"], "count": b["count"], "coverage": b["coverage"],
                         "outcomes": (grouped.get(b["date"], dict.fromkeys((*OUTCOMES, "unavailable"), 0))
                                      if b["count"] is not None else None)} for b in activity["buckets"]]}


def daily_measurements(conn, start: str, end: str, *, now=None, time_zone=None):
    """Daily buckets from ``start`` to the exclusive ``end``, cut at midnight in ``time_zone``.

    Instants in the response (``as_of``, ``measured_since``, call times) stay UTC.
    ``time_zone`` in the response names the zone the buckets were actually cut in.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.isoformat() != start or last.isoformat() != end or not 0 < (last-first).days <= 366:
        raise ValueError("Expected an exclusive end and a range of 1–366 UTC days")
    zone, zone_name = resolve_zone(time_zone)
    now = now or datetime.now(timezone.utc)
    as_of = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    # Local days straddle UTC dates. Fetch whole UTC dates that cover every offset
    # (−12h to +14h); rows outside the requested local days fall out when bucketed.
    utc = zone is timezone.utc
    fetch_start = start if utc else (first - timedelta(days=1)).isoformat()
    fetch_end = end if utc else (last + timedelta(days=1)).isoformat()
    if conn is None:
        return {"available": False}
    try:
        # Single read snapshot: coverage and values must refer to the same committed state.
        conn.execute("SAVEPOINT dashboard_read")
        coverage = dict(conn.execute("SELECT family, measured_since FROM dashboard_measurement_coverage"))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        from .coherence import read_claims, coherence_series
        coherence_rows = read_claims(conn)
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        # Every zone in use is a whole number of minutes from UTC, so a minute never
        # straddles a local midnight.
        rows = conn.execute(f"""SELECT substr(recorded_at,1,{10 if utc else 16}), metric, source_id, count(*)
            FROM dashboard_measurement_items WHERE recorded_at >= ? AND recorded_at < ?
            AND recorded_at <= ? GROUP BY 1,2,3""", (fetch_start, fetch_end, as_of)).fetchall()
        earliest, request_rows, call_rows = None, [], []
        verification_rows = []
        routine_earliest, routine_rows = None, []
        serve_earliest, serve_rows = None, []
        if "mcp_request_log" in tables and "requested_at" in _columns(conn, "mcp_request_log"):
            mcp_columns = _columns(conn, "mcp_request_log")
            selected = ["requested_at"] + [name for name in ("tool_name", "source", "access_context", "verification_outcome") if name in mcp_columns]
            earliest = conn.execute("SELECT MIN(requested_at) FROM mcp_request_log").fetchone()[0]
            for record in conn.execute(
                f"SELECT {', '.join(selected)} FROM mcp_request_log WHERE requested_at >= ? AND requested_at < ?",
                (fetch_start, fetch_end)):
                values = dict(zip(selected, record))
                request_rows.append(values["requested_at"])
                if values.get("tool_name") == "verify_claim":
                    verification_rows.append((values["requested_at"], values.get("verification_outcome")))
                call_rows.append((values["requested_at"], values.get("tool_name"), values.get("source"), values.get("access_context")))
        if "routine_runs" in tables and {"updated_at", "status"} <= _columns(conn, "routine_runs"):
            routine_earliest = conn.execute("SELECT MIN(updated_at) FROM routine_runs").fetchone()[0]
            routine_rows = conn.execute(
                "SELECT updated_at, status FROM routine_runs WHERE updated_at >= ? AND updated_at < ?",
                (fetch_start, fetch_end)).fetchall()
        if "serve_spans" in tables and {"recorded_at", "kind", "duration_ms", "ttfb_ms"} <= _columns(conn, "serve_spans"):
            serve_earliest = conn.execute("SELECT MIN(recorded_at) FROM serve_spans").fetchone()[0]
            serve_rows = conn.execute(
                "SELECT recorded_at, kind, duration_ms, ttfb_ms FROM serve_spans WHERE recorded_at >= ? AND recorded_at < ?",
                (fetch_start, fetch_end)).fetchall()
    except sqlite3.OperationalError:
        return {"available": False}
    finally:
        try:
            conn.execute("RELEASE dashboard_read")
        except sqlite3.OperationalError:
            pass
    counted, unplaced = {}, set()
    for stamp, metric, source_id, n in rows:
        key = _day_key(stamp + ("" if utc else "Z"), zone)
        if key is None:
            unplaced.add(metric)
            continue
        per_source = counted.setdefault((key, metric), {})
        per_source[source_id] = per_source.get(source_id, 0) + n
    as_of_key = _day_key(as_of, zone)
    metrics = {}
    for metric, families in (("inbound_records", list(INBOUND_TABLES)), ("new_facts", ["signal_objects"])):
        present = [t for t in families if t in tables]
        ready = bool(present) and metric not in unplaced and all(t in coverage and f"dashboard_measure_{t}" in triggers and f"dashboard_measure_{t}_update" in triggers for t in present)
        since = max(coverage[t] for t in present) if ready else None
        since_key = _day_key(since, zone) if since else None
        buckets = []
        day = first
        while day < last:
            key = day.isoformat()
            measured = bool(since_key and key >= since_key and key <= as_of_key)
            sources = [{"source_id": source_id, "count": n} for source_id, n in sorted(counted.get((key, metric), {}).items())] if measured else []
            buckets.append({"date": key, "count": sum(s["count"] for s in sources) if measured else None,
                            "coverage": ("partial" if key == since_key or key == as_of_key else "complete") if measured else "unavailable",
                            "sources": sources})
            day += timedelta(days=1)
        metrics[metric] = {"measured_since": since if since_key else None, "buckets": buckets}
    requests = mcp_request_activity(earliest, request_rows, start, end, now=now, zone=zone)
    callers = mcp_caller_activity(earliest, call_rows, start, end, now=now, zone=zone)
    verification = verification_activity(earliest, verification_rows, start, end, now=now, zone=zone)
    routines = routine_run_activity(routine_earliest, routine_rows, start, end, now=now, zone=zone)
    serve = serve_activity(serve_earliest, serve_rows, start, end, now=now, zone=zone)
    return {"available": True, "schema_version": 1, "scope": "owner_node", "period_start": start,
            "period_end": end, "time_zone": zone_name, "as_of": as_of, "metrics": metrics, "requests": requests,
            "callers": callers, "verify_claim": verification, "routine_runs": routines, "serve": serve, "inbound_bytes": None,
            "self_coherence": coherence_series(coherence_rows, start, end, now=now, zone=zone)}
