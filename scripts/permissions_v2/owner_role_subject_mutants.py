"""Mutation run over the owner-role fallback and the attested fact subject.

Two structural repairs, each guarded by tests that must kill every mutant here:

* the derivation job reads an unclassified message (``actor_role`` NULL) by the role its
  provenance proves, under the source's posture, and retires the progress keys its walks
  wrote when they skipped the owner's own messages for their role;
* a derived owner fact binds to the owner's one attested ``is_self`` entity, and to
  ``owner_entity_id`` exactly as before when there is none.

As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/` and `fixtures/` are copied into a
scratch directory and each mutant is applied there, one at a time; the worktree is never modified.
A patch that no longer applies is reported as such, never as killed.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/owner_role_subject_mutants.py --out owner-role-subject-mutants.json
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
JOB = "topos/enrichment/jobs/canonical/derivation_job.py"
SURFACES = "topos/features/derivation/surfaces.py"
OWNER = "topos/features/entities/owner.py"
EXTRACT = "topos/features/facts/extract.py"
IDENTITY = "topos/permissions_v2/identity.py"
TESTS = ["tests/features/derivation/test_provenance_role_fallback.py",
         "tests/features/derivation/test_fact_owner_subject.py",
         "tests/features/derivation/test_derivation_job.py",
         "tests/features/derivation/test_backfill_run_and_index.py"]

MUTANTS = [
    # --- the role rule
    ("stored_role_ignored", JOB,
     "    stored = str(row.get(\"actor_role\") or \"\").strip()\n    if stored:\n        return stored\n",
     "    stored = str(row.get(\"actor_role\") or \"\").strip()\n"),
    ("null_reads_observed", JOB,
     "    if table in _ROLE_COLUMN_TABLES:\n        from",
     "    if False:\n        from"),
    ("posture_ignored", JOB,
     "        return record_role(row, table=table, posture=posture_for(row))",
     "        return record_role(row, table=table, posture=None)"),
    ("fallback_promotes_everyone", JOB,
     "        return record_role(row, table=table, posture=posture_for(row))",
     "        return \"authored\""),
    ("journal_default_dropped", JOB,
     "    if \"journal\" in table:\n        return \"authored\"\n    if table in _ROLE_COLUMN_TABLES:",
     "    if table in _ROLE_COLUMN_TABLES:"),
    ("other_tables_promoted", JOB,
     "        return record_role(row, table=table, posture=posture_for(row))\n    return \"observed\"",
     "        return record_role(row, table=table, posture=posture_for(row))\n"
     "    from ....features.provenance.roles import record_role\n"
     "    return record_role(row, table=\"conversation_messages\", posture=posture_for(row))"),
    ("walk_reads_stored_only", JOB,
     "                    \"date\": str(at or \"\")[:10], \"role\": role, \"source_id\": \"\",",
     "                    \"date\": str(at or \"\")[:10], \"role\": stored or \"observed\", \"source_id\": \"\","),
    ("walk_drops_owner_flag", JOB,
     "        role = _record_role_for({\"actor_role\": stored, \"is_from_self\": is_self, \"sender_id\": sender_id,",
     "        role = _record_role_for({\"actor_role\": stored, \"is_from_self\": None, \"sender_id\": None,"),
    ("trial_reads_stored_only", JOB,
     "            recent.append((tbl, rid, text, at, _record_role_for(row, tbl, posture_for)))",
     "            recent.append((tbl, rid, text, at, stored or \"observed\"))"),
    # --- retiring the walks' role-skip keys
    ("retirement_not_once", JOB,
     "        if conn.execute(\"SELECT 1 FROM derivation_progress WHERE key=?\",\n"
     "                        (ROLE_SKIP_RETIREMENT_MARKER,)).fetchone():\n            return 0\n",
     ""),
    ("retirement_touches_open_packs", JOB,
     "if \"observed\" not in pack.allowed_roles()}",
     "if True}"),
    ("retirement_ignores_role", JOB,
     "            if role in pack.allowed_roles() and key in existing:",
     "            if key in existing:"),
    ("retirement_drops_judged_keys", JOB,
     "        stale -= judged\n",
     ""),
    ("retirement_skipped_by_batch", JOB,
     "    retired = retire_role_skipped_progress(conn, all_packs, posture_for)",
     "    retired = 0"),
    ("retirement_skipped_by_backfill", SURFACES,
     "    retire_role_skipped_progress(conn, load_packs(pack_dir), posture_for)\n",
     ""),
    # --- the attested subject
    ("subject_is_the_guess", OWNER,
     "    return attested_self(conn) or owner_entity_id(conn)",
     "    return owner_entity_id(conn)"),
    ("attested_self_takes_any", IDENTITY,
     "    return next(iter(subjects)) if len(subjects) == 1 else None",
     "    return next(iter(sorted(subjects))) if subjects else None"),
    ("batch_binds_the_guess", JOB,
     "    _o = fact_owner_subject(conn)",
     "    from ....features.entities.owner import owner_entity_id\n    _o = owner_entity_id(conn)"),
    ("backfill_binds_the_guess", SURFACES,
     "    _owner = fact_owner_subject(conn)",
     "    from ..entities.owner import owner_entity_id\n    _owner = owner_entity_id(conn)"),
    ("rules_bind_the_guess", EXTRACT,
     "    attested = attested_self(conn)\n    if attested:\n        return attested\n",
     ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="owner-role-subject-mutants-") as scratch:
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
