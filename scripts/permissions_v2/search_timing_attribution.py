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
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: A text log line's own stamp (the node's coloured console format), in the machine's local time.
TEXT_STAMP = re.compile(r"^\s*(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)")
LINE = re.compile(r"permission_search_timing run=([0-9a-f]{32}) stage=([a-z_]+) elapsed_ms=(-?\d+(?:\.\d+)?)"
                  r"((?: [a-z_]+=[A-Za-z0-9_.:-]+)*)")
ADAPTER = ("runtime_setup", "admit", "index_load", "embed", "rank", "recheck", "checkpoint", "sign")
CP_STAGES = ("authentication", "issuance", "consent_before", "routing", "relay", "revalidation", "consent_send")
SEND_CHECK_PARTS = ("open", "protection", "authority", "commit", "check_own")
EXACT_GATES = ("runtime_setup", "recheck", "send_check")
PROBED_GATES = ("admit", "index_load")
SWEEPER = "p2c-index-sweep"
GATE_WAIT_FLOOR_MS = 100.0  # below this a search did not wait for the gate in any way that matters
SWEEP_COVERED_SHARE = 0.9   # a wait counts as the sweep's when a sweep held the gate for >= 90% of it
H1_SHARE = 0.6              # registered: send_check >= 60% of the relay remainder
H2_EXPLAINED, H2_REJECT, H2_HOLDER_SHARE = 0.8, 0.5, 0.75  # registered two-band rule


def timing_lines(path: Path, since: float | None = None, until: float | None = None):
    """(run, stage, ms, fields, wall-clock seconds or None) for every timing line in one log."""
    with path.open(errors="replace") as handle:
        for raw in handle:
            if "permission_search_timing" not in raw:
                continue
            wall = None
            text = ANSI.sub("", raw)
            stamp = TEXT_STAMP.match(text)
            if stamp:
                value = stamp.group(1)
                wall = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f" if "." in value else "%Y-%m-%d %H:%M:%S").timestamp()
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
            if wall is not None and ((since is not None and wall < since) or (until is not None and wall > until)):
                continue
            yield match.group(1), match.group(2), float(match.group(3)), fields, wall


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_node(path: Path, since=None, until=None):
    searches, sweeps = defaultdict(lambda: {"stages": defaultdict(float), "lines": []}), []
    for run, stage, ms, fields, _wall in timing_lines(path, since, until):
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


def load_cp(path: Path, since=None, until=None):
    by_run = defaultdict(list)
    for run, stage, ms, fields, wall in timing_lines(path, since, until):
        by_run[run].append((stage, ms, fields, wall))
    searches = {}
    for run, lines in by_run.items():
        corr = next((fields["corr"] for _stage, _ms, fields, _w in lines if fields.get("corr", "-") != "-"), None)
        if corr is None:
            continue
        stages = defaultdict(float)
        relay = {}
        end = None
        first_start = last_end = None
        for stage, ms, fields, wall in lines:
            stages[stage] += ms
            if stage == "relay":
                relay = fields
            end = max(end or 0.0, wall or 0.0)
            end_at = _float(fields.get("end_at"))
            if end_at is not None:  # epoch ms on the CP's clock: this stage ran [end_at - ms, end_at]
                first_start = end_at - ms if first_start is None else min(first_start, end_at - ms)
                last_end = end_at if last_end is None else max(last_end, end_at)
        searches[corr] = {"stages": stages, "relay": relay, "end": end, "first_start": first_start, "last_end": last_end}
    return searches


