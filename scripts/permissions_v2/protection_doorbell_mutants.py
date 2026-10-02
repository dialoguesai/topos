"""Mutation run over the node's protection doorbell (owner decision 2, 1 Oct 2026).

Every rule must be killed by a test: one ring per new revision and none for the same one, a ring marked only once
sent, an empty frame, the off switch, no control-plane connection, and the automatic re-sync client answered a
status only.

Each mutant is one textual patch. As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/`, `fixtures/`
and `scripts/` are copied into a scratch directory and each mutant is applied there, one at a time; the
worktree is never modified. A patch that no longer applies is reported as such, never as killed, and a
mutant is killed only by a failing test after a clean baseline.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/protection_doorbell_mutants.py --out doorbell-mutants.json
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
BELL = "topos/permissions_v2/protection_doorbell.py"
HANDLER = "topos/core/handlers/permissions_v2.py"
TESTS = ["tests/permissions_v2/test_protection_doorbell.py", "tests/permissions_v2/test_protocol_runtime.py"]

MUTANTS = [
    ("rings_again_for_the_same_revision", BELL,
     "        if revision == self._rung:\n            return False\n", ""),
    ("marked_rung_though_not_sent", BELL,
     "        if sent:\n            self._rung = revision\n", "        self._rung = revision\n"),
    ("the_frame_carries_something", BELL,
     "    return {\"id\": \"permissions-v2-protection-\" + secrets.token_hex(16), \"type\": FRAME_TYPE, \"payload\": {}}\n",
     "    return {\"id\": \"permissions-v2-protection-\" + secrets.token_hex(16), \"type\": FRAME_TYPE, \"payload\": {\"node\": 1}}\n"),
    ("the_frame_id_repeats", BELL,
     "    return {\"id\": \"permissions-v2-protection-\" + secrets.token_hex(16), \"type\": FRAME_TYPE, \"payload\": {}}\n",
     "    return {\"id\": \"permissions-v2-protection\", \"type\": FRAME_TYPE, \"payload\": {}}\n"),
    ("the_switch_is_ignored", BELL, "    if not enabled():\n", "    if False:\n"),
    ("sends_without_a_connection", BELL,
     "    if not callable(enqueue):\n        return False\n", ""),
    ("the_automatic_client_may_mutate", HANDLER,
     "    if principal.client_id == AUTO_RESYNC_CLIENT and operation != \"status\":\n", "    if False:\n"),
]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="protection-doorbell-mutants-") as scratch:
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
