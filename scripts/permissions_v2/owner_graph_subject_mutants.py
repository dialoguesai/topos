"""Mutation run over the owner's graph edges and known-item reads binding to the attested self.

The owner's goal (`pursues`) and place (`located_at`) edges start from the subject derived owner
facts bind to (`fact_owner_subject`), and neither owner spelling may become a goal's related entity
or a place. The facts-direct lane reads the owner's attested self as well as the fact-bearing row.
Every mutant here must be killed by a test.

As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/` and `fixtures/` are copied into a
scratch directory and each mutant is applied there, one at a time; the worktree is never modified.
A patch that no longer applies is reported as such, never as killed.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/owner_graph_subject_mutants.py --out owner-graph-subject-mutants.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENRICHERS = "topos/features/entities/graph_enrichers.py"
TESTS = ["tests/features/test_graph_owner_subject.py", "tests/features/test_graph_enrichers.py",
         "tests/features/test_materialized_edge_evidence.py"]

MUTANTS = [
    ("edges_start_at_the_guess", ENRICHERS,
     "    from .owner import fact_owner_subject\n    _o = fact_owner_subject(conn)",
     "    from .owner import owner_entity_id\n    _o = owner_entity_id(conn)"),
    ("spellings_drop_the_guess", ENRICHERS,
     "    return {str(s) for s in (owner, owner_entity_id(conn)) if s}",
     "    return {str(owner)} if owner else set()"),
    ("goal_excludes_only_the_owner", ENRICHERS,
     "                    if str(ent_id) in selves or str(ent_id) == node_id:",
     "                    if str(ent_id) == owner or str(ent_id) == node_id:"),
    ("goal_excludes_everyone", ENRICHERS,
     "                    if str(ent_id) in selves or str(ent_id) == node_id:",
     "                    if True:"),
    ("place_excludes_only_the_owner", ENRICHERS,
     "        if place_id in selves:",
     "        if place_id == owner:"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="owner-graph-subject-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(base)}
        command = [sys.executable, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider", "-m", "not e2e and not live"]
        # A mutant is killed only by a failing test. Without a clean baseline, a broken
        # environment would read as every mutant killed.
        baseline = subprocess.run(command, cwd=base, env=env, capture_output=True, text=True, timeout=1800)
        if baseline.returncode != 0:
            tail = [line for line in baseline.stdout.splitlines() if "passed" in line or "failed" in line or "error" in line][-1:]
            print(json.dumps({"baseline": "failed", "returncode": baseline.returncode, "summary": tail}))
            return 2
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text()
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                print(results[-1], flush=True)
                continue
            target.write_text(original.replace(old, new))
            try:
                run = subprocess.run(command, cwd=base, env=env, capture_output=True, text=True, timeout=1800)
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                # pytest exits 1 only when tests ran and some failed; 2-5 mean the run itself broke.
                status = ("killed" if run.returncode == 1 and failing else
                          "SURVIVED" if run.returncode == 0 else f"run_broken_exit_{run.returncode}")
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
