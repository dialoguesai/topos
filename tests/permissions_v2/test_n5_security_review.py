"""Independent security review of WS4 N5 (a55272d8): attacks on the send token.

Every test compares N5's send check with the send check as it ran before N5: the `twin` fixture is an identical node
whose token never reads (`no_token`), so its send check always runs `check_own` in full, and `base_pass_one` puts
index load's member loop back (base e0bd0928 ran `check_own` with members at index load).

Tests named `test_hole_*` FAILED on a55272d8: each was a path where N5 released and the pre-N5 node refused. They
pass since the review's fixes: R1 (the lstat state of `permissions-v2` and `ingest-snapshots` in the token) and R2
(the recheck bound to the index file index load loaded and ranked).
Tests named `test_covered_*` pass: the token (or an unconditional check) sees the change.
`test_window_*` pins the accepted cut-off shift (red line 1) with the pre-N5 outcome alongside.
"""
from __future__ import annotations

import json
import os
import sqlite3
import statistics
import threading
import time

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_search_provenance_pass import node, wal  # noqa: F401
from tests.permissions_v2.test_search_send_token import (ALIAS, after_checkpoint, commit, grant_ledger_write,
                                                         no_token, relayed, same_answer, twin, verifications)  # noqa: F401
from topos.permissions_v2 import search_release, search_transport
from topos.permissions_v2.search_index import SearchIndexService, SearchVerification, index_path
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.storage.db import write_gate


def base_pass_one(monkeypatch):
    """Index load as it ran before N5 (e0bd0928): `check_own` with the member loop."""
    original = SearchIndexService.check_own

    def check_own(self, *args, **kwargs):
        kwargs["members"] = True
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SearchIndexService, "check_own", check_own)


def before_n5(monkeypatch):
    """The whole pre-N5 search: member loop at index load and a full send check."""
    base_pass_one(monkeypatch)
    no_token(monkeypatch)


async def n5_and_base(node, twin, monkeypatch, change, request_id, *, prepare=None):
    """Run `change` between checkpoint and send on `node` (N5) and on `twin` (pre-N5 send check). Both frames."""
    for subject in (node, twin):
        if prepare is not None:
            prepare(subject)
    made = verifications(monkeypatch)
    after_checkpoint(node, monkeypatch, lambda: change(node))
    frame = await relayed(node, monkeypatch, request_id)
    no_token(monkeypatch)
    after_checkpoint(twin, monkeypatch, lambda: change(twin))
    full = await relayed(twin, monkeypatch, request_id)
    return frame, full, made[0]


# -- holes: filesystem state a send-time `check_own` refuses on, outside the token --------------------------------

def open_permissions_directory(node):
    """`permissions-v2` made group-readable. `_snapshot` (the provenance pass's re-hash) refuses a snapshot whose
    parent directories are not private (`ingest_snapshot_private_required`). The token holds the state of
    `ingest-snapshots` and of files inside `permissions-v2`, never of `permissions-v2` itself."""
    (node.corpus.path.parent / "permissions-v2").chmod(0o750)


@pytest.mark.asyncio
async def test_hole_a_group_readable_permissions_directory_after_the_checkpoint(node, twin, monkeypatch):
    frame, full, verified = await n5_and_base(node, twin, monkeypatch, open_permissions_directory, "n5r-chmod")
    assert full["status"] == "error"  # before N5: the send check's provenance pass refuses
    assert same_answer(frame, full), ("N5 released where the pre-N5 send check refused; the send check skipped: "
                                      f"{verified.reused['send'] == 1}")


def symlinked_ancestor(node):
    """The node's data directory moved and replaced by a symlink to the moved copy. Every file keeps its device,
    inode, size, mtime and ctime, and the probes keep their open files; but `canonical_token` runs the resolver's
    `_incarnation`, whose `_checked_file` refuses a symlinked ancestor, so the token is None and never matches."""
    directory = node.corpus.path.parent
    moved = directory.with_name(directory.name + "-moved")
    os.rename(directory, moved)
    os.symlink(moved, directory)


@pytest.mark.asyncio
async def test_covered_a_symlinked_ancestor_after_the_checkpoint(node, twin, monkeypatch):
    frame, full, verified = await n5_and_base(node, twin, monkeypatch, symlinked_ancestor, "n5r-symlink")
    assert verified.computed["send"] == 1 and verified.reused["send"] == 0
    assert full["status"] == "error" and same_answer(frame, full)


def _swap_for_symlink(path):
    """The file moved aside and a symlink to it left in its place. The inode behind the link is the same, but the
    rename moved its ctime, which the token holds; so the token moves and the send check runs in full."""
    moved = path.with_name(path.name + ".moved")
    os.rename(path, moved)
    os.symlink(moved, path)


