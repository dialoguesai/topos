"""Mutation run over NR187: the daemon sweep's kept interest verdict (search_index `_interests_current_kept`, its
memo maintenance in `_sweep`, and the switch). Every mutant weakens one decision: a part of the state the verdict is
kept under (the index file's identity and its re-stat after the read, the key, the canonical token read at the check
and its agreement with the one read before the snapshot, the Off-limits closure, the switches and revisions), a False
verdict kept, the memo unbounded or keeping a stale index's entry, the switch on by default or ignored, or the memo
(or the module, or the service) holding the build it decided. Since WS0's review: the switches as the decision read
them, the cap on reuses in a row, and the reviewer's eight (W1-W8). Each must be killed by at least one test.

As `n3c_mutants.py`: a scratch copy of the engine, one mutant at a time, the worktree never modified. A mutant is one
or more exact edits; one whose text does not match exactly once counts as a failure, not a pass. A run that hangs past
its timeout is recorded as HUNG, never as killed.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr187_mutants.py --out nr187-mutants.json
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
INDEX = "topos/permissions_v2/search_index.py"
SWITCHES = "topos/permissions_v2/switches.py"
TESTS = ["tests/permissions_v2/test_nr187_sweep_interest_verdict.py", "tests/permissions_v2/test_switches.py"]

STATE = "            state = (opened_state, hashlib.sha256(key).hexdigest(), self._canonical_token(), boundary.revision,\n"
PROVABLE = "        provable = (state is not None and None not in state and before is not None and state[2] == before\n"
KEEP = "            self._keep_interest_verdict(path.name, state)\n"
HIT = "        if provable and kept == state and not due:\n"
TOKEN = "        return self._probe().canonical_token()\n\n    def _interests_current_kept"
RESTAT = ('        if opened_state is None or _file_state(path) != opened_state:\n'
          '            search_timing.fast_path("nr187_interest", "miss", "file_replaced")')
DECIDE = ("        with switches.observed() as reads:\n"
          "            verdict = self._interests_current(conn, interests, None, boundary)\n")
HELD = "    return (all(name in expected and bool(value) is expected[name] for name, value in reads)\n"
BUILD = ("__import__('topos.permissions_v2.interest_family', fromlist=['build']).build(conn, "
         "owner_id=self.resolver.binding.owner_id, now_us=int(time.time()) * 1_000_000, boundary=boundary)")

MUTANTS = [
    # The index file: its identity is part of the state, and it is stat-ed again after the read.
    ("file_identity_dropped", [
        (INDEX, STATE, STATE.replace("state = (opened_state,", 'state = ("file",'))]),
    ("file_not_restated_after_the_read", [
        (INDEX, '        if opened_state is None or _file_state(path) != opened_state:\n'
                '            search_timing.fast_path("nr187_interest", "miss", "file_replaced")',
                '        if opened_state is None:\n'
                '            search_timing.fast_path("nr187_interest", "miss", "file_replaced")')]),
    # The key.
    ("key_dropped", [
        (INDEX, STATE, STATE.replace("hashlib.sha256(key).hexdigest()", '"key"'))]),
    ("key_not_compared", [
        (INDEX, HIT, "        if provable and kept is not None and kept[:1] + kept[2:] == state[:1] + state[2:] and not due:\n")]),
    # The canonical token: read at the check, and equal to the one read before the snapshot.
    ("canonical_token_not_read_at_the_check", [
        (INDEX, STATE, STATE.replace("self._canonical_token()", "before"))]),
    ("canonical_token_dropped", [
        (INDEX, STATE, STATE.replace("self._canonical_token()", '"canonical"')),
        (INDEX, PROVABLE, "        provable = (state is not None and None not in state and before is not None\n")]),
    ("before_not_compared", [
        (INDEX, PROVABLE, "        provable = (state is not None and None not in state and before is not None\n")]),
    ("open_snapshot_not_required", [
        (INDEX, '                    and getattr(conn, "in_transaction", False) is True)\n', "                    )\n")]),
    ("token_read_after_the_snapshot", [
        (INDEX, "            memo = self._canonical_token() if keep_interests else _NO_INTEREST_MEMO\n",
                "            memo = _NO_INTEREST_MEMO\n"),
        (INDEX, "                clock = clock_state(conn)\n                review_digest = None\n",
                "                clock = clock_state(conn)\n                review_digest = None\n"
                "                if keep_interests:\n                    memo = self._canonical_token()\n")]),
    # The Off-limits closure.
    ("boundary_dropped", [
        (INDEX, STATE, STATE.replace("boundary.revision,", '"boundary",'))]),
    # The switches and revisions.
    ("inputs_dropped", [
        (INDEX, "                     _interest_inputs())\n", '                     ("inputs",))\n')]),
    ("second_label_switch_not_an_input", [
        (INDEX, "        return (interest_index.enabled(), interest_relabel.enabled(), interest_review.rubric_revision(),",
                "        return (interest_index.enabled(), True, interest_review.rubric_revision(),")]),
    ("rubric_not_an_input", [
        (INDEX, "        return (interest_index.enabled(), interest_relabel.enabled(), interest_review.rubric_revision(),",
                "        return (interest_index.enabled(), interest_relabel.enabled(), 'rubric',")]),
    # A False verdict, an error, a stale index: nothing kept.
    ("false_kept", [
        (INDEX, "        if verdict is True and provable and _interest_inputs_held(state[4], reads):\n",
                "        if provable and _interest_inputs_held(state[4], reads):\n")]),
    ("old_entry_kept_on_a_miss", [
        (INDEX, "            self._keep_interest_verdict(path.name, state)\n        else:\n"
                "            self._forget_interest_verdict(path.name)\n        return verdict\n",
                "            self._keep_interest_verdict(path.name, state)\n        return verdict\n")]),
    ("stale_index_entry_kept", [
        (INDEX, "                self._forget_interest_verdict(path.name)   # NR187: a stale index keeps no verdict, removed or not\n",
                "")]),
    # Bounded, and forgetting indexes that are gone.
    ("memo_unbounded", [
        (INDEX, "            self._forget_interest_verdicts(keep={path.name for path in files})\n", "            pass\n"),
        (INDEX, "            while len(self._interest_kept) >= INTEREST_KEPT_MAX:\n"
                "                self._interest_kept.pop(next(iter(self._interest_kept)))\n", "")]),
    ("gone_indexes_kept", [
        (INDEX, "            self._forget_interest_verdicts(keep={path.name for path in files})\n", "            pass\n")]),
    ("no_cap", [
        (INDEX, "            while len(self._interest_kept) >= INTEREST_KEPT_MAX:\n"
                "                self._interest_kept.pop(next(iter(self._interest_kept)))\n", "")]),
    # The switch.
    ("switch_on_by_default", [
        (SWITCHES, '    "SWEEP_INTEREST_VERDICT", "bool", False, False,\n', '    "SWEEP_INTEREST_VERDICT", "bool", True, True,\n')]),
    ("switch_ignored", [
        (INDEX, "        keep_interests = files is not None and _sweep_interest_verdict()\n",
                "        keep_interests = files is not None\n")]),
    ("switch_off_keeps_the_memo", [
        (INDEX, "        elif self._interest_kept:\n            self._forget_interest_verdicts()\n        if not files:\n",
                "        if not files:\n")]),
    # WS0's review (16 Oct 2026): the inputs as the decision read them, and how often a verdict may be reused.
    ("switch_reads_during_the_decision_not_checked", [
        (INDEX, HELD, "    return (True\n")]),
    ("another_switch_read_allowed", [
        (INDEX, HELD, "    return (all(bool(value) is expected.get(name, bool(value)) for name, value in reads)\n")]),
    ("full_every_ignored", [
        (INDEX, "        due = reuses + 1 >= _interest_full_every()", "        due = False")]),
    ("reuses_not_counted", [
        (INDEX, "                    self._interest_reuses[path.name] = reuses + 1\n", "                    pass\n")]),
    ("full_every_not_clamped", [
        (INDEX, "        return max(1, min(INTEREST_FULL_EVERY_CEILING, switches.number(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY)))",
                "        return max(1, switches.number(switches.SWEEP_INTEREST_VERDICT_FULL_EVERY))")]),
    ("full_every_default_moved", [
        (SWITCHES, '    "SWEEP_INTEREST_VERDICT_FULL_EVERY", "int", 30, 30,\n',
                   '    "SWEEP_INTEREST_VERDICT_FULL_EVERY", "int", 300, 300,\n')]),
    # The reviewer's eight (review_mutants.py), W3b and W4 adapted to the code as it now reads.
    ("W1_token_file_states_omitted", [(INDEX, TOKEN, TOKEN.replace(
        "        return self._probe().canonical_token()\n",
        "        token = self._probe().canonical_token()\n        return None if token is None else token[:2]\n"))]),
    ("W2_token_data_version_omitted", [(INDEX, TOKEN, TOKEN.replace(
        "        return self._probe().canonical_token()\n",
        "        token = self._probe().canonical_token()\n        return None if token is None else (token[0], token[2])\n"))]),
    ("W3_unreadable_token_at_check_falls_back_to_before", [(INDEX, STATE, STATE.replace(
        "self._canonical_token()", "(self._canonical_token() or before)"))]),
    ("W3b_decision_error_returns_kept_true", [(INDEX, DECIDE, DECIDE.replace(
        "            verdict = self._interests_current(conn, interests, None, boundary)\n",
        "            try:\n                verdict = self._interests_current(conn, interests, None, boundary)\n"
        "            except (PolicyError, sqlite3.Error, KeyError):\n                if kept is not None:\n"
        "                    return True\n                raise\n"))]),
    ("W4_stat_taken_only_once_at_the_check", [
        (INDEX, RESTAT, RESTAT.replace(" or _file_state(path) != opened_state", "")),
        (INDEX, STATE, STATE.replace("state = (opened_state,", "state = (_file_state(path),"))]),
    ("W5_memo_survives_a_sweep_that_could_not_run", [
        (INDEX, "            self._forget_interest_verdicts()   # NR187: a sweep that could not run keeps no verdict\n", "")]),
    ("W6_memo_survives_every_index_removal", [
        (INDEX, "            self._forget_interest_verdicts()   # NR187: a sweep that could not run keeps no verdict\n", ""),
        (INDEX, "                self._forget_interest_verdict(path.name)   # NR187: a stale index keeps no verdict, removed or not\n", ""),
        (INDEX, "            self._forget_interest_verdicts(keep={path.name for path in files})\n", "            pass\n")]),
    ("W8_probe_reopened_per_read", [(INDEX, TOKEN, TOKEN.replace(
        "        return self._probe().canonical_token()\n",
        "        return SearchVerification(self.resolver, self.reviews).canonical_token()\n"))]),
    # What the memo holds: never the build, wherever it is put.
    ("memo_holds_the_build", [
        (INDEX, KEEP, f"            self._keep_interest_verdict(path.name, state + ({BUILD},))\n"),
        (INDEX, HIT, "        if provable and kept is not None and kept[:5] == state and not due:\n")]),
    ("module_keeps_the_build", [
        (INDEX, "INTEREST_KEPT_MAX = 64\n", "INTEREST_KEPT_MAX = 64\n_KEPT_BUILDS: dict = {}\n"),
        (INDEX, KEEP, f"            _KEPT_BUILDS[path.name] = {BUILD}\n" + KEEP)]),
    ("service_keeps_the_build", [
        (INDEX, KEEP, f"            self._kept_builds = {{path.name: {BUILD}}}\n" + KEEP)]),
    ("default_argument_keeps_the_build", [
        (INDEX, "    def _keep_interest_verdict(self, name: str, state: tuple) -> None:\n",
                "    def _keep_interest_verdict(self, name: str, state: tuple, _held=[]) -> None:\n"),
        (INDEX, KEEP, f"            self._keep_interest_verdict.__func__.__defaults__[0].append({BUILD})\n" + KEEP)]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="nr187-mutants-") as scratch:
        base = Path(scratch).resolve() / "engine"
        base.mkdir()
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
                    run = subprocess.run([sys.executable, "-m", "pytest", *(selected[0] if selected else TESTS), "-q",
                                          "-p", "no:cacheprovider"], cwd=base, env=env, capture_output=True, text=True,
                                         timeout=900)
                except subprocess.TimeoutExpired:
                    results.append({"mutant": name, "status": "HUNG", "summary": [], "killed_by": []})
                    print(results[-1], flush=True)
                    continue
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
                errored = "error" in " ".join(tail) and not failing
                results.append({"mutant": name, "status": "killed" if run.returncode != 0 and not errored else
                                ("ERRORED" if errored else "SURVIVED"), "summary": tail, "killed_by": failing[:6],
                                "killed_by_count": len(failing)})
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
