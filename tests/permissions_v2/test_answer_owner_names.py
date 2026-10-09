"""BL-146 round 2: a question that names the owner is answered from the owner's first-person items.

Real recipients do not say "the owner": they ask "What has <name> been working on lately?" or "What is she
planning?". The owner's items are first person and never carry the owner's own name, so the subject check dropped
every sentence (and a name of eight letters or more made the anchor rule abstain). The owner's own confirmed names
and handles (the entity the owner attested as themselves, its contact card, the owner's own contact card) are read as
the owner; the pronouns that point at the owner are request words.

Nothing else moves. Another person's name still binds, and a word another person on the node also carries stays a
subject. A name nobody has still abstains. An Off-limits name still refuses the question and still drops a sentence.
An unconfirmed self row names nobody. Invented names only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2 import answer_release
from topos.permissions_v2.answer_generation import (_stem, build_prompt, owner_party_words, post_check_answer,
    question_lacks_permitted_anchor)
from topos.permissions_v2.knowledge_contract import JournalEntryResult, MessageResult

MODES = ("only", "with_sources")
OWNER = frozenset({"thessaly", "marrowind", "thess"})


class Boundary:
    def mentions_protected(self, text):
        return "quillon" in text.lower()


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def _journal(letter, text):
    rid = "r." + letter * 64
    return JournalEntryResult.parse({"kind": "journal_entry", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


def _checked(question, record, sentence, mode, owner_words=OWNER):
    prompt = build_prompt(question, [record], precision="none", owner_words=owner_words)
    return prompt, post_check_answer(sentence, [record], prompt, mode=mode, boundary=Boundary())


ITEM = _message("a", "Spent the morning working on the lantern project for the studio.")
ANSWERED = [
    ("What has Thessaly been working on lately?", ITEM, "Thessaly is building the lantern project [1]."),
    ("What projects is Thessaly Marrowind working on?", ITEM, "The lantern project has their attention [1]."),
    ("What has Thess shared about her projects?", ITEM, "Her lantern project is underway [1]."),
    ("What is she working on these days?", ITEM, "The lantern project is underway [1]."),
    ("What has Thessaly written about herself in the journal?", _journal("b", "Feeling steadier after the long winter."),
     "She feels steadier now that winter is over [1]."),
]


@pytest.mark.parametrize("question,record,sentence", ANSWERED)
def test_a_question_naming_the_owner_is_answered_from_first_person_items(question, record, sentence):
    for mode in MODES:
        prompt, checked = _checked(question, record, sentence, mode)
        assert not question_lacks_permitted_anchor(prompt), question
        assert (checked.body.outcome, checked.reason, checked.kept) == ("answered", "answered", 1), (mode, question)
        assert checked.dropped_relevance == checked.dropped_question_echo == checked.dropped_copy == 0
        # The model reads the question as asked; only the checks read the owner's name as the owner.
        assert question in prompt.user


def test_without_the_owners_names_the_same_questions_still_drop():
    _prompt, checked = _checked("What has Thessaly been working on lately?", ITEM,
                                "Thessaly is building the lantern project [1].", "only", owner_words=frozenset())
    assert checked.body.outcome == "no_answer"
    prompt = build_prompt("What has Thessaly been working on lately?", [ITEM], precision="none")
    assert question_lacks_permitted_anchor(prompt)


def test_another_persons_name_still_binds_and_a_name_nobody_has_still_abstains():
    for mode in MODES:
        # "Corrigan" is not the owner: the item lacks the subject, the anchor rule abstains, the echo still drops.
        prompt, checked = _checked("What has Corrigan been working on lately?", ITEM,
                                   "Corrigan is building the lantern project [1].", mode)
        assert question_lacks_permitted_anchor(prompt)
        assert checked.body.outcome == "no_answer" and checked.dropped_question_echo == 1
        # The owner and another person in one question: the other person still binds.
        prompt, checked = _checked("Did Thessaly and Corrigan talk about the lantern project?", ITEM,
                                   "Thessaly and Corrigan discussed the lantern project [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_question_echo == 1
        # A subject word beside the owner's name still binds.
        _prompt, checked = _checked("What has Thessaly said about trips?", ITEM,
                                    "Thessaly is building the lantern project [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1


def test_an_off_limits_name_still_drops_beside_the_owners_name():
    for mode in MODES:
        _prompt, checked = _checked("What has Thessaly said about the lantern project?", ITEM,
                                    "Thessaly showed Quillon the lantern project [1].", mode)
        assert (checked.body.outcome, checked.reason) == ("no_answer", "answer_protected")


def _node_people(*, attested=False, others=(), card=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, aliases_json TEXT,
            is_self INTEGER, contact_id TEXT);
        CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT, known_usernames_json TEXT,
            is_self INTEGER);
    """)
    conn.execute("INSERT INTO entities VALUES ('ent-self','person','Thessaly Marrowind',?,1,'c-self')",
                 (json.dumps(["Thess"]),))
    if card:
        conn.execute("INSERT INTO contacts VALUES ('c-me','Thessaly M.',?,1)",
                     (json.dumps(["@thess_m", "thessaly@example.invalid"]),))
    for index, name in enumerate(others):
        conn.execute("INSERT INTO entities VALUES (?, 'person', ?, '[]', 0, NULL)", (f"ent-{index}", name))
    return conn


