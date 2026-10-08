"""BL-144: the answer step's anchor rule counts a word as in the items only when the two are one suffix apart.

The anchor rule: a question word of 8 letters or more that no item the model may see carries, in any form, makes the
answer step abstain before the model, and a sentence of the model's that echoes it is dropped. 1.5.0 compared the
words through the whole fold, applied until it settles, which takes two suffixes off a name shaped as a word plus
"-ings" ("cannings" to "canning" to "can", "herrings" to "her", "workings" to "work"). So a supplied name that no item
carries counted as present wherever a released item said the short word: the model ran on it and its echo of the
name was kept, and an Off-limits name typed in lower case (which the boundary reads as a word) could come back in
the answer as the asker's own word (review R7-L1: 16 of 33 sentences, and once through the real answer pass).

The rule now (`answer_checks.question_only_anchors`): a question word is in the items when it and an item word are
at most one suffix apart ("meetings"/"meeting", "compilers"/"compiler", "stories"/"story", "updating"/"update",
"sandwiches"/"sandwich"). The echo drop still compares through the whole fold, so it drops more, never less.

The Off-limits echo (`answer_checks.protected_question_words`): a question word no item carries as written, which
the boundary reads as protected when written as a name (as itself or with one suffix off: "cherries" as "Cherry"),
is dropped from the model's sentences in those forms, in any case and at any place in the sentence; the boundary
itself does not read a lower-case name, a name first in a sentence, or an "-ies" plural there.

Also here: a run of "y" in the question no longer ends the answer as `model_error` (R7-N3: `_vowel` recursed once per
"y"). Every person here is invented.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_subject_fold import Boundary, LAKE, MODES, _pass, _record
from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_checks import _stem, protected_question_words, question_only_anchors
from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor
from topos.permissions_v2.entity_boundary import EntityBoundary

pytestmark = pytest.mark.public

NO_ANSWER = {"version": "topos-answer/v1", "outcome": "no_answer"}


def protecting(name):
    """The real boundary over one Off-limits entry the owner made for this invented name."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)", (name.lower(), name, json.dumps([name])))
    return EntityBoundary(conn)


# --- Presence: one suffix apart, never two ------------------------------------------------------------------------

#: (the asker's word, an item that carries only its whole fold): the whole fold meets, one suffix does not.
TWO_SUFFIXES = [
    ("cannings", "We can take the early ferry to the lake."),
    ("herrings", "She left her bag at the lake cabin."),
    ("goodings", "The lake cabin was good for a long weekend."),
    ("workings", "My goal is to finish the compiler at work by Friday."),
    ("fieldings", "The field by the lake flooded after the storm."),
    ("landings", "We will land at the lake airstrip at noon."),
]


@pytest.mark.parametrize("word,item", TWO_SUFFIXES)
def test_a_word_two_suffixes_from_every_item_word_is_absent_and_abstains(word, item):
    """Rule: `question_only_anchors` compares one suffix apart. Compare through the whole fold (`_stem`) again and
    every case here counts as present: the model runs."""
    assert _stem(word) in {_stem(token) for token in item.lower().replace(".", "").split()}, "the fold meets"
    question = f"What did {word} say about the lake?"
    prompt = build_prompt(question, [_record("a", item)], precision="none")
    assert question_only_anchors(question, prompt.raw_texts) == frozenset({word})
    assert question_lacks_permitted_anchor(prompt)


@pytest.mark.parametrize("word,item", TWO_SUFFIXES)
def test_the_echo_of_a_word_two_suffixes_away_is_dropped_in_lower_case_and_first(word, item):
    """With a second anchor the items do carry, the model runs; its echo of the absent word is dropped, written
    lower case mid-sentence or first in the sentence."""
    records = [_record("a", item + " The lakeside cabin was reserved.")]
    prompt = build_prompt(f"Did {word} reserve the lakeside cabin?", records, precision="none")
    assert not question_lacks_permitted_anchor(prompt)
    for sentence in (f"Once the storm passed, {word} reported the lakeside cabin was booked [1].",
                     f"{word.capitalize()} reported the lakeside cabin was booked [1]."):
        for mode in MODES:
            checked = post_check_answer(sentence, records, prompt, mode=mode, boundary=Boundary())
            assert (checked.body.model_dump(), checked.dropped_question_echo) == (NO_ANSWER, 1), (mode, sentence)


