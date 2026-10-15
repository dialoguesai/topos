"""NR175: the forms of the Off-limits vocabulary are remembered per boundary, and never by the process.

Until NR175, 19 functions of `entity_boundary` were module-level `functools.lru_cache`s keyed on the names and
identifiers the owner protects (and `loose` on the words of the texts it read too), so a name the owner took off the
list, and its forms, stayed in memory until the node stopped. The owner's rule: nothing derived from the owner's text
outlives the request it was read for (ruling of 14 Oct 2026 on NR175_PROPOSAL.md: design B).

protects:
  - no function of the module keeps a process-wide cache, and after a boundary has matched and been dropped no module
    global, function attribute or closure holds a protected term;
  - the forms of a term set are kept on the boundary that matched (`_forms`), the context variable is reset on every
    way out of a match, a refusal included, and the switch turns the memory off;
  - each kept form answers only for its own key: two term sets in one scope get their own forms;
  - after the vocabulary changes, a new boundary matches the new vocabulary and keeps no entry of the old one;
  - `loose` (the words of texts) is remembered inside a `one_pass` only, counted against the pass's limit and emptied
    with it;
  - a match made with no boundary (the message review's floors, ingestion's name check) remembers only inside its
    `forms_memory` block, and a context copied inside the block (a task it started) keeps nothing after it;
  - two functions never share a table, whatever their names;
  - the same answers with the memory on and off.
Every person here is invented.
"""
from __future__ import annotations

import functools
import gc
import json
import sqlite3
import types

import pytest

from tests.permissions_v2.test_entity_boundary_v8 import SCHEMA
from topos.permissions_v2 import entity_boundary
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.common_given_names import GIVEN_NAMES
from topos.permissions_v2.english_short_words import WORDS_2_3, WORDS_4
from topos.permissions_v2.entity_boundary import EntityBoundary

pytestmark = pytest.mark.public

PER_SET = ("_variants", "_word_variants", "_inflections", "_named", "_short_named", "_v8_short", "_v8_long",
           "_v10_short", "_v10_long", "unspaced_terms")
PER_WORD = ("short_variants", "name_word_variants", "inflected_forms", "named_forms", "short_named_forms", "v8_forms",
            "celtic_forms", "v10_forms")
#: Invented protected people: a long name with a short alias each. None of these is a given name or English word.
FIRST = ("Quorvane Zibbet", ["Zib"])
SECOND = ("Talvoren Rulmask", ["Rul"])


def boundary_over(*people):
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    for index, (name, aliases) in enumerate(people):
        conn.execute("INSERT INTO entity_blackholes VALUES(?,?,?,?,?)",
                     (f"b{index}", "", name.lower(), name, json.dumps(aliases)))
    return EntityBoundary(conn)


def holders_of(term: str) -> list:
    """Where, in the module's globals, its functions' attributes and closures, and any container they reach, `term`
    is held (a frozenset key, a tuple, a dict's key or value): as a whole string, and for a term of five letters or
    more also inside a longer one (one of its forms). A shorter term is inside ordinary words of the module's tables
    ("rul" in "rule")."""
    found, seen = [], set()

    def walk(value, path, depth):
        if id(value) in seen or depth > 6:
            return
        seen.add(id(value))
        if isinstance(value, str):
            if value.casefold() == term or (len(term) >= 5 and term in value.casefold()):
                found.append(path)
            return
        if isinstance(value, dict):
            for key, item in list(value.items()):
                walk(key, f"{path}[key]", depth + 1)
                walk(item, f"{path}[{key!r:.40}]", depth + 1)
        elif isinstance(value, (set, frozenset, tuple, list)):
            for item in list(value):
                walk(item, f"{path}[]", depth + 1)
        elif isinstance(value, types.FunctionType):
            walk(value.__dict__, f"{path}.__dict__", depth + 1)
            for index, cell in enumerate(value.__closure__ or ()):
                try:
                    walk(cell.cell_contents, f"{path}.<closure {index}>", depth + 1)
                except ValueError:
                    pass
            walk(getattr(value, "__wrapped__", None), f"{path}.__wrapped__", depth + 1)
        elif isinstance(value, type) and value.__module__ == entity_boundary.__name__:
            for name, item in list(vars(value).items()):
                walk(item, f"{path}.{name}", depth + 1)

    for name, value in list(vars(entity_boundary).items()):
        if not name.startswith("__"):
            walk(value, name, 0)
    return found


@pytest.fixture(autouse=True)
def _memory_on():
    entity_boundary.FORMS_MEMORY = True
    yield
    entity_boundary.FORMS_MEMORY = True


def test_the_invented_names_are_in_no_table():
    """Precondition for the scans below: the module's own tables never hold these terms."""
    for term in ("zib", "rul", "quorvane", "zibbet", "talvoren", "rulmask"):
        assert term not in GIVEN_NAMES and term not in WORDS_2_3 and term not in WORDS_4
        assert holders_of(term) == []


