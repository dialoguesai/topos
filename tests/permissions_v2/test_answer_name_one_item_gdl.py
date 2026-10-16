"""Lane GD-L (15 Oct 2026, grantee density round 3): the name rule in one cited item.

The name rule (`_cites_question_subject`) drops a sentence unless every name the question carries (`Prompt.name_terms`)
is written, capitalised, in EVERY item the sentence cites: an item that names a person never lends that person the
facts of the other items cited beside it. The owner's direction of 15 Oct: for grants, the looser method that gives
the most facts, never anything the grant does not permit. With ANSWER_NAME_ONE_ITEM on (off by default), a name that
is no known person's word on this node (`people_words`) and no word of the owner's (`owner_name_words`, the account's
words, the owner words) has to be in at least ONE cited item. A known person's name still binds in every cited item;
the shared owner-word rule (review R-157), the Off-limits refusal and body check, retrieval and the grant boundary are
unchanged. Off is 1.5.9's rule. Invented names and text only.

The security review (safe to measure, not safe to ship default-on) and its mitigations, all only while the switch is
on: M1, a name is a unit ("Lena Ortiz" binds whole when Lena is known; "Dana Whitfield" when Dana is the owner's);
M2, a cited item that lacks the loosened name and names a known person keeps the every-item rule; M3, a word any
entity of any type, contact or self row on the node carries never loosens, and an empty owner-name read loosens
nothing; M4, nothing loosens unless the support judge is on; M5, two or more loosened names must all be in one cited
item.
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
from topos.permissions_v2.answer_generation import (_name_runs, account_words_as_asked, build_prompt,
    node_name_words, owner_name_words, owner_party_words, people_words, post_check_answer)
from topos.permissions_v2.canonical import PolicyError

MODES = ("only", "with_sources")
P = "TOPOS_PERMISSIONS_V2_"
SWITCH = P + "ANSWER_NAME_ONE_ITEM"
JUDGE = P + "ANSWER_SUPPORT_JUDGE"
OWNER = frozenset({"clark"})
PEOPLE = frozenset({"mara", "voss"})            # somebody the node knows, so the people set is not empty
# Everything the node is named by (M3): its people, the owner, and an organisation and a place that are nobody.
NODE = PEOPLE | OWNER | frozenset({"brightwater", "outfitters", "porto"})

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
    monkeypatch.delenv(JUDGE, raising=False)           # the judge's own default: on


@pytest.fixture
def one_item(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


def _prompt(question, records, *, owner_words=OWNER, people=PEOPLE, owner_names=OWNER, node_names=NODE,
            shared=frozenset()):
    return build_prompt(question, list(records), precision="none", owner_words=owner_words, people=people,
                        owner_names=owner_names, node_names=node_names,
                        **({"shared_owner_words": shared} if shared else {}))


def _checked(question, records, answer, mode, *, boundary=None, support=None, **kwargs):
    prompt = _prompt(question, records, **kwargs)
    return prompt, post_check_answer(answer, list(records), prompt, mode=mode, boundary=boundary or Open(),
                                     support=support)


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
    """Byte for byte: the owner's names and the node's names change nothing while the switch is off."""
    assert _prompt(QUESTION, RECORDS, owner_names=frozenset({"pella"})) == build_prompt(
        QUESTION, RECORDS, precision="none", owner_words=OWNER, people=PEOPLE)
    assert _prompt(QUESTION, RECORDS) == build_prompt(QUESTION, RECORDS, precision="none", owner_words=OWNER,
                                                      people=PEOPLE)


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
def test_on_two_unknown_names_must_be_in_one_cited_item_together(one_item, mode):
    """Security review, M5: each in an item of its own is no longer enough; one cited item carries both."""
    question = "What have Pella and Orrin been arranging for the lantern fair?"
    orrin = _message("d", "Orrin painted the banner for the lantern fair stall.")
    together = _message("4", "Pella and Orrin hung the banner on the lantern fair stall.")
    records = [PELLA, STALL, orrin, together]
    assert _prompt(question, records).one_item_names == {"pella", "orrin"}
    each = "Pella drew up a sign-up sheet and Orrin painted the banner for the stall [1, 3]."
    assert _outcome(_checked(question, records, each, mode)[1]) == ("no_answer", 0, 1)
    assert _outcome(_checked(question, records, BOTH, mode)[1]) == ("no_answer", 0, 1)     # Orrin in neither
    kept = "Two tables were booked for the stall, and Pella and Orrin hung the banner [2, 4]."
    assert _outcome(_checked(question, records, kept, mode)[1]) == ("answered", 1, 0)


def test_on_nothing_loosens_without_the_owners_names_or_without_people(one_item):
    """The service reads the owner's names only while the switch is on; None (unread or unreadable) and an empty
    people set (a store that cannot be read gives none) loosen nothing."""
    assert build_prompt(QUESTION, RECORDS, precision="none", owner_words=OWNER, people=PEOPLE).one_item_names == set()
    assert _prompt(QUESTION, RECORDS, people=frozenset()).one_item_names == frozenset()
    assert _prompt(QUESTION, RECORDS, owner_names=None).one_item_names == frozenset()
    assert _prompt(QUESTION, RECORDS, node_names=None).one_item_names == frozenset()
    assert _prompt(QUESTION, RECORDS, node_names=frozenset()).one_item_names == frozenset()
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
    """Mara is known: she binds in every cited item. Pella is not, and is a name of her own ("and" parts them), so
    the prompt loosens her; but every cited item then names Mara, so an item lacking Pella names a known person and
    the sentence keeps the every-item rule (security review, M2). A question naming a known person never loosens."""
    question = "What have Pella and Mara been arranging for the lantern fair?"
    with_mara = _message("e", "Mara booked two tables and a banner for the lantern fair stall.")
    pella_mara = _message("f", "Pella and Mara mapped out the lantern fair stall and drafted the sign-up sheet.")
    assert _prompt(question, [pella_mara, with_mara]).one_item_names == {"pella"}
    kept = "Pella and Mara drew up a sign-up sheet, and Mara booked two tables for the stall [1, 2]."
    assert _outcome(_checked(question, [pella_mara, with_mara], kept, mode)[1]) == ("no_answer", 0, 1)
    assert _outcome(_checked(question, [pella_mara, STALL], kept, mode)[1]) == ("no_answer", 0, 1)
    alone = "Pella and Mara drew up a sign-up sheet for the stall [1]."
    assert _outcome(_checked(question, [pella_mara, with_mara], alone, mode)[1]) == ("answered", 1, 0)


@pytest.mark.parametrize("mode", MODES)
def test_on_the_known_people_come_from_the_nodes_people_set(attested, one_item, mode):
    """Through the store: a person entity binds in every cited item; a name nobody here carries needs one."""
    conn = _people_db(others=("Pella Dunmore",))
    people, own = people_words(conn), owner_name_words(conn)
    assert "pella" in people and own is not None and "wrenna" in own
    words = owner_party_words(conn, Open())
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, owner_words=words, people=people, owner_names=own,
                               node_names=node_name_words(conn))
    assert prompt.one_item_names == frozenset() and _outcome(checked) == ("no_answer", 0, 1)
    conn = _people_db(others=("Mara Voss",))
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, owner_words=owner_party_words(conn, Open()),
                               people=people_words(conn), owner_names=owner_name_words(conn),
                               node_names=node_name_words(conn))
    assert prompt.one_item_names == {"pella"} and _outcome(checked) == ("answered", 1, 0)


def test_an_unreadable_store_gives_no_owners_names():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entities (entity_id TEXT)")          # no name columns: the read raises
    assert owner_name_words(conn) is None and people_words(conn) == frozenset()
    assert node_name_words(conn) is None


# --- on: the owner's words, as before -----------------------------------------------------------------------------------

def _answer_as_the_service(conn, question, records, sentence, mode, *, account=frozenset()):
    """As `AnswerService._compute` builds the prompt with the switch on."""
    account_asked = account_words_as_asked(question, account)
    words = (owner_party_words(conn, Open(), account_words=account_asked) if account_asked
             else owner_party_words(conn, Open()))
    people = people_words(conn)
    shared = account_asked & words & people
    prompt = build_prompt(question, list(records), precision="none", owner_words=words, people=people,
                          owner_names=owner_name_words(conn) | account, node_names=node_name_words(conn),
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
                               owner_names=own, node_names=node_name_words(conn))
    assert prompt.name_terms == {"calloway"} and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)
    # Without the owner's names and the node's names it would have loosened: either one keeps it.
    assert _prompt(question, [calloway, STALL], owner_words=words, people=people).one_item_names == {"calloway"}
    assert _prompt(question, [calloway, STALL], owner_words=words, people=people,
                   node_names=node_name_words(conn)).one_item_names == frozenset()


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
                               people=people_words(conn), owner_names=owner_name_words(conn),
                               node_names=node_name_words(conn))
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


def _unread(conn):
    raise AssertionError("read while it must not be")


@pytest.mark.parametrize("case", ["off", "on", "judge_off", "no_owner"])
def test_the_service_reads_the_owners_names_only_while_on(legacy, tmp_path, monkeypatch, case):
    """The owner's names and the node's names are read only while the switch and the support judge are on (M4);
    an owner-name read that gives none (finding 5) reads no node names and passes neither."""
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "people_words", lambda conn: PEOPLE)
    if case != "off":
        monkeypatch.setenv(SWITCH, "true")
    if case == "judge_off":
        monkeypatch.setenv(JUDGE, "false")
    monkeypatch.setattr(answer_release, "owner_name_words",
                        {"on": lambda conn: frozenset({"wrenna"}), "no_owner": lambda conn: None}.get(case, _unread))
    monkeypatch.setattr(answer_release, "node_name_words", (lambda conn: NODE) if case == "on" else _unread)
    seen = _spy(monkeypatch)
    service = _service(node, "Pella is working on a synthetic task [1].")
    try:
        body = _finished(node, service, _ask(node, service, "What synthetic message has Pella written?"))
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}     # no item names Pella
    finally:
        service.close()
    (kwargs, prompt), = seen
    assert prompt.name_terms == {"pella"}
    if case == "on":
        assert kwargs["owner_names"] == {"wrenna"} and kwargs["node_names"] == NODE
        assert prompt.one_item_names == {"pella"} and prompt.one_item_people == PEOPLE
    else:
        assert "owner_names" not in kwargs and "node_names" not in kwargs
        assert prompt.one_item_names == frozenset() and prompt.one_item_people == frozenset()


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


# --- the security review (safe to measure, not to ship default-on): findings 1 to 6, mitigations M1 to M5 -------------

KNOWN_LENA = PEOPLE | {"lena", "brooks"}             # Lena Brooks is somebody this node knows
LENA_ORTIZ = _message("5", "Lena Ortiz and I mapped out the lantern fair stall and drafted the sign-up sheet.")
LENA_BROOKS = _message("6", "Lena booked two tables and a banner for the lantern fair stall.")


@pytest.mark.parametrize("mode", MODES)
def test_review_1_a_shared_first_name_never_lends_the_known_persons_facts(one_item, mode):
    """Finding 1, M1: "ortiz" alone is nobody the node knows, but "Lena Ortiz" is one name and Lena is known (Lena
    Brooks). The whole name binds in every cited item, so Lena Brooks's item never lends its facts to Lena Ortiz."""
    question = "What has Lena Ortiz been arranging for the lantern fair?"
    sentence = "Lena Ortiz drew up a sign-up sheet, and two tables and a banner were booked for the stall [1, 2]."
    prompt, checked = _checked(question, [LENA_ORTIZ, LENA_BROOKS], sentence, mode, people=KNOWN_LENA,
                               node_names=NODE | KNOWN_LENA)
    assert prompt.name_terms == {"lena", "ortiz"} and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)
    alone = "Lena Ortiz drew up a sign-up sheet for the stall [1]."
    assert _outcome(_checked(question, [LENA_ORTIZ, LENA_BROOKS], alone, mode, people=KNOWN_LENA)[1]) == (
        "answered", 1, 0)


