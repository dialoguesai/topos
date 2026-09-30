"""Mutation run over WS4 N5: the send check's revision token (search_index.send_token, SearchVerification's
keep_send_token / send_unchanged), the basis-only index load and the recheck that removes a stale index.

Every mutant weakens one decision: a part the token no longer covers, where the send check reads it (under the
gate, before anything else of its own), when the recheck keeps it (only when nothing moved across its snapshot),
whether the protection sync and authority read still run on every send, whether index load still checks the basis,
and whether the recheck still removes an index it finds stale. Each must be killed by at least one test.
It reuses `n3c_mutants.py`'s runner: a scratch copy of the engine, one mutant at a time, the worktree never modified.

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
    "test_message_search_batch.py", "test_message_search_state.py")]
KEEP = ('''        same = (before is not None and after is not None
                and {k: v for k, v in before.items() if k != "ledger"} == {k: v for k, v in after.items() if k != "ledger"})
''')
GATED_READ = ('''                    token = (adapter.index.send_token(signed.grant_id, verification[0], ledger.path)
                             if verification else None)
                    timing.lap("token")
''')
UNCHANGED = "not (verification and token == verification[0]._send)"

MUTANTS = [
    # Each part of the token.
    ("token_without_canonical", [(INDEX, '            return {"canonical": canonical, "reviews": reviews,\n',
                                  '            return {"canonical": None, "reviews": reviews,\n')]),
    ("token_without_reviews", [(INDEX, '            return {"canonical": canonical, "reviews": reviews,\n',
                                '            return {"canonical": canonical, "reviews": None,\n')]),
    ("token_without_marker", [(INDEX, '                    "marker": _file_state(base / "ingest-snapshots.enrollment.json"),\n',
                               '                    "marker": None,\n')]),
    ("token_without_snapshots", [(INDEX, '                    "snapshots": (_file_state(snapshots), listing),\n',
                                  '                    "snapshots": None,\n')]),
    ("token_without_index", [(INDEX, '                    "index": _file_state(index_path(self.root, grant_id)),\n',
                              '                    "index": None,\n')]),
    ("token_without_keys", [(INDEX, '                    "keys": (verified._data_version(self.keys.path), verified._files(self.keys.path)),\n',
                             '                    "keys": None,\n')]),
    ("token_without_ledger", [(INDEX, '                    "ledger": (verified._data_version(ledger), verified._files(ledger))}\n',
                               '                    "ledger": None}\n')]),
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
    ("token_kept_across_a_racing_commit", [(INDEX, KEEP, "        same = after is not None\n")]),
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
    # Index load keeps the basis; the recheck removes what it finds stale.
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
    ("recheck_keeps_a_stale_index", [
        (RELEASE, '                    purge(self.index.root, signed.grant_id)  # N5: index load checks the basis only\n', '')]),
]

if __name__ == "__main__":
    n3c_mutants.MUTANTS, n3c_mutants.TESTS = MUTANTS, TESTS
    raise SystemExit(n3c_mutants.main())
