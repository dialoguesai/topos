"""`ANSWER_SHARED_CONTEXT` (off): the answer prompt's room for the released items' text is shared between them.

Carried forward from topos#102 (11 Nov 2026). The prompt cut every item's text at 420 characters and its supporting
line at 160, wherever that fell: a long journal entry lost everything after its first 420 characters while seven
one-line messages beside it left most of their room unused, and the cut could fall inside a sentence and lose the
negation or condition at its end. With the switch on:
  - a short item's unused room goes to the long ones, in equal shares, and the whole is what the same number of items
    (eight when there are fewer) could take before, so the prompt's ceiling does not move;
  - no item gets less room than before;
  - an item that still does not fit ends at its last whole sentence that fits; when the whole sentences that fit
    would fill less than half its room (its first sentence does not fit, or a short opening comes before one long
    sentence) it is cut at its room, as before, so it never shows less than half of what the fixed cut showed;
  - the Off-limits item filter (Fix A) reads each item exactly as the prompt will show it, again after a withheld
    item's room has gone to the others;
  - the owner's private receipt says `context_budget: shared`.
Off is the prompt, the filter and the receipt of 1.5.49. Only text of records the share already released is shown.

This file checks what the prompt holds. Whether the writer answers better with it was not measured: no model ran.
Invented data only; the writer and the judge are stand-ins.
"""
from __future__ import annotations

import random

import pytest

from tests.permissions_v2.test_answer_protected_item_filter import Recording, fact, goal, journal, message
from topos.permissions_v2 import switches
from topos.permissions_v2.answer_generation import (BODY_CHARS, SUPPORT_CHARS, _clip_sentences, _shared_room,
                                                    build_prompt, shown_item, shown_items,
                                                    withhold_protected_items)
from topos.permissions_v2.answer_protocol import K_ANSWER_LEGACY

SWITCH = switches.PREFIX + "ANSWER_SHARED_CONTEXT"
FILTER = switches.PREFIX + "ANSWER_PROTECTED_ITEM_FILTER"
QUESTION = "What happened with the kiln?"
#: Forty-three characters with its space: nine of them are 387, so the fixed cut at 420 falls inside the tenth.
FILLER = "I checked the packing list and the chairs. "
LATE = "I switched off the kiln because the sensor failed."
LONG = FILLER * 12 + LATE                                  # 516 + 50 characters: the answer is past the fixed cut
#: The whole filler sentences that fit one item's fixed room.
FITTING = (BODY_CHARS + 1) // len(FILLER)
DIGITS = "123456789abcdef"


@pytest.fixture(autouse=True)
def switches_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv(FILTER, raising=False)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


def collapsed(text):
    return " ".join(text.split())


def prompt_for(records):
    return build_prompt(QUESTION, records, precision="none")


# --- the switch ------------------------------------------------------------------------------------------------------

def test_the_switch_is_off_bound_and_unbound_and_on_is_one_environment_line(monkeypatch):
    item = switches.BY_NAME[SWITCH]
    assert item.kind == "bool" and item.unbound is False and item.bound is False
    assert switches.value(SWITCH) is False
    monkeypatch.setenv(SWITCH, "true")
    assert switches.value(SWITCH) is True


# --- off: the fixed cuts of before -----------------------------------------------------------------------------------

def test_off_every_item_is_cut_at_the_fixed_lengths_wherever_they_fall():
    records = [journal("1", LONG), message("2", "The kiln has cooled."), goal("3", "Fix the kiln", LONG)]
    shown = shown_items(records, "none")
    assert shown == [shown_item(record, "none") for record in records]
    assert shown[0].body == LONG[:BODY_CHARS] and not shown[0].body.endswith(".")       # inside a sentence
    assert shown[2].support == LONG[:SUPPORT_CHARS]
    assert LATE not in prompt_for(records).user                                         # the answer is cut away


# --- on: the room is shared ------------------------------------------------------------------------------------------

def test_on_a_long_item_takes_the_room_its_short_neighbours_leave(on):
    records = [journal("1", LONG), message("2", "The kiln has cooled."), message("3", "Glazed the blue jug.")]
    prompt = prompt_for(records)
    assert LATE in prompt.user and collapsed(LONG) in prompt.user                       # whole, answer included
    assert prompt.raw_texts[0] == collapsed(LONG) and collapsed(LONG) in prompt.record_texts[0]
    assert prompt.item_lines[0].endswith(LATE)


