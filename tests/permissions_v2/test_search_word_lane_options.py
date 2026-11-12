"""The word lane's two optional readings of a query (carried forward from topos#102, 11 Nov 2026).

`SEARCH_PLURAL_FOLD`: a query word also matches its regular English plural or singular in a member's stored word
counts. `SEARCH_REQUEST_WORDS`: the words a request is phrased with are left out of the query. Both are off by
default, and off is the lexical lane of 1.5.49, score for score. Neither reads anything but the query and the term
bags of the members inside the request's window, so nothing a grant does not permit, and nothing outside the window,
can lend a form or move a score.

Invented data only. No model, and no vectors: these are the word lane's own tests.
"""
from __future__ import annotations

import ast
import math
from collections import Counter
from pathlib import Path

import pytest

from topos.permissions_v2 import search_lanes, search_release, switches
from topos.permissions_v2.search_index import LoadedIndex, Member, tokenize
from topos.permissions_v2.search_lanes import REQUEST_WORDS, _plurals, _variants, lexical, rank

P = switches.PREFIX
FOLD, REQUEST = P + "SEARCH_PLURAL_FOLD", P + "SEARCH_REQUEST_WORDS"
WINDOW = dict(lower_us=0, upper_us=2_000_000)


@pytest.fixture(autouse=True)
def switches_unset(monkeypatch):
    monkeypatch.delenv(FOLD, raising=False)
    monkeypatch.delenv(REQUEST, raising=False)


def index_for(texts, times=None):
    members = []
    for number, text in enumerate(texts):
        terms = Counter(tokenize(text))
        when = 1_000_000 if times is None else times[number]
        members.append(Member(f"r.{number:064x}", when, sum(terms.values()), dict(terms), b""))
    return LoadedIndex({}, None, None, tuple(members), {})


def top(index, question, **options):
    return rank(index, question, None, limit=1, **WINDOW, **options)


# --- the switches ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [FOLD, REQUEST])
def test_each_switch_is_off_bound_and_unbound_and_on_is_one_environment_line(name, monkeypatch):
    item = switches.BY_NAME[name]
    assert item.kind == "bool" and item.unbound is False and item.bound is False
    assert switches.value(name) is False
    monkeypatch.setenv(name, "true")
    assert switches.value(name) is True


def test_the_release_hands_the_lane_what_the_switches_say(monkeypatch):
    assert search_release.lexical_options() == {"plural_fold": False, "drop_request_words": False}
    monkeypatch.setenv(FOLD, "true")
    assert search_release.lexical_options() == {"plural_fold": True, "drop_request_words": False}
    monkeypatch.setenv(REQUEST, "true")
    assert search_release.lexical_options() == {"plural_fold": True, "drop_request_words": True}
    # The options are exactly the lane's own keywords: a renamed one fails here, not at the first live search.
    assert rank(index_for(["a kiln"]), "kiln", None, limit=1, **WINDOW, **search_release.lexical_options())


def test_every_rank_of_the_release_passes_the_word_options():
    """Named search, batch and both ranks of an answer's retrieval: a `rank` call written without the options would
    read the query as before whatever the switches say, on that path alone."""
    tree = ast.parse(Path(search_release.__file__).read_text())
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "rank"]
    assert len(calls) == 4
    for call in calls:
        starred = [keyword.value for keyword in call.keywords if keyword.arg is None]
        assert len(starred) == 1, ast.unparse(call)
        value = starred[0]
        assert (isinstance(value, ast.Name) and value.id == "words") or ast.unparse(value) == "lexical_options()"
    # `words` is `lexical_options()` wherever it is bound.
    bound = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == "words" for target in node.targets)]
    assert len(bound) == 2 and all(ast.unparse(node.value) == "lexical_options()" for node in bound)


# --- off: the lexical lane of 1.5.49 ---------------------------------------------------------------------------------

