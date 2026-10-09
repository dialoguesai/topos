"""R-N152 probes (adversarial review of the 1.5.2 hotfix). Scratch only; proposed as patches in the report.

BL-155: changes that must still drop the index, which the lane's tests do not pin: a participant swapped for another
contact in the same roster row (the count is unchanged), a changed column on the parent row (`context_tag`), an
edited reply ancestor; and the pinned negative that an ancestor's disclosure column is bookkeeping for the revision.
BL-156: a `search_`-prefixed code outside the vocabulary is still `refused`.
Item 3: a capitalised instruction word in the middle of a question is still a name; and (documented, not a hole in
what is released) a question whose only subject is an instruction word has no subject at all.
"""
from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401 -- fixture
from tests.permissions_v2.test_evidence import corpus, edit  # noqa: F401 -- fixture
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.entity_boundary import EntityBoundary
from tests.permissions_v2.test_answer_catalog_shapes import MODES, _checked, _message
from tests.permissions_v2.test_bl155_index_survives_bookkeeping import (PARTICIPANT_UPSERT, _check, _conversations,
    _first_member, _grant, _node, _path, _sweep)
from topos.permissions_v2.answer_checks import INSTRUCTION_WORDS
from topos.permissions_v2.answer_generation import _topic_terms, build_prompt, question_lacks_permitted_anchor
from topos.permissions_v2.answer_release import receipt_reason

STALE_DEPENDENCIES = ["message search index stale (dependencies)"]


def _roster(node, contact_id, role="participant"):
    with sqlite3.connect(node.corpus.path) as conn:
        for conversation_id, _dataset_id, source_id in _conversations(node):
            conn.execute(PARTICIPANT_UPSERT, (conversation_id, twins.DATASET, source_id, contact_id, role))


# --- BL-155 -------------------------------------------------------------------------------------------------------

def test_a_participant_swapped_in_the_same_row_drops(tmp_path, caplog):
    """The roster keeps its size; only `contact_id` changes. A mutant that leaves `contact_id` out of the roster digest
    is caught by the exact-set pin only; this pins the behaviour itself."""
    node = _node(tmp_path)
    _roster(node, "contact-ordinary")
    assert node.rebuild()[_grant(node)] == "ready"
    assert _sweep(node, caplog) == [] and _path(node).exists()
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("UPDATE conversation_participants SET contact_id='contact-swapped' WHERE contact_id='contact-ordinary'")
    assert _sweep(node, caplog) == STALE_DEPENDENCIES
    assert not _path(node).exists()


def test_a_changed_column_on_the_parent_row_drops(tmp_path, caplog):
    """`context_tag` stays in the revision (BL155 §6): a tag written on the conversation row drops the index, while the
    bump that follows it does not."""
    node = _node(tmp_path)
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("ALTER TABLE conversations ADD COLUMN context_tag TEXT")
        conn.execute("ALTER TABLE conversations ADD COLUMN context_tag_source TEXT")
    assert node.rebuild()[_grant(node)] == "ready"
    assert _sweep(node, caplog) == [] and _path(node).exists()
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute("UPDATE conversations SET context_tag='work', context_tag_source='owner'")
    assert _sweep(node, caplog) == STALE_DEPENDENCIES
    assert not _path(node).exists()


def _revision(corpus):
    """`boundary.check(...)` for message-1 on a fresh boundary (what the index seals as the member's context)."""
    with sqlite3.connect(corpus[0].path) as conn:
        cursor = conn.execute("SELECT * FROM conversation_messages WHERE message_id='message-1'")
        row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
        boundary = EntityBoundary(conn)
        assert boundary.active
        return boundary.check(table="conversation_messages", record_id="message-1", source_id="source-1",
                              dataset_id="dataset-1", row=row)


