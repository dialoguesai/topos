"""BL-146 round 3: the review's H1, H2, M1 and M2, each with the case that broke it.

H1. The Off-limits echo read the owner-rewritten question, so a word read as the owner was never checked as an
Off-limits word, and owner words were never checked against the boundary. Now the echo reads the question as asked
(`Prompt.asked`), and no word the boundary protects, written either way, is ever an owner word. Shown with the real
`EntityBoundary`: a name-only Off-limits entry that shares the owner's surname, a lower-case question, a lower-case
echo.

H2. Owner words matched through the word fold, so "Browning" read as the owner "Brown". Names now match on exact
case-folded words.

M2. Owner words came from importer-set self cards and handles. Now: the attested self entity and its linked card,
names only. Every word any other entity, contact (name or handle) or Off-limits entry carries is taken away.

M1. A capitalised name was skipped as a sentence's first word, inside quotes, after a colon, and when it was a
question word ("Will"); lower case was never seen. Now a capitalised word binds in every position unless it is a
request word, or a sentence starter where a clause starts; a lower-case word binds when it equals a known person's
name on the node. Invented names only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2.answer_generation import (_as_owner, _name_terms, _owner_mentions, build_prompt,
    owner_party_words, people_words, post_check_answer, question_lacks_permitted_anchor)
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.permissions_v2.knowledge_contract import MessageResult

MODES = ("only", "with_sources")


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


ITEM = _message("a", "Spent the morning working on the lantern project for the studio.")


class Open:
    def mentions_protected(self, *texts):
        return False


def _boundary_protecting(name):
    """The real boundary over one name-only Off-limits entry (no entity), as the app makes one."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO entity_blackholes VALUES('b1','',?,?,?)", (name.lower(), name, json.dumps([name])))
    return EntityBoundary(conn)


def _people_db(*, card_handles=("climbing_x",), others=(), contacts=(), offlimits=()):
    """An attested owner "Wrenna Calloway" with a linked card, plus whoever else the node knows."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, aliases_json TEXT,
            is_self INTEGER, contact_id TEXT);
        CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT, known_usernames_json TEXT,
            is_self INTEGER);
    """)
    conn.execute("INSERT INTO entities VALUES ('ent-self','person','Wrenna Calloway',?,1,'c-self')",
                 (json.dumps(["Wren"]),))
    conn.execute("INSERT INTO contacts VALUES ('c-self','Wrenna C.',?,0)", (json.dumps(list(card_handles)),))
    conn.execute("INSERT INTO contacts VALUES ('c-import','Wrenna Importcard',?,1)", (json.dumps(["wrenimport"]),))
    for index, name in enumerate(others):
        conn.execute("INSERT INTO entities VALUES (?, 'person', ?, '[]', 0, NULL)", (f"ent-{index}", name))
    for index, (name, handles) in enumerate(contacts):
        conn.execute("INSERT INTO contacts VALUES (?, ?, ?, 0)", (f"c-{index}", name, json.dumps(list(handles))))
    if offlimits:
        conn.execute("CREATE TABLE entity_blackholes (blackhole_id TEXT, entity_id TEXT, normalized_name TEXT, "
                     "canonical_name TEXT, aliases_json TEXT)")
        for index, name in enumerate(offlimits):
            conn.execute("INSERT INTO entity_blackholes VALUES (?, '', ?, ?, ?)",
                         (f"b{index}", name.lower(), name, json.dumps([name])))
    return conn


@pytest.fixture
def attested(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})


# --- H1 ------------------------------------------------------------------------------------------------------------

def test_h1_a_word_the_boundary_protects_is_never_an_owner_word(attested):
    boundary = _boundary_protecting("Odrin Calloway")
    # Only the boundary knows this entry here: the owner's store holds no Off-limits table.
    words = owner_party_words(_people_db(), boundary)
    assert "calloway" not in words and {"wrenna", "wren"} <= words
    assert "calloway" in owner_party_words(_people_db())          # without the boundary it would be the owner's


