#!/usr/bin/env python3
"""Join CP and node search-timing lines by correlation id and attribute every search (plan MG-1..MG-3, IF-3).

Reads only lines containing ``permission_search_timing`` (the node's JSON log lines or plain text, and
the control plane's container log), so no other log content is ever parsed or printed. Output is
aggregates plus per-search rows keyed by ordinal: no correlation id, run id, query or record.

Per search:
  network_queue_ms = CP relay elapsed - node transport_total      (the brief's definition)
                   = cp_send_ms (relay lock, serialisation, socket write: relay_send_at -> relay_sent_at)
                   + outside_ms (network both ways, the node's inbound queue before the transport, the CP's resume)
  node transport_total = pre_adapter + queue_wait + adapter stages + send_check + send + node_other
  gate waits: exact at runtime_setup, recheck and send_check; at admit and index_load the probe names the
  holder, and when that holder is the sweeper its remaining hold (sweep_hold line) bounds the wait.

The two-band test (MG-3): searches split at the widest gap in their node transport_total; the verdict
compares how much of the band gap the gate waits (exact + sweep-bounded) explain.

Usage:
  search_timing_attribution.py --node-log node.log --cp-log cp.log [--since EPOCH] [--harness results.json] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"permission_search_timing run=([0-9a-f]{32}) stage=([a-z_]+) elapsed_ms=(-?\d+(?:\.\d+)?)"
                  r"((?: [a-z_]+=[A-Za-z0-9_.:-]+)*)")
ADAPTER = ("runtime_setup", "admit", "index_load", "embed", "rank", "recheck", "checkpoint", "sign")
CP_STAGES = ("authentication", "issuance", "consent_before", "routing", "relay", "revalidation", "consent_send")
SEND_CHECK_PARTS = ("open", "protection", "authority", "commit", "check_own")
# IF-3 v1.3: index_load splits into check_own (boundary, digest, members) and load; send_check's
# check_own splits the same way. Absent on older nodes, which leave these parts out of the report.
INDEX_LOAD_PARTS = ("check_own", "boundary", "digest", "members", "load")
CHECK_OWN_PARTS = ("boundary", "digest", "members")
EXACT_GATES = ("runtime_setup", "recheck", "send_check")
PROBED_GATES = ("admit", "index_load")
SWEEPER = "p2c-index-sweep"


def timing_lines(path: Path, since: float | None = None):
    """(run, stage, ms, fields, wall-clock seconds or None) for every timing line in one log."""
    with path.open(errors="replace") as handle:
        for raw in handle:
            if "permission_search_timing" not in raw:
                continue
            wall = None
            text = raw
            if raw.lstrip().startswith("{"):
                try:
                    record = json.loads(raw)
                    text = str(record.get("message", ""))
                    wall = record.get("timestamp") if isinstance(record.get("timestamp"), (int, float)) else None
                except ValueError:
                    pass
            match = LINE.search(text)
            if not match:
                continue
            fields = dict(pair.split("=", 1) for pair in match.group(4).split())
            if wall is None:
                for key in ("end_at", "sent_at", "relay_recv_at"):
                    if key in fields:
                        wall = float(fields[key]) / 1000
                        break
            if since is not None and wall is not None and wall < since:
                continue
            yield match.group(1), match.group(2), float(match.group(3)), fields, wall


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_node(path: Path, since=None):
    searches, sweeps = defaultdict(lambda: {"stages": defaultdict(float), "lines": []}), []
    for run, stage, ms, fields, _wall in timing_lines(path, since):
        if stage == "sweep_hold":
            start = _float(fields.get("start_ms"))
            if start is not None:
                sweeps.append({"start": start, "end": start + ms, "hold_ms": ms,
                               "wait_ms": _float(fields.get("wait_ms"), 0.0), "removed": fields.get("removed")})
            continue
        corr = fields.get("corr")
        if not corr or corr == "-":
            continue
        entry = searches[corr]
        entry["lines"].append((stage, ms, fields))
        if stage in ("gate_wait", "gate_probe", "queue_wait"):
            continue
        entry["stages"][stage] += ms
    return searches, sweeps


def load_cp(path: Path, since=None):
    by_run = defaultdict(list)
    for run, stage, ms, fields, wall in timing_lines(path, since):
        by_run[run].append((stage, ms, fields, wall))
    searches = {}
    for run, lines in by_run.items():
        corr = next((fields["corr"] for _stage, _ms, fields, _w in lines if fields.get("corr", "-") != "-"), None)
        if corr is None:
            continue
        stages = defaultdict(float)
        relay = {}
        end = None
        for stage, ms, fields, wall in lines:
            stages[stage] += ms
            if stage == "relay":
                relay = fields
            end = max(end or 0.0, wall or 0.0)
        searches[corr] = {"stages": stages, "relay": relay, "end": end}
    return searches


def attribute(node_search, cp_search, sweeps):
    stages = node_search["stages"]
    row = {"node": {stage: round(stages.get(stage, 0.0), 3) for stage in ADAPTER}}
    total = stages.get("transport_total")
    row["transport_total_ms"] = total
    queue = executor = resume = 0.0
    gate_exact, probes = {}, {}
    send_check_parts, index_load_parts = {}, {}
    for stage, ms, fields in node_search["lines"]:
        if stage == "queue_wait":
            queue += ms
            executor += _float(fields.get("executor_ms"), 0.0)
            resume += _float(fields.get("resume_ms"), 0.0)
        elif stage == "gate_wait":
            gate_exact[fields.get("point")] = ms
        elif stage == "gate_probe":
            probes[fields.get("point")] = {"holder": fields.get("holder"), "site": fields.get("site"), "held_ms": ms,
                                           "t_ms": _float(fields.get("t_ms"))}
        elif stage == "send_check":
            send_check_parts = {part: _float(fields.get(f"{part}_ms")) for part in SEND_CHECK_PARTS}
            send_check_parts.update({f"check_own.{part}": _float(fields.get(f"{part}_ms"))
                                     for part in CHECK_OWN_PARTS if f"{part}_ms" in fields})
        elif stage == "index_load":
            index_load_parts = {part: _float(fields.get(f"{part}_ms")) for part in INDEX_LOAD_PARTS
                                if f"{part}_ms" in fields}
        elif stage == "transport_total":
            row["outcome"] = fields.get("outcome")
            window_end = _float(fields.get("t_ms"))
            row["_window"] = (window_end - ms, window_end) if window_end is not None else None
    row["queue_wait_ms"], row["queue_executor_ms"], row["queue_resume_ms"] = queue, executor, resume
    row["pre_adapter_ms"] = stages.get("pre_adapter", 0.0)
    row["send_check_ms"] = stages.get("send_check", 0.0)
    row["send_check_parts_ms"] = send_check_parts
    row["index_load_parts_ms"] = index_load_parts
    row["send_ms"] = stages.get("send", 0.0)
    accounted = (row["pre_adapter_ms"] + queue + sum(stages.get(stage, 0.0) for stage in ADAPTER)
                 + row["send_check_ms"] + row["send_ms"])
    row["node_other_ms"] = None if total is None else total - accounted

    # Gate waits: exact where the gate is entered in timed code; bounded by the sweeper's own hold elsewhere.
    bounded = {}
    for point, probe in probes.items():
        if probe["holder"] in (None, "none") or probe["t_ms"] is None:
            continue
        stage_ms = stages.get(point, 0.0)
        if probe["holder"] == SWEEPER:
            hold = next((sweep for sweep in sweeps if sweep["start"] <= probe["t_ms"] <= sweep["end"]), None)
            remaining = (hold["end"] - probe["t_ms"]) if hold else stage_ms
            bounded[point] = min(stage_ms, max(remaining, 0.0))
        else:
            bounded[point] = None  # another holder: the stage's own time is the only bound
    row["gate_wait_exact_ms"] = {point: gate_exact.get(point) for point in EXACT_GATES}
    row["gate_wait_sweep_bounded_ms"] = bounded
    row["gate_holders"] = {point: probe["holder"] for point, probe in probes.items() if probe["holder"] not in (None, "none")}
    row["gate_holder_sites"] = {point: probe["site"] for point, probe in probes.items() if probe["holder"] not in (None, "none")}
    row["gate_wait_ms"] = sum(value for value in gate_exact.values() if value) + sum(
        value for value in bounded.values() if value)
    window = row.pop("_window", None)
    row["sweep_overlap_ms"] = 0.0 if not window else sum(
        max(0.0, min(window[1], sweep["end"]) - max(window[0], sweep["start"])) for sweep in sweeps)

    if cp_search:
        cp = cp_search["stages"]
        row["cp"] = {stage: round(cp.get(stage, 0.0), 3) for stage in CP_STAGES}
        relay = cp.get("relay")
        row["relay_ms"] = relay
        fields = cp_search["relay"]
        send_at, sent_at, recv_at = (_float(fields.get(key)) for key in ("relay_send_at", "relay_sent_at", "relay_recv_at"))
        row["cp_send_ms"] = (sent_at - send_at) if send_at is not None and sent_at is not None else None
        if relay is not None and total is not None:
            row["network_queue_ms"] = relay - total
            row["outside_ms"] = (row["network_queue_ms"] - row["cp_send_ms"]) if row["cp_send_ms"] is not None else None
        node_recv = next((_float(f.get("recv_at")) for s, _m, f in node_search["lines"] if s == "transport_total"), None)
        node_sent = next((_float(f.get("sent_at")) for s, _m, f in node_search["lines"] if s == "transport_total"), None)
        if None not in (sent_at, recv_at, node_recv, node_sent):
            # NTP's estimate: node clock minus CP clock, assuming symmetric paths.
            row["clock_offset_ms"] = ((node_recv - sent_at) + (node_sent - recv_at)) / 2
        row["cp_total_ms"] = sum(cp.values())
        row["_cp_end"] = cp_search["end"]
    return row


def two_bands(rows, key="transport_total_ms"):
    values = sorted((row[key], index) for index, row in enumerate(rows) if row.get(key) is not None)
    if len(values) < 4:
        return None
    gaps = [(values[i + 1][0] - values[i][0], i) for i in range(len(values) - 1)]
    width, cut = max(gaps)
    low = [rows[index] for _value, index in values[:cut + 1]]
    high = [rows[index] for _value, index in values[cut + 1:]]
    mean = lambda items, field: statistics.fmean(row.get(field) or 0.0 for row in items) if items else 0.0
    gap = mean(high, key) - mean(low, key)
    explained = mean(high, "gate_wait_ms") - mean(low, "gate_wait_ms")
    holders = defaultdict(int)
    for row in high:
        for holder in row.get("gate_holders", {}).values():
            holders[holder] += 1
    return {"split_by": key, "gap_between_bands_ms": width, "low": {"n": len(low), "mean_ms": mean(low, key),
            "mean_gate_wait_ms": mean(low, "gate_wait_ms"), "mean_sweep_overlap_ms": mean(low, "sweep_overlap_ms")},
            "high": {"n": len(high), "mean_ms": mean(high, key), "mean_gate_wait_ms": mean(high, "gate_wait_ms"),
                     "mean_sweep_overlap_ms": mean(high, "sweep_overlap_ms"), "holders_seen": dict(holders)},
            "band_difference_ms": gap, "explained_by_gate_waits": (explained / gap) if gap > 0 else None}


def join_harness(rows, harness: Path):
    """Order join: the i-th harness search span <-> the i-th joined search by CP end time (IF-2 has no id yet)."""
    report = json.loads(harness.read_text())
    spans = [span for case in report.get("reports", []) for span in case.get("spans", []) if span.get("stage") == "search"]
    ordered = sorted((row for row in rows if row.get("_cp_end")), key=lambda row: row["_cp_end"])
    pairs = []
    for span, row in zip(spans, ordered):
        row["harness_span_ms"] = span.get("durationMs")
        row["client_cp_and_untimed_ms"] = span.get("durationMs") - row["cp_total_ms"]
        pairs.append(row)
    return {"harness_search_spans": len(spans), "paired": len(pairs),
            "stage_sum_vs_span": (sum(row["cp_total_ms"] for row in pairs) / sum(row["harness_span_ms"] for row in pairs))
            if pairs else None}


def summarise(rows):
    def total(field):
        return sum(row.get(field) or 0.0 for row in rows)
    relay, transport = total("relay_ms"), total("transport_total_ms")
    node = defaultdict(float)
    for row in rows:
        for stage, ms in row["node"].items():
            node[stage] += ms
        for part, ms in (row.get("send_check_parts_ms") or {}).items():
            node[f"send_check.{part}"] += ms or 0.0
        for part, ms in (row.get("index_load_parts_ms") or {}).items():
            node[f"index_load.{part}"] += ms or 0.0
    return {"searches": len(rows), "relay_ms": relay, "transport_total_ms": transport,
            "network_queue_ms": total("network_queue_ms"), "cp_send_ms": total("cp_send_ms"),
            "outside_ms": total("outside_ms"), "pre_adapter_ms": total("pre_adapter_ms"),
            "queue_wait_ms": total("queue_wait_ms"), "send_check_ms": total("send_check_ms"), "send_ms": total("send_ms"),
            "node_other_ms": total("node_other_ms"), "gate_wait_ms": total("gate_wait_ms"),
            "sweep_overlap_ms": total("sweep_overlap_ms"), "node_stages_ms": dict(node),
            "unexplained_share_of_relay": (total("node_other_ms") / relay) if relay else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--node-log", type=Path, required=True)
    parser.add_argument("--cp-log", type=Path)
    parser.add_argument("--since", type=float, help="epoch seconds; earlier lines are ignored")
    parser.add_argument("--harness", type=Path, help="harness results JSON (IF-2) for the stage-sum check")
    parser.add_argument("--json", type=Path, help="write the aggregate report here")
    args = parser.parse_args(argv)

    node, sweeps = load_node(args.node_log, args.since)
    cp = load_cp(args.cp_log, args.since) if args.cp_log else {}
    rows = [attribute(node[corr], cp.get(corr), sweeps) for corr in node]
    report = {"schema": "IF-3-attribution/v1", "node_searches": len(node), "cp_searches": len(cp),
              "joined": sum(1 for corr in node if corr in cp), "cp_only": sum(1 for corr in cp if corr not in node),
              "sweeps": {"n": len(sweeps), "hold_ms_p50": statistics.median(s["hold_ms"] for s in sweeps) if sweeps else None,
                         "hold_ms_max": max((s["hold_ms"] for s in sweeps), default=None)},
              "totals": summarise(rows), "bands": two_bands(rows)}
    if args.harness:
        report["harness"] = join_harness(rows, args.harness)
    for index, row in enumerate(rows):
        row.pop("_cp_end", None)
        row["search"] = index
    report["per_search"] = rows
    text = json.dumps(report, indent=2, sort_keys=True, default=float)
    if args.json:
        args.json.write_text(text + "\n")
    totals = report["totals"]
    print(f"searches node={report['node_searches']} cp={report['cp_searches']} joined={report['joined']} "
          f"sweeps={report['sweeps']['n']}")
    for key in ("relay_ms", "transport_total_ms", "network_queue_ms", "cp_send_ms", "outside_ms", "pre_adapter_ms",
                "queue_wait_ms", "send_check_ms", "send_ms", "node_other_ms", "gate_wait_ms", "sweep_overlap_ms"):
        print(f"  {key:22} {totals[key]:12.1f}")
    for stage, ms in sorted(totals["node_stages_ms"].items()):
        print(f"  node.{stage:17} {ms:12.1f}")
    if report["bands"]:
        bands = report["bands"]
        print(f"bands: low n={bands['low']['n']} mean={bands['low']['mean_ms']:.0f} | high n={bands['high']['n']} "
              f"mean={bands['high']['mean_ms']:.0f} | explained by gate waits: {bands['explained_by_gate_waits']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
