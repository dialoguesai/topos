"""N5 security review 2 (30 Sep, c63dfd5d): the narrowed `keys` and `ledger` parts of the send token.

The `keys` part is keys.db's lstat identity plus a digest of this grant's key row; the `ledger` part is the ledger's
lstat identity plus a digest of this grant's p2a_grants / policy / bindings / authorities rows and the node-wide
singletons (p2a_node, protection observation, canonical floor). Every case here lands between the checkpoint and
the send, on the tip node and on an identical pre-N5 twin (`no_token`: the send check always runs in full), on
both doors. Expected per the red lines: this grant's own changes force a full check or an authority refusal, never
a skip; another grant's rows never move the token but never let this grant skip a check it would otherwise fail;
the released answer is the pre-N5 answer, byte for byte. Observed statuses are appended to $N5R2_OUT as JSON lines.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_provenance_pass import node  # noqa: F401
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, batch_payloads,  # noqa: F401
                                                         no_token, relayed, same_answer, same_batch_answer, twin,
                                                         verifications)
from topos.permissions_v2.search_index import SearchVerification, index_path

# Some cases here run a search under a profile a node no longer serves (N8): the suite takes the lift
# (conftest.py `retired_search_profile`) so they run as they did, for the code p2c-v3 shares with it.
pytestmark = pytest.mark.usefixtures("retired_search_profile")

OTHER = "grant-p2a"  # the p2a-v2 grant every harness Node activates beside the search grant


def grant_of(node) -> str:
    return node.search_raw["binding"]["grant_id"]


def pv2(node):
    return node.corpus.path.parent / "permissions-v2"


def _sql(path, *statements):
    conn = sqlite3.connect(path, timeout=1)
    try:
        for sql, args in statements:
            conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def ledger_sql(node, *statements):
    _sql(node.ledger.path, *statements)


def keys_sql(node, *statements):
    _sql(node.index.keys.path, *statements)


def _replace_with(path, edit):
    copy = path.with_name(path.name + ".n5r2")
    shutil.copy2(path, copy)
    if edit is not None:
        with sqlite3.connect(copy) as conn:
            edit(conn)
    os.replace(copy, path)


# -- this grant's own rows -----------------------------------------------------------------------------------------

def own_key_deleted(node):
    node.index.keys.delete(grant_of(node))


def own_grant_repolicied(node):
    """A generation bump: the same grant re-activated under a new policy version (both generations advance)."""
    node.activate({**node.search_raw, "policy_version_id": "policy-n5r2-late"}, generation=2)


def _policy_row(node):
    with sqlite3.connect(node.ledger.path) as conn:
        [(version_id, text)] = conn.execute(
            "SELECT version_id, policy_json FROM p2a_policies WHERE version_id="
            "(SELECT version_id FROM p2a_grants WHERE grant_id=?)", (grant_of(node),)).fetchall()
    return version_id, text


def own_policy_same_length_edit(node):
    """One digit of this grant's stored policy changed in place: same length, a different policy."""
    version_id, text = _policy_row(node)
    edited = text.replace('"max_k":10', '"max_k":11')
    assert edited != text and len(edited) == len(text)
    ledger_sql(node, ("UPDATE p2a_policies SET policy_json=? WHERE version_id=?", (edited, version_id)))


def own_policy_whitespace_edit(node):
    """Different bytes, the same policy (a space inside the JSON): the authority does not change."""
    version_id, text = _policy_row(node)
    edited = text.replace('"max_k":10', '"max_k": 10')
    assert edited != text and json.loads(edited) == json.loads(text)
    ledger_sql(node, ("UPDATE p2a_policies SET policy_json=? WHERE version_id=?", (edited, version_id)))


def own_binding_row_reinserted(node):
    """This grant's binding row deleted and re-inserted with the same bytes."""
    with sqlite3.connect(node.ledger.path) as conn:
        [row] = conn.execute("SELECT * FROM p2a_grant_bindings WHERE grant_id=?", (grant_of(node),)).fetchall()
    ledger_sql(node, ("DELETE FROM p2a_grant_bindings WHERE grant_id=?", (grant_of(node),)),
               ("INSERT INTO p2a_grant_bindings VALUES (?, ?)", tuple(row)))