def test_an_edited_reply_ancestor_moves_the_context_revision_and_its_disclosure_column_does_not(protected_corpus):
    """message-1 replies to `other` (unprotected). The ancestor's words are in the revision; its `content_disclosure`
    is review-surface bookkeeping, outside it (the pinned negative); the veto reads that column all the same.
    (The twins fixture cannot carry a reply: its rows are proven against the native snapshot.)"""
    edit(protected_corpus, "INSERT INTO conversation_messages(message_id,dataset_id,source_id,content,is_from_self,owner_user_id,"
         "conversation_id,sender_id) VALUES('other','dataset-1','source-1','An unrelated earlier note.',1,'owner-1','thread-1','owner-handle')")
    edit(protected_corpus, "UPDATE conversation_messages SET reply_to_message_id='other' WHERE message_id='message-1'")
    edit(protected_corpus, "ALTER TABLE conversation_messages ADD COLUMN content_disclosure TEXT")
    sealed = _revision(protected_corpus)
    edit(protected_corpus, "UPDATE conversation_messages SET content_disclosure='clear' WHERE message_id='other'")
    assert _revision(protected_corpus) == sealed                       # bookkeeping on the ancestor: unchanged
    edit(protected_corpus, "UPDATE conversation_messages SET content='An unrelated earlier note, edited.' WHERE message_id='other'")
    assert _revision(protected_corpus) != sealed                       # the ancestor's words: moved
    edit(protected_corpus, "UPDATE conversation_messages SET content_disclosure='Mara Example' WHERE message_id='other'")
    with pytest.raises(PolicyError, match="entity_protected"):         # the veto reads the column the revision leaves out
        _revision(protected_corpus)


# --- BL-156 -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["search_index_with_an_invented_name", "search_release_other", "grant_inactive_x"])
def test_a_code_shaped_like_a_vocabulary_word_is_still_refused(code):
    assert receipt_reason(code) == "refused"


# --- Item 3 ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["Page", "State", "Chronology"])
def test_a_capitalised_instruction_word_in_the_middle_of_a_question_is_still_a_name(word):
    """`_name_terms` was deliberately not changed (N152 §2): a surname such as Page binds as a name at any position
    that is not a clause start, and a sentence citing an item without it is dropped."""
    question = f"What did {word} tell the owner about the lantern project?"
    record = _message("a", "Spent the morning working on the lantern project for the studio.")
    prompt = build_prompt(question, [record], precision="none")
    assert word.casefold() in prompt.name_terms
    for mode in MODES:
        _prompt, checked = _checked(question, record, "They talked about the lantern project [1].", mode)
        assert (checked.body.outcome, checked.dropped_relevance) == ("no_answer", 1), mode


# A question per instruction word in which that word was the only subject under 1.5.1. Documented, not a hole in what
# is released: every cited item was released by the share; under amendment 5 the question has no subject, so the
# relevance check passes any cited sentence, as it does for "What happened?" today (AR152 §4 "recency questions").
ONLY_SUBJECT = {
    "stated": "What have they stated?", "state": "Which state are they in?", "states": "Which states have they been to?",
    "report": "What did the report say?", "guess": "What is their guess?", "inference": "What inference did they draw?",
    "infer": "What did they infer?", "separate": "Have they separated?", "statement": "What did their statement say?",
    "statements": "What statements were made?", "explicit": "What was explicit?", "explicitly": "What was said explicitly?",
    "connect": "Who did they connect with?", "unrelated": "Are they unrelated?", "chronology": "What is the chronology?",
    "establish": "What did they establish?", "establishes": "What establishes that?", "around": "Who is around them?",
    "pursue": "What do they pursue?", "going": "Where are they going?", "browsing": "What were they browsing?",
    "browse": "What do they browse?", "topics": "What topics come up?", "never": "What would they never do?",
    "pages": "What pages did they read?", "page": "Which page was it?",
}


@pytest.mark.parametrize("word", sorted(INSTRUCTION_WORDS))
def test_probe_a_question_whose_only_subject_is_an_instruction_word_has_no_subject(word):
    question = ONLY_SUBJECT[word]
    assert word in question.casefold()
    assert _topic_terms(question) == set(), word
    record = _message("a", "Booked a cottage by the river for the spring.")   # carries none of the question's words
    assert not question_lacks_permitted_anchor(build_prompt(question, [record], precision="none"))
    for mode in MODES:
        _prompt, checked = _checked(question, record, "A spring cottage by a river is booked [1].", mode)
        assert (checked.body.outcome, checked.dropped_relevance) == ("answered", 0), (word, mode)