SYMLINKED = {
    # `private_file` opens keys.db with O_NOFOLLOW; the token stats it through the link.
    "keys_db": lambda node: _swap_for_symlink(node.index.keys.path),
    # `_check_file` (`_checked_file`, lstat) refuses a review store that is not a regular file.
    "review_store": lambda node: _swap_for_symlink(node.corpus.reviews.path),
    # `_marker_read` (`_checked_file`) refuses a marker that is not a regular file.
    "ingest_marker": lambda node: _swap_for_symlink(
        node.corpus.path.parent / "permissions-v2" / "ingest-snapshots.enrollment.json"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", sorted(SYMLINKED))
async def test_covered_a_file_swapped_for_a_symlink_after_the_checkpoint(node, twin, monkeypatch, target):
    frame, full, verified = await n5_and_base(node, twin, monkeypatch, SYMLINKED[target], f"n5r-link-{target}")
    assert verified.computed["send"] == 1, target
    assert full["status"] == "error" and same_answer(frame, full), target


def symlinked_permissions_directory(node):
    """`permissions-v2` itself moved and replaced by a link. No file inside it changes (a rename touches only the
    directory's own inode), and the directory is not in the token. `_snapshot` (lstat) and the review store's
    `_checked_file` refuse a linked `permissions-v2`."""
    _swap_for_symlink(node.corpus.path.parent / "permissions-v2")


@pytest.mark.asyncio
async def test_hole_a_symlinked_permissions_directory_after_the_checkpoint(node, twin, monkeypatch):
    frame, full, verified = await n5_and_base(node, twin, monkeypatch, symlinked_permissions_directory, "n5r-pv2-link")
    assert full["status"] == "error"
    assert same_answer(frame, full), ("N5 released where the pre-N5 send check refused; the send check skipped: "
                                      f"{verified.reused['send'] == 1}")


# -- hole: the recheck proves the index on disk, not the index index load loaded and ranked ----------------------

def edit_first_member(node):
    member = node.corpus.units[0].message_id
    commit(node, f"UPDATE conversation_messages SET content=content || ' edited' WHERE message_id='{member}'")


def rebuild_during_ranking(node, monkeypatch):
    """The owner's rebuild (the refresh loop's restore) publishes a fresh index while this search ranks: after index
    load has loaded the old file, before the gated recheck opens the path."""
    original, fired = search_release.rank, []

    def rank(*args, **kwargs):
        order = original(*args, **kwargs)
        if not fired:
            fired.append(1)
            states = node.rebuild()
            assert states.get(node.search_raw["binding"]["grant_id"]) == "ready", states
        return order
    monkeypatch.setattr(search_release, "rank", rank)
    return fired


@pytest.mark.asyncio
async def test_hole_a_member_stale_index_replaced_while_the_search_ranks(node, twin, monkeypatch):
    """The index is member-stale when the search starts (a member's row was edited; the basis did not move). Before
    N5, index load's member loop refused it. N5's index load checks the basis only and loads it; a rebuild then
    publishes a fresh file; the recheck proves the fresh file, and the walk runs on the old file's ranking and
    members. Nothing binds the file the recheck proves to the file the search ranked."""
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    stale_inode = path.stat().st_ino
    edit_first_member(node)
    fired = rebuild_during_ranking(node, monkeypatch)
    frame = await relayed(node, monkeypatch, "n5r-rebuild")
    assert fired == [1] and path.stat().st_ino != stale_inode  # the file the recheck proved is not the one ranked
    released = [record["content"] for record in frame["payload"]["output"]["records"]] if frame["status"] == "ok" else []
    print("N5R_REBUILD " + json.dumps({"status": frame["status"], "records": len(released),
                                        "edited_member_released": any(c.endswith(" edited") for c in released)}))

    monkeypatch.undo()
    edit_first_member(twin)
    before_n5(monkeypatch)
    fired_twin = rebuild_during_ranking(twin, monkeypatch)
    full = await relayed(twin, monkeypatch, "n5r-rebuild")
    assert full["status"] == "error" and fired_twin == []  # before N5: refused at index load, never ranked
    assert same_answer(frame, full), "N5 released a search ranked on an index that was member-stale when it loaded"


@pytest.mark.asyncio
async def test_a_member_stale_index_replaced_while_a_batch_ranks_refuses(node, monkeypatch):
    """R2 on the batch door (added with the fix): the batch's recheck is bound to the file its index load loaded and
    ranked, so a rebuild published while the batch ranks refuses it rather than proving the fresh file."""
    from tests.permissions_v2.test_message_search_batch import send_batch
    from tests.permissions_v2.test_search_send_token import batch_payloads
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    stale_inode = path.stat().st_ino
    edit_first_member(node)
    fired = rebuild_during_ranking(node, monkeypatch)
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5r-batch-rebuild")
    assert fired == [1] and path.stat().st_ino != stale_inode
    assert frame["status"] == "error"
    assert path.exists()  # the fresh file may be good: refused as stale, not purged


# -- the accepted cut-off shift (red line 1): a gated commit right after the send check's ledger transaction -------

def commit_after_the_token(node, monkeypatch):
    """An Off-limits alias whose writer is waiting for the gate while the send check holds it, and commits as soon
    as the ledger transaction ends. `send_unchanged` (reached after the ledger commit) waits for that commit, so the
    pre-N5 `check_own` that follows always sees it."""
    armed, committed = threading.Event(), threading.Event()
    sync = node.protocol._sync_protection

    def writer():
        armed.wait(10)
        with write_gate.with_db_write():
            commit(node, ALIAS)
        committed.set()

    def sync_protection(db):
        if not armed.is_set() and after["checkpointed"]:
            threading.Thread(target=writer, daemon=True).start()
            armed.set()
        return sync(db)
    after = {"checkpointed": False}
    monkeypatch.setattr(node.protocol, "_sync_protection", sync_protection)
    original = SearchVerification.send_unchanged

    def send_unchanged(self, token):
        if armed.is_set():
            assert committed.wait(10)
        return original(self, token)
    monkeypatch.setattr(SearchVerification, "send_unchanged", send_unchanged)
    after_checkpoint(node, monkeypatch, lambda: after.update(checkpointed=True))
    return committed


@pytest.mark.asyncio
async def test_window_an_offlimits_alias_committed_after_the_token_read(node, twin, monkeypatch):
    committed = commit_after_the_token(node, monkeypatch)
    frame = await relayed(node, monkeypatch, "n5r-window")
    assert committed.is_set()
    monkeypatch.undo()
    no_token(monkeypatch)
    commit_after_the_token(twin, monkeypatch)
    full = await relayed(twin, monkeypatch, "n5r-window")
    # Pinned, not a defect: red line 1 makes the token read the send's cut-off. Before N5 the cut-off was the
    # send-time `check_own`'s snapshot, after the protection sync, the authority read and the ledger commit.
    assert full["status"] == "error" and frame["status"] == "ok"


# -- covered: changes the token or an unconditional check sees ----------------------------------------------------

def black_hole(node):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                     "VALUES('second-protected','person','Rowan Sample','rowan sample')")
        conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                     "VALUES('bh-2','second-protected','Rowan Sample','rowan sample','complete')")