def two_grants_rows_swapped(node):
    """This grant's p2a_grants row and the p2a grant's exchange their contents (grant_id is the key)."""
    a = grant_of(node)
    ledger_sql(node, ("UPDATE p2a_grants SET grant_id='n5r2-tmp' WHERE grant_id=?", (a,)),
               ("UPDATE p2a_grants SET grant_id=? WHERE grant_id=?", (a, OTHER)),
               ("UPDATE p2a_grants SET grant_id=? WHERE grant_id='n5r2-tmp'", (OTHER,)))


def other_key_present(node):
    node.index.keys.get(OTHER, create=True)


def two_grants_keys_swapped(node):
    a = grant_of(node)
    keys_sql(node, ("UPDATE p2c_record_keys SET grant_id='n5r2-tmp' WHERE grant_id=?", (a,)),
             ("UPDATE p2c_record_keys SET grant_id=? WHERE grant_id=?", (a, OTHER)),
             ("UPDATE p2c_record_keys SET grant_id=? WHERE grant_id='n5r2-tmp'", (OTHER,)))


def own_grant_row_generation_bumped(node):
    """This grant's p2a_grants row alone: both generations +1, no epoch move, policy row unchanged. Only the grant
    row's hash can move the ledger part; the unconditional authority read sees the new generation anyway."""
    ledger_sql(node, ("UPDATE p2a_grants SET grant_generation=grant_generation+1, "
                      "assignment_generation=assignment_generation+1 WHERE grant_id=?", (grant_of(node),)))


def observation_generation_bumped(node):
    ledger_sql(node, ("UPDATE p2a_protection_observation SET generation=generation+1 WHERE singleton=1", ()))


def keys_db_replaced_by_copy(node):
    _replace_with(node.index.keys.path, None)


def keys_db_replaced_with_rotated_key(node):
    """A new file (new inode) whose only difference is this grant's key. The probe still reads the old inode, so
    the row digest alone would not move; the file identity must."""
    _replace_with(node.index.keys.path, lambda conn: conn.execute(
        "UPDATE p2c_record_keys SET key=? WHERE grant_id=?", (os.urandom(32), grant_of(node))))


def ledger_db_replaced_with_revoked_copy(node):
    _replace_with(node.ledger.path, lambda conn: conn.execute(
        "UPDATE p2a_grants SET active=0 WHERE grant_id=?", (grant_of(node),)))


# -- another grant's rows ------------------------------------------------------------------------------------------

def other_grant_activated(node):
    """A second grant of the same capability activated: an epoch bump, nothing of this grant's own."""
    raw = {**node.search_raw, "policy_version_id": "policy-n5r2-other",
           "binding": {**node.search_raw["binding"], "grant_id": "grant-n5r2-other",
                       "assignment_id": "assignment-grant-n5r2-other", "actor_id": "actor-9", "client_id": "client-9"}}
    node.activate(raw)


def other_grant_revoked(node):
    with owner():
        node.ledger.revoke(OTHER, expected_epoch=node.epoch(), command_id="n5r2-revoke-other")


def prefix_and_extended_grant_keys(node):
    """Keys for a grant id that is a prefix of this one, and for one this grant id is a prefix of."""
    gid = grant_of(node)
    node.index.keys.get(gid[:-1], create=True)
    node.index.keys.get(gid + "-x", create=True)


def other_grant_key_deleted(node):
    node.index.keys.delete(OTHER)


def other_request_rows(node):
    """Another request's checkpoint rows (as another grant's search writes them)."""
    ledger_sql(node, ("INSERT INTO p2a_requests VALUES ('n5r2-other-request', ?, '', 'refused')", ("0" * 64,)),
               ("INSERT INTO p2a_receipts VALUES ('n5r2-other-request', '{}', '{}')", ()))


# -- filesystem state the full check reads -------------------------------------------------------------------------

def keys_db_chmod(node):
    node.index.keys.path.chmod(0o644)


