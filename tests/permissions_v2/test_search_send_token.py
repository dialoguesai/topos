"""WS4 N5: a search proves its members once, in the gated recheck. Index load checks the basis only, and the send
check skips its member loop only when a revision token, read first and under the write gate, shows that nothing
it depends on has moved since the recheck proved it.

Pinned here:
- a quiet search, single or batch, runs the member loop once and releases byte for byte what a search running the
  full send check releases;
- each thing the token covers, moved between the checkpoint and the send, forces the full send check, with exactly
  the outcome the full send check gives on an identical node, on the single and the batch door alike; another
  grant's key or ledger activity does not move it (the key and ledger parts are this grant's own). The cases: this
  grant's key rotated or its grant revoked in the ledger, an unrelated canonical row, an Off-limits
  alias, an identity attestation, a revocation, a review write, an ingest-ledger command, the ingest marker rolled
  back to an older copy (the one change only the marker shows: the ingest ledger's rows live in the canonical
  database), a replaced snapshot file, a replaced index file, a record-key write and a grant-ledger write;
- a change still in flight when the send check starts (its writer holds the gate) is seen, because the token is
  read under the gate;
- the recheck keeps a token only when nothing moved across its snapshot: a commit just after that snapshot, or a
  record-id key deleted outside the gate during the recheck (as `forget_inactive_record_keys` deletes them), is
  never trusted by the send check;
- index load no longer runs the member loop: the recheck removes an index it finds member-stale, as index load did,
  and a basis change still refuses at index load;
- the protection sync and the authority read run on every send, token or not;
- IF-3 v1.5: `token_ms` on the send check; the member split on the recheck line; no member parts at index load or at
  a skipped send check.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_message_search_refusals import Socket, relay_message, signed
from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
from tests.permissions_v2.test_search_provenance_pass import (ledger_write, node, replace_snapshot, revoke,  # noqa: F401
                                                              wal)
from tests.permissions_v2.test_search_timing_attribution import FLAG, LOGGER, by_stage, parsed
from topos.permissions_v2 import search_timing, search_transport
from topos.permissions_v2.search_index import SearchIndexService, index_path
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.storage.db import write_gate

ALIAS = "UPDATE entities SET aliases_json='[\"roadmap\"]' WHERE entity_id='protected-entity'"
UNRELATED = ("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
             "VALUES('unrelated-entity','person','Quinn Other','quinn other')")


def commit(node, sql):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute(sql)


def attest(node):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        add_entity(conn, "second-self")
        do_attest(conn, "second-self", entry_id="entry-n5", command_id="command-n5")


def review_write(node):
    with owner():
        node.corpus.reviews.opt_out("fact-outside-the-grant", now=node.now[0])


def replace_index(node):
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    copy = path.with_name(path.name + ".copy")
    shutil.copy2(path, copy)
    os.replace(copy, path)


def key_write(node):
    node.index.keys.get("another-grant", create=True)


def grant_ledger_write(node):
    """A ledger write about no grant in particular (a system action receipt): outside this grant's rows."""
    with owner():
        node.ledger.record_system_action({"version": "n5-test"}, now=node.now[0])


def own_key_rotated(node):
    """This grant's record-id key replaced: the members were sealed under the old one, so the full check refuses."""
    grant_id = node.search_raw["binding"]["grant_id"]
    node.index.keys.delete(grant_id)
    node.index.keys.get(grant_id, create=True)


def own_grant_revoked(node):
    """This grant revoked in the ledger. Only the ledger part moves; the unconditional authority read refuses too."""
    grant_id = node.search_raw["binding"]["grant_id"]
    with owner():
        node.ledger.revoke(grant_id, expected_epoch=node.epoch(), command_id="n5-own-revoke")


def marker_path(node):
    return node.corpus.path.parent / "permissions-v2" / "ingest-snapshots.enrollment.json"


def older_marker(node):
    """Before the search: keep today's marker, then move the ledger on, so the kept one is older than the ledger."""
    node.older_marker = marker_path(node).read_bytes()
    ledger_write(node)


