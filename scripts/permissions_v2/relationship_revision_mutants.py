"""Mutation run over the 1.4.4 hotfix: a relationship's revision, and the restore of an index dropped unobserved.

Two rules, each mutated here, and every mutant must be killed by a test:

1. A relationship's revision pins exactly what its release and eligibility read of its edge and endpoint rows
   (knowledge_projections.EDGE_COLUMNS, ENTITY_COLUMNS: one mutant per column flips its class), the goal link inside
   the edge's metadata, and the goal's own revision; the Off-limits scan, which reads the volatile columns too, is
   run again on the current rows by every currency check.
2. The refresh loop's observation counts every index the service published since the last observation, so a drop
   between a publish and that observation is restored (live on 2 Oct, 14:32Z, the grant stayed dark 55 minutes).

As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/` and `fixtures/` are copied into a scratch directory
and each mutant is applied there, one at a time; the worktree is never modified. A patch that no longer applies is
reported as such, never as killed.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/relationship_revision_mutants.py --out relationship-revision-mutants.json
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
PROJECTIONS = "topos/permissions_v2/knowledge_projections.py"
INDEX = "topos/permissions_v2/search_index.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
TESTS = ["tests/permissions_v2/test_relationship_revision.py", "tests/permissions_v2/test_refresh_loop_publish_race.py",
         "tests/permissions_v2/test_knowledge_search.py", "tests/permissions_v2/test_refresh_loop.py"]

EDGE_PINNED = ("edge_id", "src_entity_id", "dst_entity_id", "edge_type", "valid_from", "valid_to")
EDGE_VOLATILE = ("weight", "evidence_count", "last_event_at", "created_at", "updated_at")
ENTITY_PINNED = ("entity_id", "entity_type", "canonical_name", "normalized_name", "aliases_json", "identifiers_json",
                 "contact_id", "is_self")
ENTITY_VOLATILE = ("embedding_blob", "first_seen", "last_seen", "mention_count", "metadata_json", "created_at",
                   "updated_at")


def _flip(table: str, column: str, was: str, now: str):
    """One column's class flipped, in its own table's dict only (both dicts share a few column names)."""
    head = "EDGE_COLUMNS = {" if table == "edges" else "ENTITY_COLUMNS = {"
    tail = "'updated_at': 'volatile'}"
    return (f"{table}_{column}_{was}_to_{now}", PROJECTIONS, head, tail, f"'{column}': '{was}'", f"'{column}': '{now}'")


# A class flip is run without the bookkeeping test that pairs every column with a case, so only a behaviour test
# can kill it. Four flips are equivalent: each value is also pinned through another part of the revision. (The goal
# link's flip is still killed, by the schema guard's check that the classes in use are exactly the three.)
COVERAGE_TEST = "tests/permissions_v2/test_relationship_revision.py::test_every_classified_column_has_its_case_here"
EQUIVALENT = {
    "edges_edge_id_pinned_to_volatile": "the member's edge is read by this id, so another value is another row, "
                                        "which the currency check finds missing",
    "edges_dst_entity_id_pinned_to_volatile": "the endpoint's entity_id, pinned, always equals it",
    "entities_entity_id_pinned_to_volatile": "the endpoint is read by the edge's dst_entity_id, pinned, so it always "
                                             "equals that",
    "edges_metadata_json_goal_link_to_volatile": "the goal row the link names is pinned whole, goal_id included",
}
CLASSES = ([_flip("edges", c, "pinned", "volatile") for c in EDGE_PINNED]
           + [_flip("edges", c, "volatile", "pinned") for c in EDGE_VOLATILE]
           + [_flip("edges", "metadata_json", "goal_link", "volatile")]
           + [_flip("entities", c, "pinned", "volatile") for c in ENTITY_PINNED]
           + [_flip("entities", c, "volatile", "pinned") for c in ENTITY_VOLATILE])

MUTANTS = [
    # --- rule 1: the revision
    ("revision_is_whole_rows_again", PROJECTIONS,
     "'rows': rows_revision([[_pinned(edge, EDGE_COLUMNS)], [_pinned(endpoint, ENTITY_COLUMNS)]]),",
     "'rows': rows_revision([[edge, endpoint]]),"),
    ("revision_drops_the_goal", PROJECTIONS,
     "                   'source': source_revision})", "                   'source': None})"),
    ("goal_link_pins_the_whole_metadata", PROJECTIONS,
     "            pinned[name + '.source_object_id'] = _json(value, dict).get('source_object_id')",
     "            pinned[name] = value"),
    ("goal_link_pins_nothing", PROJECTIONS,
     "            pinned[name + '.source_object_id'] = _json(value, dict).get('source_object_id')",
     "            pass"),
    ("goal_link_parsed_leniently", PROJECTIONS,
     "            pinned[name + '.source_object_id'] = _json(value, dict).get('source_object_id')",
     "            pinned[name + '.source_object_id'] = __import__('json').loads(value).get('source_object_id')"),
    ("unclassified_column_is_volatile", PROJECTIONS,
     "        kind = columns.get(name, 'pinned')", "        kind = columns.get(name, 'volatile')"),
    ("null_is_a_value", PROJECTIONS,
     "        if kind == 'volatile' or value is None:", "        if kind == 'volatile':"),
    ("no_off_limits_recheck", PROJECTIONS,
     "    if boundary is not None and (boundary.legacy_veto('entity_edges',row) or boundary.legacy_veto('entities',endpoint)):",
     "    if False:"),
    ("off_limits_recheck_skips_the_endpoint", PROJECTIONS,
     "(boundary.legacy_veto('entity_edges',row) or boundary.legacy_veto('entities',endpoint))",
     "boundary.legacy_veto('entity_edges',row)"),
    ("off_limits_recheck_skips_the_edge", PROJECTIONS,
     "(boundary.legacy_veto('entity_edges',row) or boundary.legacy_veto('entities',endpoint))",
     "boundary.legacy_veto('entities',endpoint)"),
    ("currency_check_passes_no_boundary", INDEX,
     "revision=current_revision(conn,projection['table'],projection['record_id'],boundary=boundary)",
     "revision=current_revision(conn,projection['table'],projection['record_id'])"),
    ("currency_check_forgives_a_veto", INDEX,
     "                        except PolicyError:\n                            return stale('projection')\n",
     "                        except PolicyError:\n                            revision=projection['revision']\n"),
    # --- rule 2: the restore sees a drop it never observed
    ("observation_ignores_publishes", LOOP,
     "            previous = seen | published\n", "            previous = seen\n"),
    ("publishes_taken_after_the_listing", LOOP,
     "        take = getattr(service, \"take_published\", None)\n"
     "        published = set(take()) if callable(take) else set()\n"
     "        names = {path.name for path in self.root.glob(\"grant-*.db\")}\n",
     "        names = {path.name for path in self.root.glob(\"grant-*.db\")}\n"
     "        take = getattr(service, \"take_published\", None)\n"
     "        published = set(take()) if callable(take) else set()\n"),
    ("signals_reset_only_by_the_listing", LOOP,
     "                if restart or names - seen:\n", "                if restart or names - previous:\n"),
    ("restore_forgets_what_it_published", LOOP,
     "            self._persist_names({path.name for path in self.root.glob(\"grant-*.db\")} | published)\n",
     "            self._persist_names({path.name for path in self.root.glob(\"grant-*.db\")})\n"),
    ("publish_unreported", INDEX,
     "        with self._published_lock:\n            self._published.add(final.name)\n", ""),
    ("publish_reported_forever", INDEX,
     "            taken, self._published = self._published, set()\n", "            taken = set(self._published)\n"),
]


def _apply(text: str, mutant) -> str | None:
    """The patched text, or None when the patch does not apply exactly once (inside its region, for a class flip)."""
    if len(mutant) == 6:
        _name, _path, head, tail, old, new = mutant
        start = text.find(head)
        end = text.find(tail, start)
        if start < 0 or end < 0 or text.count(head) != 1:
            return None
        region = text[start:end + len(tail)]
        if region.count(old) != 1:
            return None
        return text[:start] + region.replace(old, new) + text[end + len(tail):]
    _name, _path, old, new = mutant
    if text.count(old) != 1:
        return None
    return text.replace(old, new)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="relationship-revision-mutants-") as scratch:
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
        for mutant in [*CLASSES, *MUTANTS]:
            name, path = mutant[0], mutant[1]
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text()
            patched = _apply(original, mutant)
            if patched is None:
                results.append({"mutant": name, "status": "patch_not_applicable"})
                print(results[-1], flush=True)
                continue
            target.write_text(patched)
            run_command = command + (["--deselect", COVERAGE_TEST] if len(mutant) == 6 else [])
            try:
                run = subprocess.run(run_command, cwd=base, env=env, capture_output=True, text=True, timeout=1800)
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                # pytest exits 1 only when tests ran and some failed; 2-5 mean the run itself broke.
                status = ("killed" if run.returncode == 1 and failing else
                          ("equivalent" if name in EQUIVALENT else "SURVIVED") if run.returncode == 0
                          else f"run_broken_exit_{run.returncode}")
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing,
                                **({"why_equivalent": EQUIVALENT[name]} if status == "equivalent" else {})})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    equivalent = sum(result["status"] == "equivalent" for result in results)
    report = {"mutants": len(results), "killed": killed, "equivalent": equivalent, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed, "equivalent": equivalent}))
    return 0 if killed + equivalent == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