@pytest.mark.parametrize("question,item", [
    ("What meetings are planned?", "The design review meeting is on Thursday."),
    ("What happened to the compilers?", "The compiler was rebuilt on Friday."),
    ("Which projects were shared?", "The garden project starts in May."),
    ("What did the newsletters say?", "The newsletter went out on Monday."),
    ("Which activities came up?", "The activity on Saturday is a guided kayak trip."),
    ("Which sandwiches were ordered?", "One sandwich was ordered for the trip."),
    ("Who is updating the timetable?", "Please update the timetable before the trip."),
    ("Who is organising the trip?", "We organise the trip each spring."),
    ("How did the planning go?", "The plan is to leave at dawn with the tent."),
    ("What did the cabinet hold?", "Both cabinets held fishing gear."),
])
def test_a_word_one_suffix_from_an_item_word_is_present_both_ways(question, item):
    prompt = build_prompt(question, [_record("a", item)], precision="none")
    assert question_only_anchors(question, prompt.raw_texts) == frozenset()
    assert not question_lacks_permitted_anchor(prompt)


@pytest.mark.parametrize("question,item", [
    # shares its first letters with an item word, never a suffix apart (the reviewer's F1)
    ("What did the compilations include?", "The compiler was rebuilt on Friday."),
    # its fold stands inside a longer item word (the reviewer's F2)
    ("Which cabinets were painted?", "The cabinetmaker painted the doors."),
    ("Who are the shadowglasses?", "The shadowglassworks opened at the lake."),
])
def test_a_word_sharing_only_letters_with_an_item_word_is_absent(question, item):
    prompt = build_prompt(question, [_record("a", item)], precision="none")
    assert question_lacks_permitted_anchor(prompt)


def test_an_echo_shorter_than_an_anchor_and_an_echo_in_a_third_form_are_dropped():
    """The reviewer's F3 (the echo read only on long words) and F6 (the anchors folded, the sentence not)."""
    records = [_record("a", LAKE)]
    for question, sentence in (("Did the hartleys reserve the lakeside cabin?", "Hartley reserved it [1]."),
                               ("Did the shadowglasses affect the lakeside cabin?", "It was shadowglassed [1].")):
        prompt = build_prompt(question, records, precision="none")
        assert not question_lacks_permitted_anchor(prompt)
        for mode in MODES:
            checked = post_check_answer(sentence, records, prompt, mode=mode, boundary=Boundary())
            assert (checked.body.model_dump(), checked.dropped_question_echo) == (NO_ANSWER, 1), (mode, sentence)


# --- The Off-limits echo, with the real boundary --------------------------------------------------------------------

#: (an invented Off-limits name, the asker's form the boundary does not read in the question, an item that carries a
#: form one suffix away, the model's sentence echoing a form the boundary does not read in the answer)
PROTECTED_FORMS = [
    ("Cherry Vandal", "cherries", "The cherry orchard by the lake was in bloom.", "Cherries said the orchard bloomed [1]."),
    ("Cherry Vandal", "cherries", "The cherry orchard by the lake was in bloom.", "The orchard bloomed, cherries said [1]."),
    ("Odette Fieldings", "fieldings", "The fielding drills by the lake ran long.", "fielding said the drills ran long [1]."),
    ("Tamsin Workings", "workings", "Working by the lake, the crew finished early.", "Once done, workings said the crew finished [1]."),
]


@pytest.mark.parametrize("name,form,item,sentence", PROTECTED_FORMS)
def test_an_off_limits_name_in_a_form_the_boundary_misses_is_dropped_from_the_answer(name, form, item, sentence):
    """Rule: `protected_question_words` and its echo drop. Without them each sentence is ANSWERED (the boundary's own
    answer check does not read these forms), so the asker's form of the protected name would reach the recipient."""
    boundary = protecting(name)
    question = f"What did {form} say about the lake?"
    assert not boundary.mentions_protected(question), "the question is not refused as written"
    assert not boundary.mentions_protected(sentence), "nor is the model's sentence"
    records = [_record("a", item)]
    prompt = build_prompt(question, records, precision="none")
    assert protected_question_words(question, prompt.raw_texts, boundary)
    for mode in MODES:
        checked = post_check_answer(sentence, records, prompt, mode=mode, boundary=boundary)
        assert (checked.body.model_dump(), checked.dropped_question_echo, checked.reason) == \
               (NO_ANSWER, 1, "all_sentences_dropped"), mode