def entity_merge(node):
    """The protected entity absorbs the owner's own entity's alias set (a merge's canonical write)."""
    commit(node, "UPDATE entities SET aliases_json=(SELECT aliases_json FROM entities WHERE entity_id='owner-entity') "
                 "WHERE entity_id='protected-entity'")


def ledger_revoke(node):
    grant_id = node.search_raw["binding"]["grant_id"]
    with owner():
        node.ledger.revoke(grant_id, expected_epoch=node.epoch(), command_id="n5r-revoke")


def ledger_protection(node):
    with owner():
        node.ledger.update_protection("b" * 64, expected_epoch=node.epoch(), command_id="n5r-protection")


def flag_off(node):
    os.environ[search_transport.FLAG] = "false"


def wal_alias(node):
    """A WAL-only commit: the writer keeps its connection (and the WAL) open, nothing is checkpointed."""
    conn = sqlite3.connect(node.corpus.path, timeout=0.5, check_same_thread=False)
    conn.execute(ALIAS)
    conn.commit()
    node.kept_open = conn


COVERED = {"black_hole": black_hole, "entity_merge": entity_merge, "ledger_revoke": ledger_revoke,
           "ledger_protection": ledger_protection, "flag_off": flag_off}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(COVERED))
async def test_covered_change_between_checkpoint_and_send(node, twin, monkeypatch, change):
    monkeypatch.setenv(search_transport.FLAG, "true")
    frame, full, _verified = await n5_and_base(node, twin, monkeypatch, COVERED[change], f"n5r-{change}")
    assert same_answer(frame, full), change
    if change != "entity_merge":  # the merge leaves the answer as it was, on both nodes
        assert frame["status"] == "error", change  # each refuses (not vacuous)


@pytest.mark.asyncio
async def test_covered_a_wal_only_commit_moves_the_token(node, twin, monkeypatch):
    frame, full, verified = await n5_and_base(node, twin, monkeypatch, wal_alias, "n5r-wal", prepare=wal)
    assert verified.computed["send"] == 1 and verified.reused["send"] == 0
    assert frame["status"] == "error" and same_answer(frame, full)
    for subject in (node, twin):
        subject.kept_open.close()


def _token(node, verified):
    return node.index.send_token(node.search_raw["binding"]["grant_id"], verified, node.ledger.path)


