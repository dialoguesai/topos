"""v11: a journal row withholds on a WHOLE Off-limits name or alias only, never on one word of a longer name.

protects: on a copy of the owner's node one Off-limits entry whose name holds two everyday words withheld 61 of 710
journal entries from routines for those words alone ("a 60-year-old", "a taco place"); the daily routine lost an
evening's entry whole. The owner ruled (20 Nov 2026) that any single word on its own is fine, and that the full name,
or a variation of it, is what is Off-limits. These tests pin both halves:
  - released in a journal item: one word of a longer name, in lower case, capitalised, or in a form ("Place's");
  - still withheld: the whole name in any case, with doubled spaces, punctuation or an invisible character inside
    it; a one-word alias (it IS a whole alias) and its pet-name forms;
  - messages and every other kind keep v8 (a capitalised word of the name withholds there).
Fixtures are synthetic invented names.
"""
from __future__ import annotations

import pytest

from topos.permissions_v2 import entity_boundary
from tests.permissions_v2.test_entity_boundary_short_variants import boundary

NAME = "Juniper Meadowlark Hollis"


def withheld_in_journal(text, *aliases, canonical=NAME, **extra):
    """The routine lane's item rule over one answer item built from a journal entry."""
    item = {"record_id": "tl-synthetic", "retrieval_source": "canonical:journal_entries", "summary_text": text,
            **extra}
    return boundary(*aliases, canonical=canonical).item_names_protected(item, bare_parts_anywhere=True)


@pytest.mark.parametrize("text", [
    "After the show we ended up at a taco place, and it was great.",
    "I met a 60-year-old guy who lives on the coast.",
])
def test_everyday_words_of_an_off_limits_name_release_a_journal_entry(text):
    assert not withheld_in_journal(text, canonical="Old Place Hollis")


@pytest.mark.parametrize("text", [
    "ran into hollis at the market",          # one word, lower case
    "Hollis came too.",                       # one word, capitalised
    "Saw Juniper's new bike.",                # one word, in a form
    "the meadowlark sang all morning",        # one word that is also an ordinary word
])
def test_one_word_of_a_longer_name_releases_a_journal_entry(text):
    assert not withheld_in_journal(text)


@pytest.mark.parametrize("text", [
    "Dinner with Juniper Meadowlark Hollis tonight.",
    "dinner with juniper meadowlark hollis tonight",       # any case
    "Dinner with Juniper  Meadowlark   Hollis tonight.",   # doubled spaces
    "Dinner with Juniper-Meadowlark-Hollis tonight.",      # punctuation inside the name
    "Dinner with Juniper Mea​dowlark Hollis tonight.",  # an invisible character inside it
])
def test_the_whole_name_withholds_in_any_case_and_spacing(text):
    assert withheld_in_journal(text)


def test_a_one_word_alias_is_a_whole_alias_and_withholds_with_its_forms():
    assert withheld_in_journal("Coffee with Zeb this morning.", "Zeb")
    assert withheld_in_journal("Coffee with Zebby this morning.", "Zeb")


def test_the_people_column_reads_whole_names_too():
    assert not withheld_in_journal("a quiet evening", people="Hollis")
    assert withheld_in_journal("a quiet evening", people="Juniper Meadowlark Hollis")


def test_messages_keep_the_v8_rule():
    """Outside a journal row a capitalised word of the name still withholds; v11 changes nothing there."""
    b = boundary(canonical=NAME)
    assert b.item_names_protected({"text": "Lunch with Hollis today."}, bare_parts_anywhere=False)
    assert not b.item_names_protected({"text": "ran into hollis at the market"}, bare_parts_anywhere=False)


def test_the_boundary_version_is_v11():
    assert entity_boundary.VERSION == "node-observed-entity-boundary/v11"
