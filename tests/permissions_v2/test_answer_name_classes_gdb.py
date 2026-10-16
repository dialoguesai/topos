"""Lane GD-B (15 Oct 2026, grantee density): capitalised words of closed non-person classes name nobody.

On the synthetic grantee fixture the name rule read "October", "Monday", "AI", "Q4" and a function word after a
comma as people's names, and then required each in every item a sentence cited: supported sentences dropped as
off-subject (`dropped_relevance`). With ANSWER_NAME_CLASSES on (the default) those classes do not bind. A known
person's word still binds whatever its class, an unknown capitalised name binds as before (BL-146 rounds 2-5), and
Off-limits is untouched. Off reads every word as 1.5.9 did. Invented names only.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_answer_owner_names_r3 import Open, _boundary_protecting, _message
from topos.permissions_v2.answer_generation import _name_terms, build_prompt, post_check_answer

MODES = ("only", "with_sources")
SWITCH = "TOPOS_PERMISSIONS_V2_ANSWER_NAME_CLASSES"
OWNER = frozenset({"clark"})

FAIR_1 = _message("a", "Signed us up for a stall at the October volunteer fair and drafted the sign-up sheet.")
FAIR_2 = _message("b", "Booked two tables and a banner for the fair stall, and asked the bakery about donations.")
FAIR_3 = _message("c", "Made the volunteer rota for the fair stall: four shifts of two people each.")
SEED = _message("d", "Catalogued forty seed packets for the seed library and fixed the inventory app import bug.")


def _checked(question, records, answer, mode, *, owner_words=OWNER, boundary=None, people=frozenset()):
    prompt = build_prompt(question, records, precision="none", owner_words=owner_words, people=people)
    return post_check_answer(answer, records, prompt, mode=mode, boundary=boundary or Open())


@pytest.fixture
def classes_off(monkeypatch):
    monkeypatch.setenv(SWITCH, "false")


# --- what no longer binds ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What is the owner organising for the October volunteer fair?",
    "What did the owner work on last Monday?",
    "What has the owner said about the AI chats project?",
    "What are the owner's Q4 plans for the archive?",
    "What did the owner do in June?",
    "What happened on May 3 at the fair?",
    "Besides the archive, What else has the owner worked on?",
    "Since the spring, As planned, what has the owner finished?",
])
def test_closed_classes_bind_nothing(question):
    assert _name_terms(question, OWNER) == frozenset(), question


def test_gd11_a_month_word_no_longer_drops_supported_sentences():
    """GD-11: only one of three fair items says "October"; sentences citing the other two are kept."""
    question = "What is the owner organising for the October volunteer fair?"
    answer = ("The owner signed up for a stall at the fair [1]. Two tables and a banner were booked [2]. "
              "A rota of four shifts was made [3].")
    for mode in MODES:
        checked = _checked(question, [FAIR_1, FAIR_2, FAIR_3], answer, mode)
        assert (checked.body.outcome, checked.kept, checked.dropped_relevance) == ("answered", 3, 0), mode


def test_gd11_off_restores_1_5_9(classes_off):
    question = "What is the owner organising for the October volunteer fair?"
    assert _name_terms(question, OWNER) == {"october"}
    answer = ("The owner signed up for a stall at the fair [1]. Two tables and a banner were booked [2]. "
              "A rota of four shifts was made [3].")
    for mode in MODES:
        checked = _checked(question, [FAIR_1, FAIR_2, FAIR_3], answer, mode)
        assert (checked.kept, checked.dropped_relevance) == (1, 2), mode


# --- what still binds -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,name", [
    ("What has Ivo been working on lately?", "ivo"),
    ("Tell me, Ivo, what has been going on?", "ivo"),        # a name after a comma is no function word
    ("Tell me, Will, what has been going on?", "will"),      # "will" is a starter, not a function word
    ("What has June been doing?", "june"),                    # a given-name month with no time word before it
    ("Did May and the owner meet?", "may"),
    ("What did the owner plan with August?", "august"),
    ("What has IVO been working on?", "ivo"),                 # three or more capitals: a name or product binds
    ("What is TOPOS and what is its purpose?", "topos"),      # AR152's must-abstain case rests on it
])
def test_unknown_names_still_bind(question, name):
    assert name in _name_terms(question, OWNER), question


def test_an_unknown_name_still_refuses():
    for mode in MODES:
        checked = _checked("What has Ivo been working on lately?", [SEED], "Forty seed packets were catalogued [1].", mode)
        assert (checked.body.outcome, checked.dropped_relevance) == ("no_answer", 1), mode


@pytest.mark.parametrize("question,word", [
    ("What did the owner plan for October?", "october"),
    ("What has the owner done with AI lately?", "ai"),
    ("What did the owner do in June?", "june"),
    ("Besides the archive, What else did the owner do?", "what"),
])
def test_a_known_persons_word_binds_whatever_its_class(question, word):
    assert word in _name_terms(question, OWNER, frozenset({word})), question


# --- unchanged: the owner's name and Off-limits -----------------------------------------------------------------------

def test_gd12_the_owners_name_is_the_owner_with_the_stamp_and_binds_without_it():
    question = "What has Clark been working on recently?"
    for mode in MODES:
        assert _checked(question, [SEED], "Forty seed packets were catalogued [1].", mode).body.outcome == "answered"
        refused = _checked(question, [SEED], "Forty seed packets were catalogued [1].", mode, owner_words=frozenset())
        assert (refused.body.outcome, refused.dropped_relevance) == ("no_answer", 1), mode


def test_gd14_an_off_limits_name_still_refuses():
    boundary = _boundary_protecting("Harlan Ostrow")
    question = "What has the owner arranged with Harlan Ostrow?"
    assert {"harlan", "ostrow"} <= _name_terms(question, OWNER)
    for mode in MODES:
        checked = _checked(question, [SEED], "Forty seed packets were catalogued [1].", mode, boundary=boundary)
        assert checked.body.outcome == "no_answer", mode
