#!/usr/bin/env python3
"""Measure the head against the keyword rules on REAL captured traffic.

Reads the shadow log and prints the promotion report: the turn-level agreement between
Horos and the incumbent keyword router, in the Horos card's own metric vocabulary, plus
which promotion gates in ``topos/query/scope_arbiter.py`` that clears.

The method IS the script — the numbers in ``NOTES_HOROS_ROUTING_ENDGAME.md`` are its
output, so a reader can regenerate them rather than take them on trust.

Read-only. It opens one JSONL file and writes nothing; it never touches the node
database. Usage (from ``topos/``)::

    .venv/bin/python scripts/horos_routing_endgame.py
    .venv/bin/python scripts/horos_routing_endgame.py --log /path/to/scope_shadow.jsonl --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from topos.query.scope_arbiter import promotion_status
    from topos.query.scope_shadow import ShadowLog, default_log_path, summarize, turn_coverage

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=str(default_log_path()))
    parser.add_argument("--json", action="store_true", help="machine-readable, text-free")
    args = parser.parse_args()

    path = Path(args.log).expanduser()
    if not path.is_file():
        print(f"no shadow log at {path} — arm it with TOPOS_SCOPE_SHADOW=1 first")
        return 1
    rows = ShadowLog(path).read()
    status = promotion_status(rows)

    if args.json:
        print(json.dumps(
            {
                "rows": len(rows),
                "row_level": summarize(rows).as_telemetry(),
                "turn_coverage": turn_coverage(rows),
                "promotion": status.as_telemetry(),
            },
            indent=2,
        ))
        return 0

    c = status.comparison
    print(f"shadow log            {path}  ({len(rows)} rows)")
    print(f"routed turns          {c['turns']}")
    print(f"  by verdict          {c['by_verdict']}")
    print(f"  exact               {c['exact']}   (card, held-out: see head.json metrics.exact)")
    print(f"  dead_rate           {c['dead_rate']}   (card: metrics.dead_rate)")
    print(f"  disjoint_rate       {c['disjoint_rate']}   (card: metrics.disjoint_rate)")
    print(f"  escalation_rate     {c['escalation_rate']}")
    print(f"  multi-scope turns   rules {c['multi_scope_turns']} / head {c['multi_scope_predicted']}")
    print("  per-scope recall vs the rules (n = turns the rules routed there):")
    for scope, recall in c["per_scope_recall"].items():
        print(f"    {scope:28s} {recall:.3f}  n={c['scope_turns'][scope]}")
    if c["divergence"]:
        print("  divergence (rules -> head, disjoint turns only):")
        for pair, n in c["divergence"].items():
            print(f"    {pair:52s} {n}")
    print()
    print(f"row-level (summarize) {summarize(rows).as_telemetry()}")
    print(f"turn coverage         {turn_coverage(rows)}")
    print()
    print(f"PROMOTION READY: {status.ready}")
    for failure in status.failures:
        print(f"  FAIL  {failure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
