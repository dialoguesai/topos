"""Lane GD-L (15 Oct 2026, grantee density round 3): the name rule in one cited item.

The name rule (`_cites_question_subject`) drops a sentence unless every name the question carries (`Prompt.name_terms`)
is written, capitalised, in EVERY item the sentence cites: an item that names a person never lends that person the
facts of the other items cited beside it. The owner's direction of 15 Oct: for grants, the looser method that gives
the most facts, never anything the grant does not permit. With ANSWER_NAME_ONE_ITEM on (off by default), a name that
is no known person's word on this node (`people_words`) and no word of the owner's (`owner_name_words`, the account's
words, the owner words) has to be in at least ONE cited item. A known person's name still binds in every cited item;
the shared owner-word rule (review R-157), the Off-limits refusal and body check, retrieval and the grant boundary are
unchanged. Off is 1.5.9's rule. Invented names and text only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_owner_account_words import ACCOUNT, NAMESAKE, _node
from tests.permissions_v2.test_answer_owner_account_words import ITEM as ACCOUNT_ITEM
from tests.permissions_v2.test_answer_owner_names_r3 import (Open, _boundary_protecting, _message, _people_db,
    attested)  # noqa: F401 -- `attested` is a fixture
from tests.permissions_v2.test_answer_release import _ask, _finished, _service
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from topos.permissions_v2 import answer_release, switches
from topos.permissions_v2.answer_generation import (account_words_as_asked, build_prompt, owner_name_words,
    owner_party_words, people_words, post_check_answer)

MODES = ("only", "with_sources")
P = "TOPOS_PERMISSIONS_V2_"
SWITCH = P + "ANSWER_NAME_ONE_ITEM"
OWNER = frozenset({"clark"})
PEOPLE = frozenset({"mara", "voss"})            # somebody the node knows, so the people set is not empty

QUESTION = "What has Pella been arranging for the lantern fair?"
PELLA = _message("a", "Pella and I mapped out the lantern fair stall and drafted the sign-up sheet.")
STALL = _message("b", "Booked two tables and a banner for the lantern fair stall.")
SEED = _message("c", "Catalogued forty seed packets for the seed library.")
RECORDS = [PELLA, STALL, SEED]
# Pella is written in item 1 only; item 2 is the same stall; neither item 2 nor item 3 names Pella.
BOTH = "Pella drew up a sign-up sheet, and two tables and a banner were booked for the stall [1, 2]."
ONE = "Pella drew up a sign-up sheet for the stall [1]."
NONE = "Two tables and a banner were booked, and forty seed packets were catalogued [2, 3]."


@pytest.fixture(autouse=True)
def clean_switch(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


@pytest.fixture
def one_item(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


def _prompt(question, records, *, owner_words=OWNER, people=PEOPLE, owner_names=frozenset(), shared=frozenset()):
    return build_prompt(question, list(records), precision="none", owner_words=owner_words, people=people,
                        owner_names=owner_names, **({"shared_owner_words": shared} if shared else {}))


def _checked(question, records, answer, mode, *, boundary=None, **kwargs):
    prompt = _prompt(question, records, **kwargs)
    return prompt, post_check_answer(answer, list(records), prompt, mode=mode, boundary=boundary or Open())


def _outcome(checked):
    return checked.body.outcome, checked.kept, checked.dropped_relevance


# --- off: 1.5.9 ---------------------------------------------------------------------------------------------------------

def test_the_switch_is_off_by_default_and_the_prompt_carries_nothing():
    assert switches.on(switches.ANSWER_NAME_ONE_ITEM) is False
    prompt = _prompt(QUESTION, RECORDS)
    assert prompt.name_terms == {"pella"} and prompt.one_item_names == frozenset()


@pytest.mark.parametrize("mode", MODES)
def test_off_an_unknown_name_must_be_in_every_cited_item(mode):
    assert _outcome(_checked(QUESTION, RECORDS, BOTH, mode)[1]) == ("no_answer", 0, 1)
    assert _outcome(_checked(QUESTION, RECORDS, ONE, mode)[1]) == ("answered", 1, 0)
    assert _outcome(_checked(QUESTION, RECORDS, NONE, mode)[1]) == ("no_answer", 0, 1)


def test_off_the_prompt_equals_the_prompt_built_without_the_new_argument():
    """Byte for byte: the owner's names change nothing while the switch is off."""
    assert _prompt(QUESTION, RECORDS, owner_names=frozenset({"pella"})) == build_prompt(
        QUESTION, RECORDS, precision="none", owner_words=OWNER, people=PEOPLE)