def test_covered_data_version_moves_through_a_wal_commit_and_a_truncating_checkpoint(node):
    wal(node)
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        first = _token(node, verified)
        conn = sqlite3.connect(node.corpus.path)
        conn.execute(ALIAS)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        second = _token(node, verified)
    assert first["canonical"][1] != second["canonical"][1]  # data_version itself, not only the file state


def test_covered_an_in_place_same_size_rewrite_with_the_old_mtime_moves_the_token(node):
    """A stat-collision attempt: same inode, same size, mtime restored with utime. ctime cannot be set from user
    space, so the index part moves. (A collision needs the clock stepped back to the same ctime tick.)"""
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        first = _token(node, verified)
        info = path.stat()
        data = bytearray(path.read_bytes())
        data[-1] ^= 0xFF
        time.sleep(0.01)
        with open(path, "r+b") as handle:
            handle.write(bytes(data))
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        second = _token(node, verified)
    assert path.stat().st_mtime_ns == info.st_mtime_ns and path.stat().st_size == info.st_size
    assert first["index"] != second["index"]


def test_covered_each_search_gets_its_own_verification(node):
    first, second = node.search.verification(), node.search.verification()
    try:
        assert first is not second and first._send is None and second._send is None
    finally:
        first.close()
        second.close()


# -- timing: which send-check classes a recipient can tell apart ---------------------------------------------------

def _new_snapshot_file(node):
    path = node.corpus.path.parent / "permissions-v2" / "ingest-snapshots" / f"other-{time.monotonic_ns()}.json"
    path.write_bytes(b"[]")
    path.chmod(0o400)


@pytest.mark.asyncio
async def test_timing_classes_of_the_send_check(node, monkeypatch, caplog, capsys):
    """Records (does not bound) the IF-3 send_check line for states between checkpoint and send: which parts
    appear, and how long. A skipped check has no check_own parts; a full one reuses N3a's boundary and digest
    unless the canonical database or the review store moved. Every non-quiet state here is outside the grant.
    Since the token's key and ledger parts were narrowed to this grant, another grant's key or ledger activity
    skips like a quiet search: those events left the timing class."""
    from tests.permissions_v2.test_search_send_token import _timed
    from tests.permissions_v2.test_search_timing_attribution import LOGGER, by_stage, parsed
    states = {"quiet": lambda: None, "ledger_other_grant": lambda: grant_ledger_write(node),
              "keys_other_grant": lambda: node.index.keys.get(f"other-grant-{time.monotonic_ns()}", create=True), "snapshot_dir_other_file": lambda: _new_snapshot_file(node),
              "canonical_unrelated_row":
                  lambda: commit(node, "INSERT INTO engine_config VALUES('n5r-' || hex(randomblob(4)),'x')")}
    rows = {name: [] for name in states}
    dispatch = node.search.dispatch  # after_checkpoint wraps whatever is there: restore it, or actions stack
    for number in range(8):
        for name, action in states.items():
            caplog.clear()
            monkeypatch.setattr(node.search, "dispatch", dispatch)
            after_checkpoint(node, monkeypatch, action)
            message = _timed(node, monkeypatch, f"n5r-t-{name}-{number}")
            socket = search_transport_socket()
            with caplog.at_level("INFO", logger=LOGGER):
                await search_transport.dispatch_message_search(socket, message)
            node.search.observe = None
            if json.loads(socket.sent[0])["status"] != "ok":
                continue
            [line] = by_stage(parsed(caplog))["send_check"]
            rows[name].append(line)
    summary = {}
    for name, lines in rows.items():
        if not lines:
            continue
        summary[name] = {"n": len(lines), "median_ms": round(statistics.median(float(line["ms"]) for line in lines), 2),
                         "check_own_ran": sum("members_ms" in line for line in lines),
                         "median_check_own_ms": round(statistics.median(float(line.get("check_own_ms", 0)) for line in lines), 2),
                         "median_boundary_ms": round(statistics.median(float(line.get("boundary_ms", 0)) for line in lines), 2),
                         **{f"median_{part}_ms": round(statistics.median(float(line.get(f"{part}_ms", 0)) for line in lines), 2)
                            for part in ("token", "protection", "authority", "commit")}}
    with capsys.disabled():
        print("\nN5R_TIMING " + json.dumps(summary, sort_keys=True))
    skipped = ("quiet", "ledger_other_grant", "keys_other_grant")  # the last two: the narrowed token (WS0)
    assert all(summary[name]["check_own_ran"] == 0 for name in skipped)
    assert all(summary[name]["check_own_ran"] == summary[name]["n"] for name in summary if name not in skipped)


def search_transport_socket():
    from tests.permissions_v2.test_message_search_refusals import Socket
    return Socket()