def test_no_function_keeps_a_process_wide_cache():
    cached = [name for name, value in vars(entity_boundary).items()
              if isinstance(value, functools._lru_cache_wrapper) or hasattr(value, "cache_info")]
    assert cached == []
    for name in PER_SET + PER_WORD + ("loose",):
        assert callable(getattr(entity_boundary, name))


def test_nothing_of_the_vocabulary_stays_in_the_module_after_a_match():
    boundary = boundary_over(FIRST)
    with boundary.one_pass():
        assert boundary.mentions_protected("Zibby and Quorvanen came over.")
        assert boundary.mentions_protected("We met Quorvanen today.")
    assert boundary.mentions_protected("Zibby called.")
    assert any("zib" in key for table in boundary._forms.values() for key in table)
    del boundary
    gc.collect()
    for term in ("zib", "quorvane", "zibbet"):
        assert holders_of(term) == [], term


def test_the_forms_are_kept_on_the_boundary_that_matched():
    boundary = boundary_over(FIRST)
    assert entity_boundary._BOUNDARY_FORMS.get() is None
    assert boundary.mentions_protected("Zibby called.")
    assert entity_boundary._BOUNDARY_FORMS.get() is None
    kept = boundary._forms
    variants, unspaced = entity_boundary._variants.__wrapped__, entity_boundary.unspaced_terms.__wrapped__
    assert variants in kept and unspaced in kept
    assert all(isinstance(key, frozenset) for table in kept.values() for key in table)
    assert any(key == frozenset({"zib"}) for key in kept[variants])
    # a rebound copy (another read of the same rows) shares the closure and so its forms
    assert boundary.rebind(sqlite3.connect(":memory:"))._forms is kept


def test_the_variable_is_reset_after_a_refusal(monkeypatch):
    boundary = boundary_over(FIRST)

    def refuse(self, row, name_parts, bare_parts_anywhere):
        assert entity_boundary._BOUNDARY_FORMS.get() is self._forms
        raise PolicyError(entity_boundary.UNAVAILABLE)

    monkeypatch.setattr(EntityBoundary, "_hits_read", refuse)
    with pytest.raises(PolicyError):
        boundary.mentions_protected("anything")
    assert entity_boundary._BOUNDARY_FORMS.get() is None


def test_the_switch_turns_the_memory_off():
    entity_boundary.FORMS_MEMORY = False
    boundary = boundary_over(FIRST)
    assert boundary.mentions_protected("Zibby called.")
    assert boundary._forms == {}


def test_each_key_has_its_own_forms():
    memory = {}
    token = entity_boundary._BOUNDARY_FORMS.set(memory)
    try:
        for name in PER_SET:
            function = getattr(entity_boundary, name)
            first, second = frozenset({"zib", "quorvane"}), frozenset({"rul", "talvoren"})
            assert function(first) == function.__wrapped__(first)
            assert function(second) == function.__wrapped__(second)
            assert function(first) == function.__wrapped__(first)
    finally:
        entity_boundary._BOUNDARY_FORMS.reset(token)
    assert len(memory[entity_boundary._variants.__wrapped__]) == 2


def test_a_changed_vocabulary_is_read_as_changed():
    """The owner protects Zib, then takes Zib off and protects Rul: the new boundary withholds Rul's forms, releases
    Zib's, and keeps no entry made of the old vocabulary."""
    before = boundary_over(FIRST)
    assert before.mentions_protected("Zibby called.")
    assert not before.mentions_protected("Rully called.")
    after = boundary_over(SECOND)
    assert after.mentions_protected("Rully called.")
    assert not after.mentions_protected("Zibby called.")
    assert not any("zib" in key for table in after._forms.values() for key in table)
    assert before.mentions_protected("Zibby called.")


def test_loose_remembers_inside_a_pass_only():
    boundary = boundary_over(FIRST)
    assert entity_boundary.loose("quorvanen") == entity_boundary._loose("quorvanen")
    assert holders_of("quorvanen") == []
    with boundary.one_pass():
        memory = entity_boundary._PASS_NORMALIZER.get()
        assert boundary.mentions_protected("We met Quorvanen today.")
        assert "quorvanen" in memory.loose
        assert memory.chars >= len("quorvanen")
    assert memory.loose == {} and not memory.open
    assert holders_of("quorvanen") == []


def test_loose_keeps_to_the_pass_limit(monkeypatch):
    monkeypatch.setattr(entity_boundary._Normalizer, "LIMIT", 0)
    boundary = boundary_over(FIRST)
    with boundary.one_pass():
        memory = entity_boundary._PASS_NORMALIZER.get()
        assert boundary.mentions_protected("We met Quorvanen today.")
        assert memory.loose == {}


@pytest.mark.parametrize("text", ["Zibby called.", "We met Quorvanen today.", "Rully called.", "zib", "Zibs car",
                                  "nothing here", "Zibbet's notes", "QUORVANE"])
