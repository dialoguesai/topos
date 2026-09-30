"""Mutation run over OD-46's permitted-message derivation lane and its release-side checks.

Each mutant weakens one decision: which messages the lane may read, what it may store, whether
Off-limits is checked, which subject it writes, and whether release holds an item to the message it
was derived from. Each must be killed by at least one test. Reuses `p2c_mutants.py` (a scratch copy of
the engine, one mutant at a time; the worktree is never modified). A mutant whose text no longer
matches counts as a failure, not a pass.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/od46_mutants.py --out od46-mutants.json

Two mutants are equivalent, kept so a later change that removes the duplicate is noticed:
`lane_ignores_owner_opt_outs` (message_evidence._floors refuses an opted-out message anyway) and
`lane_ignores_the_window` (native_time_within bounds a native row's own clock to the same window).
Both checks mirror `_rebuild_once`, which also makes them twice. A third, `route_skips_its_own_owner_check`, is
equivalent by design: the pass refuses a non-owner itself (`_require_owner`), so neither check depends on the other.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2c_mutants  # noqa: E402

P = "topos/permissions_v2/"
LANE = P + "permitted_derivation.py"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_permitted_derivation.py", "test_knowledge_search.py", "test_grant_census.py")]

MUTANTS = [
    # Which messages the lane reads.
    ("lane_ignores_the_policy_decision", LANE,
     '            if source_message_decision(policy, qualified).verdict != "permit":\n                continue\n', ""),
    ("lane_ignores_owner_opt_outs", LANE, "\n                         and fact_id not in frozen.opt_outs}", "}"),
    ("lane_ignores_the_window", LANE, " or stamp < lower\n", "\n"),
    ("lane_reads_rows_not_owner_authored", LANE,
     '            if not _is_owner_authored({**row, "_table": identity.table}, identity.table):\n',
     "            if False:\n"),
    ("lane_runs_without_the_owner", LANE, "        service._require_owner(service.resolver.binding)\n", ""),
    # What it stores.
    ("unclassed_predicate_gets_a_class", LANE,
     "        klass = CLASSES.get(spec.predicate)\n        if klass is None:",
     "        klass = CLASSES.get(spec.predicate) or CLASSES['works_on']\n        if klass is None:"),
    ("value_need_not_be_atomic", LANE, '            return "value_not_atomic"\n', "            pass\n"),
    ("goal_may_be_a_question", LANE, ' or "?" in value):\n', "):\n"),
    ("off_limits_value_not_checked", LANE, '            return "entity_protected"\n', "            pass\n"),
    ("unavailable_boundary_writes_anyway", LANE,
     '                    conn.execute("ROLLBACK")\n                    count("refused:entity_boundary_unavailable", len(derived))\n'
     '                    return counts\n', "                    boundary = None\n"),
    ("changed_message_written_anyway", LANE,
     "        if len(rows) != 1 or message_revision(identity, rows[0].get(\"content\")) != revision:\n".replace(
         "        if", "                    if"),
     "                    if len(rows) != 1:\n"),
    ("any_self_entity_is_the_subject", LANE,
     "                subject = attested_self(conn)   # OD-29: one attested is_self entity, or nothing is written\n",
     "                subject = attested_self(conn) or 'self'\n"),
    # Release holds a lane item to its message.
    ("release_skips_the_lineage_check", P + "knowledge_projections.py",
     "    check_lineage(payload,sources)\n", ""),
    ("lineage_ignores_the_revision", LANE,
     '            or message_revision(identity, rows[_key(identity)]["content"]) != lineage.get("message_revision")):\n',
     "            ):\n"),
    ("lineage_ignores_the_identity", LANE,
     '    if (identity.model_dump() != lineage.get("message")\n            or ',
     "    if (False\n            or "),
    ("unreadable_lineage_releases", LANE,
     '            raise PolicyError("lineage_revision_stale")   # a lineage this code cannot read\n', "            pass\n"),
    # The owner-socket route that runs the pass.
    ("route_ignores_its_flag", "topos/core/handlers/permissions_v2.py",
     "    if not pd.enabled():\n        return {\"id\": req_id, \"status\": \"error\", \"code\": 404,",
     "    if False:\n        return {\"id\": req_id, \"status\": \"error\", \"code\": 404,"),
    ("route_accepts_any_pack", "topos/core/handlers/permissions_v2.py",
     "            or not all(type(p) is str and p in pd.ALLOWED_PACKS for p in packs)\n", ""),
    ("route_skips_the_binding_check", "topos/core/handlers/permissions_v2.py",
     "        if EvidenceBinding.parse(payload[\"binding\"]) != actual:\n            raise PolicyError(\"evidence_target_binding\")\n"
     "        index = runtime.message_search_index()\n",
     "        index = runtime.message_search_index()\n"),
    ("route_skips_its_own_owner_check", "topos/core/handlers/permissions_v2.py",
     "        _owner(actual)\n        if EvidenceBinding.parse(payload[\"binding\"]) != actual:\n            raise PolicyError(\"evidence_target_binding\")\n        index",
     "        if EvidenceBinding.parse(payload[\"binding\"]) != actual:\n            raise PolicyError(\"evidence_target_binding\")\n        index"),
    ("release_reads_the_json_not_the_scalar", P + "knowledge_projections.py",
     "    value=scalar(predicate,payload) if predicate in CLASSES else payload.get('object_value')\n",
     "    value=payload.get('object_value')\n"),
]

def main() -> int:
    """`p2c_mutants.main`, plus `scripts/` in the scratch engine (the census tests import from it) and an
    unmutated baseline that must pass first: a collection error kills every mutant and proves nothing."""
    import argparse, json, os, shutil, subprocess, tempfile
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    root = p2c_mutants.ROOT
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def pytest(base):
        run = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider"],
                             cwd=base, env=env, capture_output=True, text=True, timeout=1800)
        tail = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line][-1:]
        failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
        return run.returncode, tail, failing

    results = []
    with tempfile.TemporaryDirectory(prefix="od46-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = root / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        code, tail, failing = pytest(base)
        baseline = {"status": "pass" if code == 0 else "FAIL", "summary": tail, "failing": failing}
        print({"baseline": baseline}, flush=True)
        if code != 0:
            args.out.write_text(json.dumps({"baseline": baseline}, indent=2) + "\n")
            return 2
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text()
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                continue
            target.write_text(original.replace(old, new))
            try:
                code, tail, failing = pytest(base)
                # Killed only by a failing test: an error before any test ran proves nothing.
                status = "killed" if code != 0 and failing else "SURVIVED" if code == 0 else "ERRORED"
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"baseline": baseline, "mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