@pytest.mark.parametrize("question", [
    "What has Lena Ortiz been arranging?",
    "What has Ortiz, Lena been arranging?",
    "What has Lena B. Ortiz been arranging?",
    "What has Lena-Ortiz been arranging?",
    "What has O'Ortiz Lena been arranging?",
    "What has lena Ortiz been arranging?",
    "What have LENA ORTIZ and the stall crew been arranging?",
])
def test_review_1_a_name_is_a_unit_however_it_is_written(one_item, question):
    """M1: a run of name words with only spaces, a hyphen, an apostrophe, a full stop or a comma between them is
    one name. A known word anywhere in it binds every word of it."""
    prompt = _prompt(question, [LENA_ORTIZ], people=KNOWN_LENA)
    assert "ortiz" in prompt.name_terms and prompt.one_item_names == frozenset()


def test_review_1_names_apart_are_apart_and_a_wholly_unknown_name_loosens_whole(one_item):
    assert _name_runs("What has Lena Ortiz been arranging?", frozenset()) == (frozenset({"lena", "ortiz"}),)
    assert _name_runs("What have Pella and Orrin been arranging?", frozenset()) == (
        frozenset({"pella"}), frozenset({"orrin"}))
    assert _prompt("What has Pella Dunmore been arranging?", RECORDS).one_item_names == {"pella", "dunmore"}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("dana", ["owner_word", "owner_name"])
