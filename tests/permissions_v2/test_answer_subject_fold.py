"""The answer step's subject check folds a word the same way in the question and in the cited items.

A sentence of the owner's model is kept only when the items it cites carry the question's subject words (in any of
their forms). Before this, "plans" was never folded (a plural of five letters) while "plan" stayed "plan", and
"meetings" folded to "meeting" while "meeting" folded to "meet", so "What plans were shared?" against an item that
says "plan" dropped the only sentence and the recipient read "has no answer to that". "shared" names the act of
sharing in a question to a share, not a subject, so it is a question word like "permitted".

The second commit: the fold also meets "-ies" and "-ied" with "-y" ("stories", "story") and a word ending in "e" with
its "-s", "-ed" and "-ing" forms ("update", "updated"), and the anchor rule (a question word of 8 letters or more must
be in the items, or the answer step abstains, and a sentence that echoes it is dropped) compares through the same
fold: "meetings" is in an item that says "meeting". A word absent from every item in every form still abstains and
is still dropped as an echo. Invented data only.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_checks import question_only_anchors
from topos.permissions_v2.answer_generation import (_stem, build_prompt, post_check_answer,
    question_lacks_permitted_anchor)
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
                                   ("lunches", "lunch"), ("stopped", "stop"),
                                   # the second commit: "-ies" and "-ied" with "-y", and words ending in "e"
                                   ("stories", "story"), ("activities", "activity"), ("cities", "city"),
                                   ("movies", "movie"), ("copies", "copied", "copy"), ("tries", "tried", "try"),
                                   ("update", "updates", "updated", "updating"), ("house", "houses", "housed", "housing"),
                                   ("note", "notes", "noted", "noting"), ("share", "shares", "shared", "sharing"),
                                   ("release", "released", "releases"), ("change", "changes", "changed"),
                                   ("agree", "agreed"), ("issue", "issued"), ("aches", "ache"), ("maps", "map")])
def test_the_forms_of_one_word_fold_alike(words):
    assert len({_stem(word) for word in words}) == 1, {word: _stem(word) for word in words}


@pytest.mark.parametrize("word,folded", [("access", "access"), ("class", "class"), ("glass", "glass"),
                                         ("called", "call"), ("missed", "miss"), ("staffed", "staff"),
                                         ("bus", "bus"), ("yes", "yes"), ("its", "its"), ("updated", "updat"),
                                         ("meeting", "meet"), ("plans", "plan"), ("noted", "note"), ("string", "string"),
                                         ("being", "being"), ("shred", "shred"), ("speed", "speed"), ("note", "note"),
                                         ("news", "news"), ("does", "does"), ("boss", "boss")])
def test_the_fold_keeps_ss_and_double_l_s_f_and_never_makes_a_short_word(word, folded):
    assert _stem(word) == folded


@pytest.mark.parametrize("one,other", [("note", "not"), ("noted", "not"), ("news", "new"), ("does", "doe"),
                                       ("this", "thi"), ("plane", "plan"), ("planed", "plan"), ("quite", "quit"),
                                       ("state", "stat"), ("stated", "stats"), ("write", "writ"), ("stare", "star"),
                                       ("hated", "hat"), ("cared", "car"), ("tense", "ten"), ("dense", "den"),
                                       ("please", "plea"), ("lapse", "lap"), ("striped", "strips")])
def test_the_fold_keeps_a_word_apart_from_a_different_common_word(one, other):
    assert _stem(one) != _stem(other), (_stem(one), _stem(other))


def test_the_fold_is_idempotent():
    words = ("plans plan goals trips meetings meeting meet notes classes class access running run updated boxes "
             "planned planning stopped shared sharing shares share hobbies bakeries status menus herring evenings "
             "addresses glasses wishes matches buses yes its axes stories story activities movies movie copies tried "
             "update updates updating house houses housed housing note noted noting release released tense dense "
             "please pleased agree agreed issue issued focus focused quite plane state stated maps news does").split()
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


# --- The anchor rule (second commit): an anchor is in the items when its fold is ---------------------------------

LAKE = "A lakeside cabin was reserved for the trip."


@pytest.mark.parametrize("question,item", [
    ("What meetings are planned?", "The design review meeting is on Thursday."),
    ("Which projects were shared?", "The garden project starts in May."),
    ("What happened to the compiler?", "Both compilers were rebuilt on Friday."),
    ("What did the newsletters say?", "The newsletter went out on Monday."),
])
def test_an_anchor_in_another_form_is_in_the_items_both_ways(question, item):
    prompt = build_prompt(question, [_record("a", item)], precision="none")
    assert not question_lacks_permitted_anchor(prompt)
    assert question_only_anchors(question, prompt.raw_texts) == frozenset()


def test_a_sentence_using_the_questions_form_of_a_word_the_items_carry_is_no_echo():
    _kept("Which projects were shared?", "The garden project starts in May.",
          "The projects include a garden one from spring [1].")
    _kept("What happened to the compiler?", "Both compilers were rebuilt on Friday.",
          "The compiler builds were redone before the weekend [1].")


@pytest.mark.parametrize("question", ["What does shadowglass mean?", "What do the shadowglasses mean?",
                                      "Who were the shadowglassers?"])
def test_a_word_absent_from_the_items_in_every_form_still_abstains(question):
    prompt = build_prompt(question, [_record("a", LAKE)], precision="none")
    assert question_lacks_permitted_anchor(prompt)


def test_a_word_absent_in_every_form_is_dropped_as_an_echo_in_any_form():
    records = [_record("a", LAKE)]
    prompt = build_prompt("Did the shadowglasses affect the lakeside cabin?", records, precision="none")
    assert not question_lacks_permitted_anchor(prompt)
    for mode in MODES:
        for sentence in ("Shadowglass affected the cabin [1].", "The shadowglasses changed the cabin [1]."):
            checked = post_check_answer(sentence, records, prompt, mode=mode, boundary=Boundary())
            assert (checked.body.outcome, checked.dropped_question_echo, checked.dropped_relevance) == \
                   ("no_answer", 1, 0), (mode, sentence)


@pytest.mark.parametrize("question", ["What did Secretperson book?", "What did the Secretpersons book?",
                                      "Which cabin is Secretperson's?"])
def test_a_protected_name_that_never_reaches_the_items_still_abstains_in_any_form(question):
    records = [_record("a", LAKE)]
    assert question_lacks_permitted_anchor(build_prompt(question, records, precision="none"))

    class Protected:
        def mentions_protected(self, text):
            return "secretperson" in text.lower()

    mixed = build_prompt(question.rstrip("?") + " at the lakeside cabin?", records, precision="none")
    for mode in MODES:
        checked = post_check_answer("Secretperson booked the cabin [1].", records, mixed, mode=mode, boundary=Protected())
        assert checked.body.model_dump() == {"version": "topos-answer/v1", "outcome": "no_answer"}


def _pass(node, answer, question):
    calls = []

    async def generate(_prompt, *, deadline):
        calls.append(deadline)
        return answer

    service = _service(node, "unused")
    service.generate = generate
    try:
        answer_id = _ask(node, service, question)
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        return body, receipt, calls
    finally:
        service.close()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_does_not_abstain_when_an_anchor_is_in_the_item_in_another_form(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    body, receipt, calls = _pass(node, "The aim is a finished build before the weekend [1].",
                                 "What goals at work were shared about the compilers?")
    assert len(calls) == 1 and body["outcome"] == "answered", receipt
    assert receipt["reason"] == "answered"


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_abstains_on_a_word_absent_in_every_form(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    body, receipt, calls = _pass(node, "The aim is a finished build before the weekend [1].",
                                 "What did the Zentravolks say at work?")
    assert calls == [] and body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "question_not_supported"


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_drops_an_echo_of_an_absent_word_in_another_form(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    body, receipt, calls = _pass(node, "Zentravolk finished the compiler work [1].",
                                 "What did the Zentravolks say about the compiler at work?")
    assert len(calls) == 1 and body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "all_sentences_dropped"
    assert receipt["sentences"]["dropped_question_echo"] == 1 and receipt["sentences"]["dropped_relevance"] == 0


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_refuses_a_protected_name_in_any_form_before_the_model(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    original = node.search.resolver.entity_boundary

    class Protected:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, text):
            return "secretperson" in text.lower() or self.inner.mentions_protected(text)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(node.search.resolver, "entity_boundary", lambda conn: Protected(original(conn)))
    body, receipt, calls = _pass(node, "Secretperson finished the compiler [1].",
                                 "What did the Secretpersons say about the compiler at work?")
    assert calls == [] and body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "question_protected"