def test_a_word_the_items_carry_as_written_is_never_an_off_limits_echo():
    """The narrow side: a question word an item carries as written is the share's own word, not the asker's, and a
    word the boundary does not read as the name in any of the forms checked is not dropped."""
    boundary = protecting("Cherry Vandal")
    records = [_record("a", "The cherry orchard by the lake was in bloom.")]
    prompt = build_prompt("What about the cherry orchard by the lake?", records, precision="none")
    assert protected_question_words(prompt.question, prompt.raw_texts, boundary) == frozenset()
    for mode in MODES:
        checked = post_check_answer("The orchard by the lake bloomed with cherry trees [1].", records, prompt,
                                    mode=mode, boundary=boundary)
        assert checked.body.outcome == "answered" and checked.dropped_question_echo == 0, mode
    # "Tamsin Workings": the asker's "working" is read as the name, its fold "work" is not, and stays.
    boundary = protecting("Tamsin Workings")
    records = [_record("a", "The crew finished their work at the lake cabin.")]
    prompt = build_prompt("Who was working on the lake cabin?", records, precision="none")
    assert protected_question_words(prompt.question, prompt.raw_texts, boundary) == frozenset({"working"})
    for mode in MODES:
        checked = post_check_answer("Work at the cabin by the water ended ahead of time [1].", records, prompt,
                                    mode=mode, boundary=boundary)
        assert checked.body.outcome == "answered" and checked.dropped_question_echo == 0, mode


# --- Through the answer pass ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_abstains_on_an_off_limits_surname_asked_in_lower_case(legacy, tmp_path, monkeypatch):
    """The reviewer's case: an Off-limits "Tamsin Workings", asked as "workings" (the whole fold is "work", which the
    goal fixture's item says). 1.5.0 called the model and answered with the asker's word; now no model call."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    original = node.search.resolver.entity_boundary
    protected = protecting("Tamsin Workings")

    class Both:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, *texts):
            return protected.mentions_protected(*texts) or self.inner.mentions_protected(*texts)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(node.search.resolver, "entity_boundary", lambda conn: Both(original(conn)))
    body, receipt, calls = _pass(node, "once the compiler is done, workings will celebrate [1].",
                                 "What did workings say at work?")
    assert calls == [] and body == NO_ANSWER
    assert receipt["reason"] == "question_not_supported"


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_drops_the_echo_of_an_off_limits_surname_asked_in_lower_case(legacy, tmp_path, monkeypatch):
    """The same with a second anchor the item carries ("compiler"): the model runs, and its echo of "workings" is
    dropped, written lower case or first in the sentence."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    original = node.search.resolver.entity_boundary
    protected = protecting("Tamsin Workings")

    class Both:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, *texts):
            return protected.mentions_protected(*texts) or self.inner.mentions_protected(*texts)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(node.search.resolver, "entity_boundary", lambda conn: Both(original(conn)))
    for answer in ("once the compiler is done, workings will celebrate [1].", "Workings will celebrate the compiler [1]."):
        body, receipt, calls = _pass(node, answer, "What did workings say about the compiler at work?")
        assert len(calls) == 1 and body == NO_ANSWER, answer
        assert receipt["reason"] == "all_sentences_dropped" and receipt["sentences"]["dropped_question_echo"] == 1


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_answers_a_long_run_of_y_instead_of_failing(legacy, tmp_path, monkeypatch):
    """R7-N3: a run of 1,200 "y" ending "-ing" raised RecursionError in the fold and ended as `model_error` with no
    model call. Now the fold settles and the run is an absent anchor like any invented word."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    body, receipt, calls = _pass(node, "The aim is a finished build before the weekend [1].",
                                 "What did " + "y" * 1200 + "ing say about the compiler at work?")
    assert receipt["reason"] != "model_error", receipt
    assert len(calls) == 1 and body == NO_ANSWER and receipt["reason"] == "all_sentences_dropped"


def test_the_fold_settles_on_a_long_run_of_y_and_reads_it_as_before():
    for word in ("y" * 1200 + "ing", "y" * 3000 + "ed", "y" * 2001 + "e"):
        assert _stem(_stem(word)) == _stem(word)
    # The same reading as the recursive rule (compared on 258,052 words: the dictionary and every run of "y" up to 39
    # letters with each suffix): "y" after a consonant is a vowel, and a run alternates from there.
    assert [_stem(word) for word in ("stories", "playing", "buyers", "yyyying", "toyyed")] == \
           ["story", "play", "buyer", "yyyy", "toyy"]