# --- on: one cited item -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_on_an_unknown_name_in_one_of_two_cited_items_keeps_the_sentence(one_item, mode):
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode)
    assert prompt.one_item_names == {"pella"}
    assert _outcome(checked) == ("answered", 1, 0)
    assert _outcome(_checked(QUESTION, RECORDS, ONE, mode)[1]) == ("answered", 1, 0)


@pytest.mark.parametrize("mode", MODES)
def test_on_an_unknown_name_in_no_cited_item_still_drops(one_item, mode):
    assert _outcome(_checked(QUESTION, RECORDS, NONE, mode)[1]) == ("no_answer", 0, 1)
    assert _outcome(_checked(QUESTION, [SEED], "Forty seed packets were catalogued [1].", mode)[1]) == (
        "no_answer", 0, 1)


@pytest.mark.parametrize("mode", MODES)
def test_on_two_unknown_names_each_need_one_cited_item(one_item, mode):
    question = "What have Pella and Orrin been arranging for the lantern fair?"
    orrin = _message("d", "Orrin painted the banner for the lantern fair stall.")
    records = [PELLA, STALL, orrin]
    assert _prompt(question, records).one_item_names == {"pella", "orrin"}
    each = "Pella drew up a sign-up sheet and Orrin painted the banner for the stall [1, 3]."
    assert _outcome(_checked(question, records, each, mode)[1]) == ("answered", 1, 0)
    assert _outcome(_checked(question, records, BOTH, mode)[1]) == ("no_answer", 0, 1)     # Orrin in neither


def test_on_nothing_loosens_without_the_owners_names_or_without_people(one_item):
    """The service reads the owner's names only while the switch is on; None (unread or unreadable) and an empty
    people set (a store that cannot be read gives none) loosen nothing."""
    assert build_prompt(QUESTION, RECORDS, precision="none", owner_words=OWNER, people=PEOPLE).one_item_names == set()
    assert _prompt(QUESTION, RECORDS, people=frozenset()).one_item_names == frozenset()
    for mode in MODES:
        prompt = build_prompt(QUESTION, RECORDS, precision="none", owner_words=OWNER, people=PEOPLE)
        checked = post_check_answer(BOTH, RECORDS, prompt, mode=mode, boundary=Open())
        assert _outcome(checked) == ("no_answer", 0, 1), mode


def test_a_prompt_built_on_and_checked_off_binds_in_every_item(one_item, monkeypatch):
    prompt = _prompt(QUESTION, RECORDS)
    assert prompt.one_item_names == {"pella"}
    monkeypatch.setenv(SWITCH, "false")
    checked = post_check_answer(BOTH, RECORDS, prompt, mode="only", boundary=Open())
    assert _outcome(checked) == ("no_answer", 0, 1)


# --- on: a known person's name still binds in every cited item ----------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_on_a_known_persons_name_still_needs_every_cited_item(one_item, mode):
    people = PEOPLE | {"pella"}
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, people=people)
    assert prompt.name_terms == {"pella"} and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)
    assert _outcome(_checked(QUESTION, RECORDS, ONE, mode, people=people)[1]) == ("answered", 1, 0)


@pytest.mark.parametrize("mode", MODES)
def test_on_a_known_and_an_unknown_name_in_one_question(one_item, mode):
    """Mara is known: she binds in every cited item; Pella is not: one item is enough for her."""
    question = "What have Pella and Mara been arranging for the lantern fair?"
    with_mara = _message("e", "Mara booked two tables and a banner for the lantern fair stall.")
    pella_mara = _message("f", "Pella and Mara mapped out the lantern fair stall and drafted the sign-up sheet.")
    assert _prompt(question, [pella_mara, with_mara]).one_item_names == {"pella"}
    kept = "Pella and Mara drew up a sign-up sheet, and Mara booked two tables for the stall [1, 2]."
    assert _outcome(_checked(question, [pella_mara, with_mara], kept, mode)[1]) == ("answered", 1, 0)
    assert _outcome(_checked(question, [pella_mara, STALL], kept, mode)[1]) == ("no_answer", 0, 1)


