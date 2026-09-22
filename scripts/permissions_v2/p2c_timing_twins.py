"""p2c-v1 timing independence from the hidden set: measured on twin corpora, per stage.

Twin corpora share one permitted set byte for byte (same seed, same positive
units) and differ only in hidden data: unreviewed messages with embeddings, whose
words include every query term (salted), and reviewed-but-withheld units of every
kind. For each cell the same queries run interleaved; wall time is recorded per
adapter stage. The report gives, per stage and cell, the median and the
Hodges-Lehmann shift against the no-hidden cell with a bootstrap 95% CI, and
checks every answer is byte-identical across cells.

Gate (design review condition 6): the discovery stages (index_load + embed +
rank) must show a shift CI inside +/- max(1 ms, 5% of the baseline median). The
re-check stage reuses p2a's qualification, whose sibling-fact scan (R2) and copy
count (R1) grow with node size until the bookkeeping stream lands; it is reported,
with its measured cost per re-checked fact, not gated.

Synthetic only. Everything is written under a temporary directory; no server, no
port, no real database. Run from the engine root:
    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/p2c_timing_twins.py --out report.json
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


def build_cell(root: Path, *, positives: int, hidden: int, denied_per_kind: int, seed: int):
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import twin
    counts = {"clean_positive_C": positives}
    extra = {kind: denied_per_kind for kind in mc.HIDDEN_KINDS} if denied_per_kind else None
    return twin(root, f"p{positives}-h{hidden}-d{denied_per_kind}", seed=seed, counts=counts,
                hidden_messages=hidden, extra_withheld=extra)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=10)
    parser.add_argument("--positives", type=int, nargs="+", default=[25, 250])
    parser.add_argument("--hidden", type=str, nargs="+", default=["0:0", "1000:2", "10000:12", "100000:12"],
                        help="hidden_messages:denied_units_per_withheld_kind")
    args = parser.parse_args()
    from tests.permissions_v2 import message_search_corpus as mc

    queries = (["roadmap", "deploy invoice", "sprint review budget", "release latency", "vendor contract onboarding"]
               + [f"{w} {v}" for w, v in zip(mc.WORK_WORDS, mc.PRIVATE_WORDS)] + ["oncologist", "mortgage rent"])[:20]
    report = {"queries": len(queries), "reps": args.reps, "cells": {}, "gate": {}}
    with tempfile.TemporaryDirectory(prefix="p2c-timing-") as scratch:
        for positives in args.positives:
            cells = {}
            for spec in args.hidden:
                hidden, denied = (int(part) for part in spec.split(":"))
                started = time.perf_counter()
                node = build_cell(Path(scratch) / f"p{positives}", positives=positives, hidden=hidden,
                                  denied_per_kind=denied, seed=31)
                cells[spec] = {"node": node, "build_s": time.perf_counter() - started,
                               "stages": {stage: [] for stage in STAGES + ("discovery", "total", "recheck_facts")},
                               "answers": []}
            order = [(spec, query) for _ in range(args.reps) for query in queries for spec in cells]
            random.Random(3).shuffle(order)
            for spec, query in order:
                cell = cells[spec]
                observed = {}
                cell["node"].search.observe = lambda name, value, observed=observed: observed.__setitem__(name, value)
                started = time.perf_counter()
                output, refused = cell["node"].search_request(query, k=10)
                total = time.perf_counter() - started
                assert refused is None, refused
                for stage in STAGES + ("recheck_facts",):
                    cell["stages"][stage].append(observed.get(stage, 0.0))
                cell["stages"]["discovery"].append(sum(observed.get(stage, 0.0) for stage in DISCOVERY))
                cell["stages"]["total"].append(total)
                cell["answers"].append((query, json.dumps(output, sort_keys=True)))
            baseline = cells[args.hidden[0]]
            identical = all(sorted(cell["answers"]) == sorted(baseline["answers"]) for cell in cells.values())
            out = {"identical_answers_across_cells": identical, "cells": {}}
            gate_ok = identical
            for spec, cell in cells.items():
                entry = {"build_s": round(cell["build_s"], 3), "stages_ms": {}}
                for stage in STAGES + ("discovery", "total"):
                    values = [value * 1000 for value in cell["stages"][stage]]
                    base = [value * 1000 for value in baseline["stages"][stage]]
                    low, high = bootstrap_ci(base, values)
                    entry["stages_ms"][stage] = {"median": round(statistics.median(values), 4),
                        "iqr": [round(v, 4) for v in statistics.quantiles(values, n=4)[::2]],
                        "hl_shift_vs_baseline": round(hodges_lehmann(base, values), 4),
                        "ci95": [round(low, 4), round(high, 4)]}
                facts = sum(cell["stages"]["recheck_facts"]) or 1
                entry["recheck_ms_per_fact"] = round(1000 * sum(cell["stages"]["recheck"]) / facts, 4)
                bound = max(1.0, 0.05 * entry["stages_ms"]["discovery"]["median"]) if spec == args.hidden[0] else \
                    max(1.0, 0.05 * statistics.median([v * 1000 for v in baseline["stages"]["discovery"]]))
                low, high = entry["stages_ms"]["discovery"]["ci95"]
                entry["discovery_gate"] = {"bound_ms": round(bound, 4), "pass": -bound <= low and high <= bound}
                gate_ok = gate_ok and entry["discovery_gate"]["pass"]
                out["cells"][spec] = entry
            report["cells"][f"positives={positives}"] = out
            report["gate"][f"positives={positives}"] = gate_ok
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["gate"]))
    return 0 if all(report["gate"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
