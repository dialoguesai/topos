"""N5's timing class, sized: the send check when the node committed something between the recheck and the send,
against the send check when it did not, on the same queries. What is released must not change.

The fixture is `direct_search_twins.build(protected=True)`: recovered iMessages with an active Off-limits boundary,
so the send check's member loop, when it runs, re-proves every member. Every query runs twice per rep, the order
shuffled: quiet, and with one unrelated canonical commit (an entity row no decision reads) landing after the
checkpoint. The transport's own `send_check` timing line gives the send check's duration; the search's
SearchVerification says whether it skipped its member loop.

Reported per members size: the send check's median quiet and written, the paired shift (run r of query q written
minus the same quiet) with a bootstrap 95% CI -- the size of the 1-bit class WS0 registered with N3a's --, how
often each variant skipped, and whether every output is byte-identical between the two. Exit 0 only when outputs
are identical, something was released, every quiet search skipped and every written one did not.

Synthetic only, under a resolved temporary directory. Run from the engine root:
    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/n5_write_twins.py --out report.json
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import sqlite3
import statistics
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TOPOS_KEY", "synthetic-timing-key")


def paired(quiet: dict, written: dict, rounds: int = 400, seed: int = 11):
    diffs = [written[key] - quiet[key] for key in sorted(quiet) if key in written]
    rng = random.Random(seed)
    medians = sorted(statistics.median([rng.choice(diffs) for _ in diffs]) for _ in range(rounds))
    return statistics.median(diffs), medians[int(0.025 * rounds)], medians[int(0.975 * rounds) - 1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--members", type=int, nargs="+", default=[25, 86])
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--seed", type=int, default=31)
    args = parser.parse_args()

    import pytest
    from tests.permissions_v2 import direct_search_twins as dst
    from tests.permissions_v2.test_message_search_refusals import Socket, relay_message, signed
    from topos.permissions_v2 import search_transport
    from topos.permissions_v2.canonical import canonical_bytes
    from topos.permissions_v2.search_release import MessageSearchRelease

    lines = []

    class Keep(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            if "stage=send_check " in message:
                lines.append(float(message.split("elapsed_ms=")[1].split()[0]))
    logger = logging.getLogger("topos.permissions_v2.search_timing")
    logger.setLevel(logging.INFO)
    logger.addHandler(Keep())

    report = {"reps": args.reps, "queries": args.queries, "sizes": {}}
    ok = True
    with tempfile.TemporaryDirectory(prefix="n5-write-twins-") as scratch, pytest.MonkeyPatch.context() as mp:
        scratch = Path(scratch).resolve()
        mp.setenv("TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS", "true")
        made = []
        original_verification = MessageSearchRelease.verification

        def verification(self):
            made.append(original_verification(self))
            return made[-1]
        mp.setattr(MessageSearchRelease, "verification", verification)
        for members in args.members:
            node = dst.build(scratch / f"m{members}", members=members, hidden_facts=0, seed=args.seed, protected=True)
            queries = dst.queries(members, args.seed, args.queries)
            written = {"n": 0}
            original_dispatch = node.search.dispatch
            state = {"write": False}

            def dispatch(**kwargs):
                result = original_dispatch(**kwargs)
                if state["write"]:
                    written["n"] += 1
                    with sqlite3.connect(node.corpus.path, timeout=5) as conn:
                        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                                     "VALUES(?,'person','Quinn Other','quinn other')", (f"unrelated-{written['n']}",))
                return result
            mp.setattr(node.search, "dispatch", dispatch)
            order = [(variant, query, rep) for rep in range(args.reps) for query in queries
                     for variant in ("quiet", "write")]
            random.Random(3).shuffle(order)
            timings = {"quiet": {}, "write": {}}
            outputs = {"quiet": {}, "write": {}}
            skipped = {"quiet": 0, "write": 0}
            released = 0
            for number, (variant, query, rep) in enumerate(order):
                state["write"] = variant == "write"
                payload = {"query": query, "k": 10}
                request_id = f"w-{members}-{number}"
                message = relay_message(node, signed(node, payload=payload, request_id=request_id), payload, mp,
                                        request_id=request_id)
                lines.clear()
                made.clear()
                socket = Socket()
                asyncio.run(search_transport.dispatch_message_search(socket, message))
                [frame] = [json.loads(value) for value in socket.sent]
                if frame["status"] != "ok" or len(lines) != 1 or len(made) != 1:
                    raise SystemExit(f"search refused or unmeasured on the fixture ({variant})")
                records = frame["payload"]["output"]["records"]
                released += len(records)
                key = (query, rep)
                timings[variant][key] = lines[0]
                outputs[variant][key] = hashlib.sha256(canonical_bytes(records)).hexdigest()
                skipped[variant] += made[0].reused["send"]
            identical = outputs["quiet"] == outputs["write"]
            shift, low, high = paired(timings["quiet"], timings["write"])
            size = len(order) // 2
            entry = {"searches_per_variant": size, "released_total": released,
                     "identical_outputs_quiet_vs_written": identical,
                     "send_check_ms": {variant: {"median": round(statistics.median(values.values()), 3),
                                                 "p95": round(sorted(values.values())[int(0.95 * (len(values) - 1))], 3)}
                                       for variant, values in timings.items()},
                     "paired_shift_ms": round(shift, 3), "paired_ci95_ms": [round(low, 3), round(high, 3)],
                     "skipped": {variant: f"{count}/{size}" for variant, count in skipped.items()}}
            report["sizes"][f"members={members}"] = entry
            ok = ok and identical and released > 0 and skipped["quiet"] == size and skipped["write"] == 0
            print(json.dumps({f"members={members}": entry}), flush=True)
    report["pass"] = ok
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