def test_the_same_answers_with_the_memory_off(text):
    answers = []
    for memory in (False, True, True):
        entity_boundary.FORMS_MEMORY = memory
        boundary = boundary_over(FIRST, SECOND)
        answers.append([boundary.mentions_protected(text), boundary._hits({"content": text}),
                        boundary._hits({"content": text}, True, True)])
    assert answers[0] == answers[1] == answers[2]


def test_a_direct_match_remembers_inside_its_block_only(monkeypatch):
    """`forms_memory`: a caller of `text_hits` with no boundary (the message review's floors) makes a term set's forms
    once for several texts, and keeps nothing after the block."""
    made = []
    raw = entity_boundary.v8_forms
    monkeypatch.setattr(entity_boundary, "v8_forms", lambda term: made.append(term) or raw(term))
    short, long_terms = entity_boundary.split_terms({"zib", "quorvanezibbet"})
    texts = ["Zibby called.", "nothing", "We met them.", "Zibs car", "later"]
    with entity_boundary.forms_memory():
        memory = entity_boundary._BOUNDARY_FORMS.get()
        assert isinstance(memory, dict)
        found = [entity_boundary.text_hits(text, short, long_terms) for text in texts]
    assert entity_boundary._BOUNDARY_FORMS.get() is None
    assert made == ["zib"]
    assert found == [entity_boundary.text_hits(text, short, long_terms) for text in texts]
    del memory
    monkeypatch.undo()   # the counter above holds what it saw
    made.clear()
    assert holders_of("zib") == []


def test_a_block_inside_a_match_uses_the_boundarys_memory():
    boundary = boundary_over(FIRST)
    with boundary._forms_scope():
        with entity_boundary.forms_memory():
            assert entity_boundary._BOUNDARY_FORMS.get() is boundary._forms
        assert entity_boundary._BOUNDARY_FORMS.get() is boundary._forms
    assert entity_boundary._BOUNDARY_FORMS.get() is None


def test_a_block_is_reset_after_a_raise():
    with pytest.raises(RuntimeError):
        with entity_boundary.forms_memory():
            raise RuntimeError("refused")
    assert entity_boundary._BOUNDARY_FORMS.get() is None


def test_the_review_floors_match_inside_one_memory(monkeypatch):
    from topos.permissions_v2 import automatic_message_review as review
    seen = []

    def probe(labels, inputs):
        seen.append(entity_boundary._BOUNDARY_FORMS.get())
        return labels

    monkeypatch.setattr(review, "apply_floors", probe)
    labels = object()
    assert review.apply_family_floors("conversation_messages", labels, {"target": "x"}) is labels
    assert len(seen) == 1 and isinstance(seen[0], dict)
    assert entity_boundary._BOUNDARY_FORMS.get() is None


def test_a_task_started_inside_a_block_keeps_nothing_after_it(monkeypatch):
    """A context copied inside the block (as `loop.create_task` copies it) still holds the block's dict when the block
    has ended: it finds it emptied and marked ended, and computes in full."""
    import contextvars
    short, long_terms = entity_boundary.split_terms({"zib", "quorvanezibbet"})
    with entity_boundary.forms_memory():
        copied = contextvars.copy_context()
        memory = entity_boundary._BOUNDARY_FORMS.get()
        entity_boundary.text_hits("Zibby called.", short, long_terms)
        assert memory
    assert list(memory) == [entity_boundary._ENDED]
    made = []
    raw = entity_boundary.short_variants
    monkeypatch.setattr(entity_boundary, "short_variants", lambda term: made.append(term) or raw(term))
    for _ in range(2):
        assert copied.run(entity_boundary.text_hits, "Zibby called.", short, long_terms)
    assert len(made) == 2
    assert list(memory) == [entity_boundary._ENDED]


def test_two_functions_of_one_name_have_their_own_tables():
    def first(key):
        return ("first", key)

    def second(key):
        return ("second", key)

    second.__name__ = first.__name__
    first, second = entity_boundary._per_boundary(first), entity_boundary._per_boundary(second)
    with entity_boundary.forms_memory():
        assert first(frozenset({"zib"})) == ("first", frozenset({"zib"}))
        assert second(frozenset({"zib"})) == ("second", frozenset({"zib"}))


def test_ingestion_matches_inside_one_memory(monkeypatch):
    from topos.features.lifecycle import blackhole_llm
    seen, raw = [], entity_boundary.text_hits

    def probe(*args, **kwargs):
        seen.append(entity_boundary._BOUNDARY_FORMS.get())
        return raw(*args, **kwargs)

    monkeypatch.setattr(entity_boundary, "text_hits", probe)
    assert blackhole_llm._boundary_names_hit(["Zib Quorvane", "Zib"], "Zibby called.") is True
    assert blackhole_llm._boundary_names_hit(["Zib"], "Nobody called.") is False
    assert len(seen) == 2 and all(isinstance(memory, dict) for memory in seen)
    assert all(list(memory) == [entity_boundary._ENDED] for memory in seen)
    assert entity_boundary._BOUNDARY_FORMS.get() is None
