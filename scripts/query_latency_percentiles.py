"""End-to-end latency percentiles per entrance. The fifth §09 metric.

§09 ships four quality metrics and no latency metric. "What did the redesign cost per
request" has been answered by recollection because there was nothing on disk to read.
This reads what ITEM 4 put on disk and reports it, per entrance, with the sample count
and the window printed beside every number.

    python scripts/query_latency_percentiles.py
    python scripts/query_latency_percentiles.py --db /tmp/copy.db --days 30
    python scripts/query_latency_percentiles.py --json

ENTRANCES. Two, because the two are measured by different code in different repos:

* ``home_chat`` — the front end's turn total, written into the step trace as
  ``[latency] leg=turn elapsed_ms=N`` and persisted in
  ``home_chat_sessions.history_json``. The grammar is defined once, in
  ``next/lib/mcp/latencyTrace.ts``; :func:`parse_latency_step` below is the second
  implementation of it and is deliberately as strict, so a line the front end could not
  have written is not counted.
* ``routines`` — a run's wall clock, ``payload_json.created_at`` to
  ``payload_json.completed_at`` on ``routine_runs``.

A THIRD SERIES, printed separately and NOT as an entrance: ``query_artifacts.duration_ms``,
the engine's own retrieve-to-persist total. One entrance turn issues up to four of these,
so mixing them into an entrance's percentile would answer a different question with the
same word. It is reported as what it is — per scope query, not per request.

WHY THE ENTRANCE CANNOT BE READ OFF THE ARTIFACT. ``query_sessions.requester_id`` is a
grant identity (or the literal ``"mcp"``), not an entrance label: the routines bridge
sends ``requester_id=None`` and the node defaults it to ``"mcp"``, which is also what an
un-attributed MCP client gets. Attributing artifacts per entrance would need a field
nothing writes today. Saying so is the honest report; inventing a split is not.

HONESTY ABOUT THE HOME CHAT SAMPLE. ``agentSteps`` is capped at the last 24 lines PER
SESSION (``homeChatStorage.ts``), and a ``[latency]`` line carries no timestamp of its
own. So the home chat series is a *sample* of recent turns bucketed by the session's
``updated_at_ms``, not a census, and this script says so in its output rather than
letting a p95 imply otherwise.

READ ONLY. Opened ``mode=ro`` with ``PRAGMA query_only = ON``. It never writes to a node
database, and it exits 0 with an empty report rather than failing when a table is absent.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_DB = Path(os.path.expanduser("~/.topos/database.db"))

#: Mirrors ``LATENCY_STEP_PREFIX`` and ``LATENCY_LEGS`` in next/lib/mcp/latencyTrace.ts.
#: Duplicated rather than imported because this is Python reading a TypeScript writer's
#: output; the round-trip test on the TS side and the strictness here are what keep the
#: two honest.
LATENCY_STEP_PREFIX = "[latency]"
LATENCY_LEGS = frozenset(
    {"scope_query", "llm_orchestration", "synthesis", "synthesis_retry", "tool", "turn"}
)

_TOKEN_INT = re.compile(r"^-?\d+$")


def parse_latency_step(line: Any) -> Optional[Dict[str, Any]]:
    """Read one ``[latency]`` line, or ``None`` for any line that is not one.

    Strict on purpose: a leg outside the closed set and a non-integer measure are both
    rejected rather than coerced, so this counts only what the front end's formatter
    could actually have emitted.
    """
    if not isinstance(line, str):
        return None
    trimmed = line.strip()
    if not trimmed.startswith(LATENCY_STEP_PREFIX):
        return None
    leg: Optional[str] = None
    elapsed: Optional[int] = None
    measures: Dict[str, int] = {}
    for token in trimmed[len(LATENCY_STEP_PREFIX) :].strip().split():
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        if key == "leg":
            leg = value if value in LATENCY_LEGS else None
            continue
        if not _TOKEN_INT.match(value):
            continue
        num = int(value)
        if num < 0:
            continue
        if key == "elapsed_ms":
            elapsed = num
        else:
            measures[key] = num
    if leg is None or elapsed is None:
        return None
    return {"leg": leg, "elapsed_ms": elapsed, "measures": measures}


def percentile(values: Sequence[int], q: float) -> Optional[int]:
    """Nearest-rank percentile. ``None`` for an empty sample.

    Nearest-rank rather than interpolated: every reported number is then a duration that
    an actual request actually took. An interpolated p95 of a 7-sample series is a number
    no turn ever produced, which is exactly the kind of figure this script exists to stop
    being quoted.
    """
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(values: Sequence[int]) -> Dict[str, Any]:
    return {
        "samples": len(values),
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json_load(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- the three series ----------------------------------------------------------------


def home_chat_legs(conn: sqlite3.Connection, since_ms: int) -> Dict[str, List[int]]:
    """Every ``[latency]`` line in every session touched inside the window, by leg."""
    legs: Dict[str, List[int]] = {}
    if not _table_exists(conn, "home_chat_sessions"):
        return legs
    rows = conn.execute(
        "SELECT history_json FROM home_chat_sessions WHERE updated_at_ms >= ?", (since_ms,)
    ).fetchall()
    for (raw,) in rows:
        steps = _json_load(raw).get("agentSteps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            sample = parse_latency_step(step)
            if sample is None:
                continue
            legs.setdefault(sample["leg"], []).append(sample["elapsed_ms"])
    return legs


def home_chat_synthesis_budgets(conn: sqlite3.Connection, since_ms: int) -> List[Dict[str, int]]:
    """Synthesis legs with the token budget they were granted, side by side.

    The scaled budget — ``min(4000, base + 400 * (parts - 1))``, doubled on an empty
    first attempt — is the programme's single largest deliberate latency addition. Cost
    against budget is the question; a duration without the budget beside it cannot
    answer it, which is why the front end records both on one line.
    """
    out: List[Dict[str, int]] = []
    if not _table_exists(conn, "home_chat_sessions"):
        return out
    rows = conn.execute(
        "SELECT history_json FROM home_chat_sessions WHERE updated_at_ms >= ?", (since_ms,)
    ).fetchall()
    for (raw,) in rows:
        steps = _json_load(raw).get("agentSteps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            sample = parse_latency_step(step)
            if sample is None or sample["leg"] not in ("synthesis", "synthesis_retry"):
                continue
            budget = sample["measures"].get("max_tokens")
            if budget is None:
                continue
            out.append(
                {
                    "leg": sample["leg"],
                    "elapsed_ms": sample["elapsed_ms"],
                    "max_tokens": budget,
                    "parts": sample["measures"].get("parts", 0),
                }
            )
    return out


#: A run in one of these finished doing work, so its wall clock is a latency. ``cancelled``
#: is excluded because the interval measures how long someone took to cancel, and
#: ``running`` has no end yet.
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed"})


def routine_run_durations(
    conn: sqlite3.Connection, since: datetime, *, stale_minutes: int
) -> Tuple[List[int], int]:
    """``started_at`` → ``completed_at`` per finished run, plus the abandoned count.

    ``started_at``, NOT ``created_at``. The row is created when the run is *scheduled*
    and started when the executor picks it up, so ``created_at`` folds queue residency
    into the measurement: on the live node that produced a p95 of 86,429,354 ms — one
    day and a quarter second, which is a cron interval wearing a latency's name.

    And a run longer than the stale-run reaper's threshold did not finish, it was closed
    for silence (``routines_stale_runs.STALE_RUN_ERROR``, threshold
    ``routines_stale_run_minutes``, 60 by default). Its ``completed_at`` is the reaper's
    clock, so the interval measures time-to-reap. On the live node three such rows put a
    23.8-hour maximum on a series whose p50 is a minute. They are counted and reported
    rather than dropped, because "three runs were abandoned this week" is information
    and a silently shorter list is not.
    """
    if not _table_exists(conn, "routine_runs"):
        return [], 0
    ceiling_ms = max(1, int(stale_minutes)) * 60 * 1000
    values: List[int] = []
    abandoned = 0
    for (raw,) in conn.execute("SELECT payload_json FROM routine_runs").fetchall():
        payload = _json_load(raw)
        if str(payload.get("status") or "").strip() not in _TERMINAL_RUN_STATUSES:
            continue
        started = _parse_iso(payload.get("started_at"))
        finished = _parse_iso(payload.get("completed_at"))
        if started is None or finished is None or finished < started:
            continue
        if finished < since:
            continue
        elapsed = int((finished - started).total_seconds() * 1000)
        if elapsed > ceiling_ms:
            abandoned += 1
            continue
        values.append(elapsed)
    return values, abandoned


def engine_query_durations(conn: sqlite3.Connection, since: datetime) -> Tuple[List[int], int]:
    """``query_artifacts.duration_ms`` in the window, plus the count of unmeasured rows.

    NULL is reported separately rather than folded in as a zero. A turn nobody timed was
    not instant, and counting it as one would pull p50 toward the floor exactly while the
    instrumentation was still rolling out — the failure mode most likely to produce a
    reassuring number from an unmeasured system.
    """
    if not _table_exists(conn, "query_artifacts"):
        return [], 0
    cols = {r[1] for r in conn.execute("PRAGMA table_info(query_artifacts)").fetchall()}
    if "duration_ms" not in cols:
        return [], 0
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT duration_ms FROM query_artifacts WHERE created_at >= ?", (stamp,)
    ).fetchall()
    values = [int(r[0]) for r in rows if r[0] is not None]
    return values, len(rows) - len(values)


# --- report ---------------------------------------------------------------------------


def build_report(conn: sqlite3.Connection, *, days: int, stale_minutes: int) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_ms = int(since.timestamp() * 1000)

    legs = home_chat_legs(conn, since_ms)
    artifacts, unmeasured = engine_query_durations(conn, since)
    routines, abandoned = routine_run_durations(conn, since, stale_minutes=stale_minutes)

    return {
        "window": {
            "days": days,
            "since": since.isoformat(),
            "until": now.isoformat(),
        },
        "entrances": {
            "home_chat": {
                **summarize(legs.get("turn", [])),
                "source": "home_chat_sessions.history_json agentSteps '[latency] leg=turn'",
                "caveat": "agentSteps keeps the last 24 lines per session; this is a sample of recent turns, not a census",
            },
            "routines": {
                **summarize(routines),
                "abandoned_runs": abandoned,
                "source": "routine_runs.payload_json started_at -> completed_at (completed/failed only)",
                "caveat": f"runs longer than {stale_minutes}m were closed by the stale-run reaper, not finished; excluded and counted as abandoned",
            },
        },
        "front_end_legs": {
            leg: summarize(values) for leg, values in sorted(legs.items()) if leg != "turn"
        },
        "synthesis_budget_vs_cost": home_chat_synthesis_budgets(conn, since_ms),
        "engine_per_scope_query": {
            **summarize(artifacts),
            "unmeasured_rows": unmeasured,
            "source": "query_artifacts.duration_ms",
            "note": "one entrance turn issues up to four of these; NOT an entrance total",
        },
    }


def _fmt(value: Optional[int]) -> str:
    return "-" if value is None else f"{value} ms"


def print_report(report: Dict[str, Any]) -> None:
    window = report["window"]
    print(f"query latency percentiles — last {window['days']}d")
    print(f"  window: {window['since']} .. {window['until']}")
    print()
    print("  entrance          samples      p50      p95      max")
    total = 0
    for name, stats in report["entrances"].items():
        total += stats["samples"]
        print(
            f"  {name:<16} {stats['samples']:>7}  {_fmt(stats['p50_ms']):>7}  "
            f"{_fmt(stats['p95_ms']):>7}  {_fmt(stats['max_ms']):>7}"
        )
    abandoned = report["entrances"]["routines"].get("abandoned_runs", 0)
    if abandoned:
        print(f"  ({abandoned} routine run(s) closed by the stale-run reaper, not counted)")

    legs = report["front_end_legs"]
    if legs:
        print()
        print("  front-end leg     samples      p50      p95      max")
        for name, stats in legs.items():
            print(
                f"  {name:<16} {stats['samples']:>7}  {_fmt(stats['p50_ms']):>7}  "
                f"{_fmt(stats['p95_ms']):>7}  {_fmt(stats['max_ms']):>7}"
            )

    budgets = report["synthesis_budget_vs_cost"]
    if budgets:
        print()
        print("  synthesis budget vs cost")
        for row in budgets[:20]:
            print(
                f"    {row['leg']:<16} max_tokens={row['max_tokens']:<5} "
                f"parts={row['parts']:<3} elapsed={row['elapsed_ms']} ms"
            )

    engine = report["engine_per_scope_query"]
    print()
    print("  engine, per scope query (NOT an entrance total)")
    print(
        f"    query_artifacts.duration_ms  samples={engine['samples']}  "
        f"p50={_fmt(engine['p50_ms'])}  p95={_fmt(engine['p95_ms'])}  "
        f"unmeasured_rows={engine['unmeasured_rows']}"
    )

    unmeasured_series = [
        name
        for name, count in (
            ("home_chat", report["entrances"]["home_chat"]["samples"]),
            ("engine per scope query", engine["samples"]),
        )
        if count == 0
    ]
    if unmeasured_series:
        print()
        print(f"  0 samples for: {', '.join(unmeasured_series)}.")
        print("  This is the correct output, not a failure. The duration column and the")
        print("  [latency] step lines ship in this change, so nothing on this database was")
        print("  written by instrumented code yet. A number here today would be invented;")
        print("  re-run after real traffic. (The routines series is computed from timestamps")
        print("  that already existed, which is why it can report today and the others cannot.)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB), help="node database (read-only)")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument(
        "--stale-run-minutes",
        type=int,
        default=60,
        help="mirror of the control plane's routines_stale_run_minutes (default 60); "
        "a routine run longer than this was reaped, not finished",
    )
    args = parser.parse_args(argv)

    db_path = Path(os.path.expanduser(args.db))
    if not db_path.exists():
        print(f"no database at {db_path}", file=sys.stderr)
        return 1
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"cannot open {db_path} read-only: {exc}", file=sys.stderr)
        return 1
    try:
        # Belt and braces: `mode=ro` already refuses writes, and this refuses them again
        # from inside. This script points at the owner's live node by default.
        conn.execute("PRAGMA query_only = ON")
        report = build_report(
            conn, days=max(1, args.days), stale_minutes=max(1, args.stale_run_minutes)
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
