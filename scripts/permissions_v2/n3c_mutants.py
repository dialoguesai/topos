"""Mutation run over WS4 N3c's provenance pass (reconciliation_provenance.ExistingProvenancePass and its search_index
call sites). Every mutant weakens one decision: where the pass's one `_check` and snapshot re-hash run relative to
its members (the revocation caveat: after the last member, never at the start), whether the members and that check
read one snapshot, what a member's own reads still require, whether the pass can outlive its pass, and whether the
gate is released when the check refuses. Each must be killed by at least one test.

As `p2c_mutants.py`: a scratch copy of the engine, one mutant at a time, the worktree never modified. A mutant is
one or more exact edits; one whose text does not match exactly once counts as a failure, not a pass. A mutant may
name the tests that must kill it first (a leaked gate deadlocks every later test that needs it from another
thread); a run that hangs past its timeout is recorded as HUNG, never as killed.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/n3c_mutants.py --out n3c-mutants.json
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
PASS = "topos/permissions_v2/reconciliation_provenance.py"
INDEX = "topos/permissions_v2/search_index.py"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_search_provenance_pass.py", "test_reconciliation_provenance.py", "test_search_verification.py")]

MUTANTS = [
    # The revocation caveat: the one check runs after the last member.
    ("check_at_the_start_of_the_pass", [
        (PASS, "            self._generation = _source_generation(conn)\n",
               "            self._generation = _source_generation(conn)\n"
               "            with self._gate_wait('check'):\n"
               "                self._start_check = self._service._check(conn)\n"),
        (PASS, "                    generation = self._service._check(self.conn)\n",
               "                    generation = self._start_check\n")]),
    ("no_check_at_the_end", [
        (PASS, "                    generation = self._service._check(self.conn)\n",
               "                    generation = self._generation\n")]),
    ("finish_before_the_member_loop", [
        (INDEX, "        checked = {}\n        if laps is not None:\n            laps.update(dependencies=0.0, dependency_boundary=0.0)\n",
                "        checked = {}\n        if provenance is not None:\n            provenance.finish()\n"
                "        if laps is not None:\n            laps.update(dependencies=0.0, dependency_boundary=0.0)\n")]),
    ("finish_never_called", [
        (INDEX, "        if provenance is not None:\n            # After the last member",
                "        if False:\n            # After the last member")]),
    ("finish_refusal_ignored", [
        (INDEX, "                provenance.finish()\n            except PolicyError:\n                return stale(\"member_unavailable\")\n",
                "                provenance.finish()\n            except PolicyError:\n                pass\n")]),
    # The snapshot file is outside the snapshot too: re-hashed after the last member.
    ("rehash_at_the_start_of_the_pass", [
        (PASS, "        pinned = self._snapshots.setdefault(snapshot['snapshot_id'], [])\n",
               "        if snapshot['snapshot_id'] not in self._snapshots and service._snapshot(\n"
               "                snapshot['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != snapshot:\n"
               "            raise PolicyError('ingest_snapshot_changed')\n"
               "        pinned = self._snapshots.setdefault(snapshot['snapshot_id'], [])\n"),
        (PASS, "                for snapshot_id, pinned in self._snapshots.items():\n",
               "                for snapshot_id, pinned in {}.items():\n")]),
    ("no_rehash", [
        (PASS, "                for snapshot_id, pinned in self._snapshots.items():\n",
               "                for snapshot_id, pinned in {}.items():\n")]),
    # One snapshot for the members and the check.
    ("generation_not_compared", [
        (PASS, "            if (generation != self._generation\n                    or self.conn",
               "            if (False\n                    or self.conn")]),
    ("snapshot_identity_not_compared", [
        (PASS, "                    or self.conn.execute('PRAGMA data_version').fetchone()[0] != self._version):\n",
               "                    or False):\n")]),
    ("proves_outside_a_read_transaction", [
        (PASS, "        if self._closed or conn is not self.conn or not conn.in_transaction:\n",
               "        if self._closed or conn is not self.conn:\n")]),
    ("proves_on_another_connection", [
        (PASS, "        if self._closed or conn is not self.conn or not conn.in_transaction:\n",
               "        if self._closed or not conn.in_transaction:\n")]),
    ("proves_after_the_check", [
        (PASS, "        if self._closed or conn is not self.conn or not conn.in_transaction:\n",
               "        if conn is not self.conn or not conn.in_transaction:\n")]),
    # A member's own reads are unchanged.
    ("member_enrollment_not_required_active", [
        (PASS, "            conn, enrollment_id, generation, active=True, source_id='imessage'))",
               "            conn, enrollment_id, generation, active=False, source_id='imessage'))")]),
    # Once per pass, and never across passes or searches.
    ("service_per_member", [
        (PASS, "        if self._service is None:\n            started = time.perf_counter()",
               "        if True:\n            started = time.perf_counter()")]),
    ("check_per_member", [
        (PASS, "lambda enrollment_id: service._enrollment_at(\n            conn, enrollment_id, generation, active=True,",
               "lambda enrollment_id: service._enrollment(\n            conn, enrollment_id, active=True,")]),
    ("service_shared_across_passes", [
        (PASS, "class ExistingProvenancePass:\n", "_SHARED = {}\n\n\nclass ExistingProvenancePass:\n"),
        (PASS, "                    self._service = IngestProvenanceService(",
               "                    self._service = _SHARED.get(str(self._canonical_database)) or IngestProvenanceService("),
        (PASS, "        if self._generation is None:\n",
               "        _SHARED[str(self._canonical_database)] = self._service\n        if self._generation is None:\n")]),
    # The gate on a refusing check: the gate test runs alone, since a leaked gate deadlocks later tests.
    ("gate_kept_when_the_check_refuses", [
        (PASS, "                with self._gate_wait('check'):\n                    generation = self._service._check(self.conn)\n",
               "                gate = __import__('topos.storage.db.write_gate', fromlist=['db_write_lock']).db_write_lock()\n"
               "                gate.acquire()\n"
               "                generation = self._service._check(self.conn)\n"
               "                gate.release()\n")],
     ["tests/permissions_v2/test_search_provenance_pass.py::test_the_gate_is_released_on_every_path"]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="n3c-mutants-") as scratch:
        base = Path(scratch).resolve() / "engine"
        base.mkdir()
        # `scripts` too: the timing tests load the attribution script from the tree they run in, and a copy
        # without it fails them for every mutant (the N5 review read that as a flake under load).
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        for name, edits, *selected in MUTANTS:
            if args.only and name not in args.only:
                continue
            originals = {path: (base / path).read_text() for path, _old, _new in edits}
            patched, applicable = dict(originals), True
            for path, old, new in edits:
                if patched[path].count(old) != 1:
                    applicable = False
                    results.append({"mutant": name, "status": "patch_not_applicable", "count": patched[path].count(old)})
                    break
                patched[path] = patched[path].replace(old, new)
            if not applicable:
                print(results[-1], flush=True)
                continue
            try:
                for path, text in patched.items():
                    (base / path).write_text(text)
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                try:
                    run = subprocess.run([sys.executable, "-m", "pytest", *(selected[0] if selected else TESTS), "-q", "-x",
                                          "-p", "no:cacheprovider"], cwd=base, env=env, capture_output=True, text=True,
                                         timeout=600)
                except subprocess.TimeoutExpired:
                    results.append({"mutant": name, "status": "HUNG", "summary": [], "killed_by": []})
                    print(results[-1], flush=True)
                    continue
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                errored = "error" in " ".join(tail) and not failing
                results.append({"mutant": name, "status": "killed" if run.returncode != 0 and not errored else
                                ("ERRORED" if errored else "SURVIVED"), "summary": tail, "killed_by": failing})
            finally:
                for path, text in originals.items():
                    (base / path).write_text(text)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"mutants": len(results), "killed": killed, "tests": TESTS, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