def test_the_owners_words_come_only_from_the_attested_entity_and_its_linked_card(monkeypatch):
    from topos.permissions_v2 import identity
    # An unattested self row and an importer's self card (`contacts.is_self`) name nobody (round 3, M2).
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: set())
    assert owner_party_words(_node_people()) == frozenset()
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})
    assert owner_party_words(_node_people(card=False)) == {"thessaly", "marrowind", "thess"}
    # A word another person carries stays a subject.
    words = owner_party_words(_node_people(others=["Thessaly Brooke"]))
    assert "thessaly" not in words and "marrowind" in words
    # A store that cannot be read names nobody.
    broken = sqlite3.connect(":memory:")
    broken.close()
    assert owner_party_words(broken) == frozenset()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_reads_the_owners_name_as_the_owner(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "owner_party_words", lambda conn, boundary=None: OWNER)
    service = _service(node, "Thessaly aims for a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals has Thessaly shared about the compiler?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body["outcome"] == "answered", receipt
        assert receipt["reason"] == "answered" and receipt["sentences"]["dropped_relevance"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_still_drops_another_persons_name(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "owner_party_words", lambda conn, boundary=None: OWNER)
    service = _service(node, "Corrigan aims for a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals has Corrigan shared about the compiler?")
        body = _finished(node, service, answer_id)
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    finally:
        service.close()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_the_answer_pass_still_refuses_a_question_naming_an_off_limits_person(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "owner_party_words", lambda conn, boundary=None: OWNER)
    real = node.search.resolver.entity_boundary

    class Flagged:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, *texts):
            return any("quillon" in str(text).lower() for text in texts) or self.inner.mentions_protected(*texts)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(node.search.resolver, "entity_boundary", lambda conn: Flagged(real(conn)))
    service = _service(node, "Thessaly aims for a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals has Thessaly shared with Quillon about the compiler?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
        assert receipt["reason"] == "question_protected"
    finally:
        service.close()


def test_the_writer_is_told_which_of_the_askers_words_mean_the_owner_and_nothing_more():
    prompt = build_prompt("What has Thessaly been working on lately?", [ITEM], precision="none", owner_words=OWNER)
    assert "In this question, Thessaly is the owner, who wrote every item." in prompt.user
    # Only the asker's own words are said back: the owner's other confirmed names never enter the prompt.
    assert "Marrowind" not in prompt.user and "marrowind" not in prompt.user.casefold()
    assert "is the owner" not in build_prompt("What has Corrigan been working on lately?", [ITEM], precision="none",
                                              owner_words=OWNER).user
    assert "is the owner" not in build_prompt("What has Thessaly been working on lately?", [ITEM],
                                              precision="none").user


def test_a_short_name_of_another_person_binds_at_any_length():
    # Subject words are five letters or more, so "Ivo" was no subject at all: 1.5.0 answered "What is Ivo working
    # on?" from the owner's own items, naming Ivo. A capitalised word inside the question is a subject at any length.
    with_ivo = _message("c", "Ivo and I worked on the lantern project.")
    for mode in MODES:
        _prompt, checked = _checked("What is Ivo working on?", ITEM, "Ivo is building the lantern project [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        _prompt, checked = _checked("What is Ivo working on?", with_ivo,
                                    "They and Ivo built the lantern project together [1].", mode)
        assert checked.body.outcome == "answered" and checked.dropped_relevance == 0, mode


def test_a_name_term_is_a_capitalised_word_inside_a_sentence_and_never_the_owner_or_a_request_word():
    from topos.permissions_v2.answer_generation import _name_terms
    assert _name_terms("Did the owner meet Ivo in Lisbon?", OWNER) == {"ivo", "lisbon"}
    assert _name_terms("What has Thessaly said? Cite the shared Messages.", OWNER) == frozenset()
    assert _name_terms("Separate stated facts from inference. Only report what Thess wrote.", OWNER) == frozenset()
    assert _name_terms("what is ivo working on?", OWNER) == frozenset()   # lower case is not seen: only tightens
