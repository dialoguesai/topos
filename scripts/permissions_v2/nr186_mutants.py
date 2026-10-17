"""Mutation run over NR186: an automatic-review pass's one Off-limits boundary, proven current at each row or built
again, and kept by the pass, never the process. Every mutant weakens one decision and must be killed by a real test.

The proof: whether the currency check runs at all; whether the reads are compared, and each of them (the Off-limits
list, the protected mentions) recorded; whether a fingerprint reads the values; whether the token a boundary is keyed
by is read before its snapshot (in `_read`, before the first statement), not after the build or after a proven
read's snapshot. The scope: whether the pass ends empty (its template, its forms memory marked ended); whether a
closed keeper still hands out; whether the boundary is kept by the process; whether the switch is on by default;
whether another thread, another resolver or a read outside a transaction gets the pass's boundary; whether the row
scope is reset between rows; whether each read gets its own copy with its own per-record caches. From WS0's review
(16 Oct): its nine mutants W1-W9; the token's ctime; both switches read at every row; the age cap and the stall drop;
one factory for the pass's boundary and a plain read's.

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
SEARCH = "topos/permissions_v2/search_index.py"
SWITCHES = "topos/permissions_v2/switches.py"
TESTS = ["tests/permissions_v2/test_nr186_review_pass_boundary.py"]
SWITCH_TESTS = [*TESTS, "tests/permissions_v2/test_switches.py"]
RECORD = ("            reads.append(((table, frozenset(required), where, tuple(args), limit, projected), "
          "_fingerprint(seen)))\n")
OPENED = ("                review_pass_boundary.opened(self, conn)\n                self._incarnation()\n"
          "                conn.execute(\"BEGIN\")\n")
YIELD = "                self._entity_boundaries[conn] = None\n                yield conn, floor\n"
FP = '        found.update(("\\n" + "\\n".join(map(repr, rows))).encode("utf-8", "surrogatepass"))\n'
FRESH = "        self._kept = _Kept(fresh, before, reads, self._clock())\n"
BEFORE = "        before = self._before.get(conn)   # None for a read not opened inside a row: then no token is kept for it\n"
CLOSE = "        self._drop()\n        self._before.clear()\n        self._verification.close()\n"
DROP = "    def _drop(self) -> None:\n        kept, self._kept = self._kept, None\n        if kept is not None:\n            _end(kept.template)\n"
SWITCHED = "            if not switched_on():\n"
AGED = "        if kept is not None and self._clock() - kept.built_at > self._max_age:\n"
LAZY_STALL = "        if self._last_row is not None and self._clock() - self._last_row > self._max_age:\n            self._stall()\n"
ARM = "        if self._loop is None or self._kept is None:\n            return\n"

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
    ("fingerprint_ignores_the_values", [(BOUNDARY, FP, "")], TESTS),
    ("keyed_by_a_token_read_after_the_build", [
        (KEEPER, FRESH, FRESH.replace("(fresh, before,", "(fresh, self._token(),"))], TESTS),
    ("proven_reads_keyed_by_the_token_read_after_the_snapshot", [
        (KEEPER, "            if unchanged_on(conn, reads):\n                return \"reads\"\n",
                 "            if unchanged_on(conn, reads):\n                self._after = after\n"
                 "                return \"reads\"\n"),
        (KEEPER, "                    kept.token = before\n", "                    kept.token = self._after\n")], TESTS),
    ("token_read_after_the_snapshot", [
        (RESOLVER, OPENED, "                self._incarnation()\n                conn.execute(\"BEGIN\")\n"),
        (RESOLVER, YIELD, "                review_pass_boundary.opened(self, conn)\n" + YIELD)], TESTS),
    # The scope.
    ("pass_end_keeps_the_template", [(KEEPER, CLOSE, "        self._before.clear()\n        self._verification.close()\n")],
     TESTS),
    ("forms_not_emptied_or_marked", [
        (KEEPER, "        forms.clear()\n        forms[_ENDED] = True\n", "        pass\n")], TESTS),
    ("a_replaced_template_not_ended", [
        (KEEPER, DROP, DROP.replace("            _end(kept.template)\n", "            pass\n"))], TESTS),
    ("a_closed_keeper_still_hands_out", [
        (KEEPER, "        return not self._closed and resolver is self._resolver",
                 "        return resolver is self._resolver")], TESTS),
    ("boundary_kept_by_the_process", [
        (KEEPER, "class _Kept:\n", "_KEPT = {}\n\n\nclass _Kept:\n"),
        (KEEPER, "        self._kept = None    # a _Kept, or None\n",
                 "        self._kept = _KEPT.get(str(resolver.path))    # a _Kept, or None\n"),
        (KEEPER, FRESH, FRESH.replace("        self._kept = _Kept(", "        self._kept = _KEPT[str(self._resolver.path)] = _Kept("))],
     TESTS),
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
        (KEEPER, "                self.counts[proof] += 1\n                return kept.template.rebind(conn)\n",
                 "                self.counts[proof] += 1\n                kept.template.conn = conn\n"
                 "                return kept.template\n")], TESTS),
    # WS0's review (16 Oct), W1-W9; W2, W4, W4b and W5 survived the lane's first 65 tests.
    ("W1_before_token_read_at_boundary_time", [(KEEPER, BEFORE, "        before = self._token()\n")], TESTS),
    ("W2_fingerprint_none_equals_empty", [
        (BOUNDARY, FP, '        found.update(("\\n" + "\\n".join(repr(tuple("" if v is None else v for v in r)) for r in rows))'
                       '.encode("utf-8", "surrogatepass"))\n')], TESTS),
    ("W3_proof_error_hands_out_template", [
        (KEEPER, "        except Exception:  # noqa: BLE001 -- what cannot be proven is built afresh\n            return None\n",
                 "        except Exception:  # noqa: BLE001 -- what cannot be proven is built afresh\n            return \"token\"\n")],
     TESTS),
    ("W4_pass_end_keeps_terms_in_a_module", [
        (KEEPER, "class _Kept:\n", "_SEEN_TERMS = set()\n\n\nclass _Kept:\n"),
        (KEEPER, CLOSE, "        if self._kept is not None:\n            _SEEN_TERMS.update(self._kept.template.terms)\n" + CLOSE)],
     TESTS),
    ("W4b_name_parts_kept_in_a_default", [
        (KEEPER, "def _end(template) -> None:\n", "def _end(template, _seen=set()) -> None:\n"),
        (KEEPER, "    forms = getattr(template, \"_forms\", None)\n",
                 "    _seen.update(template.name_parts)\n    forms = getattr(template, \"_forms\", None)\n")], TESTS),
    ("W5_fresh_probe_each_token", [
        (SEARCH, "            probe = self._probes.get(key)\n            if probe is None:\n"
                 "                # Never holds a transaction",
                 "            probe = None\n            if probe is None:\n"
                 "                # Never holds a transaction")], TESTS),
    ("W6_replay_skips_last_read", [
        (BOUNDARY, "    for (table, required, where, args, limit, projected), fingerprint in reads:\n",
                   "    for (table, required, where, args, limit, projected), fingerprint in reads[:-1]:\n")], TESTS),
    ("W8_keeper_not_closed_on_error", [
        (KEEPER, "    try:\n        yield keeper.row\n    finally:\n        keeper.close()\n",
                 "    yield keeper.row\n    keeper.close()\n")], TESTS),
    ("W9_unopened_read_keyed_by_after_token", [
        (KEEPER, FRESH, FRESH.replace("(fresh, before,", "(fresh, before if before is not None else self._token(),"))],
     TESTS),
    # The token's file stats: an in-place rewrite with its mtime put back moves only the ctime.
    ("stats_without_the_ctime", [
        (SEARCH, "    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_uid)\n",
                 "    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode, info.st_uid)\n")], TESTS),
    # The switch at every row (review item 4).
    ("switch_read_once_per_pass", [(KEEPER, SWITCHED, "            if False:\n")], TESTS),
    ("switch_off_keeps_the_template", [
        (KEEPER, "                    self.counts[\"switched_off\"] += 1\n                self._drop()\n",
                 "                    self.counts[\"switched_off\"] += 1\n")], TESTS),
    # The age cap and a stalled pass (review item 5).
    ("age_cap_ignored", [(KEEPER, AGED, AGED.replace(" > self._max_age:", " > 10 ** 9:"))], TESTS),
    ("age_cap_not_clamped", [
        (KEEPER, "    return max(switches.REVIEW_PASS_BOUNDARY_MAX_AGE.floor, min(int(found), MAX_AGE_CEILING))\n",
                 "    return max(switches.REVIEW_PASS_BOUNDARY_MAX_AGE.floor, int(found))\n")], TESTS),
    ("age_cap_read_once_per_pass", [(KEEPER, "        self._max_age = max_age()\n        if self._last_row",
                                     "        if self._last_row")], TESTS),
    ("stall_never_checked_at_the_next_row", [(KEEPER, LAZY_STALL, "")], TESTS),
    ("stall_callback_never_armed", [(KEEPER, ARM, "        return\n")], TESTS),
    ("stall_callback_drops_early", [
        (KEEPER, "        if left <= 0:\n            self._stall()\n", "        if True:\n            self._stall()\n")], TESTS),
    ("stall_callback_left_armed_at_the_end", [
        (KEEPER, "        self._closed = True\n        if self._idle is not None:\n            self._idle.cancel()\n"
                 "            self._idle = None\n", "        self._closed = True\n")], TESTS),
    # One construction (review item 6).
    ("keeper_builds_with_its_own_arguments", [
        (KEEPER, "        fresh = resolver._new_entity_boundary(conn, record=True)\n",
                 "        from .entity_boundary import EntityBoundary\n"
                 "        fresh = EntityBoundary(conn, waiting=True, record=True)\n")], TESTS),
    ("resolver_builds_around_the_factory", [
        (RESOLVER, "            self._entity_boundaries[conn] = self._new_entity_boundary(conn) if kept is None else kept\n",
                   "            from .entity_boundary import EntityBoundary\n"
                   "            self._entity_boundaries[conn] = EntityBoundary(conn) if kept is None else kept\n")], TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS = MUTANTS
    runner.TESTS = TESTS
    raise SystemExit(runner.main())
