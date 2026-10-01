"""N5 security review 3 (candidate 11 @ 8c641e5e): Lane A's restore-side protection sync, landed between the
checkpoint and the send, on the direct-search twins (an active Off-limits boundary). Scratch only."""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, batch_payloads,  # noqa: F401
                                                         commit, no_token, node, relayed, same_answer,
                                                         same_batch_answer, twin, verifications)
from topos.permissions_v2 import refresh_loop

BLACK_HOLE = ("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
              "VALUES('bh-n5r3','','Quillon Marsh','quillon marsh','complete')")


def restore_sync_after_a_protection_move(node):
    """A protection clock move (a new Off-limits entry), then the refresh loop's own sync (Lane A, 443b6afc)."""
    commit(node, BLACK_HOLE)
    assert refresh_loop.protection_sync(node.protocol)() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_the_refresh_loops_protection_sync_after_the_checkpoint_refuses_on_both(node, twin, monkeypatch, door):
    made = verifications(monkeypatch)
    if door == "single":
        after_checkpoint(node, monkeypatch, lambda: restore_sync_after_a_protection_move(node))
        frame = await relayed(node, monkeypatch, "n5r3-restore-sync")
        no_token(monkeypatch)
        after_checkpoint(twin, monkeypatch, lambda: restore_sync_after_a_protection_move(twin))
        full = await relayed(twin, monkeypatch, "n5r3-restore-sync")
        assert same_answer(frame, full)
    else:
        after_batch_checkpoint(node, monkeypatch, lambda: restore_sync_after_a_protection_move(node))
        frame = await send_batch(node, batch_payloads(), monkeypatch, batch_id="n5r3b-restore-sync")
        no_token(monkeypatch)
        after_batch_checkpoint(twin, monkeypatch, lambda: restore_sync_after_a_protection_move(twin))
        full = await send_batch(twin, batch_payloads(), monkeypatch, batch_id="n5r3b-restore-sync")
        assert same_batch_answer(frame, full)
    assert frame["status"] == "error"   # authority_stale: the synced revision is not the checkpoint's
    assert made[0].computed["send"] == 1 and made[0].reused["send"] == 0   # canonical and ledger parts moved
