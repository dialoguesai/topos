"""Turn one load run (the node's log, the driver's records, the guards' files) into gates and a verdict.

Pure functions over text and dicts, so the thresholds and the parser are tested
without a node (tests/release_policy/test_load_gate.py).

The node's own write-gate lines are the primary evidence. They are the lines the
post-install stress runs were read for by hand:

* ``[WRITE_GATE] write at <site> raised <error>; rolled back ...`` (topos#261):
  a gated write failed. On 1.5.43 the error was ``DatabaseError: another row
  available``: SQLite's SQLITE_ROW, reported for an INSERT because another
  thread stepped a statement on the same connection first.
* ``[WRITE_GATE] slow section ... waited=Xs held=Ys``: the longest write-lock
  hold and wait. 1.5.42 lost 11.5 minutes to one hold.
* ``[WRITE_GATE] transaction opened at ... has stayed open``: the watchdog
  (``TOPOS_DB_TXN_WATCHDOG``) saw a write transaction outside the gate.
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

try:
    from .drive import sqlite_signature
except ImportError:  # pragma: no cover - imported as a top-level module
    from drive import sqlite_signature

ROLLBACK = re.compile(r"\[WRITE_GATE\] write at (?P<site>.+?) raised (?P<error>.+?); rolled back")
ROLLBACK_FAILED = re.compile(r"\[WRITE_GATE\] write at (?P<site>.+?) raised (?P<error>.+?) and rolling back")
SLOW = re.compile(r"\[WRITE_GATE\] slow section(?: at (?P<site>.+?))?: "
                  r"waited=(?P<waited>[\d.]+)s held=(?P<held>[\d.]+)s")
STUCK = re.compile(r"\[WRITE_GATE\] transaction opened at (?P<site>.+?) \(thread .*?has stayed open (?P<age>[\d.]+)s")
LOOP_GATE = re.compile(r"\[WRITE_GATE\] acquired on the event-loop thread from (?P<site>.+?) loop=")
UNGATED = re.compile(r"\[WRITE_GATE\] commit from (?P<site>.+?) arrived with an open write transaction")

#: Provisional thresholds (docs/PRE_TAG_LOAD_GATE.md, "Gates"). Zero-tolerance gates are the class this gate
#: exists for; the latency bars were set from the fixed tree's runs with headroom, not from the owner's node.
GATES = {
    "isolation": {"text": "Served the scratch file; live user_version unchanged; owner-database guard silent"},
    "node_hung": {"max": 0, "text": "no healthy probe for hung_after_s (the GIL/connection-mutex deadlock)"},
    "write_gate_rollbacks": {"max": 0},
    "write_gate_rollback_failures": {"max": 0},
    "sqlite_error_lines": {"max": 0, "text": "node log lines with a one-connection-two-threads or lock signature"},
    "request_sqlite_errors": {"max": 0},
    "stuck_transactions": {"max": 0},
    "write_gate_longest_hold_s": {"max": 30.0},
    "write_gate_longest_wait_s": {"max": 60.0},
    "query_functional_rate": {"min": 0.98},
    "query_p95_s": {"max": 60.0},
    "health_failures": {"max": 0},
    "health_longest_s": {"max": 10.0},
    "transport_errors": {"max": 0},
    "recovery_probes": {"min": 5},
}


def parse_node_log(text: str) -> dict:
    """Counts and extremes from the node's log; site and error type only, never row values."""
    rollbacks, failed, slow, stuck, loop_gate, ungated, sqlite_lines = [], [], [], [], set(), set(), []
    for line in text.splitlines():
        if "[WRITE_GATE]" in line:
            if m := ROLLBACK.search(line):
                rollbacks.append({"site": m["site"], "error": m["error"][:160]})
                continue
            if m := ROLLBACK_FAILED.search(line):
                failed.append({"site": m["site"], "error": m["error"][:160]})
                continue
            if m := SLOW.search(line):
                slow.append({"site": m["site"], "waited_s": float(m["waited"]), "held_s": float(m["held"])})
                continue
            if m := STUCK.search(line):
                stuck.append({"site": m["site"], "age_s": float(m["age"])})
                continue
            if m := LOOP_GATE.search(line):
                loop_gate.add(m["site"])
                continue
            if m := UNGATED.search(line):
                ungated.add(m["site"])
                continue
        signature = sqlite_signature(line)
        if signature:
            sqlite_lines.append(signature)
    by_signature: dict[str, int] = {}
    for s in sqlite_lines:
        by_signature[s] = by_signature.get(s, 0) + 1
    return {
        "rollbacks": rollbacks, "rollback_failures": failed, "slow_sections": slow, "stuck_transactions": stuck,
        "loop_gate_sites": sorted(loop_gate), "ungated_commit_sites": sorted(ungated),
        "sqlite_error_lines": len(sqlite_lines), "sqlite_error_signatures": by_signature,
        "longest_hold_s": max((s["held_s"] for s in slow), default=0.0),
        "longest_wait_s": max((s["waited_s"] for s in slow), default=0.0),
    }


