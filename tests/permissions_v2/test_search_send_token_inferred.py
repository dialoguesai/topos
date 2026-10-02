"""N5 x IF-6 merge seam (candidate 12): the derived-facts flag and the send check's revision token.

The full send check reads `TOPOS_PERMISSIONS_V2_DERIVED_FACTS` twice: in the basis (`_family_rubric_basis` carries the
inferred-fact guards' version only while the flag is on) and in `fact_projection`'s step 7 (an inferred fact releases
only while it is on). The flag is process-local, so switching it moves no file the token covers; unless the token
holds it, a flag switched after the checkpoint, or inside the recheck, lets the send check skip and release where the
full check refuses (IF-6 §7). Each case runs on the single and the batch door, against an identical node whose send
check always runs in full (`no_token`, the send check as before N5):
- a quiet search releases the inferred fact and the entry it cites, skips the member loop, and answers the same;
- the flag switched off after the checkpoint (the basis loses its key), and switched on over an index built while it
  was off (the basis gains it): the full check, refused as before;
- the flag switched off inside the recheck, between its two token reads: nothing is kept, the full check refuses;
- the families part of the token moves with the flag, and no other part does.
Synthetic fixtures only.
"""
from __future__ import annotations

import os

import pytest

from tests.permissions_v2.test_journal_family import AFTER_ITS_DAY, JOURNAL_FLAG, SOURCE, node, owner  # noqa: F401
from tests.permissions_v2.test_journal_typed_items import _attest_owner, _cites, _fact, _node, _publish
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, no_token, relayed,
                                                         verifications)
from tests.permissions_v2.test_search_send_token_journal import copy_of, outputs
from tests.permissions_v2.test_zz_n5r3_journal import during_recheck
from topos.permissions_v2.inferred_facts import FLAG
from topos.permissions_v2.search_index import SearchVerification

PROSE = "Long day on the parser and the release build."   # states nothing a class form would ground
QUERY = {"query": "Atlas parser", "k": 10}


def build(canonical, monkeypatch):
    """A journal entry, the extractor's fact drawn from it, its assessment, and a grant's index with the flag on."""
    from tests.permissions_v2.test_journal_family import _entry
    _attest_owner(canonical)
    _entry(canonical, "e1", PROSE)
    _fact(canonical, _cites("e1"), value="Atlas")
    _publish(canonical, "e1")
    search, state = _node(canonical, canonical.parent, monkeypatch)
    assert state == {"state": "ready", "member_count": 2}, state     # the entry, and the fact drawn from it
    search.query = dict(QUERY)
    return search


def subjects(node, tmp_path, monkeypatch):
    """(N5 node, reference node), each over its own copy of the canonical database, built the same way."""
    monkeypatch.setenv(FLAG, "true")
    reference = copy_of(node, tmp_path / "reference")
    return [(build(node, monkeypatch), node), (build(reference, monkeypatch), reference)]