def snapshots_dir_chmod(node):
    (pv2(node) / "ingest-snapshots").chmod(0o750)


def reviews_db_chmod(node):
    node.corpus.reviews.path.chmod(0o644)


def index_file_chmod(node):
    index_path(node.index.root, grant_of(node)).chmod(0o644)


def marker_chmod(node):
    (pv2(node) / "ingest-snapshots.enrollment.json").chmod(0o644)


def ledger_db_chmod(node):
    node.ledger.path.chmod(0o644)


def index_root_chmod(node):
    node.index.root.chmod(0o750)


def node_dir_chmod(node):
    node.corpus.path.parent.chmod(0o750)


def snapshot_hardlinked(node):
    path = pv2(node) / "ingest-snapshots" / "canary.db"
    os.link(path, path.with_name("canary.db.link"))


PREPARE = {"two_grants_keys_swapped": other_key_present, "other_grant_key_deleted": other_key_present}
CHANGES = {name: globals()[name] for name in (
    "own_key_deleted", "own_grant_repolicied", "own_policy_same_length_edit", "own_policy_whitespace_edit",
    "own_binding_row_reinserted", "two_grants_rows_swapped", "two_grants_keys_swapped", "observation_generation_bumped",
    "own_grant_row_generation_bumped", "keys_db_replaced_by_copy", "keys_db_replaced_with_rotated_key", "ledger_db_replaced_with_revoked_copy",
    "other_grant_activated", "other_grant_revoked", "prefix_and_extended_grant_keys", "other_grant_key_deleted",
    "other_request_rows",
    "keys_db_chmod", "snapshots_dir_chmod", "reviews_db_chmod", "index_file_chmod", "marker_chmod", "ledger_db_chmod",
    "index_root_chmod", "node_dir_chmod", "snapshot_hardlinked")}
# (computed, reused) of the send check's member loop. (0, 0): refused at the unconditional protection sync or
# authority read, before the token is compared. (0, 1): the token did not move, the send check skipped.
SEND_CHECK = {"own_policy_same_length_edit": (0, 0), "observation_generation_bumped": (0, 0),
              "ledger_db_replaced_with_revoked_copy": (0, 0),
              "own_binding_row_reinserted": (0, 1), "prefix_and_extended_grant_keys": (0, 1),
              "other_grant_key_deleted": (0, 1), "other_request_rows": (0, 1),
              "index_root_chmod": (0, 1), "node_dir_chmod": (0, 1)}
# Where the red lines fix the outcome: this grant's own change must refuse (or the skip must release the same bytes).
STATUS = {"own_key_deleted": "error", "own_grant_repolicied": "error", "own_policy_same_length_edit": "error",
          "own_policy_whitespace_edit": "ok", "own_binding_row_reinserted": "ok", "two_grants_rows_swapped": "error",
          "two_grants_keys_swapped": "error", "observation_generation_bumped": "error", "own_grant_row_generation_bumped": "error",
          "keys_db_replaced_by_copy": "ok", "keys_db_replaced_with_rotated_key": "error",
          "ledger_db_replaced_with_revoked_copy": "error", "other_grant_activated": "error",
          "other_grant_revoked": "error", "prefix_and_extended_grant_keys": "ok", "other_grant_key_deleted": "ok",
          "other_request_rows": "ok", "keys_db_chmod": "error", "snapshots_dir_chmod": "error",
          "reviews_db_chmod": "error", "snapshot_hardlinked": "error"}


