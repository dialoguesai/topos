"""p2c-v3 timing independence from hidden facts, on the direct-message path that runs `_floors`.

The p2c-v1 twins (`p2c_timing_twins.py`) exercise fact-backed members, whose re-check never runs
`message_evidence._floors`. Every qualification of a direct-message member does: the release
re-check runs it once per ranked candidate, under the node write gate. Before WS4 N2 it walked every
fact on the node, so the re-check stage grew with facts the recipient can never see.

Twin corpora here (`tests/permissions_v2/direct_search_twins.py`) share one permitted set byte for
byte (the same recovered, machine-assessed iMessages, the same sibling facts, the same pinned record
key) and differ only in `hidden_facts`: facts that name only messages outside the corpus. For each
cell the same queries run interleaved, and wall time is recorded per adapter stage. The report gives,
per stage and cell, the median and the Hodges-Lehmann shift against the no-hidden cell with a
bootstrap 95% CI, and checks that every answer is byte-identical across cells.

Gate, per cell: the discovery stages (index_load + embed + rank) as in the p2c-v1 twins, and the
re-check stage, which is where `_floors` runs: each shift CI must sit inside
+/- max(1 ms, 5% of the no-hidden cell's median). Every cell runs the same queries the same number
of times, so the gate reads the PAIRED shift: run r of query q in a cell against run r of q in the
no-hidden cell, median of those differences, bootstrap CI over the pairs. A re-check's cost follows
how many candidates the query reaches (1 to 10 here), and an unpaired comparison folds that query
mix into its interval; the unpaired shift is still reported, as the p2c-v1 twins report it.
Exit 0 only if both gates hold everywhere, every answer is identical, and something was released.

Synthetic only. Everything is written under a temporary directory (resolved, so a symlinked system
temp path such as macOS /var does not trip the evidence path checks); no server, no port, no real
database. Run from the engine root:
    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/p2c_direct_timing_twins.py --out report.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TOPOS_KEY", "synthetic-timing-key")

DISCOVERY = ("index_load", "embed", "rank")
STAGES = ("admit",) + DISCOVERY + ("recheck", "checkpoint", "sign")
GATED = ("discovery", "recheck")


def units(queries: list[str], size: int) -> list:
    """What one timed request asks: a query, or with --batch N a batch of N distinct queries.

    Batches are consecutive windows over the query list, wrapping around, so there are as many
    units as queries and each query appears in N of them: every cell times the same units.
    """
    if not size:
        return list(queries)
    if not 1 <= size <= min(6, len(set(queries))):
        raise SystemExit("--batch must be 1..6 and at most the number of distinct queries")
    distinct = list(dict.fromkeys(queries))
    return [tuple(distinct[(start + offset) % len(distinct)] for offset in range(size)) for start in range(len(distinct))]


def ask(node, unit, *, k: int):
    """(outputs, refusal) for one unit: a single search, or one batch frame's items (OD-36)."""
    if isinstance(unit, tuple):
        return node.search_batch_request(list(unit), k=k)
    output, refused = node.search_request(unit, k=k)
    return ([output] if output is not None else None), refused


def hodges_lehmann(a: list[float], b: list[float]) -> float:
    return statistics.median([y - x for x in a for y in b])


def bootstrap_ci(a: list[float], b: list[float], rounds: int = 400, seed: int = 7) -> tuple[float, float]:
    rng = random.Random(seed)
    shifts = []
    for _ in range(rounds):
        sa = [rng.choice(a) for _ in a]
        sb = [rng.choice(b) for _ in b]
        shifts.append(statistics.median(sb) - statistics.median(sa))
    shifts.sort()
    return shifts[int(0.025 * rounds)], shifts[int(0.975 * rounds) - 1]


