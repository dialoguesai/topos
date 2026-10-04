"""Mutation run over N3 (many shares on one node): the daily limit on the node, the change path that acknowledges first
and rebuilds one share after, off the gate, and the restore order. Every mutant weakens one decision; each must be
killed by at least one test.

As `n3c_mutants.py`: a scratch copy of the engine under TMPDIR, one mutant at a time, the worktree never modified. A
mutant is one or more exact edits; one whose text does not match exactly once counts as a failure, not a pass. A run
that hangs past its timeout is recorded as HUNG, never as killed.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/n3_mutants.py --out n3-mutants.json
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
LEDGER = "topos/permissions_v2/ledger.py"
QUEUE = "topos/permissions_v2/index_rebuilds.py"
INDEX = "topos/permissions_v2/search_index.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
HANDLERS = "topos/core/handlers/permissions_v2.py"
LIMIT_TESTS = ["tests/permissions_v2/test_n3_daily_limit.py"]
SHARE_TESTS = ["tests/permissions_v2/test_n3_many_shares.py"]
TESTS = LIMIT_TESTS + SHARE_TESTS

COUNT_SINGLE = ('            lease = self._claim(conn, admission, envelope_json=admission.encoded, status="admitted", now=now)\n'
                '            self._count_question(conn, [admission.envelope], now=now)\n'
                '        admission.status = "admitted"\n        return lease\n\n    def _count_question(')
COUNT_BATCH = "            self._count_question(conn, [admission.envelope for admission, *_rest in entries], now=now)\n"
QUEUED_ONE = "                _refresh_message_search(runtime, [ack.receipt.authority.grant_id])\n"

MUTANTS = [
    # The daily limit, on the node.
    ("limit_never_refuses", [
        (LEDGER, '            if budget is not None and (row["questions"] if row else 0) >= budget:\n',
                 '            if False:\n')], LIMIT_TESTS),
    ("limit_one_question_late", [
        (LEDGER, '(row["questions"] if row else 0) >= budget:', '(row["questions"] if row else 0) > budget:')],
     LIMIT_TESTS),
    ("single_search_not_counted", [
        (LEDGER, COUNT_SINGLE, COUNT_SINGLE.replace("            self._count_question(conn, [admission.envelope], "
                                                    "now=now)\n", ""))], LIMIT_TESTS),
    ("batch_not_counted", [(LEDGER, COUNT_BATCH, "")], LIMIT_TESTS),
    ("batch_counts_each_query", [
        (LEDGER, COUNT_BATCH, "            for admission, *_rest in entries:\n"
                              "                self._count_question(conn, [admission.envelope], now=now)\n")],
     LIMIT_TESTS),
    ("refusal_counted", [
        (LEDGER, '            self._claim(conn, admission, envelope_json="", status="refused", now=now)\n',
                 '            self._claim(conn, admission, envelope_json="", status="refused", now=now)\n'
                 '            self._count_question(conn, [admission.envelope], now=now)\n')], LIMIT_TESTS),
    ("count_outside_the_claiming_transaction", [
        (LEDGER, COUNT_SINGLE, COUNT_SINGLE.replace(
            "            self._count_question(conn, [admission.envelope], now=now)\n",
            "        with self._transaction() as conn:\n"
            "            self._count_question(conn, [admission.envelope], now=now)\n"))], LIMIT_TESTS),
    ("day_not_utc", [(LEDGER, 'time.strftime("%Y-%m-%d", time.gmtime(int(now)))',
                      'time.strftime("%Y-%m-%d", time.localtime(int(now)))')], LIMIT_TESTS),
    ("never_pruned", [
        (LEDGER, '        conn.execute("DELETE FROM p2a_question_days WHERE (grant_id, utc_day) IN',
                 '        False and conn.execute("DELETE FROM p2a_question_days WHERE (grant_id, utc_day) IN')],
     LIMIT_TESTS),
    ("pruned_a_day_early", [
        (LEDGER, "(utc_day(max(0, now - QUESTION_DAY_RETENTION_DAYS * 86_400)), QUESTION_DAY_PRUNE_BATCH))",
                 "(utc_day(max(0, now - (QUESTION_DAY_RETENTION_DAYS - 1) * 86_400)), QUESTION_DAY_PRUNE_BATCH))")],
     LIMIT_TESTS),
    # One change, one index, after the ack, off the gate.
    ("mutation_queues_every_share", [
        (HANDLERS, QUEUED_ONE + "            elif", "                _refresh_message_search(runtime)\n            elif")],
     SHARE_TESTS),
    ("mutation_builds_under_the_gate_before_the_ack", [
        (HANDLERS, "    try:\n        runtime.index_rebuilds().request(grant_ids)\n",
                   "    try:\n        index = runtime.message_search_index()\n"
                   "        for grant in (grant_ids or index._search_grants(int(time.time()))):\n"
                   "            index.rebuild(grant)\n")], SHARE_TESTS),
    ("retried_change_not_queued", [
        (HANDLERS, '            elif operation == "mutate" and ack.outcome == "already_applied":\n',
                   '            elif False:\n')], SHARE_TESTS),
    ("review_change_keeps_the_unguarded_indexes", [
        (INDEX, "            if authority.capability_version == CAPABILITY_SEARCH and index_path(root, row[\"grant_id\"]).exists():\n",
                "            if False:\n")], SHARE_TESTS),
    ("message_review_queues_after_its_gate", [
        (HANDLERS, "            # N3: in the change's own critical section, so an index the guard cannot see the change in is gone before\n"
                   "            # the gate is released; the rebuilds are only queued.\n"
                   "            _refresh_message_search(runtime)\n        return result\n",
                   "        _refresh_message_search(runtime)\n        return result\n")], SHARE_TESTS),
    ("again_ignored", [
        (QUEUE, "                if grant_id == self._running:\n                    self._again.add(grant_id)\n",
                "                if grant_id == self._running:\n                    pass\n")], SHARE_TESTS),
    ("queued_twice", [(QUEUE, "                elif grant_id not in self._queue:\n", "                else:\n")],
     SHARE_TESTS),
    ("every_grant_by_grant_id", [
        (QUEUE, "    return sorted(dict.fromkeys(grant_ids), key=lambda grant_id: (-int(counts.get(grant_id, 0)), grant_id))\n",
                "    return sorted(dict.fromkeys(grant_ids))\n")], SHARE_TESTS),
    ("queue_builds_without_the_slot", [(QUEUE, "        with BUILD_SLOT:\n", "        with threading.Lock():\n")],
     SHARE_TESTS),
    # The refresh loop's restore.
    ("restore_by_grant_id", [
        (LOOP, "        for grant_id in most_read_first(due, self._question_counts()):\n",
               "        for grant_id in sorted(due):\n")], SHARE_TESTS),
    ("restore_ignores_what_the_queue_owes", [
        (LOOP, "                   if entry[\"not_before\"] <= now and grant_id not in owed}\n",
               "                   if entry[\"not_before\"] <= now}\n")], SHARE_TESTS),
    ("restore_without_the_slot", [
        (LOOP, "                with BUILD_SLOT, node_principal(self.owner_id):\n",
               "                with node_principal(self.owner_id):\n")], SHARE_TESTS),
    ("publish_never_settles", [
        (LOOP, "        from .search_index import index_path\n        if not republished:\n",
               "        from .search_index import index_path\n        if True:\n")], SHARE_TESTS),
    ("publish_settles_any_cause", [
        (LOOP, "            if (not entry.get(\"running\") and entry[\"causes\"] <= DROP_CAUSES\n",
               "            if (not entry.get(\"running\")\n")], SHARE_TESTS),
    ("missing_indexes_not_queued_at_start", [
        (LOOP, "        return runtime.index_rebuilds().request_missing()\n", "        return []\n")], SHARE_TESTS),
    ("missing_indexes_queued_after_the_loop_starts", [
        (LOOP, "            queue_missing_indexes(runtime)\n            with node_principal(runtime.protocol.ledger.identity.owner_id):\n"
               "                runtime.refresh_loop()\n",
               "            with node_principal(runtime.protocol.ledger.identity.owner_id):\n"
               "                runtime.refresh_loop()\n            queue_missing_indexes(runtime)\n")], SHARE_TESTS),
    # A2A-4 Q5: the re-stamp.
    ("restamp_any_change", [(INDEX, "    return heavy(old) == heavy(new)\n", "    return True\n")], SHARE_TESTS),
    ("restamp_across_a_protection_revision", [
        (INDEX, '                or basis.get("protection_revision") != authority.protection_revision\n', "")],
     SHARE_TESTS),
    ("restamp_without_the_guard", [
        (INDEX, "            if not self._stamp_current(temporary, grant_id, authority):\n                _shred(temporary)\n"
                "                return None\n",
                "            if False:\n                _shred(temporary)\n                return None\n"),
        (INDEX, "                if (current != authority or _file_state(path) != checked\n"
                "                        or not self._stamp_current(temporary, grant_id, authority)):\n",
                "                if (current != authority or _file_state(path) != checked):\n")], SHARE_TESTS),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="n3-mutants-") as scratch:
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
                                          "-x", "-p", "no:cacheprovider"], cwd=base, env=env, capture_output=True,
                                         text=True, timeout=900)
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