def record(**fields):
    out = os.environ.get("N5R2_OUT")
    if out:
        with open(out, "a") as handle:
            handle.write(json.dumps(fields, sort_keys=True) + "\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_change_to_the_narrowed_parts_gives_the_pre_n5_answer(node, twin, monkeypatch, change):
    for subject in (node, twin):
        PREPARE.get(change, lambda _node: None)(subject)
    made = verifications(monkeypatch)
    after_checkpoint(node, monkeypatch, lambda: CHANGES[change](node))
    frame = await relayed(node, monkeypatch, f"n5r2-{change}")
    [verified] = made
    counts = (verified.computed["send"], verified.reused["send"])
    no_token(monkeypatch)
    after_checkpoint(twin, monkeypatch, lambda: CHANGES[change](twin))
    full = await relayed(twin, monkeypatch, f"n5r2-{change}")
    record(door="single", change=change, n5=frame["status"], pre_n5=full["status"], computed=counts[0], reused=counts[1])
    assert counts == SEND_CHECK.get(change, (1, 0)), change
    assert same_answer(frame, full), change
    if change in STATUS:
        assert frame["status"] == STATUS[change], change


@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_batch_change_to_the_narrowed_parts_gives_the_pre_n5_answer(node, twin, monkeypatch, change):
    for subject in (node, twin):
        PREPARE.get(change, lambda _node: None)(subject)
    made = verifications(monkeypatch)
    after_batch_checkpoint(node, monkeypatch, lambda: CHANGES[change](node))
    frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id=f"n5r2b-{change}")
    [verified] = made
    counts = (verified.computed["send"], verified.reused["send"])
    no_token(monkeypatch)
    after_batch_checkpoint(twin, monkeypatch, lambda: CHANGES[change](twin))
    full = await send_batch(twin, batch_payloads(), monkeypatch, batch_id=f"n5r2b-{change}")
    record(door="batch", change=change, n5=frame["status"], pre_n5=full["status"], computed=counts[0], reused=counts[1])
    assert counts == SEND_CHECK.get(change, (1, 0)), change
    assert same_batch_answer(frame, full), change
    if change in STATUS:
        assert frame["status"] == STATUS[change], change


# -- which part moves, read directly -------------------------------------------------------------------------------

MOVED = {"own_key_deleted": ["keys"], "own_policy_same_length_edit": ["ledger"], "own_policy_whitespace_edit": ["ledger"],
         "own_binding_row_reinserted": [], "two_grants_rows_swapped": ["ledger"], "two_grants_keys_swapped": ["keys"],
         "observation_generation_bumped": ["ledger"], "own_grant_row_generation_bumped": ["ledger"],
         "keys_db_replaced_by_copy": ["keys"],
         "keys_db_replaced_with_rotated_key": ["keys"], "ledger_db_replaced_with_revoked_copy": ["ledger"],
         "other_grant_activated": ["ledger"], "other_grant_revoked": ["ledger"], "prefix_and_extended_grant_keys": [],
         "other_grant_key_deleted": [], "other_request_rows": [], "keys_db_chmod": ["keys"], "ledger_db_chmod": ["ledger"],
         "index_root_chmod": [], "node_dir_chmod": []}


@pytest.mark.parametrize("change", sorted(MOVED))
def test_which_part_moves(node, change):
    PREPARE.get(change, lambda _node: None)(node)
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        first = node.index.send_token(grant_of(node), verified, node.ledger.path)
        assert first is not None
        CHANGES[change](node)
        second = node.index.send_token(grant_of(node), verified, node.ledger.path)
        assert second is not None, change
        assert sorted(part for part in first if first[part] != second[part]) == MOVED[change], change


def test_the_key_row_digest_moves_on_a_rotation_read_through_the_same_probe(node):
    """The digest half of the keys part: a rotation in place (same inode) moves it on the cached probe."""
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        first = node.index.send_token(grant_of(node), verified, node.ledger.path)
        node.index.keys.delete(grant_of(node))
        node.index.keys.get(grant_of(node), create=True)
        second = node.index.send_token(grant_of(node), verified, node.ledger.path)
    assert first["keys"][0] == second["keys"][0] and first["keys"][1] != second["keys"][1]


# -- the recheck's kept token: does the ledger part move across a quiet recheck? ---------------------------------

def kept_tokens(monkeypatch) -> list:
    seen = []
    original = SearchVerification.keep_send_token

    def keep(self, before, after):
        seen.append((before, after))
        return original(self, before, after)
    monkeypatch.setattr(SearchVerification, "keep_send_token", keep)
    return seen


