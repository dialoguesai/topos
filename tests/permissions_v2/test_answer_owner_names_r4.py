"""BL-146 round 4: the R-N2-151 recheck's U1, U2, N1, N2 and its test gap, each with the case that showed it.

U1. Round 3 bound a capitalised first word as a name unless it was a question word, so "Hey, what has the owner
been working on?" could never be answered (18 of 23 casual openers bound). Where a clause starts, a capitalised word
now binds only when it is a known person's word or is not on the closed, pinned list of common openers
(SENTENCE_STARTERS): "Ivo: ..." binds, "Hey, ..." does not.

U2. "Known people" took every entity of every type and other contacts' handles, so "week", "report" and "three"
bound in lower case. People are person entities, other contacts' display names and Off-limits names: no topic, no
handle, no self row.

N1. The cited-item name check read the union of the cited items, so a sentence citing three items passed when one
named Ivo, and the owner's own facts from the other two were attributed to him. Every cited item must carry it.

N2. Third-party names matched through the fold ("Williams" and "William"). Names are exact case-folded words on the
question side and the item side; a possessive's "s" is a word of its own.

Gap. A word both the owner and another party carry is never the owner's; in lower case it binds, as ambiguity binds.
Invented names only.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.permissions_v2.answer_generation import (SENTENCE_STARTERS, _name_terms, build_prompt, owner_party_words,
    people_words, post_check_answer)
from topos.permissions_v2.knowledge_contract import MessageResult

MODES = ("only", "with_sources")


class Open:
    def mentions_protected(self, *texts):
        return False


def _message(letter, text):
    rid = "r." + letter * 64
    return MessageResult.parse({"kind": "message", "record_id": rid, "content": text,
        "source_ids": ["ownerimport"], "citations": [{"record_id": rid, "source_id": "ownerimport", "content": text}]})


ITEM = _message("a", "Spent the morning working on the lantern project for the studio.")


def _answer(question, records, sentence, mode, **kwargs):
    prompt = build_prompt(question, list(records), precision="none", **kwargs)
    return post_check_answer(sentence, list(records), prompt, mode=mode, boundary=Open())


# --- U1 ------------------------------------------------------------------------------------------------------------

OPENERS = ["Anything", "Hey", "Hi", "Thanks", "Quick", "Remind", "Share", "Recap", "Update", "Catch", "Just",
           "Overall", "Last", "Next", "Earlier", "We", "My", "She"]


@pytest.mark.parametrize("opener", OPENERS)
def test_u1_a_casual_opener_binds_no_name(opener):
    question = f"{opener}, what has the owner been working on lately?"
    assert _name_terms(question, frozenset()) == frozenset(), opener
    for mode in MODES:
        checked = _answer(question, [ITEM], "They are building the lantern project [1].", mode)
        assert checked.body.outcome == "answered", (opener, mode)


def test_u1_a_name_at_a_clause_start_still_binds_and_a_known_person_beats_the_list():
    assert _name_terms("Ivo: what has he been working on?", frozenset()) == {"ivo"}
    assert _name_terms("Will, what have you been up to?", frozenset()) == frozenset()
    assert _name_terms("Will, what have you been up to?", frozenset(), frozenset({"will"})) == {"will"}
    assert _name_terms("What has Will been up to?", frozenset()) == {"will"}


def test_u1_the_opener_list_is_closed_and_pinned():
    # A word joins or leaves by amendment: this test fails on either.
    assert SENTENCE_STARTERS == frozenset({
        "what", "who", "whom", "whose", "when", "where", "why", "how", "which", "is", "are", "was", "were", "am",
        "has", "have", "had", "do", "does", "did", "can", "could", "will", "would", "should", "shall", "may",
        "might", "must", "tell", "show", "give", "list", "describe", "summarize", "summarise", "explain", "cite",
        "separate", "only", "ignore", "if", "please", "and", "but", "or", "so", "also", "any", "in", "on", "at",
        "for", "from", "about", "since", "during", "after", "before", "the", "a", "an", "this", "that", "these",
        "those", "there", "here", "name", "compare", "include", "answer", "say", "write", "find", "not", "no",
        "yes", "don", "according", "besides", "other", "lately", "recently", "today", "yesterday", "now", "then",
        "hey", "hi", "hello", "thanks", "thank", "ok", "okay", "oh", "well", "sorry", "quick", "just", "anything",
        "something", "everything", "nothing", "morning", "evening", "afternoon", "honestly", "actually",
        "basically", "right", "sure",
        "remind", "share", "recap", "update", "catch", "let", "help", "check", "note", "walk", "go", "get", "see",
        "look", "keep", "bring", "fill", "run", "talk",
        "we", "you", "he", "she", "they", "it", "my", "our", "your", "his", "her", "their", "its", "me", "us",
        "them", "someone", "anyone", "everyone", "somebody", "anybody", "everybody",
        "last", "next", "earlier", "later", "first", "overall", "finally", "again", "meanwhile", "previously",
        "currently", "tomorrow", "tonight", "week", "month", "year", "lastly", "second", "third"})
    assert {"ivo", "mark", "wrenna"}.isdisjoint(SENTENCE_STARTERS)


# --- U2 and the gap -----------------------------------------------------------------------------------------------

def _node(*, attested=True):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT, canonical_name TEXT, aliases_json TEXT,
            is_self INTEGER, contact_id TEXT);
        CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT, known_usernames_json TEXT,
            is_self INTEGER);
        CREATE TABLE entity_blackholes (blackhole_id TEXT, entity_id TEXT, normalized_name TEXT, canonical_name TEXT,
            aliases_json TEXT);
    """)
    conn.execute("INSERT INTO entities VALUES ('ent-self','person','Wrenna Calloway','[]',1,'c-self')")
    conn.execute("INSERT INTO entities VALUES ('ent-dup','person','Wrennie Selfrow','[]',1,NULL)")   # unattested self
    conn.execute("INSERT INTO entities VALUES ('ent-ivo','person','Ivo Brandt',?,0,NULL)", (json.dumps(["Ivi"]),))
    conn.execute("INSERT INTO entities VALUES ('ent-topic','topic','Week Report',?,0,NULL)", (json.dumps(["three"]),))
    conn.execute("INSERT INTO contacts VALUES ('c-self','Wrenna C.','[]',0)")
    conn.execute("INSERT INTO contacts VALUES ('c-tam','Tamsin Ostrey',?,0)", (json.dumps(["first_again"]),))
    conn.execute("INSERT INTO contacts VALUES ('c-name','Wrenna Fairholt','[]',0)")    # shares the owner's first name
    conn.execute("INSERT INTO entity_blackholes VALUES ('b1','','odrin vale','Odrin Vale',?)", (json.dumps(["Odrin Vale"]),))
    return conn