def paired(base: dict, cell: dict, rounds: int = 400, seed: int = 11) -> tuple[float, float, float]:
    """Median of per-(query, run) differences cell - base, and its bootstrap 95% CI over the pairs."""
    diffs = [cell[key] - base[key] for key in sorted(base) if key in cell]
    rng = random.Random(seed)
    medians = sorted(statistics.median([rng.choice(diffs) for _ in diffs]) for _ in range(rounds))
    return statistics.median(diffs), medians[int(0.025 * rounds)], medians[int(0.975 * rounds) - 1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--members", type=int, nargs="+", default=[25, 250])
    parser.add_argument("--hidden-facts", type=int, nargs="+", default=[0, 1000, 10000, 100000])
    parser.add_argument("--seed", type=int, default=31)
    parser.add_argument("--batch", type=int, default=0,
                        help="0: single searches (default). N in 1..6: every request is one batch of N queries (OD-36, G7)")
    args = parser.parse_args()
    from tests.permissions_v2 import direct_search_twins as dst

    report = {"path": "p2c-v3 direct messages (_floors on every re-check)", "reps": args.reps,
              "queries": args.queries, "batch": args.batch, "cells": {}, "gate": {}}
    with tempfile.TemporaryDirectory(prefix="p2c-direct-timing-") as scratch:
        scratch = Path(scratch).resolve()
        for members in args.members:
            queries = units(dst.queries(members, args.seed, args.queries), args.batch)
            cells = {}
            for hidden in args.hidden_facts:
                started = time.perf_counter()
                node = dst.build(scratch / f"m{members}" / f"h{hidden}", members=members, hidden_facts=hidden,
                                 seed=args.seed)
                cells[hidden] = {"node": node, "build_s": time.perf_counter() - started,
                                 "stages": {stage: [] for stage in STAGES + ("discovery", "total", "recheck_facts")},
                                 "answers": [], "released": 0, "runs": {}, "keyed": {stage: {} for stage in GATED}}
            order = [(hidden, query) for _ in range(args.reps) for query in queries for hidden in cells]
            random.Random(3).shuffle(order)
            for hidden, query in order:
                cell = cells[hidden]
                observed = {}
                # A batch reports its per-item stages once per item: a unit's stage time is their sum.
                cell["node"].search.observe = lambda name, value, observed=observed, **fields: observed.__setitem__(
                    name, observed.get(name, 0.0) + value)
                started = time.perf_counter()
                outputs, refused = ask(cell["node"], query, k=10)
                total = time.perf_counter() - started
                assert refused is None, refused
                for stage in STAGES + ("recheck_facts",):
                    cell["stages"][stage].append(observed.get(stage, 0.0))
                cell["stages"]["discovery"].append(sum(observed.get(stage, 0.0) for stage in DISCOVERY))
                cell["stages"]["total"].append(total)
                cell["answers"].append((json.dumps(query), json.dumps(outputs, sort_keys=True)))
                cell["released"] += sum(len(output["records"]) for output in outputs)
                run = cell["runs"][query] = cell["runs"].get(query, -1) + 1
                cell["keyed"]["discovery"][(query, run)] = cell["stages"]["discovery"][-1] * 1000
                cell["keyed"]["recheck"][(query, run)] = cell["stages"]["recheck"][-1] * 1000
            baseline = cells[args.hidden_facts[0]]
            identical = all(sorted(cell["answers"]) == sorted(baseline["answers"]) for cell in cells.values())
            out = {"identical_answers_across_cells": identical, "released_per_cell": baseline["released"], "cells": {}}
            gate_ok = identical and baseline["released"] > 0
            for hidden, cell in cells.items():
                entry = {"build_s": round(cell["build_s"], 3), "stages_ms": {}, "gates": {}}
                for stage in STAGES + ("discovery", "total"):
                    values = [value * 1000 for value in cell["stages"][stage]]
                    base = [value * 1000 for value in baseline["stages"][stage]]
                    low, high = bootstrap_ci(base, values)
                    entry["stages_ms"][stage] = {"median": round(statistics.median(values), 4),
                        "iqr": [round(v, 4) for v in statistics.quantiles(values, n=4)[::2]],
                        "hl_shift_vs_baseline": round(hodges_lehmann(base, values), 4),
                        "ci95": [round(low, 4), round(high, 4)]}
                facts = sum(cell["stages"]["recheck_facts"]) or 1
                entry["recheck_ms_per_candidate"] = round(1000 * sum(cell["stages"]["recheck"]) / facts, 4)
                for stage in GATED:
                    bound = max(1.0, 0.05 * statistics.median([v * 1000 for v in baseline["stages"][stage]]))
                    shift, low, high = paired(baseline["keyed"][stage], cell["keyed"][stage])
                    entry["gates"][stage] = {"bound_ms": round(bound, 4), "paired_shift_ms": round(shift, 4),
                                             "paired_ci95": [round(low, 4), round(high, 4)],
                                             "pass": -bound <= low and high <= bound}
                    gate_ok = gate_ok and entry["gates"][stage]["pass"]
                out["cells"][str(hidden)] = entry
            report["cells"][f"members={members}"] = out
            report["gate"][f"members={members}"] = gate_ok
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["gate"]))
    return 0 if all(report["gate"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