def test_on_the_supporting_line_takes_the_room_other_lines_leave(on):
    records = [goal("3", "Fix the kiln", LONG), fact("4", "Delta Bakery"), message("2", "The kiln has cooled.")]
    prompt = prompt_for(records)
    assert "Supporting text: " + collapsed(LONG) in prompt.user
    assert prompt.user.count("Supporting text: ") == 2                                  # one line an item, as before
    assert all(block.count("\n\nSupporting text: ") <= 1 for block in prompt.item_lines)


def test_on_items_that_fit_the_fixed_cuts_make_the_prompt_of_before_byte_for_byte(monkeypatch):
    records = [message("1", "Booked  the lunch\ntable."), journal("2", "Long walk\n\nthen soup."),
               goal("3", "Plan the Saturday lunch", "My goal is to plan\n the Saturday lunch"),
               fact("4", "Delta Bakery"), message("5", "x" * BODY_CHARS)]
    before = prompt_for(records)
    monkeypatch.setenv(SWITCH, "true")
    assert prompt_for(records) == before


def test_on_no_item_shows_less_room_than_before_and_none_more_than_its_own_text(on):
    rng = random.Random(102)
    for _ in range(200):
        lengths = [rng.choice([0, 1, 40, 419, 420, 421, 700, 5000]) for _ in range(rng.randint(1, 30))]
        slots = max(len(lengths), K_ANSWER_LEGACY)
        room = _shared_room(lengths, BODY_CHARS, slots)
        assert all(min(length, BODY_CHARS) <= got <= length for got, length in zip(room, lengths))
        assert sum(room) <= slots * BODY_CHARS
        # Nothing is left unused while a text is still cut.
        assert sum(room) == slots * BODY_CHARS or room == lengths
        assert room == _shared_room(lengths, BODY_CHARS, slots)


def test_on_the_spare_room_is_dealt_in_equal_shares(on):
    assert _shared_room([5000, 5000, 20, 20], 420, 8) == [1660, 1660, 20, 20]
    assert _shared_room([5000, 600, 20], 420, 8) == [2740, 600, 20]            # what one cannot use goes to the other
    assert _shared_room([5000] * 8, 420, 8) == [420] * 8                       # eight long items: the cuts of before
    assert _shared_room([5000] * 30, 420, 30) == [420] * 30
    assert _shared_room([], 420, 8) == []


@pytest.mark.parametrize("count", [1, 3, 8, 9, 16, 30])
def test_on_the_prompt_is_never_longer_than_the_fixed_cuts_allowed_that_many_items(on, count):
    text = "Sentence number one is here. " * 60
    records = [goal(DIGITS[number % 15], text, text) for number in range(count)]
    shown = shown_items(records, "none")
    slots = max(count, K_ANSWER_LEGACY)
    assert sum(len(item.body) for item in shown) <= slots * BODY_CHARS
    assert sum(len(item.support) for item in shown) <= slots * SUPPORT_CHARS
    if count >= K_ANSWER_LEGACY:
        assert all(len(item.body) <= BODY_CHARS and len(item.support) <= SUPPORT_CHARS for item in shown)


# --- on: an item that does not fit ends at a sentence end ------------------------------------------------------------

def test_on_an_item_that_does_not_fit_ends_at_its_last_whole_sentence(on):
    records = [journal(DIGITS[number], LONG) for number in range(8)]              # eight long items: 420 each
    shown = shown_items(records, "none")
    for item in shown:
        assert item.body == (FILLER * FITTING).strip() and FITTING == 9           # nine whole sentences, 386 chars
        assert collapsed(LONG).startswith(item.body + " ")
    # Off, the same items are cut inside the tenth sentence.
    assert shown_item(records[0], "none").body == LONG[:BODY_CHARS] != shown[0].body


def test_on_a_cut_never_keeps_the_start_of_a_sentence_without_its_end(on):
    """The condition at the end of a sentence is not lost: the sentence is shown whole or not at all."""
    promise = "I agreed to lend the kiln to the studio only if the roof is fixed first."
    text = FILLER * FITTING + promise + " " + FILLER * 4
    shown = shown_items([journal(DIGITS[number], text) for number in range(8)], "none")[0]
    assert "I agreed to lend the kiln" not in shown.body and shown.body.endswith("chairs.")
    assert "I agreed to lend the kiln" in shown_item(journal("1", text), "none").body       # off: the half sentence
    assert "only if the roof" not in shown_item(journal("1", text), "none").body


