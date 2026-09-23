"""Locator-path re-check timing against the hidden set, on production-DDL twin corpora.

Bookkeeping batch 3, plan §1.4(2) and §6. Every cell shares one permitted set
(same seed, same positive units, byte for byte) and differs only in hidden data:
messages, facts (including malformed and non-ASCII shapes) and protection
events. Per cell, each positive fact is qualified the way the locator door does
(`with_qualified(..., contract=attested, discloses_sources=True)`), with the
rollback floor attached when `--floor` is given, in interleaved order. The
report gives per-cell medians, the Hodges-Lehmann shift against the first cell
with a bootstrap 95% CI, and the gate: every shift CI inside +/- max(1 ms, 5%
of the baseline median). Verdicts must be identical across cells.

Synthetic only; a temporary directory, no server, no port, no real database.
    TOPOS_ENV_FILE=<scratch> TMPDIR=<non-symlinked dir> python scripts/permissions_v2/bk3_timing_twins.py --out r.json
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def hodges_lehmann(a, b):
    return statistics.median([y - x for x in a for y in b])


def bootstrap_ci(a, b, rounds=400, seed=7):
    rng = random.Random(seed)
    shifts = sorted(statistics.median([rng.choice(b) for _ in b]) - statistics.median([rng.choice(a) for _ in a])
                    for _ in range(rounds))
    return shifts[int(0.025 * rounds)], shifts[int(0.975 * rounds) - 1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--positives", type=int, default=20)
    parser.add_argument("--cells", nargs="+", default=["0:0:0", "100000:0:0", "0:10000:0", "0:0:10000"],
                        help="hidden_messages:hidden_facts:protection_events[:opaque_share]")
    parser.add_argument("--floor", action="store_true", help="attach the rollback floor (R4) to every read")
    args = parser.parse_args()
    from tests.permissions_v2 import production_corpus as pc
    from topos.permissions_v2.canonical_floor import CanonicalFloorStore
    from topos.permissions_v2.identity import ATTESTED_CONTRACT
    import sqlite3

    report = {"positives": args.positives, "reps": args.reps, "floor": args.floor, "cells": {}}
    with tempfile.TemporaryDirectory(prefix="bk3-timing-") as scratch:
        cells = {}
        for spec in args.cells:
            parts = spec.split(":")
            messages, facts, events = (int(part) for part in parts[:3])
            share = float(parts[3]) if len(parts) > 3 else 0.3
            started = time.perf_counter()
            corpus = pc.build(Path(scratch) / spec.replace(":", "-"), seed=31, positives=args.positives,
                              hidden_messages=messages, hidden_facts=facts, protection_events=events,
                              opaque_share=share)
            if args.floor:
                store = CanonicalFloorStore(Path(scratch) / (spec.replace(":", "-") + ".floor"), owner_id=pc.OWNER_ID,
                                            node_id="node-1", resource_id="resource-1")
                conn = sqlite3.connect(corpus.path)
                try:
                    conn.execute("BEGIN")
                    store.install(conn)
                finally:
                    conn.close()
                corpus.resolver.canonical_floor = store
            cells[spec] = {"corpus": corpus, "build_s": time.perf_counter() - started, "times": [], "verdicts": []}
        order = [(spec, unit) for _ in range(args.reps) for spec in cells for unit in range(args.positives)]
        random.Random(3).shuffle(order)
        for spec, unit in order:
            cell = cells[spec]
            corpus = cell["corpus"]
            # Fact ids are minted per corpus; units are compared by their position, which twins share.
            fact = corpus.positives[unit]
            started = time.perf_counter()
            try:
                verdict = corpus.resolver.with_qualified(fact, reviews=corpus.reviews, contract=ATTESTED_CONTRACT,
                                                         discloses_sources=True, callback=lambda _e, _r: "qualified")
            except Exception as exc:  # noqa: BLE001 -- the verdict is compared across cells
                verdict = getattr(exc, "code", type(exc).__name__)
            cell["times"].append((time.perf_counter() - started) * 1000)
            cell["verdicts"].append((unit, verdict))
        baseline = cells[args.cells[0]]
        gate = True
        for spec, cell in cells.items():
            base = baseline["times"]
            low, high = bootstrap_ci(base, cell["times"])
            bound = max(1.0, 0.05 * statistics.median(base))
            same = sorted(cell["verdicts"]) == sorted(baseline["verdicts"])
            passed = same and -bound <= low and high <= bound
            gate = gate and passed
            report["cells"][spec] = {"build_s": round(cell["build_s"], 2), "median_ms": round(statistics.median(cell["times"]), 3),
                "hl_shift_ms": round(hodges_lehmann(base, cell["times"]), 3), "ci95_ms": [round(low, 3), round(high, 3)],
                "bound_ms": round(bound, 3), "verdicts_identical": same, "pass": passed}
        report["gate"] = gate
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({spec: (entry["median_ms"], entry["hl_shift_ms"], entry["pass"]) for spec, entry in report["cells"].items()}))
    return 0 if gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