def roll_back_marker(node):
    path = marker_path(node)
    older = path.with_name(path.name + ".older")
    older.write_bytes(node.older_marker)
    older.chmod(0o600)
    os.replace(older, path)


PREPARE = {"marker_rollback": older_marker}
CHANGES = {"unrelated_row": lambda node: commit(node, UNRELATED), "offlimits_alias": lambda node: commit(node, ALIAS),
           "marker_rollback": roll_back_marker,
           "identity_attestation": attest, "revocation": revoke, "review_write": review_write,
           "ingest_ledger_command": ledger_write, "replaced_snapshot": replace_snapshot, "replaced_index": replace_index,
           "own_key_rotated": own_key_rotated, "own_grant_revoked": own_grant_revoked,
           "other_grant_key_write": key_write, "other_grant_ledger_write": grant_ledger_write}
# (computed, reused) of the send check's member loop. Outside this grant the narrowed token does not move, so the
# send check skips, and answers as the full one. A revoked grant refuses at the unconditional authority read, before
# the token is compared at all. Every other change runs the loop.
SEND_CHECK = {"other_grant_key_write": (0, 1), "other_grant_ledger_write": (0, 1), "own_grant_revoked": (0, 0)}
REFUSED_AT_SEND = {"offlimits_alias", "revocation", "review_write", "replaced_snapshot", "marker_rollback",
                   "own_key_rotated", "own_grant_revoked"}


def verifications(monkeypatch) -> list:
    made = []
    original = MessageSearchRelease.verification

    def verification(self):
        made.append(original(self))
        return made[-1]
    monkeypatch.setattr(MessageSearchRelease, "verification", verification)
    return made


def member_loops(monkeypatch) -> list:
    """Every `_members_current` run, as the stage it ran in (index_load, recheck or send)."""
    runs, stage = [], {"now": "index_load"}
    original = SearchIndexService._members_current

    def members_current(self, *args, **kwargs):
        runs.append(stage["now"])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SearchIndexService, "_members_current", members_current)
    return runs, stage


def after_checkpoint(node, monkeypatch, action, stage=None):
    """Run `action` once the search's checkpoint is done, before its send check (dispatch returns in between).

    Wraps the release's own `dispatch`, never whatever the instance holds, so two calls on one node replace each
    other instead of stacking (the N5 review found a run where they stacked)."""
    original = MessageSearchRelease.dispatch.__get__(node.search)

    def dispatch(**kwargs):
        result = original(**kwargs)
        if stage is not None:
            stage["now"] = "send"
        if action is not None:
            action()
        return result
    monkeypatch.setattr(node.search, "dispatch", dispatch)


def after_batch_checkpoint(node, monkeypatch, action):
    """`after_checkpoint` for the batch door: `action` runs once the batch's checkpoint is done."""
    original = MessageSearchRelease.dispatch_batch.__get__(node.search)

    def dispatch_batch(**kwargs):
        result = original(**kwargs)
        action()
        return result
    monkeypatch.setattr(node.search, "dispatch_batch", dispatch_batch)


def batch_payloads():
    return [{"query": query, "k": 5} for query in dict.fromkeys(dst.queries(6, 9, 12))][:3]


def same_batch_answer(first, second):
    return first["status"] == second["status"] and (
        first["status"] != "ok" or [item["output"] for item in first["payload"]["items"]]
        == [item["output"] for item in second["payload"]["items"]])


def no_token(monkeypatch):
    """The send check as before N5: the token never reads, so it never matches, and the member loop always runs."""
    monkeypatch.setattr(SearchIndexService, "send_token", lambda self, *args, **kwargs: None)


def recheck_stage(monkeypatch, stage):
    """Mark the gated recheck's member loop as such: it runs after `rank`."""
    from topos.permissions_v2 import search_release
    original = search_release.rank

    def rank(*args, **kwargs):
        stage["now"] = "recheck"
        return original(*args, **kwargs)
    monkeypatch.setattr(search_release, "rank", rank)


async def relayed(node, monkeypatch, request_id):
    message = relay_message(node, signed(node, payload=node.query, request_id=request_id), node.query, monkeypatch,
                            request_id=request_id)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