def test_on_an_item_whose_first_sentence_does_not_fit_is_cut_at_its_room_as_before(on):
    """Never nothing: a long message with no full stop still shows its first 420 characters."""
    run_on = "the kiln and the glaze and the shelf " * 40
    shown = shown_items([message(DIGITS[number], run_on) for number in range(8)], "none")
    assert all(item.body == collapsed(run_on)[:BODY_CHARS] == shown_item(message("1", run_on), "none").body
               for item in shown)


#: A message or an AI-chat reply: a one-word opening sentence, then one long sentence with no further full stop.
OPENING = "Thanks. "
RUN_ON = OPENING + "Then " + "the kiln and the glaze and the shelf " * 65       # 8 + 2,410 characters


def test_on_a_short_opening_before_a_long_sentence_is_cut_at_its_room_not_after_the_opening(on):
    """Eight such items showed 7 characters each ("Thanks.") where the fixed cut showed 420: the one whole sentence
    that fits is under half the room, so the item is cut at its room, as the fixed cut cuts it."""
    assert len(collapsed(RUN_ON)) > 2400
    records = [message(DIGITS[number], RUN_ON) for number in range(8)]
    shown = shown_items(records, "none")
    for record, item in zip(records, shown):
        assert item.body == collapsed(RUN_ON)[:BODY_CHARS] == shown_item(record, "none").body
        assert len(item.body) == BODY_CHARS
    # Beside short items it takes their room, and still shows that room, not the opening alone.
    beside = shown_items([message("1", RUN_ON), message("2", "The kiln has cooled.")], "none")[0]
    assert beside.body == collapsed(RUN_ON)[:8 * BODY_CHARS - len("The kiln has cooled.")]