def bm25_of_1_5_49(index, query):
    """`search_lanes.lexical` as it stood at 1.5.49 (main ce1c165e), copied so that off is pinned score for score."""
    terms = sorted(set(tokenize(query)))
    members = index.members
    if not terms or not members:
        return []
    count = len(members)
    average = sum(member.doc_len for member in members) / count or 1.0
    frequency = {term: sum(1 for member in members if term in member.terms) for term in terms}
    scored = []
    for member in members:
        score = 0.0
        for term in terms:
            tf = member.terms.get(term, 0)
            if not tf:
                continue
            idf = math.log(1.0 + (count - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * (search_lanes.BM25_K1 + 1) / (
                tf + search_lanes.BM25_K1 * (1 - search_lanes.BM25_B + search_lanes.BM25_B * member.doc_len / average))
        if score > 0:
            scored.append((member.opaque_id, score))
    return scored


TEXTS = ["I finished a project for the kiln.", "My hobbies include pottery and projects.", "I enrolled in a class.",
         "I can tell you about how I could help.", "The kiln has cooled. The kiln is empty.", "研究 notes café",
         "Unrelated background about the news.", "I saved recipes and a recipe."]
QUESTIONS = ["What projects?", "Can you tell me about the kiln?", "hobby", "recipe recipes", "news", "研究", "café",
             "Please summarize the classes.", "", "the and of", "electroplating"]


@pytest.mark.parametrize("question", QUESTIONS)
def test_off_is_the_lexical_lane_of_before_score_for_score(question):
    index = index_for(TEXTS)
    assert lexical(index, question) == bm25_of_1_5_49(index, question)
    assert lexical(index, question, plural_fold=False, drop_request_words=False) == bm25_of_1_5_49(index, question)


@pytest.mark.parametrize("question,text", [("What projects?", "I finished a project."),
                                           ("Which hobby?", "My hobbies include pottery.")])
def test_off_a_plural_question_does_not_find_the_singular_item(question, text):
    index = index_for([text, "A completely unrelated item."])
    assert top(index, question) == []


# --- plural forms ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,text", [
    ("What projects?", "I finished a project."),
    ("Which hobby?", "My hobbies include pottery."),
    ("What classes?", "I enrolled in a class."),
    ("What boxes?", "I moved a box."),
    ("Which recipe?", "I saved recipes."),
    ("What studies?", "I completed a study."),
    ("Which sketches?", "I drew a sketch."),
    ("Which activity?", "Activities included rowing."),
    # Forms a fold to one key gets wrong, and making the plural from the singular gets right.
    ("Which movies?", "I watched a movie."),
    ("Which movie?", "I watched two movies."),
    ("What caches?", "I cleared the cache."),
    ("Which buses?", "I took the bus."),
    ("Which lens?", "I cleaned the lenses."),
    ("What potatoes?", "I peeled a potato."),
    ("Which days?", "A quiet day."),
    ("Which cat?", "The cats slept."),
])
def test_a_question_finds_the_item_that_holds_the_other_form(question, text):
    index = index_for([text, "A completely unrelated item."])
    assert top(index, question, plural_fold=True) == [index.members[0].opaque_id]
    assert top(index, question) == []                                  # and not without the switch


@pytest.mark.parametrize("text", ["café", "研究", "проект", "glass", "status", "analysis", "news", "bus", "ai"])
def test_an_exact_word_still_finds_itself(text):
    index = index_for([text, "unrelated"])
    assert top(index, text, plural_fold=True) == [index.members[0].opaque_id]


@pytest.mark.parametrize("question,text", [
    ("news", "A new kiln."), ("new", "The news today."),            # "news" is no plural of "new"
    ("hers", "I met her."), ("her", "The bag is hers."),
    ("glass", "The glas."), ("class", "A clas."),                   # "-ss" is no plural
    ("café", "Two cafés."), ("研究", "研究s"),                       # only ASCII letters take a form
    ("leaves", "A leaf."), ("people", "One person."),               # no irregular plural
    ("planning", "A plan."), ("walked", "A walk."),                 # plural forms only: no stem
    ("katie", "A note for katy."),                                  # "-ie" and "-y" are two spellings, not a plural
    ("ox", "Two oxes."), ("as", "ases"),                            # too short to take a form
])
def test_what_is_no_regular_plural_stays_apart(question, text):
    index = index_for([text, "unrelated"])
    assert top(index, question, plural_fold=True) == []


def test_an_absent_topic_matches_nothing():
    index = index_for(["I saved a recipe.", "I saved recipes."])
    assert rank(index, "electroplating", None, limit=8, **WINDOW, plural_fold=True) == []


WORDS = ["project", "projects", "hobby", "hobbies", "hobbie", "box", "boxes", "boxe", "bus", "buses", "class",
         "classes", "movie", "movies", "movy", "cache", "caches", "cach", "potato", "potatoes", "potatos", "day",
         "days", "status", "statu", "news", "new", "hers", "her", "lens", "lenses", "len", "cat", "cats", "go",
         "goes", "study", "studies", "glass", "glasses", "tie", "ties", "quiz", "quizzes", "ax", "axes", "axe"]


def test_the_relation_is_the_same_in_both_directions():
    for one in WORDS:
        for other in WORDS:
            assert (other in _variants(one)) == (one in _variants(other)), (one, other)


