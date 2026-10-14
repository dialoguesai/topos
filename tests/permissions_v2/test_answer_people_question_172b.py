"""Lane 172b: "Who has <owner> been talking to most this week?" lost every sentence to `dropped_relevance`.

Live, 14 Oct 2026 (candidate f3f0b5ae, clean rerun): the writer generated four sentences from five items and the
relevance count took all four, while the other questions that name the owner the same way were answered. The only
branch of `_cites_question_subject` that drops every sentence whatever the items cite, with the topic-word rule
(ANSWER_SUBJECT_WORDS) and carriage off, is the name rule, and only this question opens with "Who". The owner's node
carries plain English words as person words (AQ158 found "to", "the", "this", "on", "is", "and"; descriptive entity
names such as "the guy who fixed the sink" carry "who"). Where a clause starts, "Who" was capitalised by its place,
bound as a known person's name (round 4, U1: a known person beats the opener list), and no item carried it.

The fix reads a word that is both a common opener and a closed function word, where it opens a clause, as V0 reads it
in lower case: nobody, while ANSWER_SHORT_WORD_NAMES is off. Everything else stands: another name binds in every
cited item (round 4, N1), the namesake guard (amendment 10) still drops a sentence that mixes items naming the
owner's shared word with items that do not, and a function word written as a name away from a clause start binds.
Invented names and messages only.
"""
from __future__ import annotations

import sqlite3

import pytest

from topos.permissions_v2 import switches
from topos.permissions_v2.answer_generation import (_clause_openers, _cites_question_subject, _name_terms,
    account_words_as_asked, build_prompt, owner_party_words, people_words, post_check_answer)
from topos.permissions_v2.knowledge_contract import JournalEntryResult, MessageResult, RelationshipResult

P = switches.PREFIX
MODES = ("only", "with_sources")
ACCOUNT = frozenset({"ivo", "keller"})
QUESTION = "Who has Ivo been talking to most this week?"


class Open:
    def mentions_protected(self, *texts):
        return False


def _record(cls, letter, text, source, **extra):
    rid = "r." + letter * 64
    return cls.parse({"record_id": rid, "content": text, "source_ids": [source],
                      "citations": [{"record_id": rid, "source_id": source, "content": text}], **extra})


# What retrieval hands the writer for a people question: the owner's messages, a relationship, a journal entry.
DINNER = _record(MessageResult, "a", "Are we still on for dinner with Mara on Thursday?", "imessage_import",
                 kind="message")
LATE = _record(MessageResult, "b", "Running ten minutes late, save me a seat by the window", "imessage_import",
               kind="message")
MOVE = _record(MessageResult, "c", "Tomas, thanks again for the help with the move yesterday", "imessage_import",
               kind="message")
VISIT = _record(RelationshipResult, "d", "Owner intends to visit Mara", "imessage_import", kind="relationship",
                subject="Owner", relation="intends_to", object="visit Mara")
CALL = _record(JournalEntryResult, "e", "Long call with Mara tonight about her new job.", "journal_import",
               kind="journal_entry")
ITEMS = [DINNER, LATE, MOVE, VISIT, CALL]
ANSWER = ("Ivo has been in touch most with Mara this week, planning a Thursday dinner [1, 5].\n"
          "He also thanked Tomas for helping with a move [3].\n"
          "He intends to visit Mara [4].\n"
          "He told someone he was running late [2].")
# A message that names another Ivo: the namesake the account words cannot tell from the owner (R-157, finding 1).
NAMESAKE = _record(MessageResult, "f", "Ivo Brandt from the climbing gym is moving to the coast.", "imessage_import",
                   kind="message")


@pytest.fixture(autouse=True)
def attested(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})
    for name in ("ANSWER_SHORT_WORD_NAMES", "ANSWER_SUBJECT_WORDS"):
        monkeypatch.delenv(P + name, raising=False)


def _node(*descriptive):
    """The live shape: the self entity lacks the first name, a duplicate carries it, and other person entities carry
    plain English words (an extractor's descriptive names)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, aliases_json TEXT,
            is_self INTEGER, contact_id TEXT);
        CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT, known_usernames_json TEXT,
            is_self INTEGER);
    """)
    conn.execute("INSERT INTO entities VALUES ('ent-self','person','Keller Studio Notes','[]',1,NULL)")
    conn.execute("INSERT INTO entities VALUES ('ent-dup','person','Ivo','[]',0,NULL)")
    conn.execute("INSERT INTO entities VALUES ('ent-mara','person','Mara Voss','[]',0,NULL)")
    conn.execute("INSERT INTO entities VALUES ('ent-tomas','person','Tomas Reed','[]',0,NULL)")
    for number, name in enumerate(descriptive):
        conn.execute("INSERT INTO entities VALUES (?,?,?,?,0,NULL)", (f"ent-junk-{number}", "person", name, "[]"))
    return conn


def _prompt(conn, question, records):
    """As the service builds it (`AnswerService._compute`)."""
    account = account_words_as_asked(question, ACCOUNT)
    words = owner_party_words(conn, Open(), account_words=account) if account else owner_party_words(conn, Open())
    people = people_words(conn)
    shared = account & words & people
    return build_prompt(question, list(records), precision="none", owner_words=words, people=people,
                        **({"shared_owner_words": shared} if shared else {}))


def _checked(conn, question, records, text, mode):
    prompt = _prompt(conn, question, records)
    return prompt, post_check_answer(text, list(records), prompt, mode=mode, boundary=Open())


