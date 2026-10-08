"""BL-146: an ordinary question to an answers share is answered when the items it cites carry its subject.

The live scorecard of 8 Oct (answers mode) answered 2 of 24 asks, Rig D 2 of 21 (invented data). Every question of
the recipients' catalog says "the owner", and many add "Cite the supporting evidence", "mentioned", "Cite the shared
messages" or "lately". No item says "owner" (the owner writes in the first person), so the subject check dropped every
sentence, and the anchor rule abstained on "messages". Those words name whose share it is, the share's own record
forms, how to answer or when: they are request words now (A2A-4 amendment 4), read after the fold like the others.

Nothing else moves. A subject word still binds: a sentence whose cited items lack it is dropped. An invented word
still abstains and is still dropped as an echo. The item's kind is on its prompt line, so a journal question is
answered from a journal entry and never from a message; no source or record identifier enters the prompt.
Invented data only.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_checks import FORM_WORDS, TEMPLATE_VERSION, question_only_anchors
from topos.permissions_v2.answer_generation import (_stem, _topic_terms, build_prompt, post_check_answer,
    question_lacks_permitted_anchor)
from topos.permissions_v2.knowledge_contract import JournalEntryResult, MessageResult

MODES = ("only", "with_sources")


class Boundary:
    def mentions_protected(self, text):
        return False


def _journal(letter, text):
    rid = "r." + letter * 64
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def _checked(question, record, sentence, mode):
    prompt = build_prompt(question, [record], precision="none")
    return prompt, post_check_answer(sentence, [record], prompt, mode=mode, boundary=Boundary())


# The catalog's shapes (app `features/home/granteeEvalCatalog.ts`), each with an invented item that carries its subject.
ANSWERED = [
    ("What work and projects is the owner working on? Cite the supporting evidence.",
     _message("a", "Spent the morning working on the lantern project for the studio."),
     "They are building the lantern project [1]."),
    ("What has the owner been up to lately?",
     _message("b", "Booked a cottage by the river for the spring."),
     "A spring cottage stay is booked [1]."),
    ("What has the owner mentioned about the bakery? Cite the shared messages.",
     _message("c", "The corner bakery closed for good last week."),
     "Their usual bakery has shut down [1]."),
    ("What has the owner discussed in their chats about bouldering? Cite the shared records.",
     _message("d", "Bouldering at the quarry wall felt easy today."),
     "Bouldering went smoothly for them [1]."),
    ("What has the owner written in their journal about climbing? Cite the entries.",
     _journal("e", "Climbing at the quarry wall felt easy today."),
     "Climbing felt easy for them today [1]."),
    ("Has the owner described any recent plans for the garden?",
     _journal("f", "The garden needs new beds before the frost."),
     "New garden beds are due before the frost [1]."),
]


@pytest.mark.parametrize("question,record,sentence", ANSWERED)
def test_a_catalog_question_is_answered_from_an_item_that_carries_its_subject(question, record, sentence):
    for mode in MODES:
        prompt, checked = _checked(question, record, sentence, mode)
        assert not question_lacks_permitted_anchor(prompt), question
        assert (checked.body.outcome, checked.reason, checked.kept) == ("answered", "answered", 1), (mode, question)
        assert checked.dropped_relevance == checked.dropped_question_echo == checked.dropped_copy == 0


def test_the_request_words_leave_the_subject_and_only_the_subject():
    assert _topic_terms("What work and projects is the owner working on? Cite the supporting evidence.") == \
        {"project", "work"}
    assert _topic_terms("What has the owner been up to lately?") == set()
    assert _topic_terms("What has the owner mentioned about trips recently? Cite the shared messages.") == {"trip"}
    prompt = build_prompt("Where is the owner staying? Only report what the messages state.",
                          [_message("a", "Staying at the harbour flat this month.")], precision="none")
    assert question_only_anchors(prompt.question, prompt.raw_texts) == frozenset()


# Each of these was dropped before BL-146 and is still dropped: the subject is absent from the cited item.
DROPPED = [
    ("What has the owner said about trips lately? Cite the supporting evidence.",
     _message("a", "Dinner at the harbour cafe on Friday."), "They had dinner at a harbour cafe [1]."),
    ("What has the owner written in their journal about climbing? Cite the entries.",
     _message("b", "Climbing at the quarry wall felt easy today."), "Climbing felt easy for them today [1]."),
    ("What has the owner mentioned about their career? Cite the shared messages.",
     _message("c", "Shipped the lantern build to the studio today."), "The lantern build went out [1]."),
]


@pytest.mark.parametrize("question,record,sentence", DROPPED)
def test_a_subject_word_still_binds_beside_the_request_words(question, record, sentence):
    for mode in MODES:
        _prompt, checked = _checked(question, record, sentence, mode)
        assert (checked.body.outcome, checked.reason, checked.kept, checked.dropped_relevance) == \
               ("no_answer", "all_sentences_dropped", 0, 1), (mode, question)


def test_an_invented_word_still_abstains_and_is_still_an_echo():
    record = _message("a", "The harbourside bakery closed for good last week.")
    prompt = build_prompt("What has the owner mentioned about shadowglass? Cite the shared messages.", [record],
                          precision="none")
    assert question_lacks_permitted_anchor(prompt)
    for mode in MODES:
        prompt, checked = _checked("Did the owner mention shadowglass at the harbourside bakery? Cite the supporting "
                                   "evidence.", record, "Shadowglass shut the harbourside bakery [1].", mode)
        assert not question_lacks_permitted_anchor(prompt)
        assert (checked.body.outcome, checked.dropped_question_echo) == ("no_answer", 1), mode


def test_the_kind_is_evidence_and_no_identifier_enters_the_prompt():
    record = _journal("a", "Climbing at the quarry wall felt easy today.")
    prompt = build_prompt("What did the owner write in their journal?", [record], precision="none")
    assert "journal_entry" in prompt.raw_texts and "journal_entry" in prompt.record_texts[0]
    for text in (prompt.user, *prompt.raw_texts, *prompt.record_texts):
        assert "ownerimport" not in text and record.record_id not in text
    assert TEMPLATE_VERSION == "topos-answer-template/v5"


def test_the_request_words_are_pinned_and_name_no_subject():
    # A word added here changes what every share's answers may say; it is a contract amendment, never a fix to pass.
    assert FORM_WORDS == frozenset({"owner", "owners", "herself", "himself", "theirs", "themself", "themselves",
        "message", "messages", "chat", "chats", "record", "records",
        "entry", "entries", "cite", "evidence", "support", "supporting", "supported", "describe", "described",
        "mention", "mentioned", "discuss", "discussed", "written", "lately", "recent", "recently"})
    folds = {_stem(word) for word in FORM_WORDS}
    for subject in ("trips", "glass", "olympics", "holidays", "relationships", "projects", "career", "statements",
                    "health", "finance", "family", "goals", "journal", "interests", "learning"):
        assert _stem(subject) not in folds, subject


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_answers_a_catalog_shaped_question(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The aim is a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals has the owner shared about the compiler? Cite the supporting "
                                        "evidence.")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body["outcome"] == "answered", receipt
        assert receipt["reason"] == "answered" and receipt["sentences"]["dropped_relevance"] == 0
        assert receipt["template_version"] == "topos-answer-template/v5"
    finally:
        service.close()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_still_drops_a_catalog_question_whose_item_lacks_the_subject(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The aim is a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What trips has the owner shared about the compiler? Cite the supporting "
                                        "evidence.")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
        assert receipt["reason"] == "all_sentences_dropped" and receipt["sentences"]["dropped_relevance"] == 1
    finally:
        service.close()