def test_a_form_is_the_word_or_differs_from_it_by_a_regular_plural():
    for word in WORDS:
        forms = _variants(word)
        assert forms[0] == word and len(set(forms)) == len(forms)
        for form in forms[1:]:
            assert form in _plurals(word) or word in _plurals(form), (word, form)


def test_both_forms_in_one_item_or_one_question_are_one_word_counted_once():
    index = index_for(["A project. Two projects.", "A project.", "Unrelated note."])
    both, one = index.members[0].opaque_id, index.members[1].opaque_id
    scores = dict(lexical(index, "project", plural_fold=True))
    # The item holding both forms holds the word twice; it is in two of three items either way.
    assert set(scores) == {both, one} and scores[both] != scores[one]
    assert lexical(index, "project projects", plural_fold=True) == lexical(index, "project", plural_fold=True)
    assert lexical(index, "projects", plural_fold=True) == lexical(index, "project", plural_fold=True)
    # Off, the two forms are two words with their own counts.
    assert lexical(index, "project projects") != lexical(index, "project")


def test_a_member_outside_the_window_lends_no_form_and_moves_no_score():
    """The twin-corpus oracle of `within`, with the fold on: a permitted member outside the request's window holds the
    plural, a rare word and many words, and the ranking and the scores are those of the index without it."""
    inside = ["I finished a project.", "I read about a kiln.", "A second note about a project and a kiln."]
    hidden = "Projects projects projects kiln kilns and a long tail of other words to move the average length."
    with_hidden = index_for(inside + [hidden], times=[1_000_000] * 3 + [9_000_000])
    without = index_for(inside)
    for question in ("projects", "kilns", "project kiln", "What projects?"):
        options = dict(limit=8, **WINDOW, plural_fold=True, drop_request_words=True)
        assert rank(with_hidden, question, None, **options) == rank(without, question, None, **options)
        assert (lexical(search_lanes.within(with_hidden, 0, 2_000_000), question, plural_fold=True)
                == lexical(without, question, plural_fold=True))


def test_the_fold_reads_no_bag_but_the_members_it_is_given():
    """The forms come from the query word alone: no vocabulary is gathered from the index, so a member's other words
    are never read."""
    class Bag(dict):
        looked_up: set = set()

        def get(self, key, default=None):
            Bag.looked_up.add(key)
            return super().get(key, default)

        def __iter__(self):
            raise AssertionError("the lane walked a member's words")

        keys = items = values = __iter__
    index = index_for(["I finished a project.", "Unrelated secretword."])
    members = tuple(Member(member.opaque_id, member.event_at_us, member.doc_len, Bag(member.terms), b"")
                    for member in index.members)
    index = LoadedIndex({}, None, None, members, {})
    assert [opaque for opaque, _score in lexical(index, "projects", plural_fold=True)] == [members[0].opaque_id]
    assert Bag.looked_up == set(_variants("projects")) and "secretword" not in Bag.looked_up


# --- request words ---------------------------------------------------------------------------------------------------

def test_request_words_do_not_outrank_the_question_topic():
    index = index_for(["I can tell you about how I could help.", "The kiln has cooled."])
    question = "Can you tell me about the kiln?"
    assert top(index, question, drop_request_words=True) == [index.members[1].opaque_id]
    assert top(index, question) == [index.members[0].opaque_id]         # off: the request's own words win


def test_an_absent_topic_no_longer_finds_the_item_that_only_shares_the_request():
    index = index_for(["I can tell you about how I could help.", "I saved a recipe."])
    question = "Could you describe the electroplating?"
    assert rank(index, question, None, limit=8, **WINDOW, drop_request_words=True) == []
    assert rank(index, question, None, limit=8, **WINDOW) == [index.members[0].opaque_id]


def test_a_question_made_only_of_request_words_keeps_them():
    index = index_for(["I can tell you about how I could help.", "The kiln has cooled."])
    for question in ("Can you tell me how?", "show", "Please describe"):
        assert (lexical(index, question, drop_request_words=True) == lexical(index, question)), question
    assert top(index, "Can you tell me how?", drop_request_words=True) == [index.members[0].opaque_id]


def test_known_limit_a_subject_that_is_a_request_word_is_left_out_of_the_words():
    """Why this is a switch of its own: it can return fewer permitted items. "show" here is the subject."""
    index = index_for(["We saw the show on Friday.", "The kiln has cooled."])
    question = "What show did they watch?"
    assert top(index, question) == [index.members[0].opaque_id]
    assert top(index, question, drop_request_words=True) == []


