#!/usr/bin/env python3
"""Pre-tag load gate (prototype): a scratch node from the candidate tree, a large invented database, concurrent load.

    uv run python scripts/node_load_gate.py                      # this checkout: one run, ~3.5 min (+1 cold seed)
    uv run python scripts/node_load_gate.py --node-tree ../wt --repeats 3
    uv run python scripts/node_load_gate.py --record             # also post release/load-gate on HEAD

Writes ``load-gate.json`` (verdict, gates, per-run metrics) under ``--out``. The
design, the gates kept from the focused-45 stress report and the open questions
are in docs/PRE_TAG_LOAD_GATE.md. This is ADVISORY: nothing in the release
policy (select_release_checks.py, node_release_gate.py, publish.yml) requires it.

Safety: one heavy run at a time (refuses while Rig D, a stress run or another
load gate is running, and on battery); the node gets a scratch HOME, its database and socket
inside it, a dummy key, no control plane and a non-local model URL; the live
database's ``user_version`` is read (read-only) before and after every run, and
the node's ``Serving database`` line must name the scratch file or it is killed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

try:
    from scripts.load_gate import drive, seed, verdict
    from scripts.load_gate.node import (ScratchNode, host_state, live_db_user_version, live_node_actions,
                                        other_heavy_runs, real_home)
except ModuleNotFoundError:  # direct `python scripts/node_load_gate.py`
    from load_gate import drive, seed, verdict
    from load_gate.node import (ScratchNode, host_state, live_db_user_version, live_node_actions, other_heavy_runs,
                                real_home)

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = "release/load-gate"
SCHEMA = 1
MIN_FREE_BYTES = 10 * 1024**3


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {message}", flush=True)


def git(tree: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(tree), *args], text=True).strip()


def tree_identity(tree: Path) -> dict:
    return {"head": git(tree, "rev-parse", "HEAD"), "tree": git(tree, "rev-parse", "HEAD^{tree}"),
            "topos_tree": git(tree, "rev-parse", "HEAD:topos"),
            "clean": not git(tree, "status", "--porcelain", "--untracked-files=normal")}


def description(tree: str) -> str:
    return f"Load gate passed; tree={tree}"


def record_status(head: str, tree: str, state: str, repository: str) -> None:
    detail = description(tree) if state == "success" else f"Load gate {state}; tree={tree}"
    subprocess.run(["gh", "api", "--method", "POST", f"repos/{repository}/statuses/{head}", "-f", f"state={state}",
                    "-f", f"context={CONTEXT}", "-f", f"description={detail}"], cwd=ROOT, check=True,
                   stdout=subprocess.DEVNULL)


def _innermost_topos_frames(stacks: str) -> list[str]:
    """Each thread's innermost topos frame from a faulthandler dump (most recent call first): where it waits."""
    frames, want = [], False
    for line in stacks.splitlines():
        line = line.strip()
        if line.startswith(("Thread 0x", "Current thread")):
            want = True
        elif want and line.startswith("File ") and "/topos/" in line:
            frames.append(line.split("/topos/", 1)[1].replace('", line ', ":"))
            want = False
    return sorted(set(frames))[:30]


def remove_database(db: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)