def attribute(node_search, cp_search, sweeps):
    stages = node_search["stages"]
    row = {"node": {stage: round(stages.get(stage, 0.0), 3) for stage in ADAPTER}}
    total = stages.get("transport_total")
    row["transport_total_ms"] = total
    queue = executor = resume = 0.0
    gate_exact, probes, gate_intervals = {}, {}, []
    send_check_parts = {}
    for stage, ms, fields in node_search["lines"]:
        if stage == "queue_wait":
            queue += ms
            executor += _float(fields.get("executor_ms"), 0.0)
            resume += _float(fields.get("resume_ms"), 0.0)
        elif stage == "gate_wait":
            gate_exact[fields.get("point")] = ms
            start = _float(fields.get("start_ms"))
            if start is not None:
                gate_intervals.append((start, start + ms))
        elif stage == "gate_probe":
            probes[fields.get("point")] = {"holder": fields.get("holder"), "site": fields.get("site"), "held_ms": ms,
                                           "t_ms": _float(fields.get("t_ms"))}
        elif stage == "send_check":
            send_check_parts = {part: _float(fields.get(f"{part}_ms")) for part in SEND_CHECK_PARTS}
        elif stage == "transport_total":
            row["outcome"] = fields.get("outcome")
            window_end = _float(fields.get("t_ms"))
            row["_window"] = (window_end - ms, window_end) if window_end is not None else None
            row["_wall"] = (_float(fields.get("recv_at")), _float(fields.get("sent_at")))
    row["queue_wait_ms"], row["queue_executor_ms"], row["queue_resume_ms"] = queue, executor, resume
    row["pre_adapter_ms"] = stages.get("pre_adapter", 0.0)
    row["send_check_ms"] = stages.get("send_check", 0.0)
    row["send_check_parts_ms"] = send_check_parts
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
    # Who the exact waits waited for: the part of each wait's own interval a sweep held the gate.
    exact_total = sum(value for value in gate_exact.values() if value)
    covered = sum(max(0.0, min(end, sweep["end"]) - max(start, sweep["start"]))
                  for start, end in gate_intervals for sweep in sweeps)
    row["gate_wait_exact_total_ms"], row["gate_wait_sweep_covered_ms"] = exact_total, covered
    row["gate_wait_holder"] = ("none" if exact_total < GATE_WAIT_FLOOR_MS
                               else "sweep" if covered >= SWEEP_COVERED_SHARE * exact_total else "other")
    row["relay_remainder_ms"] = None  # CP relay minus the adapter's own stages (H1), set below when the CP is joined

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
        row["cp_stages_ms"] = sum(ms for stage, ms in cp.items() if stage != "relay")
        if cp_search.get("first_start") is not None and cp_search.get("last_end") is not None:
            row["cp_wall_ms"] = cp_search["last_end"] - cp_search["first_start"]
            row["cp_untimed_ms"] = row["cp_wall_ms"] - row["cp_total_ms"]
            row["_cp_span"] = (cp_search["first_start"], cp_search["last_end"])
        if relay is not None:
            row["relay_remainder_ms"] = relay - sum(stages.get(stage, 0.0) for stage in ADAPTER)
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
    ratio = (explained / gap) if gap > 0 else None
    sweep_share = (sum(1 for row in high if row.get("gate_wait_holder") == "sweep") / len(high)) if high else None
    verdict = ("inconclusive" if ratio is None or sweep_share is None
               else "supported" if ratio >= H2_EXPLAINED and sweep_share >= H2_HOLDER_SHARE
               else "rejected" if ratio < H2_REJECT else "inconclusive")
    # Secondary, NOT the registered rule: how much of the spread in `key` gate waits carry, by least squares.
    pairs = [(row.get("gate_wait_ms") or 0.0, row[key]) for row in rows if row.get(key) is not None]
    fit = None
    if len(pairs) >= 3:
        mx, my = statistics.fmean(x for x, _ in pairs), statistics.fmean(y for _, y in pairs)
        sxx = sum((x - mx) ** 2 for x, _ in pairs)
        syy = sum((y - my) ** 2 for _, y in pairs)
        sxy = sum((x - mx) * (y - my) for x, y in pairs)
        if sxx > 0 and syy > 0:
            fit = {"slope": sxy / sxx, "r2": (sxy * sxy) / (sxx * syy)}
    return {"split_by": key, "gap_between_bands_ms": width, "low": {"n": len(low), "mean_ms": mean(low, key),
            "mean_gate_wait_ms": mean(low, "gate_wait_ms"), "mean_sweep_overlap_ms": mean(low, "sweep_overlap_ms")},
            "high": {"n": len(high), "mean_ms": mean(high, key), "mean_gate_wait_ms": mean(high, "gate_wait_ms"),
                     "mean_sweep_overlap_ms": mean(high, "sweep_overlap_ms"), "holders_seen": dict(holders),
                     "holder_sweep_share": sweep_share},
            "band_difference_ms": gap, "explained_by_gate_waits": ratio, "h2_verdict": verdict,
            "secondary_gate_wait_fit": fit}


def join_harness_v2(rows, report: dict):
    """IF-2/v2 (`cases[].spans`, `cases[].per_search[].sent_at_ms`), joined to the node by time.

    The harness runs on the node's own machine, so its send time and the node's wall-clock receive and
    send marks share one clock: each search span holds exactly one node transport window, and the
    span splits exactly into before the node (client, CP pre-relay stages, uplink, the node's inbound
    queue), the node transport, and after it (downlink, CP post-relay stages, response). With CP lines
    joined too, the CP stages come off the before/after legs; without them the legs stay whole.
    """
    searches = []
    for case in report.get("cases", []):
        spans = [span for span in case.get("spans", []) if span.get("stage") == "search"]
        sent = [item.get("sent_at_ms") for item in case.get("per_search", [])]
        searches += [(start, span["durationMs"], span.get("correlation_id")) for start, span in zip(sent, spans)
                     if isinstance(start, (int, float))]
    by_corr = {row.get("_corr"): row for row in rows if row.get("_corr")}
    paired, ambiguous, by_id = [], 0, 0
    for start, duration, corr in searches:
        end = start + duration
        if corr and corr in by_corr and "harness_span_ms" not in by_corr[corr]:
            row = by_corr[corr]
            by_id += 1
        else:
            inside = [row for row in rows if row.get("_wall") and None not in row["_wall"]
                      and start <= row["_wall"][0] and row["_wall"][1] <= end and "harness_span_ms" not in row]
            if len(inside) != 1:
                ambiguous += len(inside) > 1
                continue
            row = inside[0]
        recv, sent_at = row["_wall"]
        row["harness_span_ms"] = duration
        row["before_node_ms"], row["after_node_ms"] = recv - start, end - sent_at
        row["outside_node_ms"] = duration - (row["transport_total_ms"] or 0.0)
        if row.get("_cp_span") and row.get("clock_offset_ms") is not None:
            # The CP's clock moved onto the harness/node clock with the relay's NTP offset estimate.
            first, last = (value + row["clock_offset_ms"] for value in row["_cp_span"])
            row["client_uplink_ms"], row["client_downlink_ms"] = first - start, end - last
        paired.append(row)
    total = sum(row["harness_span_ms"] for row in paired)
    return {"format": "IF-2/v2", "harness_search_spans": len(searches), "paired": len(paired), "paired_by_id": by_id,
            "ambiguous": ambiguous,
            "before_node_ms": sum(row["before_node_ms"] for row in paired),
            "node_transport_ms": sum(row["transport_total_ms"] or 0.0 for row in paired),
            "after_node_ms": sum(row["after_node_ms"] for row in paired), "harness_search_ms": total}


