"""Mutation run over WS4 N3a's reuse/invalidation guard (search_index.SearchVerification, EntityBoundary.rebind).

Every mutant below weakens one decision the guard makes: whether a kept closure or digest may be
reused, when it may be kept, where its token is read relative to the snapshot, what the token is
made of, and what a re-bound closure re-reads. Each must be killed by at least one test. The run
reuses `p2c_mutants.py`'s machinery (a scratch copy of the engine, one mutant at a time, the worktree
never modified). A mutant whose text no longer matches counts as a failure, not a pass.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/n3a_guard_mutants.py --out n3a-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2c_mutants  # noqa: E402

P = "topos/permissions_v2/"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_search_verification.py", "test_search_timing_review_digest.py", "test_entity_boundary_search.py")]

MUTANTS = [
    ("closure_reused_without_its_token", P + "search_index.py",
     "        if kept is not None and after is not None and kept[0] == after:\n",
     "        if kept is not None:\n"),
    ("closure_kept_across_a_racing_commit", P + "search_index.py",
     "            self._boundary = (after, boundary) if after is not None and before == after else None\n",
     "            self._boundary = (after, boundary)\n"),
    ("load_token_read_after_the_snapshot", P + "search_index.py",
     "digest_point=digest_point, verified=verified, before=before)",
     "digest_point=digest_point, verified=verified, before=verified.canonical_token() if verified else None)"),
    ("recheck_token_read_after_the_snapshot", P + "search_release.py",
     "                self.reviews._observe_clock(conn)\n",
     "                self.reviews._observe_clock(conn)\n                before = verified.canonical_token()\n"),
    ("canonical_token_without_data_version", P + "search_index.py",
     '            return ("canonical", self._data_version(path), self._files(path))\n',
     '            return ("canonical", self._files(path))\n'),
    ("canonical_token_without_file_state", P + "search_index.py",
     '            return ("canonical", self._data_version(path), self._files(path))\n',
     '            return ("canonical", self._data_version(path))\n'),
    ("digest_reused_without_its_token", P + "search_index.py",
     "        if kept is not None and token is not None and kept[0] == token:\n",
     "        if kept is not None:\n"),
    ("digest_kept_across_a_racing_write", P + "search_index.py",
     "        self._digest = (after, value) if after is not None and token == after else None\n",
     "        self._digest = (after, value)\n"),
    ("digest_reuse_skips_the_store_file_checks", P + "search_index.py",
     "            self._reviews._check_file()\n            self.reused[\"digest\"] += 1\n",
     "            self.reused[\"digest\"] += 1\n"),
    ("review_token_without_the_store_itself", P + "search_index.py",
     '            return ("reviews", self._data_version(reviews.path), self._files(reviews.path), expected,\n',
     '            return ("reviews", expected,\n'),
    ("rebind_keeps_the_old_connection", P + "entity_boundary.py",
     "        other.conn = conn\n", "        pass\n"),
    ("rebind_keeps_the_context_cache", P + "entity_boundary.py",
     "        other._context_cache = {}\n", "        other._context_cache = self._context_cache\n"),
]

if __name__ == "__main__":
    p2c_mutants.MUTANTS, p2c_mutants.TESTS = MUTANTS, TESTS
    raise SystemExit(p2c_mutants.main())