def test_review_2_the_owners_first_name_with_an_unknown_surname_binds_whole(one_item, mode, dana):
    """Finding 2, M1: "dana" is the owner's, as an owner word or as one of the owner's own names another party also
    carries. "Dana Whitfield" is one name, so "whitfield" never loosens and the owner's own items (first person,
    naming nobody) never go to a namesake."""
    question = "What has Dana Whitfield been arranging for the lantern fair?"
    namesake = _message("7", "Dana Whitfield is leaving the bakery to run the lantern fair committee.")
    sentence = "Two tables and a banner were booked for the stall by Dana Whitfield [1, 2]."
    owner = {"owner_names": OWNER | {"dana"}}
    if dana == "owner_word":
        owner["owner_words"] = OWNER | {"dana"}
    prompt, checked = _checked(question, [namesake, STALL], sentence, mode, **owner)
    assert "whitfield" in prompt.name_terms and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)


@pytest.mark.parametrize("mode", MODES)
def test_review_3_a_cited_item_naming_a_known_person_keeps_the_every_item_rule(one_item, mode):
    """Finding 3, M2: item 2 does not name Pella and names Mara, whom the node knows, capitalised. Cited beside
    Pella's item it could lend Mara's facts to Pella, so the sentence needs Pella in every cited item."""
    with_mara = _message("8", "Mara booked two tables and a banner for the lantern fair stall.")
    sentence = "Pella drew up a sign-up sheet, and two tables and a banner were booked for the stall [1, 2]."
    prompt, checked = _checked(QUESTION, [PELLA, with_mara], sentence, mode)
    assert prompt.one_item_names == {"pella"} and prompt.one_item_people == PEOPLE
    assert _outcome(checked) == ("no_answer", 0, 1)
    assert _outcome(_checked(QUESTION, [PELLA, STALL], sentence, mode)[1]) == ("answered", 1, 0)   # nobody named