def test_u2_people_are_person_entities_contact_names_and_off_limits_names_only(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})
    people = people_words(_node())
    assert {"ivo", "brandt", "ivi", "tamsin", "ostrey", "fairholt", "odrin", "vale"} <= people
    assert not {"week", "report", "three"} & people            # a topic is nobody
    assert not {"first", "again", "first_again"} & people      # a handle is no name
    assert not {"wrennie", "selfrow"} & people                 # a self row, attested or not, is nobody
    assert "calloway" not in people                            # the owner's own word, nobody else's


def test_u2_an_ordinary_lower_case_word_binds_nothing_from_a_topic_or_a_handle(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})
    people = people_words(_node())
    for mode in MODES:
        checked = _answer("What has the owner been working on this week?", [ITEM],
                          "They are building the lantern project [1].", mode, people=people)
        assert checked.body.outcome == "answered", mode


def test_gap_a_word_the_owner_and_another_party_carry_binds_in_lower_case(monkeypatch):
    from topos.permissions_v2 import identity
    monkeypatch.setattr(identity, "attested_subjects", lambda conn: {"ent-self"})
    conn = _node()
    owner, people = owner_party_words(conn), people_words(conn)
    assert "wrenna" not in owner and "wrenna" in people and "calloway" in owner
    assert _name_terms("what has wrenna been up to lately?", owner, people) == {"wrenna"}
    for mode in MODES:
        checked = _answer("what has wrenna been up to lately?", [ITEM], "They are building the lantern project [1].",
                          mode, owner_words=owner, people=people)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode


# --- N1 ------------------------------------------------------------------------------------------------------------

def test_n1_every_cited_item_must_name_the_person():
    named = _message("b", "Ivo and I worked on the lantern project build.")
    wrist = _message("c", "Physio says my wrist needs rest from work for four weeks.")
    for mode in MODES:
        checked = _answer("What has Ivo been working on lately?", [named, wrist],
                          "Ivo worked on the build and needs wrist rest [1, 2].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, mode
        checked = _answer("What has Ivo been working on lately?", [named, wrist],
                          "Ivo helped with the lantern build [1].", mode)
        assert checked.body.outcome == "answered", mode


# --- N2 ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("asked,carried", [("Williams", "William"), ("Adams", "Adam"), ("Roberts", "Robert"),
                                           ("Browning", "Brown"), ("William", "Williams")])
def test_n2_a_name_matches_exactly_on_both_sides(asked, carried):
    item = _message("d", f"Met {carried} at the studio about the lantern project.")
    exact = _message("e", f"Met {asked} at the studio about the lantern project.")
    possessive = _message("f", f"Borrowed {asked}'s ladder for the lantern project.")
    for mode in MODES:
        checked = _answer(f"What has {asked} been doing lately?", [item], "A studio meeting happened [1].", mode)
        assert checked.body.outcome == "no_answer" and checked.dropped_relevance == 1, (asked, mode)
        assert _answer(f"What has {asked} been doing lately?", [exact], "A studio meeting happened [1].",
                       mode).body.outcome == "answered", (asked, mode)
        assert _answer(f"What has {asked} been doing lately?", [possessive], "A ladder was borrowed [1].",
                       mode).body.outcome == "answered", (asked, mode)


@pytest.mark.parametrize("opener", ["Honestly", "Previously", "Meanwhile", "Actually"])
def test_u1_a_long_opener_is_no_anchor(opener):
    from topos.permissions_v2.answer_generation import question_lacks_permitted_anchor
    question = f"{opener}, what has the owner been working on?"
    prompt = build_prompt(question, [ITEM], precision="none")
    assert not question_lacks_permitted_anchor(prompt), opener
    # The same word as a subject inside the question still anchors.
    assert question_lacks_permitted_anchor(build_prompt("What did the owner say previously about meanwhile plans?",
                                                        [ITEM], precision="none"))


def test_u1_an_opener_never_rescues_an_absent_subject():
    from topos.permissions_v2.answer_generation import question_lacks_permitted_anchor
    prompt = build_prompt("Honestly, what did the owner say about shadowglass?", [ITEM], precision="none")
    assert question_lacks_permitted_anchor(prompt)
