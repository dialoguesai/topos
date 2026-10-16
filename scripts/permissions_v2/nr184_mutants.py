"""Mutation run over the rules NR166 R1 (`DECIDE_OUTSIDE_GATE`, off) relies on that its own tests did not pin, from the
security review of 15 Oct (`lanes/reports/NR166_R1_SECURITY_REVIEW.md`). Every mutant weakens one decision of
`search_release._Decision` and must be killed by at least one real test.

The reviewer's six (`candidates/nr166-r1-review/r1_extra_mutants.py`): the token read after the snapshot (at
`expect`), on every node and only outside the gate; an unreadable token at both ends counted as unmoved; the kinds
switched on, the rubric, the marker and the directories left out of the token together, and the kinds alone; and the
confirm's floor comparison dropped (the authority equality kept). Added here: the token read again after the
snapshot at each door on its own (search, batch, the answers retrieval), and the rubric, the marker and the
directories each left out alone.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed). The new
tests run first, so a mutant the R1 file already kills is still named by the test that kills it.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr184_mutants.py --out nr184-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

RELEASE = "topos/permissions_v2/search_release.py"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_nr184_r1_review_gaps.py", "test_nr166_decide_outside_gate.py")]

ENTER = ("            self._before = self._token()\n"
         "            self._stack.enter_context(self._release.reviews.release_reads())\n")
EXPECT = "    def expect(self, floor, current) -> None:\n        self._expected = (floor, current)\n"
SAME = "        same = self._before is not None and now is not None and now == self._before\n"
FLOOR = "        if again != current or floor is None or floor != again.protection_revision:\n"
TOKEN = "        return release.index.send_token(self._grant_id, self._verified, release.protocol.ledger.path)\n"
# Each door's `expect`, by the line around it (the three calls differ only in their neighbours).
SEARCH_EXPECT = "no commit has landed since.\n                deciding.expect(floor, current)\n"
BATCH_EXPECT = "PolicyError(\"answers_only_share\")\n                deciding.expect(floor, current)\n"
ANSWERS_EXPECT = "                    deciding.expect(floor, current)\n                    path = index_path("


def without(*parts):
    """The decision's token with these parts left out, at both ends."""
    pops = "".join(f"            token.pop({part!r}, None)\n" for part in parts)
    return [(RELEASE, TOKEN, "        token = release.index.send_token(self._grant_id, self._verified, "
                             "release.protocol.ledger.path)\n        if token is not None:\n" + pops +
                             "        return token\n")]


def read_again_after(expect: str, indent: str):
    """One door reads its decision's token again after the snapshot (with the switch on), as if read only there."""
    return [(RELEASE, expect, expect.replace("deciding.expect(floor, current)\n",
                                             "deciding.expect(floor, current)\n"
                                             f"{indent}if not deciding.gated:\n"
                                             f"{indent}    deciding._before = deciding._token()\n", 1))]


MUTANTS = [
    # --- finding 1: the token is read before the snapshot -------------------------------------------------------------
    ("token_read_after_the_snapshot", [
        (RELEASE, ENTER, "            self._stack.enter_context(self._release.reviews.release_reads())\n"),
        (RELEASE, EXPECT, EXPECT + "        self._before = self._token()\n")]),
    # The same, only outside the gate (the off-switch test's own `_before is None` does not kill it by accident).
    ("token_read_after_the_snapshot_ungated_only", [
        (RELEASE, ENTER, "            self._stack.enter_context(self._release.reviews.release_reads())\n"),
        (RELEASE, EXPECT, EXPECT + "        if not self.gated:\n            self._before = self._token()\n")]),
    ("search_token_read_again_after_the_snapshot", read_again_after(SEARCH_EXPECT, " " * 16)),
    ("batch_token_read_again_after_the_snapshot", read_again_after(BATCH_EXPECT, " " * 16)),
    ("answers_token_read_again_after_the_snapshot", read_again_after(ANSWERS_EXPECT, " " * 20)),
    # --- finding 3: an unreadable token never matches ---------------------------------------------------------------
    ("none_matches_none", [(RELEASE, SAME, "        same = now == self._before\n")]),
    # --- the confirm's own floor (the door compares it first today) ---------------------------------------------------
    ("confirm_skips_the_floor", [(RELEASE, FLOOR, "        if again != current:\n")]),
    # --- finding 4: the parts no consent write moves alone -------------------------------------------------------------
    ("families_rubric_marker_directories_left_out", without("families", "rubric", "marker", "directories")),
    ("families_left_out", without("families")),
    ("rubric_left_out", without("rubric")),
    ("marker_left_out", without("marker")),
    ("directories_left_out", without("directories")),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