@pytest.mark.parametrize("mode", MODES)
def test_review_4_an_organisation_topic_or_place_on_the_node_never_loosens(attested, one_item, mode):
    """Finding 4, M3: Brightwater (an organisation), Porto (a place) and the Lantern Guild (a topic) are entities on
    this node and nobody's person name. A word any entity carries binds in every cited item, as before."""
    conn = _people_db(others=("Mara Voss",))
    for number, (kind, name) in enumerate((("organization", "Brightwater Outfitters"), ("place", "Porto"),
                                           ("topic", "Lantern Guild")), 1):
        conn.execute("INSERT INTO entities VALUES (?,?,?,'[]',0,NULL)", (f"ent-x{number}", kind, name))
    people, named = people_words(conn), node_name_words(conn)
    assert {"brightwater", "porto", "guild"} <= named and not {"brightwater", "porto", "guild"} & people
    question = "What has Brightwater been arranging in Porto?"
    item = _message("5", "Brightwater sent a crate of lanterns to Porto.")
    sentence = "Brightwater sent lanterns to Porto, and two tables and a banner were booked for the stall [1, 2]."
    prompt, checked = _checked(question, [item, STALL], sentence, mode, owner_words=owner_party_words(conn, Open()),
                               people=people, owner_names=owner_name_words(conn), node_names=named)
    assert prompt.name_terms == {"brightwater", "porto"} and prompt.one_item_names == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)
    # Without the node's names (GD-L as reviewed) both would have loosened.
    assert _prompt(question, [item, STALL], people=people, node_names=PEOPLE).one_item_names == {
        "brightwater", "porto"}


@pytest.mark.parametrize("word", ["tidepool", "fenwick", "importcard", "wrenimport"])
def test_review_5_handles_unconfirmed_self_rows_and_importer_cards_never_loosen(attested, one_item, word):
    """Finding 5, M3: a contact's handle, a self row the owner never attested and an importer's self card (its name
    and its handle) are in neither the people nor the owner's names. Each is a word the node carries, so none
    loosens."""
    conn = _people_db(others=("Mara Voss",), contacts=(("Mara Voss", ("tidepool_m",)),))
    conn.execute("INSERT INTO entities VALUES ('ent-self-2','person','Odile Fenwick','[]',1,NULL)")
    people, own, named = people_words(conn), owner_name_words(conn), node_name_words(conn)
    assert word not in people and word not in own and word in named
    question = f"What has {word.capitalize()} been arranging for the lantern fair?"
    prompt = _prompt(question, RECORDS, owner_words=owner_party_words(conn, Open()), people=people, owner_names=own,
                     node_names=named)
    assert prompt.name_terms == {word} and prompt.one_item_names == frozenset()