@pytest.mark.parametrize("mode", MODES)
def test_on_the_known_people_come_from_the_nodes_people_set(attested, one_item, mode):
    """Through the store: a person entity binds in every cited item; a name nobody here carries needs one."""
    conn = _people_db(others=("Pella Dunmore",))
    people, own = people_words(conn), owner_name_words(conn)
    assert "pella" in people and own is not None and "wrenna" in own
    words = owner_party_words(conn, Open())
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, owner_words=words, people=people, owner_names=own)
    assert prompt.one_item_names == frozenset() and _outcome(checked) == ("no_answer", 0, 1)
    conn = _people_db(others=("Mara Voss",))
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, owner_words=owner_party_words(conn, Open()),
                               people=people_words(conn), owner_names=owner_name_words(conn))
    assert prompt.one_item_names == {"pella"} and _outcome(checked) == ("answered", 1, 0)


def test_an_unreadable_store_gives_no_owners_names():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entities (entity_id TEXT)")          # no name columns: the read raises
    assert owner_name_words(conn) is None and people_words(conn) == frozenset()


# --- on: the owner's words, as before -----------------------------------------------------------------------------------

def _answer_as_the_service(conn, question, records, sentence, mode, *, account=frozenset()):
    """As `AnswerService._compute` builds the prompt with the switch on."""
    account_asked = account_words_as_asked(question, account)
    words = (owner_party_words(conn, Open(), account_words=account_asked) if account_asked
             else owner_party_words(conn, Open()))
    people = people_words(conn)
    shared = account_asked & words & people
    prompt = build_prompt(question, list(records), precision="none", owner_words=words, people=people,
                          owner_names=owner_name_words(conn) | account,
                          **({"shared_owner_words": shared} if shared else {}))
    return prompt, post_check_answer(sentence, list(records), prompt, mode=mode, boundary=Open())


@pytest.mark.parametrize("sentence, outcome", [
    ("Ivo worked on the lantern project and is leaving the bakery [1, 2].", "no_answer"),   # the owner's and a namesake's
    ("Ivo spent the week on the lantern project [1].", "answered"),                        # the owner's own item
    ("Ivo is leaving the bakery [2].", "answered"),                                        # the item that names him
])
def test_on_the_shared_owner_word_rule_is_unchanged(attested, one_item, sentence, outcome):
    """Review R-157, finding 1, with the switch on: the account word is the owner's and another person's here."""
    conn = _node()
    for mode in MODES:
        prompt, checked = _answer_as_the_service(conn, "What has Ivo been doing?", [ACCOUNT_ITEM, NAMESAKE], sentence,
                                                 mode, account=ACCOUNT)
        assert prompt.shared_owner_words == {"ivo"} and prompt.one_item_names == frozenset()
        assert checked.body.outcome == outcome, (mode, sentence)


@pytest.mark.parametrize("mode", MODES)
def test_on_an_owners_name_another_party_carries_still_binds_in_every_item(attested, one_item, mode):
    """The owner's surname is also an organisation's: no owner word (another party carries it) and no person's. It
    is still the owner's name, so it never takes the one-item rule."""
    conn = _people_db(others=("Mara Voss",))
    conn.execute("INSERT INTO entities VALUES ('ent-org','organization','Calloway Hardware','[]',0,NULL)")
    question = "What has Calloway been arranging for the lantern fair?"
    words, people, own = owner_party_words(conn, Open()), people_words(conn), owner_name_words(conn)
    assert "calloway" not in words and "calloway" not in people and "calloway" in own
    calloway = _message("1", "Calloway and I mapped out the lantern fair stall and drafted the sign-up sheet.")
    sentence = "Calloway drew up a sign-up sheet, and two tables and a banner were booked for the stall [1, 2]."
    prompt, checked = _checked(question, [calloway, STALL], sentence, mode, owner_words=words, people=people,
                               owner_names=own)
    assert prompt.name_terms == {"calloway"} and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)
    # Without the owner's names it would have loosened: they are what keeps it.
    assert _prompt(question, [calloway, STALL], owner_words=words, people=people).one_item_names == {"calloway"}


def test_on_an_account_word_never_takes_the_one_item_rule(one_item):
    """An attested account word that the owner-word rule leaves a name (here passed as one of the owner's names)."""
    prompt = _prompt(QUESTION, RECORDS, owner_names=frozenset({"pella"}))
    assert prompt.name_terms == {"pella"} and prompt.one_item_names == frozenset()