def same_answer(first, second):
    return first["status"] == second["status"] and (
        first["status"] != "ok" or json.dumps(first["payload"]["output"], sort_keys=True)
        == json.dumps(second["payload"]["output"], sort_keys=True))


@pytest.fixture
def twin(tmp_path):
    """A second node identical to `node` (same seed), for the full-send-check reference."""
    from tests.permissions_v2.test_search_provenance_pass import MEMBERS
    built = dst.build(tmp_path / "twin", members=MEMBERS, hidden_facts=0, seed=9, protected=True)
    built.query = {"query": dst.queries(MEMBERS, 9, 1)[0], "k": 5}
    return built


# -- a quiet search proves its members once ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_quiet_search_proves_its_members_once_and_releases_the_same_bytes(node, twin, monkeypatch):
    made = verifications(monkeypatch)
    runs, stage = member_loops(monkeypatch)
    recheck_stage(monkeypatch, stage)
    after_checkpoint(node, monkeypatch, None, stage)
    frame = await relayed(node, monkeypatch, "n5-quiet")
    assert frame["status"] == "ok" and frame["payload"]["output"]["records"]
    assert runs == ["recheck"]  # not at index load, not at the send check
    [verified] = made
    assert verified.reused["send"] == 1 and verified.computed["send"] == 0
    assert verified._probes == {} and verified._send is None  # closed with the search
    no_token(monkeypatch)
    runs.clear()
    stage["now"] = "index_load"
    after_checkpoint(twin, monkeypatch, None, stage)
    full = await relayed(twin, monkeypatch, "n5-quiet")
    assert runs == ["recheck", "send"]  # the send check as before N5
    assert same_answer(frame, full)


@pytest.mark.asyncio
async def test_a_quiet_batch_proves_its_members_once_and_releases_the_same_bytes(node, twin, monkeypatch):
    payloads = [{"query": query, "k": 5} for query in dict.fromkeys(dst.queries(6, 9, 12))][:3]
    made = verifications(monkeypatch)
    runs, _stage = member_loops(monkeypatch)
    frame = await send_batch(node, payloads, monkeypatch, batch_id="n5-batch")
    assert frame["status"] == "ok" and any(item["output"]["records"] for item in frame["payload"]["items"])
    assert len(runs) == 1
    [verified] = made
    assert verified.reused["send"] == 1 and verified.computed["send"] == 0
    no_token(monkeypatch)
    runs.clear()
    full = await send_batch(twin, payloads, monkeypatch, batch_id="n5-batch")
    assert len(runs) == 2
    assert [item["output"] for item in frame["payload"]["items"]] == [item["output"] for item in full["payload"]["items"]]


# -- anything the token covers, moved after the checkpoint, forces the full send check ---------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_change_between_checkpoint_and_send_gives_the_full_send_checks_answer(node, twin, monkeypatch, change):
    for subject in (node, twin):
        PREPARE.get(change, lambda _node: None)(subject)
    made = verifications(monkeypatch)
    after_checkpoint(node, monkeypatch, lambda: CHANGES[change](node))
    frame = await relayed(node, monkeypatch, f"n5-{change}")
    [verified] = made
    assert (verified.computed["send"], verified.reused["send"]) == SEND_CHECK.get(change, (1, 0)), change
    # The reference: an identical node whose send check always runs in full, as before N5.
    no_token(monkeypatch)
    after_checkpoint(twin, monkeypatch, lambda: CHANGES[change](twin))
    full = await relayed(twin, monkeypatch, f"n5-{change}")
    assert same_answer(frame, full), change
    if change in REFUSED_AT_SEND:
        assert frame["status"] == "error"  # these refuse at send, as they did before N5 (not vacuous)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_batch_change_between_checkpoint_and_send_gives_the_full_send_checks_answer(node, twin, monkeypatch,
                                                                                             change):
    """The same matrix on the batch door (N5 review, R3): its send check reads the token and skips the same way."""
    for subject in (node, twin):
        PREPARE.get(change, lambda _node: None)(subject)
    made = verifications(monkeypatch)
    after_batch_checkpoint(node, monkeypatch, lambda: CHANGES[change](node))
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id=f"n5b-{change}")
    [verified] = made
    assert (verified.computed["send"], verified.reused["send"]) == SEND_CHECK.get(change, (1, 0)), change
    no_token(monkeypatch)
    after_batch_checkpoint(twin, monkeypatch, lambda: CHANGES[change](twin))
    full = await send_batch(twin, batch_payloads(), monkeypatch, batch_id=f"n5b-{change}")
    assert same_batch_answer(frame, full), change
    if change in REFUSED_AT_SEND:
        assert frame["status"] == "error"


