"""N5 security review 3 (candidate 11 @ 8c641e5e): adversarial cases on a journal member. Scratch only.

Each case lands a change between the checkpoint and the send on an N5 node, then on an identical node whose send
check always runs in full (`no_token`, the pre-N5 send check), on the single and the batch door, and asserts the
same frame. A different frame = N5 released (or refused) where the pre-N5 send check did not.

The inputs are candidate 10's: the owner proof of a journal row (`capture_receipts.proven`: the source's install
and the owner's receipt, Lanes F/G), the writer columns (Lane G), OD-59's closed-fact markers (Lane D), and the
journal flag switched INSIDE the recheck (between the recheck's two token reads), which the tree's seam tests do
not cover (they switch it after the checkpoint).
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2.test_closed_fact_floor import _writer
from tests.permissions_v2.test_journal_family import (APP, JOURNAL_FLAG, OWNER, SOURCE, _db, _entry, _journal_node,  # noqa: F401
                                                      _off_limits, node)
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, no_token, relayed,
                                                         verifications)
from tests.permissions_v2.test_search_send_token_journal import QUERY, copy_of, outputs
from topos.permissions_v2 import capture_receipts as cr
from topos.permissions_v2.search_release import MessageSearchRelease

CITE = {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE}
PROTECTED = "Pemberly Hollis"   # an Off-limits person the entry never names: an ACTIVE boundary, nothing withheld


def build(canonical, directory, monkeypatch, *, entry=None, prepare=None):
    _entry(canonical, "e1", **(entry or {}))
    if prepare is not None:
        prepare(canonical)
    search, state = _journal_node(canonical, directory, monkeypatch)
    assert state == {"state": "ready", "member_count": 1}, state
    search.query = dict(QUERY)
    return search


def subjects(node, tmp_path, monkeypatch, *, boundary=False, entry=None, prepare=None):
    """(N5 node, reference node), each over its own copy of the canonical database, built the same way."""
    reference = copy_of(node, tmp_path / "reference")
    out = []
    for canonical, directory in ((node, tmp_path / "n5"), (reference, tmp_path / "reference")):
        if boundary:
            _off_limits(canonical, PROTECTED)
        out.append((build(canonical, directory, monkeypatch, entry=entry, prepare=prepare), canonical))
    return out


async def pair(subjects, monkeypatch, *, change=None, during=None, door, tag):
    """The N5 frame, the pre-N5 frame and the N5 node's SearchVerification."""
    made = verifications(monkeypatch)
    frames = []
    for number, (search, canonical) in enumerate(subjects):
        if number:
            no_token(monkeypatch)
        if during is not None:
            during_recheck(search, monkeypatch, lambda canonical=canonical: during(canonical))
        action = (lambda canonical=canonical: change(canonical)) if change is not None else (lambda: None)
        if door == "single":
            after_checkpoint(search, monkeypatch, action)
            frames.append(await relayed(search, monkeypatch, f"{tag}-{door}"))
        else:
            after_batch_checkpoint(search, monkeypatch, action)
            frames.append(await send_batch(search, [dict(QUERY)], monkeypatch, batch_id=f"{tag}-{door}"))
    return frames[0], frames[1], made[0]


def during_recheck(search, monkeypatch, action):
    """Run `action` inside the gated recheck: after the walk, before the checkpoint and the second token read."""
    original = MessageSearchRelease._walk.__get__(search.search)

    def _walk(*args, **kwargs):
        result = original(*args, **kwargs)
        action()
        return result
    monkeypatch.setattr(search.search, "_walk", _walk)


# -- the changes -----------------------------------------------------------------------------------------------------

def install_retired(canonical):
    with _db(canonical) as conn:
        conn.execute("UPDATE source_runtime_installs SET is_active=0, status='retired' WHERE source_id=?", (SOURCE,))


def attest_e1(canonical):
    """Before the build: e1 is a pre-stamp row (no writer class), proven by the owner's receipt alone."""
    with _db(canonical) as conn:
        preview = cr.preview(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE, app_id=APP)
        cr.attest(conn, owner_id=OWNER, table="journal_entries", source_id=SOURCE, app_id=APP,
                  preview_digest=preview["preview_digest"], confirm=True, now=1_700_000_000)


def receipt_revoked(canonical):
    with _db(canonical) as conn:
        [receipt] = [item["receipt_id"] for item in cr.receipts(conn, owner_id=OWNER)]
        cr.revoke(conn, owner_id=OWNER, receipt_id=receipt, now=1_700_000_500)


def writer_class_changed(canonical):
    with _db(canonical) as conn:
        conn.execute("UPDATE journal_entries SET writer_class='owner_app', writer_app_id=? WHERE entry_id='e1'", (APP,))


