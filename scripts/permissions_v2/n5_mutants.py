"""Mutation run over WS4 N5: the send check's revision token (search_index.send_token, SearchVerification's
keep_send_token / send_unchanged), the basis-only index load and the recheck that removes a stale index.

Every mutant weakens one decision:
- a part the token no longer covers, or covers too widely (the key and ledger parts are this grant's own);
- where the send check reads the token (under the gate, before anything else of its own), on either door;
- when the recheck keeps it (only when nothing moved across its snapshot);
- whether the protection sync and authority read still run on every send, on either door, and whether
  `still_current` still runs in the task that writes when the member loop was skipped;
- whether index load still checks the basis, and whether the recheck is bound to the file index load ranked (the
  N5 review's R2) and still removes an index it finds stale, on either door;
- each half of the narrowed key and ledger parts: the store's own lstat identity (a replaced store is read through
  a probe that keeps the old inode), this grant's key row digest and the rows it selects, each hashed ledger row,
  and the send comparison over the key part.
Each must be killed by at least one test. The review's extra mutants
(`n5-security-review-2026-09-30/n5r_extra_mutants.py`) are included, and so are the second review's nine on the
narrowed parts (`n5r2_extra_mutants.py`), pinned by `test_n5r2_narrowing.py`. It reuses `n3c_mutants.py`'s runner:
a scratch copy of the engine, one mutant at a time, the worktree never modified.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/n5_mutants.py --out n5-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import n3c_mutants  # noqa: E402

INDEX = "topos/permissions_v2/search_index.py"
RELEASE = "topos/permissions_v2/search_release.py"
TRANSPORT = "topos/permissions_v2/search_transport.py"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_search_send_token.py", "test_search_verification.py", "test_search_provenance_pass.py",
    "test_message_search_batch.py", "test_message_search_state.py", "test_n5_security_review.py",
    "test_n5r2_narrowing.py")]
KEEP = ('''        same = (before is not None and after is not None
                and {k: v for k, v in before.items() if k != "ledger"} == {k: v for k, v in after.items() if k != "ledger"})
''')
GATED_READ = ('''                    token = (adapter.index.send_token(signed.grant_id, verification[0], ledger.path)
                             if verification else None)
                    timing.lap("token")
''')
KEYS_PART = ('''                    "keys": (_lstat_state(self.keys.path), hashlib.sha256(repr(verified._rows(self.keys.path, (
                        ("SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)),))).encode()).hexdigest()),
''')
LEDGER_PART = '                    "ledger": (_lstat_state(ledger), hashlib.sha256(repr(verified._rows(ledger, (\n'
KEY_ROW = ('hashlib.sha256(repr(verified._rows(self.keys.path, (\n'
           '                        ("SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)),))).encode()).hexdigest()')
BOUND = '''                # The file index load checked, loaded and ranked, or a refusal (N5 review, R2).
                if _file_state(index_path(self.index.root, {grant})) != loaded_state:
                    raise PolicyError("search_index_stale")
'''
UNCHANGED = "not (verification and token == verification[0]._send)"

MUTANTS = [
    # Each part of the token.
    ("token_without_canonical", [(INDEX, '"canonical": canonical, "reviews"', '"canonical": None, "reviews"')]),
    ("token_without_reviews", [(INDEX, '"reviews": reviews, "directories"', '"reviews": None, "directories"')]),
    ("token_without_directories", [(INDEX, '"directories": directories,', '"directories": None,')]),
    ("token_without_marker", [(INDEX, '                    "marker": _file_state(base / "ingest-snapshots.enrollment.json"),\n',
                               '                    "marker": None,\n')]),
    ("token_without_snapshots", [(INDEX, '                    "snapshots": (_file_state(snapshots), listing),\n',
                                  '                    "snapshots": None,\n')]),
    ("token_without_index", [(INDEX, '                    "index": _file_state(index_path(self.root, grant_id)),\n',
                              '                    "index": None,\n')]),
    ("token_without_keys", [(INDEX, KEYS_PART, '                    "keys": None,\n')]),
    ("token_without_ledger", [(INDEX, LEDGER_PART, LEDGER_PART.replace('"ledger": (', '"ledger": None, "_unused": ('))]),
    # The narrowing (WS0, after the review): another grant's activity must not move the token.
    ("keys_part_not_narrowed", [(INDEX, KEYS_PART,
        '                    "keys": (_lstat_state(self.keys.path), verified._data_version(self.keys.path), '
        'verified._files(self.keys.path)),\n')]),
    ("ledger_part_not_narrowed", [(INDEX, LEDGER_PART, LEDGER_PART.replace(
        '"ledger": (', '"ledger": (verified._data_version(ledger), verified._files(ledger)), "_unused": ('))]),
    # Where and when it is read and kept.
    ("token_read_outside_the_gate", [
        (TRANSPORT, '''                timing.asking()
                with ledger._transaction() as db:
                    timing.acquired("send_check")
                    # N5: the state now, read first and under the gate''',
         '''                timing.asking()
                token = (adapter.index.send_token(signed.grant_id, verification[0], ledger.path)
                         if verification else None)
                with ledger._transaction() as db:
                    timing.acquired("send_check")
                    # N5: the state now, read first and under the gate'''),
        (TRANSPORT, GATED_READ, '                    timing.lap("token")\n')]),
    ("batch_token_read_outside_the_gate", [(TRANSPORT, '''                timing.asking()
                with ledger._transaction() as db:
                    timing.acquired("send_check")
                    token = adapter.index.send_token(grant_id, verification[0], ledger.path)  # N5: first, gated
''', '''                timing.asking()
                token = adapter.index.send_token(grant_id, verification[0], ledger.path)  # N5: first, gated
                with ledger._transaction() as db:
                    timing.acquired("send_check")
''')]),
    ("token_kept_across_a_racing_commit", [(INDEX, KEEP, "        same = after is not None\n")]),
    ("keep_compares_only_the_canonical_part", [(INDEX, KEEP,
        '        same = before is not None and after is not None and before.get("canonical") == after.get("canonical")\n')]),
    ("unreadable_token_matches_nothing_kept", [(INDEX,
        "        unchanged = kept is not None and token is not None and token == kept\n",
        "        unchanged = token == kept\n")]),
    # What stays unconditional on every send.
    ("protection_sync_skipped_when_unchanged", [
        (TRANSPORT, '''                    timing.lap("token")
                    # Protection first: the node's revision moves only on sync, so a black hole or
                    # tombstone committed after the checkpoint would otherwise be invisible here.
                    runtime.protocol._sync_protection(db)
''', f'''                    timing.lap("token")
                    # Protection first: the node's revision moves only on sync, so a black hole or
                    # tombstone committed after the checkpoint would otherwise be invisible here.
                    if {UNCHANGED}:
                        runtime.protocol._sync_protection(db)
''')]),
    ("authority_read_skipped_when_unchanged", [
        (TRANSPORT, '                    authority = ledger._authority(db, signed.grant_id, now)[0]\n',
         f'                    authority = (ledger._authority(db, signed.grant_id, now)[0] if {UNCHANGED} else '
         '__import__("topos.permissions_v2.signing", fromlist=["parse_authority"]).parse_authority(result["authority"]))\n')]),
    ("batch_authority_read_skipped_when_unchanged", [(TRANSPORT,
        "                    authority = ledger._authority(db, grant_id, now)[0]\n",
        "                    authority = (ledger._authority(db, grant_id, now)[0] if verification[0]._send != token "
        "else parse_authority(answered[0][0]['authority']))\n")]),
    # The send task's own re-check (flag, key, clock, runtime, the batch deadline) made conditional on the member
    # loop having run. The single door's is pinned outside this runner's default files, so its killer is named.
    ("still_current_skipped_at_send_when_unchanged", [(TRANSPORT,
        "in flight refuses.\n            still_current()\n",
        "in flight refuses.\n            if verification and verification[0].computed[\"send\"]:\n"
        "                still_current()\n")],
     ["tests/permissions_v2/test_message_search_review_fixes.py::"
      "test_a_flag_key_clock_or_runtime_change_during_the_authority_read_stops_the_send"]),
    ("batch_still_current_skipped_at_send_when_unchanged", [(TRANSPORT,
        "        async def actual_send():\n            still_current()\n",
        "        async def actual_send():\n            if verification[0].computed[\"send\"]:\n"
        "                still_current()\n")]),
    # Index load keeps the basis; the recheck is bound to the ranked file and removes what it finds stale.
    ("index_load_skips_the_basis", [
        (INDEX, '''        lap = time.perf_counter()
        if verified is None:
            from .entity_boundary import EntityBoundary
            boundary = EntityBoundary(conn)
''', '''        lap = time.perf_counter()
        if not members:
            return True
        if verified is None:
            from .entity_boundary import EntityBoundary
            boundary = EntityBoundary(conn)
''')]),
    ("index_load_runs_the_member_loop_again", [
        (RELEASE, "                             laps=laps, members=False)", "                             laps=laps, members=True)")]),
    ("recheck_not_bound_to_the_ranked_index", [(RELEASE, BOUND.format(grant="signed.grant_id"), "")]),
    ("batch_recheck_not_bound_to_the_ranked_index", [(RELEASE, BOUND.format(grant="grant_id"), "")]),
    ("recheck_keeps_a_stale_index", [
        (RELEASE, '                    purge(self.index.root, signed.grant_id)  # N5: index load checks the basis only\n', '')]),
    ("batch_recheck_keeps_a_stale_index", [(RELEASE,
        "                    purge(self.index.root, grant_id)  # N5: index load checks the basis only, so this pass removes it\n",
        "")]),
    # The narrowed parts (N5 security review 2). The file identity of each store dropped: the row digest is read
    # through a probe that keeps the old inode open, so only the identity sees a replaced store.
    ("keys_part_without_file_identity", [(INDEX, '"keys": (_lstat_state(self.keys.path), hashlib.sha256(',
                                                 '"keys": (None, hashlib.sha256(')]),
    ("ledger_part_without_file_identity", [(INDEX, '"ledger": (_lstat_state(ledger), hashlib.sha256(',
                                                   '"ledger": (None, hashlib.sha256(')]),
    # The key row digest dropped (identity only), or read for a wider set of grants than this one.
    ("keys_part_without_the_row_digest", [(INDEX, KEY_ROW, '""')]),
    ("keys_row_selected_by_prefix", [(INDEX, '"SELECT key FROM p2c_record_keys WHERE grant_id=?", (grant_id,)',
                                             '"SELECT key FROM p2c_record_keys WHERE grant_id LIKE ?||\'%\'", (grant_id,)')]),
    # Each hashed ledger row dropped.
    ("ledger_digest_without_the_grant_row", [(INDEX,
        '                        ("SELECT * FROM p2a_grants WHERE grant_id=?", (grant_id,)),\n', '')]),
    ("ledger_digest_without_the_policy_row", [(INDEX,
        '                        ("SELECT * FROM p2a_policies WHERE version_id=(SELECT version_id FROM p2a_grants WHERE grant_id=?)",\n'
        '                         (grant_id,)),\n', '')]),
    ("ledger_digest_without_p2a_node", [(INDEX, '                        ("SELECT * FROM p2a_node", ()),\n', '')]),
    ("ledger_digest_without_the_observation", [(INDEX,
        '                        ("SELECT * FROM p2a_protection_observation", ()),\n', '')]),
    # The comparison at send ignores the keys part.
    ("send_unchanged_ignores_the_keys_part", [(INDEX,
        "        unchanged = kept is not None and token is not None and token == kept\n",
        "        unchanged = (kept is not None and token is not None\n"
        "                     and {k: v for k, v in token.items() if k != 'keys'} == {k: v for k, v in kept.items() if k != 'keys'})\n")]),
]

if __name__ == "__main__":
    n3c_mutants.MUTANTS, n3c_mutants.TESTS = MUTANTS, TESTS
    raise SystemExit(n3c_mutants.main())