def test_the_request_words_are_the_closed_list_of_the_draft_and_only_query_words():
    assert REQUEST_WORDS == {"can", "could", "would", "should", "how", "about", "tell", "please", "give", "show",
                             "summarize", "summarise", "describe"}
    # Index terms are read as stored: an item is still found by a request word it holds when the query keeps it.
    index = index_for(["Please show the summary."])
    assert top(index, "show", drop_request_words=True) == [index.members[0].opaque_id]


def test_both_together_find_the_plural_topic_of_a_request():
    index = index_for(["I can tell you about how I could help.", "I repaired a kiln.", "Unrelated."])
    question = "Could you describe the kilns?"
    assert top(index, question, plural_fold=True, drop_request_words=True) == [index.members[1].opaque_id]
    assert top(index, question) == [index.members[0].opaque_id]


# --- through the real release: named search, batch and an answer's retrieval ------------------------------------------

BODIES = ["Working on the lantern project at the studio.", "Working on the garden shed roof before the rain.",
          "Working on how I could tell the board about it.", "Working on the kiln shelves and the glaze."]


def _twin(tmp_path, answers=None):
    from tests.permissions_v2 import direct_search_twins as twins
    from tests.permissions_v2.test_answer_time_tr162 import DAY, NOW

    def factory():
        raw = twins.knowledge_policy()
        if answers:
            raw["search"]["answers"] = answers
        return raw
    return twins.build(tmp_path / "twin", members=len(BODIES), hidden_facts=0, seed=102, policy_factory=factory,
                       now_override=NOW, event_times=[NOW - DAY - number * 3600 for number in range(len(BODIES))],
                       bodies=BODIES)


def _search(node, label, query, k=5):
    from tests.permissions_v2.message_search_harness import recipient
    payload = {"query": query, "k": k}
    envelope = node._envelope("grant-search", "permissions.v2.search", payload, label)
    with recipient():
        _result, output = node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=label)
    return [record["content"] for record in output["records"]]


def _batch(node, label, queries, k=5):
    from tests.permissions_v2.message_search_harness import recipient
    payloads = [{"query": query, "k": k} for query in queries]
    envelopes = [node._envelope("grant-search", "permissions.v2.search", payload, f"{label}:{number}")
                 for number, payload in enumerate(payloads)]
    with recipient():
        batch = node.search.dispatch_batch(items=[
            {"envelope": envelope.model_dump(), "payload": payload, "request_id": envelope.request_id}
            for envelope, payload in zip(envelopes, payloads)])
    return [[record["content"] for record in output["records"]] for _result, output in batch]


def test_a_named_search_finds_the_other_form_only_with_the_switch(tmp_path, monkeypatch):
    node = _twin(tmp_path)
    assert _search(node, "wl-off", "projects") == []
    monkeypatch.setenv(FOLD, "true")
    assert _search(node, "wl-on", "projects") == [BODIES[0]]
    assert _search(node, "wl-exact", "project") == [BODIES[0]]            # the exact word, as before
    assert _search(node, "wl-absent", "electroplatings") == []


def test_a_batch_finds_the_other_form_only_with_the_switch(tmp_path, monkeypatch):
    node = _twin(tmp_path)
    assert _batch(node, "wl-b-off", ["projects", "roofs"]) == [[], []]
    monkeypatch.setenv(FOLD, "true")
    assert _batch(node, "wl-b-on", ["projects", "roofs"]) == [[BODIES[0]], [BODIES[1]]]


def test_a_named_search_leaves_the_request_words_out_only_with_the_switch(tmp_path, monkeypatch):
    node = _twin(tmp_path)
    question = "Can you tell me about the kiln?"
    # Off: the item that shares "tell" and "about" with the request is found beside the one about the kiln.
    assert sorted(_search(node, "wl-r-off", question)) == sorted([BODIES[2], BODIES[3]])
    monkeypatch.setenv(REQUEST, "true")
    assert _search(node, "wl-r-on", question) == [BODIES[3]]


def test_an_answers_retrieval_reads_the_question_with_the_switches(tmp_path, monkeypatch):
    from tests.permissions_v2.test_answer_time_tr162 import _asked, _bodies
    node = _twin(tmp_path, answers="only")
    question = "Could you describe the projects?"
    body, receipt, prompts = _asked(node, question, lambda prompt: "The owner works on a lantern project [1].")
    # Off: the words find only the item that shares "could" with the request, and the writer is shown that one.
    assert [_bodies(prompt) for prompt in prompts] == [[BODIES[2]]]
    monkeypatch.setenv(FOLD, "true")
    monkeypatch.setenv(REQUEST, "true")
    body, receipt, prompts = _asked(node, question, lambda prompt: "The owner works on a lantern project [1].")
    assert _bodies(prompts[0]) == [BODIES[0]] and receipt["records_used"] == 1
    assert body["outcome"] == "answered"
