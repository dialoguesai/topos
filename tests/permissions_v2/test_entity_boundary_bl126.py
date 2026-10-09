"""BL-126: three forms of an Off-limits person the share boundary's matcher did not find.

From the node's second re-check (T4, R3-L5) and its notes. Each was released by every check a share's release reads
(a message, a journal entry, a goal), and on the routine lane as at the doors:

  - A NAME RUN TOGETHER. Ruling M (7 Oct) made an identifier of letters only match only as itself, so the username
    "samrivers" no longer withheld "Sam Rivers called." where the contact was saved under something else and no
    entity carries the name. The reading the reviewer proposed (`run_together_hits`): such an identifier of
    RUN_TOGETHER_MIN_CHARS (8) letters or more is also found where its letters stand as consecutive whole words.
    Shorter ones stay found only as themselves ("work" never in "work place"; ruling M).
  - A POSSESSIVE WITH NO APOSTROPHE opening a sentence ("Quorras car is in the drive."). Written as a proper noun
    anywhere else it was already a long form of the part; at the opening of a sentence a capital says nothing, so it
    is read as the three-letter names' s-form is: where a word that is not a function word follows (`long_possessives`,
    `_genitive_hits`).
  - A NAME PART IN A SCRIPT WRITTEN WITHOUT SPACES ("田中" of "田中 太郎" in "明日は田中さんと会議です。"). A part
    needed three letters and to stand as a whole token; in such a script a part is two characters and a letter always
    touches it. Now such a part is a part from two characters and is found anywhere in a run, as a short name in such
    a script already was (the fourth round).

Each is a tightening only: nothing withheld before is released (the last test). The cost, measured before it was
taken (14,126 lines of public English package text; the lane's report): the run-together reading withheld none for
eight name-shaped usernames and at most 3 lines (0.02%) for sixteen word compounds ("standalone"); the possessive
reading withheld nothing more for six names whose parts are words ("Rose", "Will", "Grace", "Mark", "Hope").
Every person here is invented.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_entity_boundary_identifier_aliases import ORDINARY, gate
from tests.permissions_v2.test_entity_boundary_unspaced_scripts import protecting
from tests.permissions_v2.test_entity_boundary_v8 import withheld
from topos.permissions_v2 import entity_boundary
from topos.permissions_v2.entity_boundary import RUN_TOGETHER_MIN_CHARS, name_parts, run_together_hits

pytestmark = pytest.mark.public

KINDS = ("message", "journal_entry", "goal")


def everywhere(boundary, text) -> list:
    return [withheld(kind, boundary, text) for kind in KINDS]


def a_contact_saved_as_a_nickname(username):
    """An entry saved under a nickname, with one letters-only username: no entity carries the person's name."""
    return gate("Foxglove", aliases=("foxglove", username), identifiers=[username])


# --- a name run together --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Sam Rivers called.", "sam rivers called back", "Sam-Rivers said hi.",
                                  "Lunch with SAM RIVERS."])
def test_a_username_that_is_a_name_run_together_withholds_the_name(text):
    """Rule: `_reading_hits` asks `run_together_hits` for the whole-token identifiers. Take it out and each is released."""
    assert everywhere(a_contact_saved_as_a_nickname("samrivers"), text) == [True] * 3


@pytest.mark.parametrize("text", ["Sam went down to the rivers.", "The same rivers flooded.", "Sam, Riverside, ok."]
                         + list(ORDINARY))
def test_words_that_do_not_spell_it_consecutively_are_not_it(text):
    assert everywhere(a_contact_saved_as_a_nickname("samrivers"), text) == [False] * 3


def test_a_shorter_username_is_still_found_only_as_itself():
    """Ruling M stands below the floor: "workplace" is nine letters, "homework" eight; "workday" seven is not read."""
    assert RUN_TOGETHER_MIN_CHARS == 8
    assert everywhere(a_contact_saved_as_a_nickname("workday"), "A work day at the office.") == [False] * 3
    assert everywhere(a_contact_saved_as_a_nickname("workday"), "Ask workday about it.") == [True] * 3
    assert run_together_hits("a home work sheet", frozenset({"homework"}))
    assert not run_together_hits("a home work sheet", frozenset({"homewor"}))
    assert not run_together_hits("homework", frozenset({"homework"}))      # one word is the whole-token rule's
    assert not run_together_hits("sam rivers", frozenset({"sam rivers"}))  # a term with a space is no identifier


# --- a possessive with no apostrophe opening a sentence --------------------------------------------------------------