def test_h1_the_off_limits_echo_reads_the_question_as_asked_with_the_real_boundary():
    boundary = _boundary_protecting("Odrin Calloway")
    question = "what has calloway been up to lately?"
    assert not boundary.mentions_protected(question)           # lower case: the question itself is not refused
    for mode in MODES:
        # Even if a protected word were read as the owner, the echo reads the asked question and drops the sentence.
        prompt = build_prompt(question, [ITEM], precision="none", owner_words=frozenset({"calloway"}))
        assert prompt.asked == question and "owner" in prompt.question
        checked = post_check_answer("calloway is building the lantern project [1].", [ITEM], prompt, mode=mode,
                                    boundary=boundary)
        assert checked.body.outcome == "no_answer" and checked.dropped_question_echo == 1, mode
        # And with the owner words the node actually builds (H1's filter), the anchor rule abstains first.
        prompt = build_prompt(question, [ITEM], precision="none", owner_words=frozenset({"wrenna"}))
        assert question_lacks_permitted_anchor(prompt)


# --- H2 ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("owner,other", [("brown", "Browning"), ("field", "Fielding"), ("jen", "Jennings"),
                                         ("bill", "Billings"), ("gold", "Golding"), ("down", "Downing")])
def test_h2_a_name_that_folds_onto_the_owners_is_not_the_owner(owner, other):
    words = frozenset({owner})
    question = f"What has {other} been working on lately?"
    assert _as_owner(question, words) == question and _owner_mentions(question, words) == []
    assert "is the owner" not in build_prompt(question, [ITEM], precision="none", owner_words=words).user
    for mode in MODES:
        prompt = build_prompt(question, [ITEM], precision="none", owner_words=words)
        checked = post_check_answer(f"{other} is building the lantern project [1].", [ITEM], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "no_answer", (mode, other)
    # The owner's own word, and its possessive, still read as the owner.
    asked = f"What has {owner.capitalize()}'s team been working on lately?"
    assert "owner" in _as_owner(asked, words) and _owner_mentions(asked, words) == [owner.capitalize()]


def test_h2_owner_words_are_exact_words(attested):
    assert "wrenna" in owner_party_words(_people_db(others=["Wrennaby Stone"]))   # "wrennaby" is another word
    from topos.permissions_v2 import answer_generation
    own, others, _people = answer_generation._parties(_people_db(others=["Callowayne Stone"]))
    assert "calloway" in own and "callowayne" in others and "callowayne" not in own


# --- M2 ------------------------------------------------------------------------------------------------------------

def test_m2_names_only_never_handles_and_never_an_importers_self_card(attested):
    words = owner_party_words(_people_db())
    assert {"wrenna", "calloway", "wren"} <= words
    assert "climbing" not in words and "climbing_x" not in words       # a handle of the linked card
    assert "importcard" not in words and "wrenimport" not in words      # an unlinked self card, never attested


def test_m2_a_namesake_contact_takes_the_word_away(attested):
    # Another person's contact card named like the owner: the word is theirs too, so it binds.
    words = owner_party_words(_people_db(contacts=[("Wrenna Ostrey", ())]))
    assert "wrenna" not in words and "calloway" in words
    # Another contact's handle takes a word away as well.
    assert "wren" not in owner_party_words(_people_db(contacts=[("Corrigan Vale", ("wren",))]))
    # Another contact is never the owner's.
    assert not {"corrigan", "vale"} & owner_party_words(_people_db(contacts=[("Corrigan Vale", ())]))


def test_m2_an_off_limits_entry_takes_the_word_away(attested):
    assert "calloway" not in owner_party_words(_people_db(offlimits=["Calloway"]))


# --- M1 ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,name", [
    ("Ivo: what has he been working on lately?", "ivo"),
    ('What has "Ivo" been working on lately?', "ivo"),
    ("(Ivo) what has he been working on?", "ivo"),
    ("Tell me, Ivo, what has been going on?", "ivo"),
    ("What has Will been working on lately?", "will"),
    ("Ivo has been working on what?", "ivo"),
])
def test_m1_a_capitalised_name_binds_in_every_position(question, name):
    assert name in _name_terms(question, frozenset({"wrenna"}))
    for mode in MODES:
        prompt = build_prompt(question, [ITEM], precision="none", owner_words=frozenset({"wrenna"}))
        checked = post_check_answer("They are building the lantern project [1].", [ITEM], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, (mode, question)


def test_m1_starters_bind_nothing_where_a_clause_starts_and_known_people_bind_in_lower_case():
    assert _name_terms("Will the owner travel this spring?", frozenset()) == frozenset()
    assert _name_terms("What has Wrenna been working on?", frozenset({"wrenna"})) == frozenset()
    assert _name_terms("what has ivo been working on?", frozenset()) == frozenset()
    assert _name_terms("what has ivo been working on?", frozenset(), frozenset({"ivo"})) == {"ivo"}
    assert _name_terms("what will the owner do?", frozenset(), frozenset({"will"})) == frozenset()
    assert people_words(_people_db(contacts=[("Ivo Brandt", ())])) >= {"ivo", "brandt"}
    for mode in MODES:
        prompt = build_prompt("what has ivo been working on?", [ITEM], precision="none", people=frozenset({"ivo"}))
        checked = post_check_answer("They are building the lantern project [1].", [ITEM], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode


# --- L2 ------------------------------------------------------------------------------------------------------------

def test_l2_a_quoted_first_person_line_is_not_the_owners():
    from topos.permissions_v2.answer_checks import TEMPLATE_VERSION
    from topos.permissions_v2.answer_generation import SYSTEM_PROMPT
    assert ('"I" in an item is the owner, except inside quotation marks, where the words and their "I" belong to '
            'someone else the owner is quoting.') in SYSTEM_PROMPT
    assert TEMPLATE_VERSION == "topos-answer-template/v7"


def test_m2_an_email_is_never_a_name(attested):
    conn = _people_db()
    conn.execute("UPDATE entities SET aliases_json=? WHERE entity_id='ent-self'",
                 (json.dumps(["Wren", "wrenna@example.invalid"]),))
    words = owner_party_words(conn)
    assert "wrenna" in words and not {"example", "invalid"} & words


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_m1_the_answer_pass_binds_a_known_person_in_lower_case(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.message_search_harness import owner
    from tests.permissions_v2.test_answer_release import _ask, _finished, _service
    from tests.permissions_v2.test_knowledge_search import node_for
    from topos.permissions_v2 import answer_release
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    monkeypatch.setattr(answer_release, "people_words", lambda conn: frozenset({"ivo"}))
    service = _service(node, "The aim is a finished build before the weekend [1].")
    try:
        answer_id = _ask(node, service, "what goals has ivo shared about the compiler?")
        assert _finished(node, service, answer_id) == {"version": "topos-answer/v1", "outcome": "no_answer"}
    finally:
        service.close()


def test_m1_a_name_binds_as_a_name_in_the_cited_item():
    # "What has Will been up to?" is not answered from "I will finish the checklist": the item carries the verb, not
    # the name. An item that names Will answers it.
    verb = _message("b", "I will finish the launch checklist before the demo.")
    named = _message("c", "I finished the launch checklist with Will before the demo.")
    starter = _message("d", "Will do. I will finish the launch checklist before the demo.")
    for mode in MODES:
        prompt = build_prompt("What has Will been up to lately?", [verb], precision="none")
        checked = post_check_answer("The launch checklist is nearly done [1].", [verb], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        prompt = build_prompt("What has Will been up to lately?", [starter], precision="none")
        checked = post_check_answer("The launch checklist is nearly done [1].", [starter], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        prompt = build_prompt("What has Will been up to lately?", [named], precision="none")
        checked = post_check_answer("Will helped wrap up the launch list [1].", [named], prompt, mode=mode,
                                    boundary=Open())
        assert checked.body.outcome == "answered", mode