def test_the_token_moves_on_this_grants_key_and_ledger_rows_and_on_nothing_another_grant_writes(node):
    """The narrowed parts (WS0, after the N5 review): another grant's key or a ledger write about no grant in
    particular leaves the whole token equal; this grant's key rotation moves only the key part, and this grant's
    revocation only the ledger part. (Its revocation never reaches the comparison at send: the authority read
    refuses first. This pins the part itself.)"""
    from topos.permissions_v2.search_index import SearchVerification
    grant_id = node.search_raw["binding"]["grant_id"]
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        def token():
            return node.index.send_token(grant_id, verified, node.ledger.path)

        def moved(first, second):
            return sorted(part for part in first if first[part] != second[part])
        first = token()
        assert first is not None and first["keys"][1] is not None
        key_write(node)
        grant_ledger_write(node)
        assert token() == first
        own_key_rotated(node)
        second = token()
        assert moved(first, second) == ["keys"]
        own_grant_revoked(node)
        assert moved(second, token()) == ["ledger"]


@pytest.mark.asyncio
async def test_a_change_in_flight_when_the_send_check_starts_is_seen(node, monkeypatch):
    """An alias edit whose writer holds the gate from before the send check until after its commit. Read under the
    gate, the token waits for the commit and sees it; read outside the gate it would see the old state and skip.
    (An alias does not move the protection clock, so only the member loop can refuse it.)"""
    made = verifications(monkeypatch)
    held, done = threading.Event(), threading.Event()

    def writer():
        with write_gate.with_db_write():
            held.set()
            time.sleep(0.5)
            commit(node, ALIAS)
        done.set()

    def start_writer():
        threading.Thread(target=writer, daemon=True).start()
        assert held.wait(5)
    after_checkpoint(node, monkeypatch, start_writer)
    frame = await relayed(node, monkeypatch, "n5-in-flight")
    assert done.wait(5)
    [verified] = made
    assert frame["status"] == "error" and verified.computed["send"] == 1


@pytest.mark.asyncio
async def test_a_change_in_flight_when_a_batch_send_check_starts_is_seen(node, monkeypatch):
    """The batch door's token is read under the gate too (N5 review, R3): an alias its writer commits while holding
    the gate across the start of the batch's send check refuses the batch."""
    made = verifications(monkeypatch)
    held, done = threading.Event(), threading.Event()

    def writer():
        with write_gate.with_db_write():
            held.set()
            time.sleep(0.5)
            commit(node, ALIAS)
        done.set()

    def start_writer():
        threading.Thread(target=writer, daemon=True).start()
        assert held.wait(5)
    after_batch_checkpoint(node, monkeypatch, start_writer)
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5b-in-flight")
    assert done.wait(5)
    [verified] = made
    assert frame["status"] == "error" and verified.computed["send"] == 1


@pytest.mark.asyncio
async def test_a_revocation_in_flight_when_the_send_check_starts_refuses(node, monkeypatch):
    held, done = threading.Event(), threading.Event()

    def revoker():
        with write_gate.with_db_write():  # the revocation's own transaction re-enters it
            held.set()
            time.sleep(0.3)
            revoke(node)
        done.set()

    def start():
        threading.Thread(target=revoker, daemon=True).start()
        assert held.wait(5)
    after_checkpoint(node, monkeypatch, start)
    frame = await relayed(node, monkeypatch, "n5-revoke-in-flight")
    assert done.wait(5) and frame["status"] == "error"


