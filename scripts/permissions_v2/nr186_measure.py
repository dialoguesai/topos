#!/usr/bin/env python3
"""NR186: CPU per scanned row of an automatic-review pass, the switch off against on, on invented data with a large
name set, and the two passes' decisions compared.

The corpus is `tests/permissions_v2/review_pass_corpus.build` with `--rows` recovered owner messages in one
conversation and an identity spine of `--universe` units (per unit 300 people with three names each, 30 merge
tombstones and 150 contacts with two addresses: 40 units is about 43,000 names, the size measured on a copy of the
owner's node). Every arm runs on a byte copy of one corpus put back at the same path, so their reviews compare.

Passes, each off then on:
- `assess`: no row assessed yet; each row is prepared, classified by a fixed stand-in and published (publish prepares
  again under the gate);
- `rescan`: every row current (the catch-up's usual case); each row is prepared and found current;
- `rescan_commits`: the same with an unrelated commit before every 10th row (the token moves, the reads prove it);
- `rescan_offlimits`: the same with an Off-limits alias added before the middle row (the boundary is built again).
CPU is `time.process_time()` of the whole pass; `prepare` CPU (held under the write gate) is summed separately.
Decisions compared per pass: every status update, every classifier input, every published assessment.

    <topos venv python> scripts/permissions_v2/nr186_measure.py --out <file.json> [--rows 200] [--universe 40]
Run from the engine root with TOPOS_KEY=synthetic and TOPOS_ENV_FILE / TOPOS_DATABASE_PATH in scratch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SWITCH = "TOPOS_PERMISSIONS_V2_REVIEW_PASS_BOUNDARY"


def run(corpus, *, switch: bool, every=None, change=None, at=None):
    from tests.permissions_v2 import review_pass_corpus as rc
    from tests.permissions_v2.message_search_harness import owner
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2 import automatic_review_worker as arw
    from topos.permissions_v2 import review_pass_boundary as rpb
    os.environ[SWITCH] = "true" if switch else "false"
    calls, updates, prepare_cpu, made = [], [], [0.0], []
    real_prepare, real_keeper = arw.prepare, rpb.ReviewPassBoundary

    def timed(*args, **kwargs):
        started = time.process_time()
        try:
            return real_prepare(*args, **kwargs)
        finally:
            prepare_cpu[0] += time.process_time() - started

    class Seen(real_keeper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    async def classify(prepared):
        calls.append((prepared["snapshot"].message.identity.record_id, prepared["context_revision"], prepared["input"]))
        return rc.answer(prepared)
    worker = arw.AutomaticReviewWorker(corpus.resolver, corpus.reviews, classifier=classify)
    original, scanned = worker._update, [0]

    def update(**counts):
        if counts.get("scanned"):
            scanned[0] += 1
            if every and scanned[0] % every == 0:
                rc.CHANGES["unrelated_commit"](corpus.canonical, corpus.natives)
            if change and scanned[0] == at:
                rc.CHANGES[change](corpus.canonical, corpus.natives)
        updates.append(tuple(sorted(counts.items())))
        original(**counts)
    worker._update = update
    arw.prepare, rpb.ReviewPassBoundary = timed, Seen
    try:
        with owner():
            started, wall = time.process_time(), time.perf_counter()
            asyncio.run(worker._process(corpus.request, refresh=False))
            cpu, wall = time.process_time() - started, time.perf_counter() - wall
            status = worker.status()
    finally:
        arw.prepare, rpb.ReviewPassBoundary = real_prepare, real_keeper
        os.environ.pop(SWITCH, None)
    published = {}
    with corpus.reviews._db() as db:
        for native in corpus.natives:
            review = corpus.reviews._current_in(db, amr.machine_key(corpus.identity(native.message_id)))
            if review is not None:
                dump = review.model_dump()
                dump.pop("review_id")
                dump.pop("reviewed_at")
                published[native.message_id] = dump
    rows = max(scanned[0], 1)
    return {"cpu_s": round(cpu, 3), "wall_s": round(wall, 3), "scanned": scanned[0],
            "cpu_ms_per_row": round(1000 * cpu / rows, 2),
            "prepare_cpu_ms_per_row": round(1000 * prepare_cpu[0] / rows, 2),
            "prepares": scanned[0] + len(calls), "status": status.model_dump(),
            "keeper": made[0].counts if made else None,
            "_decisions": {"updates": updates, "calls": calls, "published": published}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=200)
    parser.add_argument("--universe", type=int, default=40)
    parser.add_argument("--scratch", type=Path, default=None)
    args = parser.parse_args(argv)
    import topos
    from tests.permissions_v2 import review_pass_corpus as rc
    scratch = Path(tempfile.mkdtemp(prefix="nr186-measure-", dir=args.scratch))
    bodies = [f"{rc.BODIES[number % len(rc.BODIES)]} Note {number}." for number in range(args.rows)]
    corpus = rc.build(scratch / "corpus", bodies=bodies, universe=args.universe)
    fresh = rc.keep(corpus, scratch / "fresh")
    report = {"engine": str(Path(topos.__file__).parent), "rows": args.rows, "universe": corpus.universe,
              "load_before": os.getloadavg(), "passes": {}}
    assessed = None
    for name, options in (("assess", {}), ("rescan", {}), ("rescan_commits", {"every": 10}),
                          ("rescan_offlimits", {"change": "offlimits_alias", "at": args.rows // 2})):
        base = fresh if name == "assess" else assessed
        arms = {}
        for switch in (False, True):
            corpus = rc.restore(corpus, base)
            arms["on" if switch else "off"] = run(corpus, switch=switch, **options)
            if name == "assess" and not switch:
                assessed = rc.keep(corpus, scratch / "assessed")   # the off arm's reviews: every rescan starts there
        same = {key: arms["off"]["_decisions"][key] == arms["on"]["_decisions"][key]
                for key in ("updates", "calls", "published")}
        counts = {key: len(arms["off"]["_decisions"][key]) for key in ("updates", "calls", "published")}
        for arm in arms.values():
            arm.pop("_decisions")
        report["passes"][name] = {"off": arms["off"], "on": arms["on"], "identical": same, "compared": counts,
                                  "cpu_ratio_on_off": round(arms["on"]["cpu_s"] / max(arms["off"]["cpu_s"], 1e-9), 4)}
        print(json.dumps({name: report["passes"][name]}), flush=True)
    report["load_after"] = os.getloadavg()
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    ok = all(all(entry["identical"].values()) for entry in report["passes"].values())
    print(json.dumps({"identical": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
