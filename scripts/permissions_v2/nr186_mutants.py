"""Mutation run over NR186: an automatic-review pass's one Off-limits boundary, proven current at each row or built
again, and kept by the pass, never the process. Every mutant weakens one decision and must be killed by a real test.

The proof: whether the currency check runs at all; whether the reads are compared, and each of them (the Off-limits
list, the protected mentions) recorded; whether a fingerprint reads the values; whether the token a boundary is keyed
by is read before its snapshot (in `_read`, before the first statement), not after the build or after a proven
read's snapshot. The scope: whether the pass ends empty (its template, its forms memory marked ended); whether a
closed keeper still hands out; whether the boundary is kept by the process; whether the switch is on by default;
whether another thread, another resolver or a read outside a transaction gets the pass's boundary; whether the row
scope is reset between rows; whether each read gets its own copy with its own per-record caches.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr186_mutants.py --out nr186-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

KEEPER = "topos/permissions_v2/review_pass_boundary.py"
BOUNDARY = "topos/permissions_v2/entity_boundary.py"
RESOLVER = "topos/permissions_v2/evidence.py"
SWITCHES = "topos/permissions_v2/switches.py"
TESTS = ["tests/permissions_v2/test_nr186_review_pass_boundary.py"]
SWITCH_TESTS = [*TESTS, "tests/permissions_v2/test_switches.py"]
RECORD = ("            reads.append(((table, frozenset(required), where, tuple(args), limit, projected), "
          "_fingerprint(seen)))\n")
OPENED = ("                review_pass_boundary.opened(self, conn)\n                self._incarnation()\n"
          "                conn.execute(\"BEGIN\")\n")
YIELD = "                self._entity_boundaries[conn] = None\n                yield conn, floor\n"

MUTANTS = [
    # The proof at every row.
    ("currency_check_skipped", [
        (KEEPER, "            if token is not None and after is not None and after == token:\n",
                 "            if True:\n")], TESTS),
    ("reads_never_compared", [
        (BOUNDARY, '    if not reads:\n        return False\n    for (table, required, where, args, limit, projected)',
                   '    if True:\n        return True\n    for (table, required, where, args, limit, projected)')], TESTS),
    ("offlimits_list_not_recorded", [
        (BOUNDARY, RECORD, RECORD.replace("            reads.append(",
                                          "            table == 'entity_blackholes' or reads.append("))], TESTS),
    ("protected_mentions_not_recorded", [
        (BOUNDARY, RECORD, RECORD.replace("            reads.append(",
                                          "            table == 'entity_mentions' or reads.append("))], TESTS),
    ("fingerprint_ignores_the_values", [
        (BOUNDARY, '        found.update(("\\n" + "\\n".join(map(repr, rows))).encode("utf-8", "surrogatepass"))\n',
                   "")], TESTS),
    ("keyed_by_a_token_read_after_the_build", [
        (KEEPER, "        self._kept = (fresh, before, reads)\n", "        self._kept = (fresh, self._token(), reads)\n")],
     TESTS),
    ("proven_reads_keyed_by_the_token_read_after_the_snapshot", [
        (KEEPER, "            if unchanged_on(conn, reads):\n                return \"reads\"\n",
                 "            if unchanged_on(conn, reads):\n                self._after = after\n"
                 "                return \"reads\"\n"),
        (KEEPER, "                    self._kept = (template, before, reads)\n",
                 "                    self._kept = (template, self._after, reads)\n")], TESTS),
    ("token_read_after_the_snapshot", [
        (RESOLVER, OPENED, "                self._incarnation()\n                conn.execute(\"BEGIN\")\n"),
        (RESOLVER, YIELD, "                review_pass_boundary.opened(self, conn)\n" + YIELD)], TESTS),
    # The scope.
    ("pass_end_keeps_the_template", [
        (KEEPER, "        kept, self._kept = self._kept, None\n", "        kept = self._kept\n")], TESTS),
    ("forms_not_emptied_or_marked", [
        (KEEPER, "        forms.clear()\n        forms[_ENDED] = True\n", "        pass\n")], TESTS),
    ("a_replaced_template_not_ended", [
        (KEEPER, "            self._kept = None\n            _end(kept[0])\n", "            self._kept = None\n")], TESTS),
    ("a_closed_keeper_still_hands_out", [
        (KEEPER, "        return not self._closed and resolver is self._resolver",
                 "        return resolver is self._resolver")], TESTS),
    ("boundary_kept_by_the_process", [
        (KEEPER, "class ReviewPassBoundary:\n", "_KEPT = {}\n\n\nclass ReviewPassBoundary:\n"),
        (KEEPER, "        self._kept = None    # (template EntityBoundary",
                 "        self._kept = _KEPT.get(str(resolver.path))    # (template EntityBoundary"),
        (KEEPER, "        self._kept = (fresh, before, reads)\n",
                 "        self._kept = _KEPT[str(self._resolver.path)] = (fresh, before, reads)\n")], TESTS),
    ("switch_on_by_default", [
        (SWITCHES, '    "REVIEW_PASS_BOUNDARY", "bool", False, False,', '    "REVIEW_PASS_BOUNDARY", "bool", True, True,')],
     SWITCH_TESTS),
    ("any_thread_may_use_it", [
        (KEEPER, " and resolver is self._resolver and threading.get_ident() == self._thread\n",
                 " and resolver is self._resolver\n")], TESTS),
    ("any_resolver_may_use_it", [
        (KEEPER, " and resolver is self._resolver and threading.get_ident() == self._thread\n",
                 " and threading.get_ident() == self._thread\n")], TESTS),
    ("a_read_outside_a_transaction_may_use_it", [
        (KEEPER, "        if not self._usable(resolver) or not _in_transaction(conn):\n",
                 "        if not self._usable(resolver):\n")], TESTS),
    ("row_scope_never_reset", [(KEEPER, "            _CURRENT.reset(token)\n", "            pass\n")], TESTS),
    ("copies_share_the_per_record_caches", [
        (BOUNDARY, "        other._context_cache = {}\n        other._reached_ids, other._absent_tables = {}, {}\n", "")],
     TESTS),
    ("the_template_itself_handed_out", [
        (KEEPER, "                self.counts[proof] += 1\n                return template.rebind(conn)\n",
                 "                self.counts[proof] += 1\n                template.conn = conn\n                return template\n")],
     TESTS),
]


if __name__ == "__main__":
    runner.MUTANTS = MUTANTS
    runner.TESTS = TESTS
    raise SystemExit(runner.main())