# -- the recheck keeps a token only when nothing moved across its snapshot --------------------------------------

@pytest.mark.asyncio
async def test_a_commit_just_after_the_rechecks_snapshot_is_never_trusted_by_the_send_check(node, monkeypatch):
    """The alias lands after the recheck's snapshot was established: the recheck proves the old rows and answers,
    and its token, read after the checkpoint, would describe the new state. Kept, the send check would find it
    unchanged and skip; the recheck keeps nothing, so the send check runs and refuses. WAL: only there can a
    commit land while the recheck's read is open."""
    wal(node)
    made = verifications(monkeypatch)
    reviews, calls = node.search.reviews, []
    original = reviews._observe_clock

    def observe(conn):  # the recheck's snapshot was established just before this
        if not calls:
            calls.append(1)
            commit(node, ALIAS)
        return original(conn)
    monkeypatch.setattr(reviews, "_observe_clock", observe)
    frame = await relayed(node, monkeypatch, "n5-race")
    assert calls == [1]
    [verified] = made
    assert frame["status"] == "error" and verified.computed["send"] == 1


@pytest.mark.asyncio
async def test_a_key_deleted_during_the_recheck_is_never_trusted_by_the_send_check(node, monkeypatch):
    """`forget_inactive_record_keys` deletes a grant's record-id key after its ledger transaction, outside the
    write gate, so the key store can change while the recheck holds the gate. Here the key goes after the recheck's
    member loop and before its checkpoint: the recheck already holds the key it needs and answers, and its token
    read after the checkpoint describes a store without it. Kept, the send check would find that unchanged and
    skip. The recheck keeps nothing, so the send check runs, finds the key missing and refuses."""
    made = verifications(monkeypatch)
    grant_id = node.search_raw["binding"]["grant_id"]
    original = MessageSearchRelease._walk

    def walk(self, *args, **kwargs):
        node.index.keys.delete(grant_id)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_walk", walk)
    frame = await relayed(node, monkeypatch, "n5-key-deleted")
    [verified] = made
    assert frame["status"] == "error" and verified.computed["send"] == 1
    assert node.index.keys.get(grant_id, create=False) is None  # the deletion really happened


# -- index load checks the basis; the recheck removes a stale index ---------------------------------------------

