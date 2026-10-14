"""Mutation run over NR166's changes to the release path. Every mutant weakens one decision and must be killed by
at least one real test.

R2, first item (one provenance pass per candidate walk and per sweep pass): whether the walk's pass is checked at
all, whether its refusal is obeyed, whether a walk decided again reuses what it decided under the refused pass,
and whether the walk and the sweep still build a service for every row.

R1 (a release decided outside the write gate, `DECIDE_OUTSIDE_GATE`): whether each site confirms before it writes
(or, for the answers retrieval, before it returns), whether the confirm compares the token, whether a part of the
token that only one kind of write moves is in it, whether the authority is read again, whether there is a third
attempt or an attempt that holds the gate after two moved ones, and whether a stale verdict reached while the
stores moved removes an index. Two checks are deliberately redundant and have no mutant of their own, because
each alone is covered by the other: the token's ledger part and the confirm's re-read of the authority (a mutant
drops both), and the review store's rollback check at confirm, which the decision's own check already made.

NR166B, cache 1 (`EntityBoundary.one_pass`: `observe`'s answer for a row kept for one pass): whether the key holds
the row's content, its column order and only the columns `observe` never reads left out; whether the token holds the
commits others made (data_version), this connection's own writes (total_changes) and the transaction; whether a
refusal is kept, another thread or a rebound copy is served, the memory outlives its scope, and the member loop opens
a pass at all.

NR166B, cache 2 (`EntityBoundary._text_hits`: each text's match kept for one pass): whether the key holds the text,
each part of the reading (name parts, a bare part anywhere, a names-only column, the identifier reading), whether
another thread is served, and whether the texts are emptied with the pass.

NR166B, cache 3 (`_Normalizer`: `normalized` and `skeleton` remembered per string inside the scope): whether the key is
the exact string, the two tables are kept apart, the scope resets the variable and empties the memory, another
thread or a copied context is served, and the limit holds.

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
R1_TESTS = ["tests/permissions_v2/test_nr166_decide_outside_gate.py"]
BOUNDARY = "topos/permissions_v2/entity_boundary.py"
PASS_TESTS = ["tests/permissions_v2/" + name for name in ("test_nr166b_boundary_pass.py", "test_nr166b_member_loop.py")]
TOKEN_READ = '            return (self.conn.execute("PRAGMA data_version").fetchone()[0], self.conn.total_changes)\n'
CELL = "        cells.append([column, type(value).__name__, value])\n"
READING = '("name", bool(name_parts), bool(bare_parts_anywhere), key in NAMES_ONLY_COLUMNS)'
STORE = ("        answer = self._observe(table=table, record_id=record_id, source_id=source_id, dataset_id=dataset_id, "
         "row=row)\n")
TOKEN = "        return release.index.send_token(self._grant_id, self._verified, release.protocol.ledger.path)\n"
REREAD = ("        with ledger._transaction() as db:\n"
          "            release.protocol._sync_protection(db)\n"
          "            again = ledger._authority(db, self._grant_id, release.clock())[0]\n"
          "        if again != current or floor is None or floor != again.protection_revision:\n"
          "            raise PolicyError(MOVED)\n")
SECOND = ("    except PolicyError as moved:\n"
          "        if moved.code != MOVED:\n"
          "            raise\n"
          "    return decide()\n")


def without(part: str):
    """The decision's token with one part left out, at both ends."""
    return [(RELEASE, TOKEN, "        token = release.index.send_token(self._grant_id, self._verified, "
                             "release.protocol.ledger.path)\n"
                             f"        if token is not None:\n            token.pop({part!r}, None)\n"
                             "        return token\n")]

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
    # n3c_mutants.py's `finish_before_the_member_loop` no longer applies (its text predates the interest members);
    # the same mutant against today's text.
    ("pass_finished_before_the_member_loop", [
        (INDEX, "        checked = {}\n        interests = []\n        if laps is not None:\n",
                "        checked = {}\n        interests = []\n        if provenance is not None:\n"
                "            provenance.finish()\n        if laps is not None:\n")]),

    # --- R1: decided outside the gate ---------------------------------------------------------------------------
    ("search_checkpoint_written_before_the_confirm", [
        (RELEASE, "                deciding.confirm()\n                lease = ledger.admit_verified(admission, now=self.clock())\n",
                  "                lease = ledger.admit_verified(admission, now=self.clock())\n")], R1_TESTS),
    ("batch_checkpoint_written_before_the_confirm", [
        (RELEASE, "                deciding.confirm()\n                # One ledger transaction claims every id",
                  "                # One ledger transaction claims every id")], R1_TESTS),
    ("answers_retrieval_never_confirmed", [
        (RELEASE, "            if kind is None:\n                self.confirm()\n",
                  "            if kind is None:\n                pass\n")], R1_TESTS),
    ("confirm_does_not_compare_the_token", [
        (RELEASE, "        if not same:\n            raise PolicyError(MOVED)\n",
                  "        if False:\n            raise PolicyError(MOVED)\n")], R1_TESTS),
    ("canonical_database_left_out_of_the_token", without("canonical"), R1_TESTS),
    ("review_store_left_out_of_the_token", without("reviews"), R1_TESTS),
    ("native_snapshot_files_left_out_of_the_token", without("snapshots"), R1_TESTS),
    ("record_key_left_out_of_the_token", without("keys"), R1_TESTS),
    ("index_file_left_out_of_the_token", without("index"), R1_TESTS),
    ("ledger_left_out_and_authority_not_read_again", without("ledger") + [(RELEASE, REREAD, "")], R1_TESTS),
    ("authority_not_read_again", [(RELEASE, REREAD, "")], R1_TESTS),
    ("a_third_attempt", [
        (RELEASE, SECOND, "    except PolicyError as moved:\n        if moved.code != MOVED:\n            raise\n"
                          "    try:\n        return decide()\n"
                          "    except PolicyError as moved:\n        if moved.code != MOVED:\n            raise\n"
                          "    return decide()\n")], R1_TESTS),
    ("the_gate_held_for_a_third_attempt", [
        (RELEASE, SECOND, "    except PolicyError as moved:\n        if moved.code != MOVED:\n            raise\n"
                          "    try:\n        return decide()\n"
                          "    except PolicyError as moved:\n        if moved.code != MOVED:\n            raise\n"
                          "    import os\n"
                          "    os.environ['TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE'] = 'false'\n"
                          "    return decide()\n")], R1_TESTS),
    ("stale_index_removed_on_a_moved_verdict", [
        (RELEASE, "        with with_db_write():\n            self._unmoved()\n            if _file_state(",
                  "        with with_db_write():\n            if _file_state(")], R1_TESTS),

    # --- NR166B cache 1: observe's answer kept for one pass ---------------------------------------------------------
    ("observed_key_without_the_row", [(BOUNDARY, CELL, "        pass\n")], PASS_TESTS),
    ("observed_key_without_the_column_order", [
        (BOUNDARY, "        encoded = json.dumps([[type(value).__name__ for value in identity], identity, cells],\n",
                   "        encoded = json.dumps([[type(value).__name__ for value in identity], identity, sorted(cells)],\n")],
     PASS_TESTS),
    ("observed_key_without_a_column_observe_reads", [
        (BOUNDARY, "        if column.startswith(UNREAD_PREFIX):   # never read",
                   "        if column.startswith(UNREAD_PREFIX) or column == \"content\":   # never read")], PASS_TESTS),
    ("token_without_the_commits_of_others", [
        (BOUNDARY, TOKEN_READ, "            return (0, self.conn.total_changes)\n")], PASS_TESTS),
    ("token_without_the_connections_own_writes", [
        (BOUNDARY, TOKEN_READ, '            return (self.conn.execute("PRAGMA data_version").fetchone()[0], 0)\n')],
     PASS_TESTS),
    ("token_outside_a_transaction", [
        (BOUNDARY, "            if not self.conn.in_transaction:\n                return None\n", "")], PASS_TESTS),
    ("a_refusal_is_kept", [
        (BOUNDARY, STORE, "        try:\n    " + STORE + "        except PolicyError:\n"
                          "            memory.observed[key] = (token, (False, self.revision))\n            raise\n")],
     PASS_TESTS),
    ("another_thread_is_served", [
        (BOUNDARY, "        return self.open and self.thread == threading.get_ident()\n\n    def close(self) -> None:\n"
                   "        self.open = False\n        self.observed.clear()\n",
                   "        return self.open\n\n    def close(self) -> None:\n"
                   "        self.open = False\n        self.observed.clear()\n")], PASS_TESTS),
    ("a_rebound_copy_keeps_the_pass", [
        (BOUNDARY, "        other._pass = None   # NR166B", "        pass   # NR166B")], PASS_TESTS),
    ("the_memory_is_not_emptied_at_the_end", [
        (BOUNDARY, "            self._pass = None\n            memory.close()\n", "            self._pass = None\n")],
     PASS_TESTS),
    ("the_pass_outlives_its_scope", [
        (BOUNDARY, "            self._pass = None\n            memory.close()\n", "            memory.close()\n")],
     PASS_TESTS),
    ("the_member_loop_opens_no_pass", [
        (INDEX, "            with boundary.one_pass():\n                current = self._members_current(",
                "            if True:\n                current = self._members_current(")], PASS_TESTS),

    # --- NR166B cache 2: a text's match kept for one pass ------------------------------------------------------------
    ("text_key_without_the_text", [
        (BOUNDARY, '        key = (reading, hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest())\n',
                   "        key = (reading,)\n")], PASS_TESTS),
    ("reading_without_name_parts", [(BOUNDARY, READING, '("name", bool(bare_parts_anywhere), key in NAMES_ONLY_COLUMNS)')],
     PASS_TESTS),
    ("reading_without_bare_parts", [(BOUNDARY, READING, '("name", bool(name_parts), key in NAMES_ONLY_COLUMNS)')],
     PASS_TESTS),
    ("reading_without_names_only", [(BOUNDARY, READING, '("name", bool(name_parts), bool(bare_parts_anywhere))')],
     PASS_TESTS),
    ("identifier_reading_under_a_name_key", [
        (BOUNDARY, 'self._text_hits(memory, ("identifier",), text,', 'self._text_hits(memory, ("name", True, False, False), text,')],
     PASS_TESTS),
    ("texts_served_to_another_thread", [
        (BOUNDARY, "        memory = self._pass if self._pass is not None and self._pass.usable() else None\n",
                   "        memory = self._pass\n")], PASS_TESTS),
    ("texts_not_emptied_at_the_end", [(BOUNDARY, "        self.texts.clear()\n", "")], PASS_TESTS),

    # --- NR166B cache 3: the normalisers remembered inside the scope -----------------------------------------------
    ("normalized_key_loosened", [
        (BOUNDARY, "    found = memory.normalized.get(value)\n", "    found = memory.normalized.get(value.strip())\n")],
     PASS_TESTS),
    ("skeleton_read_from_the_normalized_table", [
        (BOUNDARY, "    found = memory.skeleton.get(value)\n", "    found = memory.normalized.get(value)\n")], PASS_TESTS),
    ("normalizer_variable_never_reset", [
        (BOUNDARY, "                try:\n                    _PASS_NORMALIZER.reset(token)\n",
                   "                try:\n                    pass\n")], PASS_TESTS),
    ("normalizer_not_emptied", [
        (BOUNDARY, "            if normalizer is not None:\n                normalizer.close()\n",
                   "            if normalizer is not None:\n                pass\n")], PASS_TESTS),
    ("normalizer_served_to_another_thread", [
        (BOUNDARY, "        return self.open and self.thread == threading.get_ident()\n\n    def keep(",
                   "        return self.open\n\n    def keep(")], PASS_TESTS),
    ("normalizer_limit_ignored", [
        (BOUNDARY, "        if self.chars + size <= self.LIMIT:\n", "        if True:\n")], PASS_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