DESCRIPTIVE = "the guy who fixed the sink"


# --- the live case ---------------------------------------------------------------------------------------------------

def test_on_a_clean_node_the_people_question_keeps_every_sentence():
    for mode in MODES:
        prompt, checked = _checked(_node(), QUESTION, ITEMS, ANSWER, mode)
        assert prompt.name_terms == frozenset()
        assert (checked.body.outcome, checked.kept, checked.dropped_relevance) == ("answered", 4, 0), mode


def test_under_1_5_6s_binding_a_person_word_who_drops_every_sentence_of_the_people_question(monkeypatch):
    """The mechanism of the live row, reachable by the switch: "Who" opens the question, a person on the node
    carries "who", the name rule binds it, and no cited item carries it."""
    monkeypatch.setenv(P + "ANSWER_SHORT_WORD_NAMES", "true")
    conn = _node(DESCRIPTIVE)
    assert "who" in people_words(conn)
    for mode in MODES:
        prompt, checked = _checked(conn, QUESTION, ITEMS, ANSWER, mode)
        assert prompt.name_terms == frozenset({"who"})
        assert (checked.body.outcome, checked.reason, checked.dropped_relevance) == (
            "no_answer", "all_sentences_dropped", 4), mode


def test_by_default_a_clause_opening_who_names_nobody_and_the_people_question_is_answered():
    conn = _node(DESCRIPTIVE)
    for mode in MODES:
        prompt, checked = _checked(conn, QUESTION, ITEMS, ANSWER, mode)
        assert prompt.name_terms == frozenset() and "who" in prompt.openers
        assert (checked.body.outcome, checked.kept, checked.dropped_relevance) == ("answered", 4, 0), mode


@pytest.mark.parametrize("question", [
    "Who has Ivo been talking to most this week?",
    "What has Ivo been talking about this week?",
    "Is Ivo busy this week?",
    "The people Ivo talks to most: who are they?",
    "This week, who did Ivo message?",
    "Ivo: who has he been talking to?",
])
def test_a_function_word_opening_a_clause_is_never_a_name_by_default(question):
    people = frozenset({"who", "what", "is", "the", "this"})
    assert _name_terms(question, ACCOUNT, people) == frozenset(), question


# --- what still binds ------------------------------------------------------------------------------------------------

def test_a_function_word_written_as_a_name_away_from_a_clause_start_still_binds():
    people = frozenset({"who"})
    assert _name_terms("What did Ivo tell Who?", ACCOUNT, people) == {"who"}
    for mode in MODES:
        _prompt_, checked = _checked(_node(DESCRIPTIVE), "What did Ivo tell Who?", [DINNER],
                                     "He asked about dinner with Mara [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode


def test_a_known_persons_word_that_is_no_function_word_still_beats_the_opener_list():
    """Round 4, U1, unchanged: "Will" and "May" are given names and no function words."""
    assert _name_terms("Will, who has Ivo been talking to?", ACCOUNT, frozenset({"will"})) == {"will"}
    assert _name_terms("May: who has Ivo been talking to?", ACCOUNT, frozenset({"may"})) == {"may"}
    assert "will" not in _clause_openers("Will, who has Ivo been talking to?", frozenset({"will"}))


def test_another_persons_name_in_the_people_question_still_binds_in_every_cited_item():
    """Round 4, N1: a sentence about Mara may cite only items that name her; an item about something else is still
    dropped, with "Who" opening the question and a person carrying "who"."""
    conn = _node(DESCRIPTIVE)
    question = "Who has Ivo been talking to about Mara this week?"
    for mode in MODES:
        prompt, checked = _checked(conn, question, ITEMS, "Ivo talked with Mara about dinner and a move [1, 3].", mode)
        assert prompt.name_terms == frozenset({"mara"})
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        _prompt_, checked = _checked(conn, question, ITEMS, "Ivo and Mara planned a Thursday dinner [1, 5].", mode)
        assert checked.body.outcome == "answered" and checked.kept == 1, mode


def test_a_sentence_citing_an_item_about_another_subject_still_drops_under_the_topic_word_rule(monkeypatch):
    """1.5.6's topic-word rule, by its switch: the change touches the name rule only."""
    monkeypatch.setenv(P + "ANSWER_SUBJECT_WORDS", "true")
    conn = _node(DESCRIPTIVE)
    for mode in MODES:
        _prompt_, checked = _checked(conn, "Who has Ivo been climbing with this week?", [LATE],
                                     "He was running late [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode


def test_the_namesake_guard_still_drops_a_sentence_mixing_the_shared_word_with_the_owners_own_items():
    """Amendment 10, review R-157 finding 1: "ivo" is the owner's by the account and another person's on this node.
    An item that names Ivo never lends its facts to the owner's own items in one sentence, nor theirs to it."""
    conn = _node(DESCRIPTIVE)
    records = [*ITEMS, NAMESAKE]
    for mode in MODES:
        prompt, checked = _checked(conn, QUESTION, records, "Ivo talked with Mara and is moving to the coast [1, 6].",
                                   mode)
        assert prompt.shared_owner_words == frozenset({"ivo"}) and prompt.name_terms == frozenset()
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        assert not _cites_question_subject("Ivo talked with Mara and is moving to the coast [1, 6].", prompt)
        _prompt_, checked = _checked(conn, QUESTION, records, "Ivo Brandt is moving to the coast [6].", mode)
        assert checked.body.outcome == "answered", mode