def one_run(*, index: int, run_root: Path, args, seed_manifest: dict, live_before) -> dict:
    node = ScratchNode(root=run_root / f"{index}", node_tree=args.node_tree, node_python=args.node_python,
                       label="node")
    if args.txn_watchdog == "off":
        node.extra_env["TOPOS_DB_TXN_WATCHDOG"] = "0"
    node.env()
    shutil.copyfile(seed_manifest["path"], node.db_path)
    started = time.monotonic()
    timings: dict = {}
    run: dict = {"index": index, "root": str(node.root), "host": {"before": host_state()}}
    window_start = time.time()
    node.start()
    try:
        node.wait_serving()
        timings["boot_s"] = round(node.wait_healthy(), 1)
        log(f"run {index}: node pid {node.process.pid} healthy in {timings['boot_s']} s on port {node.port}")
        profile = drive.Profile(load_s=args.load_s, query_workers=args.query_workers,
                                analytics_workers=args.analytics_workers, seed=args.seed + index)
        t = time.monotonic()
        driven = drive.run(socket_path=str(node.socket_path), port=node.port, profile=profile)
        timings["drive_s"] = round(time.monotonic() - t, 1)
        if driven["hung_at_s"] is not None:
            stacks = node.dump_stacks()
            run["hung"] = {"at_s": driven["hung_at_s"],
                           "stacks_file": str(node.root / f"stacks-{node.process.pid}.txt"),
                           "threads": stacks.count("Thread 0x"),
                           "innermost_topos_frames": _innermost_topos_frames(stacks)}
            log(f"run {index}: node hung at {driven['hung_at_s']} s; stacks in {run['hung']['stacks_file']}")
    finally:
        code = node.stop()
        if not args.keep:
            remove_database(node.db_path)
    run["host"]["after"] = host_state()
    run["host"]["live_node_actions"] = live_node_actions(window_start, time.time())
    timings["total_s"] = round(time.monotonic() - started, 1)
    live_after = live_db_user_version()
    text = node.log_path.read_text(encoding="utf-8", errors="replace")
    parsed = verdict.parse_node_log(text)
    requests = verdict.summarize_records(driven["records"])
    requests["hung_at_s"] = driven["hung_at_s"]
    requests["queries_per_minute"] = round(requests["queries"] / (args.load_s / 60), 1)
    isolation = {"serving_scratch": bool(node.serving), "live_db_unchanged": live_after == live_before,
                 "live_user_version": live_after, "guard_journal_empty": not node.journal_lines(),
                 "egress_blocked": len(node.egress_lines()), "node_exit": code}
    result = verdict.evaluate(log=parsed, requests=requests, isolation=isolation)
    (node.root / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in driven["records"]),
                                             encoding="utf-8")
    run.update(timings=timings, isolation=isolation, requests=requests,
               log={k: v for k, v in parsed.items() if k != "slow_sections"},
               slow_sections=sorted(parsed["slow_sections"], key=lambda s: -s["held_s"])[:10], **result)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--node-tree", type=Path, default=ROOT, help="Candidate checkout (default: this one)")
    parser.add_argument("--node-python", type=Path,
                        help="Python 3.12 with the candidate's deps (default: <tree>/.venv)")
    parser.add_argument("--base", type=Path, default=real_home() / "Library" / "Caches" / "topos-load-gate")
    parser.add_argument("--out", type=Path, help="Evidence folder (default: the run folder)")
    parser.add_argument("--scale", type=float, default=1.0, help="Seed size; 1.0 is about 0.4 M rows")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--load-s", type=float, default=180.0)
    parser.add_argument("--query-workers", type=int, default=12)
    parser.add_argument("--analytics-workers", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--keep", action="store_true", help="Keep each run's database copy")
    parser.add_argument("--record", action="store_true", help=f"Post {CONTEXT} on the candidate's HEAD (clean tree)")
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "dialoguesai/topos"))
    parser.add_argument("--txn-watchdog", choices=("on", "off"), default="on",
                        help="The write gate's transaction watchdog. On, its SQLite trace callback turns two threads "
                             "on one connection into a hang (loud, fast); off, the same bug shows as rollbacks and "
                             "SQLite errors, as on the owner's node")
    parser.add_argument("--allow-battery", action="store_true", help="Run on battery power (refused by default)")
    args = parser.parse_args()
    args.node_tree = args.node_tree.resolve()
    args.node_python = (args.node_python or args.node_tree / ".venv" / "bin" / "python").absolute()

    busy = other_heavy_runs()
    if busy:
        print(f"Load gate refused: another heavy run is in progress ({'; '.join(busy)})", file=sys.stderr)
        return 2
    free = shutil.disk_usage(args.base.parent if args.base.parent.exists() else Path.home()).free
    if free < MIN_FREE_BYTES:
        print(f"Load gate refused: {free / 1024**3:.1f} GiB free, needs {MIN_FREE_BYTES / 1024**3:.0f}",
              file=sys.stderr)
        return 2
    host = host_state()
    if host["power"] == "battery" and not args.allow_battery:
        print(f"Load gate refused: on battery ({host['battery_pct']}%); pass --allow-battery to run anyway",
              file=sys.stderr)
        return 2
    identity = tree_identity(args.node_tree)
    if args.record and not identity["clean"]:
        print("Load gate refused: --record needs a clean candidate tree", file=sys.stderr)
        return 2
    live_before = live_db_user_version()
    stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    run_root = args.base / "r" / stamp
    out = (args.out or run_root).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log(f"candidate {identity['head'][:12]} tree {identity['tree'][:12]} clean={identity['clean']}; "
        f"live user_version {live_before}; runs under {run_root}")
    if args.record:
        record_status(identity["head"], identity["tree"], "pending", args.repository)

    started = time.monotonic()
    evidence = {"schema": SCHEMA, "gate": "pre-tag-load", "advisory": True, **identity,
                "node_tree_path": str(args.node_tree), "started_at": datetime.now(timezone.utc).isoformat(),
                "profile": {"load_s": args.load_s, "query_workers": args.query_workers,
                            "analytics_workers": args.analytics_workers, "scale": args.scale, "seed": args.seed,
                            "repeats": args.repeats, "txn_watchdog": args.txn_watchdog},
                "host_at_start": host, "gates_source": verdict.GATES, "runs": []}
    try:
        probe = ScratchNode(root=run_root / "probe", node_tree=args.node_tree, node_python=args.node_python,
                            label="probe")
        evidence["interpreter"] = probe.verify_python()
        t = time.monotonic()
        manifest = seed.build(
            node_factory=lambda label: ScratchNode(root=run_root / label, node_tree=args.node_tree,
                                                   node_python=args.node_python, label="node"),
            cache_dir=args.base / "seed-cache", tree_id=identity["topos_tree"], scale=args.scale, seed=args.seed,
            log=log)
        evidence["seed"] = {**manifest, "seed_s": round(time.monotonic() - t, 1)}
        log(f"seed {'reused' if manifest['cached'] else 'built'}: {manifest['bytes'] / 1024**2:.0f} MB, "
            f"user_version {manifest['user_version']}, {sum(manifest['rows'].values()):,} rows")
        for i in range(1, args.repeats + 1):
            run = one_run(index=i, run_root=run_root, args=args, seed_manifest=manifest, live_before=live_before)
            evidence["runs"].append(run)
            r = run["requests"]
            log(f"run {i}: {'PASS' if run['pass'] else 'FAIL ' + ','.join(run['failures'])}; "
                f"queries {r['queries_ok']}/{r['queries']} (p95 {r['query_p95_s']} s), rollbacks "
                f"{run['log']['rollbacks'].__len__()}, sqlite lines {run['log']['sqlite_error_lines']}, longest hold "
                f"{run['log']['longest_hold_s']} s, health max {r['health_longest_s']} s, {run['timings']}")
    except Exception as exc:  # noqa: BLE001 - any setup failure is a failed gate, never a pass
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        log(f"load gate stopped: {evidence['error']}")
    evidence["total_s"] = round(time.monotonic() - started, 1)
    evidence["pass"] = bool(evidence["runs"]) and "error" not in evidence and all(r["pass"] for r in evidence["runs"])
    evidence["failures"] = sorted({f for r in evidence["runs"] for f in r["failures"]}) + (
        ["setup"] if "error" in evidence else [])
    evidence["runs_failed"] = sum(1 for r in evidence["runs"] if not r["pass"])
    path = out / "load-gate.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    log(f"{'PASS' if evidence['pass'] else 'FAIL'}: {evidence['runs_failed']}/{len(evidence['runs'])} runs failed; "
        f"{evidence['total_s']} s; evidence {path}")
    if args.record:
        record_status(identity["head"], identity["tree"], "success" if evidence["pass"] else "failure", args.repository)
    return 0 if evidence["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