@pytest.mark.parametrize("mode", MODES)
def test_on_the_owners_own_name_is_the_owner_as_before(one_item, mode):
    question = "What has Clark been arranging for the lantern fair?"
    prompt, checked = _checked(question, RECORDS, "Two tables and a banner were booked for the stall [2].", mode)
    assert prompt.name_terms == frozenset() and _outcome(checked) == ("answered", 1, 0)


# --- on: Off-limits, as before ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", MODES)
def test_on_an_off_limits_name_binds_in_every_item_and_still_refuses(attested, one_item, mode):
    conn = _people_db(others=("Mara Voss",), offlimits=("Harlan Ostrow",))
    boundary = _boundary_protecting("Harlan Ostrow")
    question = "What has the owner arranged with Harlan Ostrow?"
    harlan = _message("2", "Harlan Ostrow and I arranged the stall rota.")
    prompt, checked = _checked(question, [harlan, STALL], "The stall rota was arranged and tables were booked [1, 2].",
                               mode, boundary=boundary, owner_words=owner_party_words(conn, boundary),
                               people=people_words(conn), owner_names=owner_name_words(conn))
    assert {"harlan", "ostrow"} <= prompt.name_terms and not prompt.one_item_names & {"harlan", "ostrow"}
    assert checked.body.outcome == "no_answer"


@pytest.mark.parametrize("mode", MODES)
def test_on_the_off_limits_body_check_still_withholds_a_kept_sentence(one_item, mode):
    """The one-item rule keeps the sentence; the Off-limits check on the body still withholds the answer."""
    boundary = _boundary_protecting("Quillon")
    with_quillon = _message("3", "Pella and Quillon mapped out the lantern fair stall and drafted the sign-up sheet.")
    sentence = "Pella and Quillon drew up a sign-up sheet, and two tables were booked for the stall [1, 2]."
    _prompt_on, checked = _checked(QUESTION, [with_quillon, STALL], sentence, mode, boundary=boundary)
    assert (checked.body.outcome, checked.reason, checked.dropped_relevance) == ("no_answer", "answer_protected", 0)


# --- the service --------------------------------------------------------------------------------------------------------

def _spy(monkeypatch):
    seen = []
    real = answer_release.build_prompt

    def build(*args, **kwargs):
        prompt = real(*args, **kwargs)
        seen.append((kwargs, prompt))
        return prompt
    monkeypatch.setattr(answer_release, "build_prompt", build)
    return seen


@pytest.mark.parametrize("on", [False, True], ids=["off", "on"])
def test_the_service_reads_the_owners_names_only_while_on(legacy, tmp_path, monkeypatch, on):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "people_words", lambda conn: PEOPLE)
    if on:
        monkeypatch.setenv(SWITCH, "true")
        monkeypatch.setattr(answer_release, "owner_name_words", lambda conn: frozenset({"wrenna"}))
    else:
        def unread(conn):
            raise AssertionError("read while off")
        monkeypatch.setattr(answer_release, "owner_name_words", unread)
    seen = _spy(monkeypatch)
    service = _service(node, "Pella is working on a synthetic task [1].")
    try:
        body = _finished(node, service, _ask(node, service, "What synthetic message has Pella written?"))
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}     # no item names Pella
    finally:
        service.close()
    (kwargs, prompt), = seen
    assert prompt.name_terms == {"pella"}
    if on:
        assert kwargs["owner_names"] == {"wrenna"} and prompt.one_item_names == {"pella"}
    else:
        assert "owner_names" not in kwargs and prompt.one_item_names == frozenset()


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_on_the_service_still_refuses_a_protected_question_before_any_prompt(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setenv(SWITCH, "true")
    real = node.search.resolver.entity_boundary

    class Flagged:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, *texts):
            return any("quillon" in str(text).lower() for text in texts) or self.inner.mentions_protected(*texts)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(node.search.resolver, "entity_boundary", lambda conn: Flagged(real(conn)))
    seen = _spy(monkeypatch)
    service = _service(node, "Pella aims for a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "What goals has Pella shared with Quillon about the compiler?")
        body = _finished(node, service, answer_id)
        with node.ledger._transaction() as db:
            receipt = json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?",
                                            ("answer-1",)).fetchone()[0])
    finally:
        service.close()
    assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    assert receipt["reason"] == "question_protected" and seen == []