@pytest.mark.asyncio
async def test_no_part_of_the_token_moves_across_a_quiet_recheck_on_either_door(node, monkeypatch):
    """`keep_send_token` excludes the ledger part because the checkpoint writes the ledger. With the narrowed
    part (this grant's rows and the singletons, not data_version or the file state) the checkpoint's own writes
    (p2a_requests, p2a_receipts) leave it equal, so the exclusion no longer carries the quiet path."""
    seen = kept_tokens(monkeypatch)
    frame = await relayed(node, monkeypatch, "n5r2-quiet")
    assert frame["status"] == "ok"
    batch = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5r2-quiet-batch")
    assert batch["status"] == "ok"
    assert len(seen) == 2
    for before, after in seen:
        assert before is not None and after is not None
        assert before == after  # the ledger part included


# -- None never matches ---------------------------------------------------------------------------------------------

def test_a_missing_key_store_reads_as_none(node):
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        os.unlink(node.index.keys.path)
        assert node.index.send_token(grant_of(node), verified, node.ledger.path) is None


def test_a_ledger_missing_a_hashed_table_reads_as_none(node):
    ledger_sql(node, ("DROP TABLE p2a_grant_authorities", ()))
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        assert node.index.send_token(grant_of(node), verified, node.ledger.path) is None


def test_a_closed_verification_reads_as_none(node):
    verified = SearchVerification(node.search.resolver, node.search.reviews)
    verified.close()
    assert node.index.send_token(grant_of(node), verified, node.ledger.path) is None


def test_none_never_matches_and_is_never_kept(node):
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        token = node.index.send_token(grant_of(node), verified, node.ledger.path)
        assert token is not None
        for before, after in ((None, token), (token, None), (None, None)):
            verified.keep_send_token(before, after)
            assert verified._send is None
            assert verified.send_unchanged(token) is False and verified.send_unchanged(None) is False
        verified.keep_send_token(token, token)
        assert verified._send == token
        assert verified.send_unchanged(None) is False and verified.send_unchanged(dict(token)) is True


# -- a protection revision moved into p2a_node by another request's sync ------------------------------------------

def black_hole_synced_by_another_request(node):
    """A new black hole (the canonical protection clock moves), then another request's protection sync writes the
    new revision into p2a_node (epoch+1) before this search's send check runs."""
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                     "VALUES('n5r2-protected','person','Rowan Sample','rowan sample')")
        conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                     "VALUES('bh-n5r2','n5r2-protected','Rowan Sample','rowan sample','complete')")
    with node.ledger._transaction() as db:
        node.protocol._sync_protection(db)


def test_a_synced_protection_move_moves_the_canonical_and_ledger_parts(node):
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        first = node.index.send_token(grant_of(node), verified, node.ledger.path)
        black_hole_synced_by_another_request(node)
        second = node.index.send_token(grant_of(node), verified, node.ledger.path)
    assert sorted(part for part in first if first[part] != second[part]) == ["canonical", "ledger"]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_a_protection_move_synced_by_another_request_refuses_on_both_doors(node, twin, monkeypatch, door):
    made = verifications(monkeypatch)
    if door == "single":
        after_checkpoint(node, monkeypatch, lambda: black_hole_synced_by_another_request(node))
        frame = await relayed(node, monkeypatch, "n5r2-synced-bh")
    else:
        after_batch_checkpoint(node, monkeypatch, lambda: black_hole_synced_by_another_request(node))
        frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5r2b-synced-bh")
    [verified] = made
    counts = (verified.computed["send"], verified.reused["send"])
    no_token(monkeypatch)
    if door == "single":
        after_checkpoint(twin, monkeypatch, lambda: black_hole_synced_by_another_request(twin))
        full = await relayed(twin, monkeypatch, "n5r2-synced-bh")
        same = same_answer(frame, full)
    else:
        after_batch_checkpoint(twin, monkeypatch, lambda: black_hole_synced_by_another_request(twin))
        full = await send_batch(twin, batch_payloads(), monkeypatch, batch_id="n5r2b-synced-bh")
        same = same_batch_answer(frame, full)
    record(door=door, change="black_hole_synced_by_another_request", n5=frame["status"], pre_n5=full["status"],
           computed=counts[0], reused=counts[1])
    assert counts == (1, 0) and same and frame["status"] == "error"