def writer_dataset_changed(canonical):
    with _db(canonical) as conn:
        conn.execute("UPDATE journal_entries SET writer_dataset_id=? WHERE entry_id='e1'", (f"{OWNER}:x:y",))


def closed_fact_rederived(canonical):
    """Before the build: a fact naming e1, closed by the derivation writer's supersession, so OD-59 releases e1."""
    conn = sqlite3.connect(str(canonical))
    try:
        _writer("superseded")(conn, CITE)
    finally:
        conn.close()


def closure_made_the_owners(canonical):
    """OD-59 fail-closed marker: an owner actor on the closed fact; `_floors` withholds e1 again."""
    with _db(canonical) as conn:
        conn.execute("UPDATE signal_objects SET updated_by='owner_revision' WHERE object_id='f-closed'")


def successor_made_the_owners(canonical):
    """OD-59: the machine successor becomes an owner promotion; the closure stops counting as re-derivation."""
    with _db(canonical) as conn:
        conn.execute("UPDATE signal_objects SET payload_json=json_set(payload_json,'$.extractor.model','owner-promote') "
                     "WHERE object_id='f-next'")


def journal_flag_off(_canonical, monkeypatch=None):
    import os
    os.environ.pop(JOURNAL_FLAG, None)


# -- the owner proof (Lanes F/G), read by the full check only through a dependency `_load` -------------------------

PROOF = {"install_retired": (install_retired, {}, None),
         "receipt_revoked": (receipt_revoked, {"writer_class": None, "dataset": None}, attest_e1)}
# With an inactive boundary no dependency is sealed, so neither send check re-reads the proof: both release (a
# pre-existing property of the send check, not N5's). With an active boundary the member's own row is a sealed
# dependency and the full check's `_load` re-proves it: both refuse.
PROOF_STATUS = {False: "ok", True: "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("boundary", [False, True], ids=["boundary_inactive", "boundary_active"])
@pytest.mark.parametrize("change", sorted(PROOF))
async def test_a_proof_change_after_the_checkpoint_gives_the_full_send_checks_answer(node, tmp_path, monkeypatch,
                                                                                     change, boundary, door):
    act, entry, prepare = PROOF[change]
    built = subjects(node, tmp_path, monkeypatch, boundary=boundary, entry=entry, prepare=prepare)
    frame, full, verified = await pair(built, monkeypatch, change=act, door=door, tag=f"n5r3-{change}")
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0), "the canonical part moved: full check"
    assert frame["status"] == full["status"], (change, boundary, frame["status"], full["status"])
    assert outputs(frame) == outputs(full)
    assert frame["status"] == PROOF_STATUS[boundary], (change, boundary, frame["status"])


# -- the writer columns (Lane G): in the member fingerprint ----------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", ["writer_class_changed", "writer_dataset_changed"])
async def test_a_writer_column_change_after_the_checkpoint_refuses_on_both(node, tmp_path, monkeypatch, change, door):
    act = {"writer_class_changed": writer_class_changed, "writer_dataset_changed": writer_dataset_changed}[change]
    built = subjects(node, tmp_path, monkeypatch)
    frame, full, verified = await pair(built, monkeypatch, change=act, door=door, tag=f"n5r3-{change}")
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0)
    assert frame["status"] == full["status"] == "error", (change, frame["status"], full["status"])


# -- OD-59 (Lane D): a closed naming fact's marker, read by `_floors` only (the walk), never by the send check ------

@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", ["closure_made_the_owners", "successor_made_the_owners"])
async def test_an_od59_marker_change_after_the_checkpoint_gives_the_full_send_checks_answer(node, tmp_path, monkeypatch,
                                                                                            change, door):
    act = {"closure_made_the_owners": closure_made_the_owners, "successor_made_the_owners": successor_made_the_owners}[change]
    built = subjects(node, tmp_path, monkeypatch, prepare=closed_fact_rederived)
    frame, full, verified = await pair(built, monkeypatch, change=act, door=door, tag=f"n5r3-{change}")
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0), "the canonical part moved: full check"
    assert frame["status"] == full["status"], (change, frame["status"], full["status"])
    assert outputs(frame) == outputs(full)
    # Recorded, not required: both release what the checkpoint decided (floors are not re-run at send, before or
    # after N5). The next recheck withholds it.
    assert frame["status"] == "ok"


# -- the journal flag switched INSIDE the recheck: the kept token must not survive it --------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_the_journal_flag_switched_inside_the_recheck_is_never_trusted_by_the_send_check(node, tmp_path,
                                                                                               monkeypatch, door):
    built = subjects(node, tmp_path, monkeypatch)
    frame, full, verified = await pair(built, monkeypatch, during=journal_flag_off, door=door, tag="n5r3-flag-recheck")
    # `keep_send_token` saw the families part move between its two reads: nothing kept, the member loop ran.
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0)
    assert frame["status"] == full["status"] == "error", (frame["status"], full["status"])