@pytest.mark.asyncio
async def test_index_load_checks_the_basis_only_and_the_recheck_removes_a_member_stale_index(node, monkeypatch, caplog):
    runs, stage = member_loops(monkeypatch)
    recheck_stage(monkeypatch, stage)
    member = node.corpus.units[0].message_id
    commit(node, f"UPDATE conversation_messages SET content=content || ' edited' WHERE message_id='{member}'")
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    assert path.exists()
    frame = await relayed(node, monkeypatch, "n5-member-stale")
    assert frame["status"] == "error" and runs == ["recheck"]  # index load never looked at the members
    assert not path.exists()  # removed by the pass that found it stale, as index load removed it before N5
    assert any("message search index stale" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_the_batch_recheck_removes_a_member_stale_index(node, monkeypatch):
    """The batch door's recheck purges what it finds member-stale, as the single door's does (N5 review, R3)."""
    runs, stage = member_loops(monkeypatch)
    recheck_stage(monkeypatch, stage)
    member = node.corpus.units[0].message_id
    commit(node, f"UPDATE conversation_messages SET content=content || ' edited' WHERE message_id='{member}'")
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    assert path.exists()
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5b-member-stale")
    assert frame["status"] == "error" and runs == ["recheck"] and not path.exists()


@pytest.mark.asyncio
async def test_a_basis_change_still_refuses_at_index_load(node, monkeypatch):
    runs, stage = member_loops(monkeypatch)
    recheck_stage(monkeypatch, stage)
    commit(node, ALIAS)
    path = index_path(node.index.root, node.search_raw["binding"]["grant_id"])
    frame = await relayed(node, monkeypatch, "n5-basis-stale")
    assert frame["status"] == "error" and runs == [] and not path.exists()
    assert stage["now"] == "index_load"  # refused at index load, before the query was embedded or ranked


# -- the send check's protection and authority reads are unconditional -------------------------------------------

@pytest.mark.asyncio
async def test_the_send_check_syncs_protection_and_reads_authority_even_when_it_skips(node, monkeypatch):
    made, after = verifications(monkeypatch), {"checkpointed": False}
    calls = {"sync": 0, "authority": 0}
    protocol, ledger = node.protocol, node.protocol.ledger
    sync, authority = protocol._sync_protection, ledger._authority

    def counted_sync(db):
        calls["sync"] += after["checkpointed"]
        return sync(db)

    def counted_authority(db, grant_id, now):
        calls["authority"] += after["checkpointed"]
        return authority(db, grant_id, now)
    monkeypatch.setattr(protocol, "_sync_protection", counted_sync)
    monkeypatch.setattr(ledger, "_authority", counted_authority)
    after_checkpoint(node, monkeypatch, lambda: after.update(checkpointed=True))
    frame = await relayed(node, monkeypatch, "n5-authority")
    [verified] = made
    assert frame["status"] == "ok" and verified.reused["send"] == 1  # the member loop was skipped
    assert calls == {"sync": 1, "authority": 1}  # the send check's own protection sync and authority read


@pytest.mark.asyncio
async def test_the_batch_send_check_syncs_protection_and_reads_authority_even_when_it_skips(node, monkeypatch):
    """N5 review, R3: the batch door's protection sync and authority read are unconditional too."""
    made, after = verifications(monkeypatch), {"checkpointed": False}
    calls = {"sync": 0, "authority": 0}
    protocol, ledger = node.protocol, node.protocol.ledger
    sync, authority = protocol._sync_protection, ledger._authority

    def counted_sync(db):
        calls["sync"] += after["checkpointed"]
        return sync(db)

    def counted_authority(db, grant_id, now):
        calls["authority"] += after["checkpointed"]
        return authority(db, grant_id, now)
    monkeypatch.setattr(protocol, "_sync_protection", counted_sync)
    monkeypatch.setattr(ledger, "_authority", counted_authority)
    after_batch_checkpoint(node, monkeypatch, lambda: after.update(checkpointed=True))
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5b-authority")
    [verified] = made
    assert frame["status"] == "ok" and verified.reused["send"] == 1
    assert calls == {"sync": 1, "authority": 1}


# -- IF-3 v1.5 ----------------------------------------------------------------------------------------------------

def _timed(node, monkeypatch, request_id):
    monkeypatch.setenv(FLAG, "true")
    message = relay_message(node, signed(node, payload=node.query, request_id=request_id), node.query, monkeypatch,
                            request_id=request_id)

    def message_search():
        timing = search_timing.for_adapter()
        node.search.observe = timing.observe if timing is not None else None
        return node.search
    search_transport.get_runtime().message_search = message_search
    return message


@pytest.mark.asyncio
async def test_the_member_split_moves_to_the_recheck_line_and_the_send_check_reads_its_token(node, monkeypatch, caplog):
    message = _timed(node, monkeypatch, "n5-timing")
    socket = Socket()
    with caplog.at_level("INFO", logger=LOGGER):
        await search_transport.dispatch_message_search(socket, message)
    node.search.observe = None
    assert json.loads(socket.sent[0])["status"] == "ok"
    stages = by_stage(parsed(caplog))
    [index_load], [recheck], [check] = stages["index_load"], stages["recheck"], stages["send_check"]
    assert "members_ms" not in index_load and float(index_load["boundary_ms"]) >= 0
    for part in ("members_ms", "dependencies_ms", "provenance_check_ms"):
        assert float(recheck[part]) >= 0
    assert float(recheck["members_ms"]) <= recheck["ms"] + 0.05
    assert float(check["token_ms"]) >= 0 and "members_ms" not in check  # skipped: nothing moved
    waits = {line["point"] for line in stages.get("gate_wait", [])}
    assert not {point for point in waits if "provenance" in point}  # only the recheck proved, under its own gate
