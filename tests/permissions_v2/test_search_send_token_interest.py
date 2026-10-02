"""N5 x IF-5 I7 merge seam (rehearsal): the interest family's flag and the send check's revision token.

The full send check reads the interest flag in the basis (`_family_rubric_basis` carries the interest label rubric
only while the flag is on). The flag is process-local, so switching it moves no file the token covers; unless the
token holds it, the send check skips and releases where the full check refuses. Each case runs against an identical
node whose send check always runs in full (`no_token`).
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_interest_door import (_built, _rebuild, browsing, canonical, flag, owner)  # noqa: F401
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, no_token, relayed,
                                                         verifications)
from tests.permissions_v2.test_search_send_token_journal import copy_of, outputs
from topos.permissions_v2 import interest_index as ii
from topos.permissions_v2.search_index import SearchVerification

QUERY = {"query": "sourdough baking", "k": 10}


def build(canonical, directory, monkeypatch):
    node, state = _built(canonical, directory, monkeypatch)
    node.query = dict(QUERY)
    return node, state


def family_switched_off(canonical, monkeypatch):
    monkeypatch.delenv(ii.FLAG, raising=False)


def family_switched_on(canonical, monkeypatch):
    monkeypatch.setenv(ii.FLAG, "true")


def index_built_with_the_family_off(node, monkeypatch):
    monkeypatch.delenv(ii.FLAG, raising=False)
    assert _rebuild(node) == {"state": "ready", "member_count": 0}


CHANGES = {"quiet": lambda canonical, monkeypatch: None, "family_switched_off": family_switched_off,
           "family_switched_on": family_switched_on}
PREPARE = {"family_switched_on": index_built_with_the_family_off}
STATUS = {"quiet": "ok", "family_switched_off": "error", "family_switched_on": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_an_interest_member_gets_the_full_send_checks_answer(browsing, tmp_path, monkeypatch, change, door):
    reference = copy_of(browsing, tmp_path / "reference")
    subjects = []
    for canonical_path, directory in ((browsing, tmp_path / "n5"), (reference, tmp_path / "reference")):
        monkeypatch.setenv(ii.FLAG, "true")
        node, state = build(canonical_path, directory, monkeypatch)
        assert state["member_count"] == 2
        PREPARE.get(change, lambda _node, _monkeypatch: None)(node, monkeypatch)
        subjects.append((node, canonical_path))
    made = verifications(monkeypatch)
    frames = []
    for number, (node, canonical_path) in enumerate(subjects):
        if number:
            no_token(monkeypatch)
        if change == "family_switched_on":
            monkeypatch.delenv(ii.FLAG, raising=False)
        else:
            monkeypatch.setenv(ii.FLAG, "true")
        action = (lambda canonical_path=canonical_path: CHANGES[change](canonical_path, monkeypatch))
        if door == "single":
            after_checkpoint(node, monkeypatch, action)
            frames.append(await relayed(node, monkeypatch, f"n5i-{change}"))
        else:
            after_batch_checkpoint(node, monkeypatch, action)
            frames.append(await send_batch(node, [dict(QUERY)], monkeypatch, batch_id=f"n5ib-{change}"))
    frame, full = frames
    assert frame["status"] == full["status"] == STATUS[change], (change, frame["status"], full["status"])
    assert outputs(frame) == outputs(full), change


def test_the_token_moves_with_the_interest_flag(browsing, tmp_path, monkeypatch):
    node, _state = build(browsing, tmp_path / "n5", monkeypatch)
    grant_id = node.search_raw["binding"]["grant_id"]
    with SearchVerification(node.search.resolver, node.search.reviews) as verified:
        on = node.index.send_token(grant_id, verified, node.ledger.path)
        monkeypatch.delenv(ii.FLAG, raising=False)
        off = node.index.send_token(grant_id, verified, node.ledger.path)
    assert on is not None and off is not None
    assert [part for part in on if on[part] != off[part]] == ["families"]
    assert "activity_events" in on["families"] and "activity_events" not in off["families"]
