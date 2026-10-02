"""WS4 N5 after the journal round (IF-5): a journal member and the send check's revision token.

A journal entry is a member of a knowledge grant's index only while its family's flag is on, and the full send check
reads that flag twice: in the basis (`_family_rubric_basis`: the journal rubric revision exists only while the family
does) and in the member loop (`_live_rows` finds no journal row while the family is off). The flag is process-local:
switching it moves neither the canonical database nor any file the token covers. So the token holds the families
that exist (`enabled_tables`), and a flag switched between the recheck and the send forces the full check. Without
that part the send check skipped and released, on both doors, where the pre-N5 send check refused (found when N5 was
rebased onto the journal round).

Pinned here, on the single and the batch door, each against an identical node whose send check always runs in full
(`no_token`, the send check as before N5):
- a quiet search releases the journal entry, runs the member loop once, and gives the same answer;
- the entry edited, or withdrawn to owner-only, after the checkpoint: the full check, refused as before;
- the family switched off after the checkpoint (its member and its basis entry vanish), and switched on over an
  index built while it was off (the basis gains the journal rubric): the full check, refused as before;
- the families part of the token moves with the flag, and no other part does.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2.test_journal_family import (AFTER_ITS_DAY, JOURNAL_FLAG, SOURCE, WORDS, _db, _entry,
                                                      _journal_node, node, owner)  # noqa: F401
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, no_token, relayed,
                                                         verifications)
from topos.permissions_v2.search_index import SearchVerification

QUERY = {"query": "draft walked home", "k": 10}


def build(canonical, directory, monkeypatch):
    """test_journal_family's end-to-end node: a knowledge grant over the journal source, one entry indexed."""
    _entry(canonical, "e1")
    search, state = _journal_node(canonical, directory, monkeypatch)
    assert state == {"state": "ready", "member_count": 1}
    search.query = dict(QUERY)
    return search


def copy_of(canonical, directory):
    """An identical canonical database for the reference node, copied before either node writes its entry."""
    directory.mkdir()
    target = directory / "canonical.db"
    source, copy = sqlite3.connect(canonical), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        source.close()
        copy.close()
    return target


def without_opaque_ids(value):
    """An answer with its record ids blanked. Each node mints its own record-id key, so two identical nodes name the
    same record differently; every other byte must be equal."""
    if isinstance(value, dict):
        return {key: "r.*" if key == "record_id" else without_opaque_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [without_opaque_ids(item) for item in value]
    return value


def outputs(frame):
    if frame["status"] != "ok":
        return None
    payload = frame["payload"]
    return without_opaque_ids([item["output"] for item in payload["items"]] if "items" in payload
                              else [payload["output"]])


# -- what lands between the checkpoint and the send -----------------------------------------------------------------

def entry_edited(canonical, monkeypatch):
    with _db(canonical) as conn:
        conn.execute("UPDATE journal_entries SET content=content || ' Then dinner.' WHERE entry_id='e1'")


def entry_withdrawn(canonical, monkeypatch):
    with _db(canonical) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e1','t','t')")


def family_switched_off(canonical, monkeypatch):
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)


def family_switched_on(canonical, monkeypatch):
    monkeypatch.setenv(JOURNAL_FLAG, "true")


def index_built_with_the_family_off(search, monkeypatch):
    monkeypatch.delenv(JOURNAL_FLAG, raising=False)
    with owner():
        state = search.index.rebuild(search.search_raw["binding"]["grant_id"], now=AFTER_ITS_DAY)
    assert state == {"state": "ready", "member_count": 0}


CHANGES = {"quiet": lambda canonical, monkeypatch: None, "entry_edited": entry_edited,
           "entry_withdrawn": entry_withdrawn, "family_switched_off": family_switched_off,
           "family_switched_on": family_switched_on}
PREPARE = {"family_switched_on": index_built_with_the_family_off}
FAMILY_OFF_AT_SEARCH = {"family_switched_on"}
# (computed, reused) of the send check's member loop; (0, 1): the token did not move and the loop was skipped.
SEND_CHECK = {"quiet": (0, 1)}
STATUS = {"quiet": "ok", "entry_edited": "error", "entry_withdrawn": "error", "family_switched_off": "error",
          "family_switched_on": "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_a_journal_member_gets_the_full_send_checks_answer(node, tmp_path, monkeypatch, change, door):
    reference = copy_of(node, tmp_path / "reference")
    subjects = [(build(node, tmp_path / "n5", monkeypatch), node),
                (build(reference, tmp_path / "reference", monkeypatch), reference)]
    for search, _canonical in subjects:
        PREPARE.get(change, lambda _search, _monkeypatch: None)(search, monkeypatch)
    made = verifications(monkeypatch)
    frames = []
    for number, (search, canonical) in enumerate(subjects):
        if number:
            no_token(monkeypatch)
        if change in FAMILY_OFF_AT_SEARCH:
            monkeypatch.delenv(JOURNAL_FLAG, raising=False)
        else:
            monkeypatch.setenv(JOURNAL_FLAG, "true")
        action = (lambda canonical=canonical: CHANGES[change](canonical, monkeypatch))
        if door == "single":
            after_checkpoint(search, monkeypatch, action)
            frames.append(await relayed(search, monkeypatch, f"n5j-{change}"))
        else:
            after_batch_checkpoint(search, monkeypatch, action)
            frames.append(await send_batch(search, [dict(QUERY)], monkeypatch, batch_id=f"n5jb-{change}"))
    frame, full = frames
    verified = made[0]
    assert (verified.computed["send"], verified.reused["send"]) == SEND_CHECK.get(change, (1, 0)), change
    assert frame["status"] == full["status"] == STATUS[change], change
    assert outputs(frame) == outputs(full), change
    if change == "quiet":
        [[record]] = [output["records"] for output in outputs(frame)]
        assert (record["kind"], record["content"], record["source_ids"]) == ("journal_entry", WORDS, [SOURCE])


def test_the_families_part_moves_with_the_flag_and_no_other_part_does(node, tmp_path, monkeypatch):
    search = build(node, tmp_path / "n5", monkeypatch)
    grant_id = search.search_raw["binding"]["grant_id"]
    with SearchVerification(search.search.resolver, search.search.reviews) as verified:
        on = search.index.send_token(grant_id, verified, search.ledger.path)
        monkeypatch.delenv(JOURNAL_FLAG, raising=False)
        off = search.index.send_token(grant_id, verified, search.ledger.path)
        monkeypatch.setenv(JOURNAL_FLAG, "true")
        again = search.index.send_token(grant_id, verified, search.ledger.path)
    assert on is not None and off is not None
    assert [part for part in on if on[part] != off[part]] == ["families"]
    assert "journal_entries" in on["families"] and "journal_entries" not in off["families"]
    assert again == on
