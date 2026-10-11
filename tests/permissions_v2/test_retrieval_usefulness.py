"""Retrieval quality on unrelated synthetic nodes, without a model or owner lane."""
from collections import Counter

import pytest

from topos.permissions_v2.search_index import LoadedIndex, Member, tokenize
from topos.permissions_v2.search_lanes import rank


def index_for(texts):
    members = []
    for number, text in enumerate(texts):
        terms = Counter(tokenize(text))
        members.append(Member(f"r.{number:064x}", 1_000_000, sum(terms.values()), dict(terms), b""))
    return LoadedIndex({}, None, None, tuple(members), {})


@pytest.mark.parametrize("question,text", [
    ("What projects?", "I finished a project."),
    ("Which hobby?", "My hobbies include pottery."),
    ("What classes?", "I enrolled in a class."),
    ("What boxes?", "I moved a box."),
    ("Which recipe?", "I saved recipes."),
    ("What studies?", "I completed a study."),
])
def test_plural_variants_retrieve_from_existing_term_bags(question, text):
    index = index_for([text, "A completely unrelated item."])
    assert rank(index, question, None, limit=1, lower_us=0, upper_us=2_000_000) == [index.members[0].opaque_id]


def test_request_words_do_not_outrank_the_question_topic():
    index = index_for(["I can tell you about how I could help.", "The kiln has cooled."])
    assert rank(index, "Can you tell me about the kiln?", None, limit=1,
                lower_us=0, upper_us=2_000_000) == [index.members[1].opaque_id]


@pytest.mark.parametrize("text", ["café", "研究", "проект", "glass", "status", "analysis"])
def test_exact_unicode_and_singular_matches_remain(text):
    index = index_for([text, "unrelated"])
    assert rank(index, text, None, limit=1, lower_us=0, upper_us=2_000_000) == [index.members[0].opaque_id]


def test_absent_topic_does_not_trigger_an_unrelated_fallback():
    index = index_for(["I saved a recipe."])
    assert rank(index, "electroplating", None, limit=8, lower_us=0, upper_us=2_000_000) == []


def test_window_excluded_vocabulary_cannot_expand_a_query_or_change_ranking():
    index = index_for(["I finished a project.", "I read about projects."])
    hidden = index.members[1]
    index = LoadedIndex({}, None, None, (index.members[0],
        Member(hidden.opaque_id, 9_000_000, hidden.doc_len, hidden.terms, b"")), {})
    small = LoadedIndex({}, None, None, (index.members[0],), {})
    kwargs = dict(limit=8, lower_us=0, upper_us=2_000_000)
    assert rank(index, "projects", None, **kwargs) == rank(small, "projects", None, **kwargs)