def test_on_the_sentence_cut_keeps_at_least_half_the_room_or_gives_way_to_the_cut_at_the_room(on):
    """The floor, at its edge: whole sentences that fill half the room or more are what is shown (the worst case:
    210 characters of 420); one character under half and the item is cut at its room."""
    tail = " " + "B" * 400                                                     # a sentence that never fits
    at_half = "a" * (BODY_CHARS // 2 - 1) + "."                                 # 210 characters
    under = "a" * (BODY_CHARS // 2 - 2) + "."                                   # 209
    assert _clip_sentences(at_half + tail, BODY_CHARS) == at_half
    assert _clip_sentences(under + tail, BODY_CHARS) == (under + tail)[:BODY_CHARS]
    shown = shown_items([journal(DIGITS[number], at_half + tail) for number in range(8)], "none")
    assert all(item.body == at_half for item in shown)
    rng = random.Random(290)
    for _ in range(300):
        text = " ".join("W" + "w" * rng.randint(0, 12) + rng.choice(["", "", ".", "!", "?"])
                        for _ in range(rng.randint(1, 200)))
        limit = rng.randint(1, 700)
        cut = _clip_sentences(text, limit)
        assert text.startswith(cut) and len(cut) <= limit
        assert cut == text or 2 * len(cut) >= limit, (text, limit)


def test_on_a_supporting_line_that_does_not_fit_ends_at_its_last_whole_sentence(on):
    """Nine long supporting lines have 160 characters each, as before, and each ends at a sentence end: three whole
    filler sentences (128 characters), where the fixed cut stops inside the fourth."""
    records = [goal(DIGITS[number], "Fix the kiln", LONG) for number in range(9)]
    shown = shown_items(records, "none")
    for record, item in zip(records, shown):
        assert item.support == (FILLER * 3).strip() and item.support.endswith("chairs.")
        assert collapsed(LONG).startswith(item.support + " ")
        assert shown_item(record, "none").support == LONG[:SUPPORT_CHARS] != item.support   # off: inside the fourth
    prompt = prompt_for(records)
    assert all(block.endswith("Supporting text: " + (FILLER * 3).strip()) for block in prompt.item_lines)
    assert prompt.user.count("Supporting text: ") == 9
    assert "Supporting text: " + LONG[:SUPPORT_CHARS] not in prompt.user


def test_on_a_supporting_line_with_a_short_opening_is_cut_at_its_room_as_before(on):
    records = [goal(DIGITS[number], "Fix the kiln", RUN_ON) for number in range(9)]
    for record, item in zip(records, shown_items(records, "none")):
        assert item.support == collapsed(RUN_ON)[:SUPPORT_CHARS] == shown_item(record, "none").support


@pytest.mark.parametrize("limit", [1, 10, 46, 47, 48, 100, 420, 600, 613, 614, 615])
def test_the_sentence_cut_is_always_the_start_of_the_text_within_its_limit(limit):
    for text in (LONG, "No full stop at all " * 30, 'She said "stop." Then left. And again! Really? Yes.',
                 "Dr. Ada met the team at 3 p.m. on Friday. They agreed.", "A [1]. B [2, 3]. C."):
        cut = _clip_sentences(text.strip(), limit)
        assert text.strip().startswith(cut) and 0 < len(cut) <= limit
        assert cut == text.strip() or len(text.strip()) > limit


# --- on: Fix A reads the items as the prompt shows them --------------------------------------------------------------

MARK = "Ostrowmark"


def crowded(marked: dict) -> list:
    """Eight long journal entries; `marked` puts the Off-limits mark at a character offset of some of them."""
    records = []
    for number in range(8):
        text = FILLER * 14
        if number in marked:
            at = marked[number]
            text = text[:at] + MARK + " came by. " + text[at:]
        records.append(journal(DIGITS[number], text))
    return records


def test_on_the_filter_reads_an_item_again_once_a_withheld_item_has_given_it_room(on):
    """Item 0 carries the mark in its second sentence, where every prompt shows it. Item 1 carries it in its eleventh,
    past its own 420 characters: beside seven other long items it is not shown, and it is once item 0 is withheld
    and its room has been shared out (480 characters each)."""
    records = crowded({0: len(FILLER), 1: (FITTING + 1) * len(FILLER)})
    boundary = Recording(flagged=[MARK])
    first = shown_items(records, "none")
    assert MARK in first[0].body and MARK not in first[1].body
    kept, withheld = withhold_protected_items(records, boundary=boundary, precision="none")
    assert kept == records[2:] and withheld == 2
    prompt = prompt_for(kept)
    assert MARK not in prompt.user and all(MARK not in text for text in prompt.raw_texts)
    # The last pass asked about exactly the texts the prompt shows.
    last = boundary.asked[-len(kept):]
    assert [texts for texts in last] == [item.texts() for item in shown_items(kept, "none")]
    assert [texts[0] for texts in last] == [block.split("\n", 1)[1] for block in prompt.item_lines]


def test_off_the_filter_is_one_pass_over_the_fixed_cuts_as_before():
    records = crowded({0: len(FILLER), 1: (FITTING + 1) * len(FILLER)})
    boundary = Recording(flagged=[MARK])
    kept, withheld = withhold_protected_items(records, boundary=boundary, precision="none")
    assert kept == records[1:] and withheld == 1                # item 1's mark is past the fixed cut: never shown
    assert len(boundary.asked) == len(records)
    assert MARK not in prompt_for(kept).user


def test_on_nothing_protected_withholds_nothing_in_one_pass(on):
    records = crowded({})
    boundary = Recording(flagged=[MARK])
    assert withhold_protected_items(records, boundary=boundary, precision="none") == (records, 0)
    assert len(boundary.asked) == len(records)


def test_on_every_item_protected_is_the_zero_records_path(on):
    records = crowded({number: len(FILLER) for number in range(8)})
    assert withhold_protected_items(records, boundary=Recording(flagged=[MARK]), precision="none") == ([], 8)


# --- through the real service: the prompt and the receipt ------------------------------------------------------------

BODIES = ["Working on the kiln. " + FILLER * 12 + LATE, "Working on the kiln shelves.",
          "Working on the kiln glaze.", "Working on the garden shed roof."]


def _kiln_node(tmp_path):
    from tests.permissions_v2.test_answer_time_tr162 import DAY, NOW, _node
    return _node(tmp_path, precision="none", times=[NOW - DAY - number * 3600 for number in range(len(BODIES))],
                 bodies=BODIES)


def test_the_service_shows_the_writer_the_late_sentence_and_says_so_on_the_receipt_only_with_the_switch(
        tmp_path, monkeypatch):
    from tests.permissions_v2.test_answer_time_tr162 import _asked
    node = _kiln_node(tmp_path)
    answer = lambda prompt: "The owner switched the kiln off after a sensor fault [1]."      # noqa: E731
    body, receipt, prompts = _asked(node, QUESTION, answer)
    assert body["outcome"] == "answered" and "context_budget" not in receipt
    assert LATE not in prompts[0].user
    template = receipt["template_version"]
    monkeypatch.setenv(SWITCH, "true")
    body, receipt, prompts = _asked(node, QUESTION, answer)
    assert body["outcome"] == "answered" and receipt["context_budget"] == "shared"
    assert LATE in prompts[0].user
    # The system text is the same template: only the items' text moved.
    assert receipt["template_version"] == template
