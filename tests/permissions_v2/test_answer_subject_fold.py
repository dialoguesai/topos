"""The answer step's subject check folds a word the same way in the question and in the cited items.

A sentence of the owner's model is kept only when the items it cites carry the question's subject words (in any of
their forms). Before this, "plans" was never folded (a plural of five letters) while "plan" stayed "plan", and
"meetings" folded to "meeting" while "meeting" folded to "meet", so "What plans were shared?" against an item that
says "plan" dropped the only sentence and the recipient read "has no answer to that". "shared" names the act of
sharing in a question to a share, not a subject, so it is a question word like "permitted". Invented data only.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_generation import _stem, build_prompt, post_check_answer
from topos.permissions_v2.knowledge_contract import JournalEntryResult

MODES = ("only", "with_sources")


class Boundary:
    def mentions_protected(self, text):
        return False


def _record(letter, text):
    rid = "r." + letter * 64
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def _check(question, item, sentence, mode):
    records = [_record("a", item)]
    prompt = build_prompt(question, records, precision="none")
    return post_check_answer(sentence, records, prompt, mode=mode, boundary=Boundary())


def _kept(question, item, sentence):
    for mode in MODES:
        checked = _check(question, item, sentence, mode)
        assert (checked.body.outcome, checked.reason, checked.dropped_relevance, checked.kept) == \
               ("answered", "answered", 0, 1), (mode, question, item)


def _dropped(question, item, sentence):
    for mode in MODES:
        checked = _check(question, item, sentence, mode)
        assert (checked.body.outcome, checked.reason, checked.dropped_relevance, checked.kept) == \
               ("no_answer", "all_sentences_dropped", 1, 0), (mode, question, item)
        assert checked.dropped_copy == checked.dropped_question_echo == checked.dropped_scrub == 0


@pytest.mark.parametrize("words", [("plans", "plan"), ("goals", "goal"), ("trips", "trip"), ("notes", "note"),
                                   ("meetings", "meeting", "meet"), ("classes", "class"), ("running", "run"),
                                   ("boxes", "box"), ("planned", "planning", "plans", "plan"), ("routes", "route"),
                                   ("lunches", "lunch"), ("stopped", "stop")])
def test_the_forms_of_one_word_fold_alike(words):
    assert len({_stem(word) for word in words}) == 1, {word: _stem(word) for word in words}


@pytest.mark.parametrize("word,folded", [("access", "access"), ("class", "class"), ("glass", "glass"),
                                         ("called", "call"), ("missed", "miss"), ("staffed", "staff"),
                                         ("bus", "bus"), ("yes", "yes"), ("its", "its"), ("updated", "updat"),
                                         ("meeting", "meet"), ("plans", "plan"), ("noted", "noted"), ("string", "string")])
def test_the_fold_keeps_ss_and_double_l_s_f_and_never_makes_a_short_word(word, folded):
    assert _stem(word) == folded


def test_the_fold_is_idempotent():
    words = ("plans plan goals trips meetings meeting meet notes classes class access running run updated boxes "
             "planned planning stopped shared sharing shares share hobbies bakeries status menus herring evenings "
             "addresses glasses wishes matches buses yes its axes").split()
    for word in words:
        assert _stem(_stem(word)) == _stem(word), word
        assert len(_stem(word)) >= 3 or _stem(word) == word, word


def test_a_plural_question_keeps_a_sentence_citing_an_item_with_the_singular():
    _kept("What goals were set?", "Her goal is to run a half marathon in October.",
          "She aims at a half marathon this autumn [1].")
    _kept("What meetings are planned?", "The design review meeting is on Thursday.",
          "A review is set for Thursday [1].")
    _kept("Which trips came up?", "They booked a trip to the coast for May.",
          "A coastal visit is set for spring [1].")


def test_a_singular_question_keeps_a_sentence_citing_an_item_with_the_plural():
    _kept("What happened at the meeting?", "Both meetings moved to the small room on Friday.",
          "They were moved to a smaller space [1].")
    _kept("Which route did they take?", "Two routes were compared on the hill map before the walk.",
          "They weighed two options for the hike [1].")


def test_ing_forms_keep_a_sentence_both_ways():
    _kept("How did the planning go?", "The plan is to leave at dawn with the tent.",
          "They intend an early start with camping gear [1].")
    _kept("What plans were made?", "We are planning a picnic by the river.",
          "An outdoor meal near the water is coming [1].")
    _kept("How was the swimming?", "We swim at the lake on Sundays before lunch.",
          "Weekend mornings mean a lake dip [1].")


def test_a_sentence_citing_an_item_without_the_subject_in_any_form_is_still_dropped():
    _dropped("What trips were shared?", "Dinner at the harbour cafe on Friday at eight.",
             "A Friday evening meal is booked [1].")
    _dropped("What meetings were planned?", "The holiday in the hills starts on the fifth.",
             "A break away begins early next month [1].")
    # A subject of the question still has to be carried: "plan", whatever "shared" now counts for.
    _dropped("What plans were shared?", "The photos from the coast are in the family album now.",
             "Pictures went into an album [1].")
    _dropped("What classes did she take?", "The glass vase cracked in the kiln.",
             "A vase broke while firing [1].")


def test_the_rigs_question_keeps_a_sentence_citing_an_item_that_says_plan():
    question = "What plans were shared?"
    _kept(question, "Plan a quiet weekend by the lake with a long walk and a good bakery.",
          "A calm lakeside weekend with a stroll is intended [1].")
    _kept(question, "We planned the lake weekend together and shared the list.",
          "A lakeside weekend was arranged [1].")
    # Two cited items may carry the subject between them, as before.
    records = [_record("a", "The lake weekend is booked."), _record("b", "The plan has a long walk on Sunday.")]
    prompt = build_prompt(question, records, precision="none")
    checked = post_check_answer("A lakeside weekend with a walk is arranged [1, 2].", records, prompt,
                                mode="with_sources", boundary=Boundary())
    assert checked.body.outcome == "answered" and checked.dropped_relevance == 0


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_answers_a_plural_question_from_an_item_with_the_singular(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The aim is a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals were shared about the compiler?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body["outcome"] == "answered", receipt
        assert receipt["reason"] == "answered" and receipt["sentences"]["dropped_relevance"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_still_drops_a_sentence_whose_item_lacks_the_subject(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The aim is a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What trips were shared about the compiler?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
        assert receipt["reason"] == "all_sentences_dropped" and receipt["sentences"]["dropped_relevance"] == 1
    finally:
        service.close()