def _p95(values: list[float]) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def summarize_records(records: list[dict]) -> dict:
    load = [r for r in records if r["phase"] == "load"]
    queries = [r for r in load if r["kind"] == "query"]
    analytics = [r for r in load if r["kind"] == "analytics"]
    health = [r for r in load if r["kind"] == "health"]
    recovery = [r for r in records if r["phase"] == "recovery"]
    times = [r["elapsed_s"] for r in queries if r["ok"]]
    # Which path the answered queries took: an all-"denied" run never reached the offloaded pipeline steps.
    outcomes: dict[str, int] = {}
    for r in queries:
        if r["ok"]:
            outcomes[r.get("outcome") or "?"] = outcomes.get(r.get("outcome") or "?", 0) + 1
    errors: dict[str, int] = {}
    for r in queries + analytics:
        if not r["ok"]:
            key = f"{r['kind']}:{r.get('sqlite_error') or r.get('transport_error') or (r.get('error') or '')[:60]}"
            errors[key] = errors.get(key, 0) + 1
    return {
        "queries": len(queries), "queries_ok": sum(r["ok"] for r in queries),
        "query_functional_rate": round(sum(r["ok"] for r in queries) / len(queries), 4) if queries else None,
        "query_median_s": round(statistics.median(times), 2) if times else None,
        "query_p95_s": round(_p95(times), 2) if times else None,
        "query_max_s": round(max(times), 2) if times else None,
        "analytics": len(analytics), "analytics_ok": sum(r["ok"] for r in analytics),
        "request_sqlite_errors": sum(1 for r in queries + analytics if r.get("sqlite_error")),
        "transport_errors": sum(1 for r in load + recovery if r.get("transport_error")),
        "health_probes": len(health), "health_failures": sum(1 for r in health if not r["ok"]),
        "health_db_unknown": sum(1 for r in health if r.get("outcome") == "db_unknown"),
        "health_longest_s": round(max((r["elapsed_s"] for r in health), default=0.0), 2),
        "recovery_ok": sum(1 for r in recovery if r["kind"] == "query" and r["ok"]),
        "recovery_health_ok": all(r["ok"] for r in recovery if r["kind"] == "health"),
        "outcomes": dict(sorted(outcomes.items(), key=lambda kv: -kv[1])),
        "errors": dict(sorted(errors.items(), key=lambda kv: -kv[1])[:12]),
    }


def evaluate(*, log: dict, requests: dict, isolation: dict, gates: dict = GATES) -> dict:
    observed = {
        "isolation": isolation,
        "node_hung": 0 if requests.get("hung_at_s") is None else 1,
        "write_gate_rollbacks": len(log["rollbacks"]),
        "write_gate_rollback_failures": len(log["rollback_failures"]),
        "sqlite_error_lines": log["sqlite_error_lines"],
        "request_sqlite_errors": requests["request_sqlite_errors"],
        "stuck_transactions": len(log["stuck_transactions"]),
        "write_gate_longest_hold_s": log["longest_hold_s"],
        "write_gate_longest_wait_s": log["longest_wait_s"],
        "query_functional_rate": requests["query_functional_rate"],
        "query_p95_s": requests["query_p95_s"],
        "health_failures": requests["health_failures"],
        "health_longest_s": requests["health_longest_s"],
        "transport_errors": requests["transport_errors"],
        "recovery_probes": requests["recovery_ok"] if requests["recovery_health_ok"] else 0,
    }
    results = []
    for name, target in gates.items():
        value = observed.get(name)
        if name == "isolation":
            status = "pass" if value and all(value.get(k) for k in ("serving_scratch", "live_db_unchanged",
                                                                        "guard_journal_empty")) else "fail"
        elif value is None:
            status = "missing"
        elif "max" in target:
            status = "pass" if value <= target["max"] else "fail"
        else:
            status = "pass" if value >= target["min"] else "fail"
        results.append({"gate": name, "status": status, "observed": value,
                        "target": {k: v for k, v in target.items() if k != "text"}})
    failures = [g["gate"] for g in results if g["status"] != "pass"]
    return {"pass": not failures, "failures": failures, "gates": results}
