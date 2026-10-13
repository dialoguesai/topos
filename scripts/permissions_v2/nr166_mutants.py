"""Mutation run over NR166's changes to the release path. Every mutant weakens one decision and must be killed by
at least one real test.

R2, first item (one provenance pass per candidate walk and per sweep pass): whether the walk's pass is checked at
all, whether its refusal is obeyed, whether a walk decided again reuses what it decided under the refused pass,
and whether the walk and the sweep still build a service for every row.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr166_mutants.py --out nr166-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

RELEASE = "topos/permissions_v2/search_release.py"
INDEX = "topos/permissions_v2/search_index.py"
EVIDENCE = "topos/permissions_v2/evidence.py"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_nr166_provenance_walk.py", "test_search_provenance_pass.py")]

REFUSED = ("            except PolicyError:\n"
           "                for added in set(decided) - known:\n"
           "                    del decided[added]\n"
           "                walked = self._walk_candidates(*arguments)\n")

MUTANTS = [
    # The walk's one check, after its last candidate.
    ("walk_pass_never_checked", [
        (RELEASE, "            try:\n                provenance.finish()\n" + REFUSED,
                  "            try:\n                pass\n" + REFUSED)]),
    ("walk_pass_refusal_ignored", [
        (RELEASE, REFUSED, "            except PolicyError:\n                pass\n")]),
    ("refused_walk_keeps_what_it_decided", [
        (RELEASE, REFUSED, "            except PolicyError:\n                walked = self._walk_candidates(*arguments)\n")]),
    ("refused_walk_decides_again_under_the_refused_pass", [
        (RELEASE, REFUSED, "            except PolicyError:\n"
                           "                for added in set(decided) - known:\n"
                           "                    del decided[added]\n"
                           "                provenance._closed = False\n"
                           "                with self.resolver.provenance_pass(conn, provenance):\n"
                           "                    walked = self._walk_candidates(*arguments)\n")]),
    # One service per walk and per sweep pass, not one per row.
    ("walk_proves_each_candidate_on_its_own", [
        (EVIDENCE, "                provenance = getattr(self, \"_provenance_passes\", {}).get(conn)  # a walk's pass on this read (R2)\n",
                   "                provenance = None\n")]),
    ("sweep_proves_each_member_on_its_own", [
        (INDEX, "        provenance = ExistingProvenancePass(conn, canonical_database=self.resolver.path, binding=self.resolver.binding,\n"
                "                                            gate_wait=_provenance_gate_wait(provenance_point))\n",
                "        provenance = None if verified is None else ExistingProvenancePass(\n"
                "            conn, canonical_database=self.resolver.path, binding=self.resolver.binding,\n"
                "            gate_wait=_provenance_gate_wait(provenance_point))\n")]),
    # The sweep's pass is obeyed (N3c's own mutant, now also the sweep's).
    ("sweep_pass_refusal_ignored", [
        (INDEX, "                provenance.finish()\n            except PolicyError:\n                return stale(\"member_unavailable\")\n",
                "                provenance.finish()\n            except PolicyError:\n                pass\n")]),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