@pytest.mark.parametrize("read", ["refused", "nobody_attested"])
def test_review_5_an_empty_owner_name_read_stays_closed(one_item, monkeypatch, read):
    """Finding 5: a failed identity read gave the owner's names as an EMPTY set, not None, and the loosening stayed
    on. Empty is None now, in the reader and in `build_prompt`."""
    from topos.permissions_v2 import identity

    def refuse(conn):
        raise PolicyError("identity_unreadable")
    monkeypatch.setattr(identity, "attested_subjects", refuse if read == "refused" else (lambda conn: set()))
    conn = _people_db(others=("Mara Voss",))
    assert owner_name_words(conn) is None
    assert _prompt(QUESTION, RECORDS, owner_names=frozenset()).one_item_names == frozenset()
    for mode in MODES:
        checked = _checked(QUESTION, RECORDS, BOTH, mode, owner_names=frozenset())[1]
        assert _outcome(checked) == ("no_answer", 0, 1), mode


@pytest.mark.parametrize("mode", MODES)
def test_review_m5_two_unknown_people_are_never_put_together_by_the_writer(one_item, mode):
    """M5: Priya and Tomas are both unknown here. "They met in Porto", citing an item about each, states a meeting
    no item states; it drops unless one cited item names both. Porto is a place on this node: every item."""
    question = "When did Priya and Tomas meet in Porto?"
    priya = _message("5", "Priya flew in to Porto for the lantern fair.")
    tomas = _message("6", "Tomas booked a table at the lantern fair in Porto.")
    met = _message("7", "Priya and Tomas met at the lantern fair in Porto.")
    records = [priya, tomas, met]
    prompt = _prompt(question, records)
    assert prompt.name_terms == {"priya", "tomas", "porto"} and prompt.one_item_names == {"priya", "tomas"}
    invented = "Priya and Tomas met in Porto at the lantern fair [1, 2]."
    assert _outcome(_checked(question, records, invented, mode)[1]) == ("no_answer", 0, 1)
    stated = "Priya reached Porto, and she and Tomas then met up at the fair [1, 3]."
    assert _outcome(_checked(question, records, stated, mode)[1]) == ("answered", 1, 0)


@pytest.mark.parametrize("mode", MODES)
def test_review_m4_with_the_support_judge_off_nothing_loosens(one_item, monkeypatch, mode):
    """Finding 6, M4: the judge is what reads a loosened sentence against its items; with it off nothing loosens."""
    monkeypatch.setenv(JUDGE, "false")
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode)
    assert prompt.one_item_names == frozenset() and prompt.one_item_people == frozenset()
    assert _outcome(checked) == ("no_answer", 0, 1)


def test_review_m4_a_prompt_built_with_the_judge_on_and_checked_with_it_off_binds_in_every_item(one_item,
                                                                                              monkeypatch):
    prompt = _prompt(QUESTION, RECORDS)
    assert prompt.one_item_names == {"pella"}
    monkeypatch.setenv(JUDGE, "false")
    checked = post_check_answer(BOTH, RECORDS, prompt, mode="only", boundary=Open())
    assert _outcome(checked) == ("no_answer", 0, 1)


@pytest.mark.parametrize("mode", MODES)
def test_review_the_positive_case_still_loosens_and_the_judge_still_reads_it(attested, one_item, monkeypatch, mode):
    """Through the store: an unknown name, no known person in the cited items, the judge on. One cited item is
    enough; the judge then reads the sentence against both items, and its "no" still drops it."""
    monkeypatch.setenv(JUDGE, "true")
    conn = _people_db(others=("Mara Voss",))
    store = {"owner_words": owner_party_words(conn, Open()), "people": people_words(conn),
             "owner_names": owner_name_words(conn), "node_names": node_name_words(conn)}
    prompt, checked = _checked(QUESTION, RECORDS, BOTH, mode, support=lambda *args, **kwargs: True, **store)
    assert prompt.one_item_names == {"pella"} and _outcome(checked) == ("answered", 1, 0)
    refused = _checked(QUESTION, RECORDS, BOTH, mode, support=lambda *args, **kwargs: False, **store)[1]
    assert (refused.body.outcome, refused.dropped_support) == ("no_answer", 1)