def join_harness(rows, harness: Path):
    """Order join: the i-th harness search span <-> the i-th joined search by CP end time (IF-2 has no id yet)."""
    report = json.loads(harness.read_text())
    if report.get("schema", "").startswith("IF-2/v2") or "cases" in report:
        return join_harness_v2(rows, report)
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
    remainder = sum(row["relay_remainder_ms"] for row in rows if row.get("relay_remainder_ms") is not None)
    send_check_joined = sum(row["send_check_ms"] for row in rows if row.get("relay_remainder_ms") is not None)
    share = (send_check_joined / remainder) if remainder > 0 else None
    cp_stage_totals = defaultdict(float)
    for row in rows:
        for stage, ms in (row.get("cp") or {}).items():
            cp_stage_totals[stage] += ms
    h1 = {"relay_remainder_ms": remainder, "send_check_ms": send_check_joined, "send_check_share": share,
          "verdict": None if share is None else ("supported" if share >= H1_SHARE else "not_supported")}
    return {"searches": len(rows), "h1": h1, "cp_stages_ms": dict(cp_stage_totals),
            "cp_untimed_ms": total("cp_untimed_ms"), "client_uplink_ms": total("client_uplink_ms"),
            "client_downlink_ms": total("client_downlink_ms"), "relay_ms": relay, "transport_total_ms": transport,
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
    parser.add_argument("--until", type=float, help="epoch seconds; later lines are ignored")
    parser.add_argument("--harness", type=Path, help="harness results JSON (IF-2) for the stage-sum check")
    parser.add_argument("--json", type=Path, help="write the aggregate report here")
    args = parser.parse_args(argv)

    node, sweeps = load_node(args.node_log, args.since, args.until)
    cp = load_cp(args.cp_log, args.since, args.until) if args.cp_log else {}
    harness_ids = None
    if args.harness:
        report_in = json.loads(args.harness.read_text())
        harness_ids = {span.get("correlation_id") for case in report_in.get("cases", []) for span in case.get("spans", [])
                       if span.get("stage") == "search" and span.get("correlation_id")} or None
    if harness_ids:  # the run is what the harness ran: other searches in the window are someone else's
        node = {corr: entry for corr, entry in node.items() if corr in harness_ids}
        cp = {corr: entry for corr, entry in cp.items() if corr in harness_ids}
    rows = []
    for corr in node:
        row = attribute(node[corr], cp.get(corr), sweeps)
        row["_corr"] = corr
        rows.append(row)
    report = {"schema": "IF-3-attribution/v1", "node_searches": len(node), "cp_searches": len(cp),
              "joined": sum(1 for corr in node if corr in cp), "cp_only": sum(1 for corr in cp if corr not in node),
              "sweeps": {"n": len(sweeps), "hold_ms_p50": statistics.median(s["hold_ms"] for s in sweeps) if sweeps else None,
                         "hold_ms_max": max((s["hold_ms"] for s in sweeps), default=None)},
              "totals": summarise(rows), "bands": two_bands(rows)}
    if args.harness:
        report["harness"] = join_harness(rows, args.harness)
    for index, row in enumerate(rows):
        for private in ("_cp_end", "_wall", "_corr", "_cp_span"):
            row.pop(private, None)
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
    if report.get("harness", {}).get("format") == "IF-2/v2":
        harness = report["harness"]
        print(f"harness IF-2/v2: {harness['paired']}/{harness['harness_search_spans']} searches paired; "
              f"before-node {harness['before_node_ms']:.0f} + node {harness['node_transport_ms']:.0f} + "
              f"after-node {harness['after_node_ms']:.0f} = {harness['harness_search_ms']:.0f} ms")
    if report["bands"]:
        bands = report["bands"]
        print(f"bands: low n={bands['low']['n']} mean={bands['low']['mean_ms']:.0f} | high n={bands['high']['n']} "
              f"mean={bands['high']['mean_ms']:.0f} | explained by gate waits: {bands['explained_by_gate_waits']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