@pytest.mark.parametrize("text", ["Quorras car is in the drive.", "Vellabys house is the blue one.",
                                  "Rain all day. Quorras bike got wet."])
def test_a_possessive_with_no_apostrophe_opening_a_sentence_withholds(text):
    """Rule: `long_possessives` in the genitive's opening place. Take it out and each is released."""
    assert everywhere(protecting("Quorra Vellaby"), text) == [True] * 3


@pytest.mark.parametrize("text", ["Quorras is not a word I know.", "Roses are red."])
def test_it_is_a_possessive_only_before_a_word_that_is_not_a_function_word(text):
    boundary = protecting("Quorra Vellaby") if text.startswith("Quorra") else protecting("Rose Tyler")
    assert everywhere(boundary, text) == [False] * 3


def test_the_possessive_mid_sentence_was_already_read():
    assert everywhere(protecting("Quorra Vellaby"), "We parked behind Quorras car.") == [True] * 3


# --- a name part in a script written without spaces ------------------------------------------------------------------

@pytest.mark.parametrize("name,text", [("田中 太郎", "明日は田中さんと会議です。"), ("田中 太郎", "太郎くんが来た。"),
                                       ("たなか たろう", "きのうたなかさんにあった。"),
                                       ("สมชาย ใจดี", "วันนี้ไปกินข้าวกับสมชายที่ร้าน")])
def test_a_name_part_in_a_script_without_spaces_is_found_inside_a_run(name, text):
    """Rule: `name_parts` keeps a two-character part in such a script, and `_reading_hits` looks for it in a run. Take
    either out and the first three are released."""
    assert everywhere(protecting(name), text) == [True] * 3


@pytest.mark.parametrize("text", ["今日は雨です。", "中田さんに会った。", "田んぼの中。"])
def test_other_text_in_that_script_is_not_withheld(text):
    assert everywhere(protecting("田中 太郎"), text) == [False] * 3


def test_a_part_needs_two_characters_of_that_script_and_three_letters_elsewhere():
    assert name_parts("田中 太郎") == {"田中", "太郎"}
    assert name_parts("王 伟") == set()                                     # one character is not a name part
    assert name_parts("Al Bo") == set()                                     # Latin parts keep three letters


# --- a tightening only ------------------------------------------------------------------------------------------------

def test_nothing_withheld_before_is_released(monkeypatch):
    texts = ["Sam Rivers called.", "Quorras car is in the drive.", "明日は田中さんと会議です。", "Quorra called.",
             "Ask samrivers.", "田中太郎", *ORDINARY]
    boundaries = [lambda: a_contact_saved_as_a_nickname("samrivers"), lambda: protecting("Quorra Vellaby"),
                  lambda: protecting("田中 太郎")]
    now = [[everywhere(make(), text) for text in texts] for make in boundaries]
    monkeypatch.setattr(entity_boundary, "run_together_hits", lambda plain, terms: False)
    monkeypatch.setattr(entity_boundary, "long_possessives", lambda parts: frozenset())

    def old_name_parts(value):
        return {part for part in map(entity_boundary.skeleton, entity_boundary.WORDS.findall(entity_boundary.normalized(value)))
                if sum(ch.isalpha() for ch in part) >= entity_boundary.MIN_NAME_PART_LETTERS}
    monkeypatch.setattr(entity_boundary, "name_parts", old_name_parts)
    try:
        before = [[everywhere(make(), text) for text in texts] for make in boundaries]
    finally:
        monkeypatch.undo()
    assert before != now                                                    # the three forms were released before
    for was, is_now in zip(before, now):
        for one, other in zip(was, is_now):
            assert [not a or b for a, b in zip(one, other)] == [True] * 3


def test_a_username_run_together_from_three_words_withholds_them():
    """Review R-N1-151 L3: the join is not limited to two words."""
    assert everywhere(a_contact_saved_as_a_nickname("annamaewong"), "Anna Mae Wong called back.") == [True] * 3
    assert everywhere(a_contact_saved_as_a_nickname("annamaewong"), "Anna and Mae met Wong.") == [False] * 3


def test_a_parts_possessive_reads_only_where_it_opens_a_sentence():
    """Review R-N1-151 L3, the usability side: mid-sentence and in lower case the s-form of a part that is a word is
    the word ("roses"), as for any bare part outside a journal row."""
    assert withheld("message", protecting("Rose Tyler"), "We planted roses near the gate.") is False
    assert withheld("message", protecting("Rose Tyler"), "Roses car is in the drive.") is True