def flag_switched_off(_canonical, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def flag_switched_on(_canonical, monkeypatch):
    monkeypatch.setenv(FLAG, "true")


def index_built_with_the_flag_off(search, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    with owner():
        state = search.index.rebuild(search.search_raw["binding"]["grant_id"], now=AFTER_ITS_DAY)
    assert state == {"state": "ready", "member_count": 1}            # the entry only: nothing inferred


CHANGES = {"quiet": lambda canonical, monkeypatch: None, "flag_switched_off": flag_switched_off,
           "flag_switched_on": flag_switched_on}
PREPARE = {"flag_switched_on": index_built_with_the_flag_off}
FLAG_OFF_AT_SEARCH = {"flag_switched_on"}
# (computed, reused) of the send check's member loop; (0, 1): the token did not move and the loop was skipped.
SEND_CHECK = {"quiet": (0, 1)}
STATUS = {"quiet": "ok", "flag_switched_off": "error", "flag_switched_on": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_an_inferred_fact_gets_the_full_send_checks_answer(node, tmp_path, monkeypatch, change, door):
    built = subjects(node, tmp_path, monkeypatch)
    for search, _canonical in built:
        PREPARE.get(change, lambda _search, _monkeypatch: None)(search, monkeypatch)
    made = verifications(monkeypatch)
    frames = []
    for number, (search, canonical) in enumerate(built):
        if number:
            no_token(monkeypatch)
        monkeypatch.setenv(JOURNAL_FLAG, "true")
        if change in FLAG_OFF_AT_SEARCH:
            monkeypatch.delenv(FLAG, raising=False)
        else:
            monkeypatch.setenv(FLAG, "true")
        action = (lambda canonical=canonical: CHANGES[change](canonical, monkeypatch))
        if door == "single":
            after_checkpoint(search, monkeypatch, action)
            frames.append(await relayed(search, monkeypatch, f"if6-{change}"))
        else:
            after_batch_checkpoint(search, monkeypatch, action)
            frames.append(await send_batch(search, [dict(QUERY)], monkeypatch, batch_id=f"if6b-{change}"))
    frame, full = frames
    verified = made[0]
    assert (verified.computed["send"], verified.reused["send"]) == SEND_CHECK.get(change, (1, 0)), change
    assert frame["status"] == full["status"] == STATUS[change], (change, frame["status"], full["status"])
    assert outputs(frame) == outputs(full), change
    if change == "quiet":
        [records] = [output["records"] for output in outputs(frame)]
        facts = [record for record in records if record["kind"] == "fact"]
        assert [(r["assertion"], r["content"], r["source_ids"]) for r in facts] == \
            [("inferred", "Owner works on Atlas.", [SOURCE])]
        assert [record["kind"] for record in records if record["kind"] == "journal_entry"] == ["journal_entry"]


def flag_off_inside_the_recheck(_canonical):
    os.environ.pop(FLAG, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_the_flag_switched_inside_the_recheck_is_never_trusted_by_the_send_check(node, tmp_path, monkeypatch,
                                                                                       door):
    built = subjects(node, tmp_path, monkeypatch)
    made = verifications(monkeypatch)
    frames = []
    for number, (search, canonical) in enumerate(built):
        if number:
            no_token(monkeypatch)
        monkeypatch.setenv(JOURNAL_FLAG, "true")
        monkeypatch.setenv(FLAG, "true")
        during_recheck(search, monkeypatch, lambda canonical=canonical: flag_off_inside_the_recheck(canonical))
        if door == "single":
            after_checkpoint(search, monkeypatch, lambda: None)
            frames.append(await relayed(search, monkeypatch, f"if6-recheck-{door}"))
        else:
            after_batch_checkpoint(search, monkeypatch, lambda: None)
            frames.append(await send_batch(search, [dict(QUERY)], monkeypatch, batch_id=f"if6-recheck-{door}"))
    frame, full = frames
    verified = made[0]
    # `keep_send_token` saw the families part move between its two reads: nothing kept, the member loop ran.
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0)
    assert frame["status"] == full["status"] == "error", (frame["status"], full["status"])


def test_the_families_part_moves_with_the_flag_and_no_other_part_does(node, tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    search = build(node, monkeypatch)
    grant_id = search.search_raw["binding"]["grant_id"]
    with SearchVerification(search.search.resolver, search.search.reviews) as verified:
        on = search.index.send_token(grant_id, verified, search.ledger.path)
        monkeypatch.delenv(FLAG, raising=False)
        off = search.index.send_token(grant_id, verified, search.ledger.path)
        monkeypatch.setenv(FLAG, "true")
        again = search.index.send_token(grant_id, verified, search.ledger.path)
        monkeypatch.delenv(JOURNAL_FLAG, raising=False)                # the flag is inert without the journal family
        no_journal = search.index.send_token(grant_id, verified, search.ledger.path)
    assert on is not None and off is not None
    assert [part for part in on if on[part] != off[part]] == ["families"]
    assert "inferred_facts" in on["families"] and "inferred_facts" not in off["families"]
    assert again == on
    assert "inferred_facts" not in no_journal["families"] and "journal_entries" not in no_journal["families"]
