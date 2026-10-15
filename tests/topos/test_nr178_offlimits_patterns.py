"""NR178: the regular expressions built from the Off-limits list live as long as the request or the pass that made them.

Until NR178 `blackhole._name_pattern`, `blackhole._identifier_pattern` and `blackhole_rebuild._term_pattern` were
module-level `functools.lru_cache`s keyed on sets of Off-limits names and identifiers, and each compiled its pattern
with `re.compile`, whose own process-wide cache (`re._cache`, and `re._cache2` from 3.12) keeps the pattern TEXT, so
the names stayed in the process whatever the caller did. Owner ruling of 15 Oct: nothing derived from the Off-limits
list or excluded entities outlives a request.

protects:
  - none of the three keeps a process-wide cache, and no module global of the lifecycle modules holds a pattern;
  - after a reader has matched and been dropped, no key of any `re` cache and no module global holds the names;
  - `compile_uncached` matches exactly as `re.compile` does;
  - the clean-up keeps a term set's pattern for one pass only (`term_patterns`), emptied and marked ended on the way
    out, a refusal included; outside a pass it compiles afresh; both the clean-up and its preview run inside one;
  - `identifiers_found` makes its reader once per `OffLimitsTerms`.
Every person here is invented.
"""
from __future__ import annotations

import contextvars
import functools
import gc
import re
import types

import pytest

from topos.features.lifecycle import blackhole, blackhole_rebuild, uncached_patterns
from topos.features.lifecycle.blackhole import OffLimitsTerms
from topos.features.lifecycle.uncached_patterns import compile_uncached

pytestmark = pytest.mark.public

MODULES = (blackhole, blackhole_rebuild, uncached_patterns)
NAMES = {"quorvane zibbet", "zib", "talvoren"}
IDENTIFIERS = {"zibhandle", "qz-7731"}
OTHER = {"wendell quarry", "ilse", "rul"}   # another term set of the same size
WORDS = ("quorvane", "zibbet", "talvoren", "zibhandle", "zib")   # "zib" is the one name `_name_pattern` takes here


def held_by_re(word: str) -> list:
    """The `re` caches (every attribute of `re` whose name starts `_cache`) with a key holding `word`. (From 3.12
    `Pattern.sub` with a backslash template also keeps the Pattern in `re._compile_template`'s lru_cache; these
    patterns are only ever `.search`ed.)"""
    found = []
    for name in dir(re):
        if not name.startswith("_cache"):
            continue
        for key in list(getattr(re, name)):
            parts = key if isinstance(key, tuple) else (key,)
            if any(isinstance(part, str) and word in part for part in parts):
                found.append(name)
    return found


def held_by_modules(word: str) -> list:
    """Where a global of the lifecycle modules (and what it holds, to a few levels; a function's attributes and
    closure; a compiled pattern's text) holds `word`."""
    found, seen = [], set()

    def walk(value, path, depth):
        if id(value) in seen or depth > 6:
            return
        seen.add(id(value))
        if isinstance(value, str):
            if word in value:
                found.append(path)
        elif isinstance(value, re.Pattern):
            walk(value.pattern, f"{path}.pattern", depth + 1)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                walk(key, f"{path}[key]", depth + 1)
                walk(item, f"{path}[]", depth + 1)
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

    for module in MODULES:
        for name, value in list(vars(module).items()):
            if not name.startswith("__"):
                walk(value, f"{module.__name__}.{name}", 0)
    return found


def test_the_invented_names_are_held_nowhere_to_begin_with():
    for word in WORDS:
        assert held_by_modules(word) == [], word


def test_no_pattern_function_keeps_a_process_wide_cache():
    for module, name in ((blackhole, "_name_pattern"), (blackhole, "_identifier_pattern"),
                         (blackhole_rebuild, "_term_pattern")):
        function = getattr(module, name)
        assert not isinstance(function, functools._lru_cache_wrapper) and not hasattr(function, "cache_info"), name
    for module in MODULES:
        cached = [name for name, value in vars(module).items() if isinstance(value, functools._lru_cache_wrapper)]
        assert cached == [], module.__name__


def test_a_reader_leaves_nothing_of_the_names_after_it():
    re.purge()
    # `loose=set()`: an entry the owner made (`terms_of`), whose short names are read as whole words (`_name_pattern`).
    terms = OffLimitsTerms(NAMES, IDENTIFIERS, loose=set())
    assert terms._named is not None and terms._whole is not None
    assert terms.found("we met zib yesterday") == "zib"
    assert terms.found("zibra crossing") is None
    assert terms.found("quorvane zibbet called") is not None
    assert terms.found("same samples") is None
    assert terms.identifiers_found("ping zibhandle now") == "zibhandle"
    assert blackhole._identifier_pattern(frozenset(IDENTIFIERS)).search("zibhandle")
    del terms
    gc.collect()
    for word in WORDS:
        assert held_by_re(word) == [], word
        assert held_by_modules(word) == [], word


def test_a_clean_up_pass_keeps_its_patterns_for_the_pass_only(monkeypatch):
    re.purge()
    made = []
    raw = blackhole_rebuild._term_pattern
    monkeypatch.setattr(blackhole_rebuild, "_term_pattern", lambda terms: made.append(terms) or raw(terms))
    texts = ["We met Zib.", "nothing here", "a talvoren note", "same samples"]
    with blackhole_rebuild.term_patterns():
        memory = blackhole_rebuild._PASS_PATTERNS.get()
        found = [blackhole_rebuild._mentions(text, NAMES) for text in texts]
        assert len(made) == 1 and frozenset(NAMES) in memory
        assert blackhole_rebuild._mentions("We met Ilse.", OTHER) and not blackhole_rebuild._mentions("We met Ilse.", NAMES)
        assert len(made) == 2
    assert found == [True, False, True, False]
    assert list(memory) == [blackhole_rebuild._ENDED]
    assert blackhole_rebuild._PASS_PATTERNS.get() is None
    assert [blackhole_rebuild._mentions(text, NAMES) for text in texts] == found
    assert len(made) == 2 + len(texts)   # outside a pass, compiled afresh at every call
    monkeypatch.undo()                     # the counter above holds what it saw
    made.clear()
    del memory
    gc.collect()
    for word in WORDS:
        assert held_by_re(word) == [], word
        assert held_by_modules(word) == [], word


def test_a_task_started_inside_a_pass_keeps_nothing_after_it(monkeypatch):
    with blackhole_rebuild.term_patterns():
        copied = contextvars.copy_context()
        memory = blackhole_rebuild._PASS_PATTERNS.get()
    made = []
    raw = blackhole_rebuild._term_pattern
    monkeypatch.setattr(blackhole_rebuild, "_term_pattern", lambda terms: made.append(terms) or raw(terms))
    for _ in range(2):
        assert copied.run(blackhole_rebuild._mentions, "We met Zib.", NAMES)
    assert len(made) == 2 and list(memory) == [blackhole_rebuild._ENDED]


def test_a_pass_is_reset_after_a_refusal():
    with pytest.raises(RuntimeError):
        with blackhole_rebuild.term_patterns():
            raise RuntimeError("refused")
    assert blackhole_rebuild._PASS_PATTERNS.get() is None


def test_a_pass_inside_a_pass_uses_the_outer_one():
    with blackhole_rebuild.term_patterns():
        outer = blackhole_rebuild._PASS_PATTERNS.get()
        with blackhole_rebuild.term_patterns():
            assert blackhole_rebuild._PASS_PATTERNS.get() is outer
        assert blackhole_rebuild._PASS_PATTERNS.get() is outer and blackhole_rebuild._ENDED not in outer


@pytest.mark.parametrize("entry", ["rebuild_for_blackhole", "preview_for_blackhole"])
def test_the_clean_up_and_its_preview_run_inside_one_pass(monkeypatch, entry):
    seen = []

    class Probe:
        def __init__(self, conn):
            seen.append(blackhole_rebuild._PASS_PATTERNS.get())
            raise RuntimeError("probe")

    monkeypatch.setattr(blackhole_rebuild, "BlackholeStore", Probe)
    with pytest.raises(RuntimeError):
        getattr(blackhole_rebuild, entry)(None, "zib")
    assert len(seen) == 1 and isinstance(seen[0], dict)
    assert list(seen[0]) == [blackhole_rebuild._ENDED]
    assert blackhole_rebuild._PASS_PATTERNS.get() is None


def test_identifiers_found_makes_its_reader_once_per_object(monkeypatch):
    terms = OffLimitsTerms(NAMES, IDENTIFIERS)
    made = []
    raw = blackhole._identifier_pattern
    monkeypatch.setattr(blackhole, "_identifier_pattern", lambda identifiers: made.append(identifiers) or raw(identifiers))
    assert terms.identifiers_found("ping zibhandle now") == "zibhandle"
    assert terms.identifiers_found("nothing") is None
    assert terms.identifiers_found("qz-7731 here") == "qz-7731"
    assert len(made) == 1
    assert OffLimitsTerms(NAMES).identifiers_found("zibhandle") is None


@pytest.mark.parametrize("pattern", [
    r"(?<![^\W_])(?:quorvane\s+zibbet|zib(?:s)?)(?:[ʼ']s)?(?![^\W_])",
    r"(?<![^\W_])(?:zibhandle|qz\-7731)(?![^\W_])",
    r"(?:王\s*伟)",
    r"(?<![^\W_])(?:talvoren)(?![^\W_])|(?:田\s*中)",
])
def test_an_uncached_pattern_matches_as_re_compile_does(pattern):
    texts = ["zib", "zibs car", "same", "quorvane  zibbet", "我和王伟吃饭", "zibʼs", "x_zib", "Zib", "zibhandle!",
             "qz-7731", "talvorens", "田中さん", ""]
    fresh, cached = compile_uncached(pattern), re.compile(pattern)
    for text in texts:
        a, b = fresh.search(text), cached.search(text)
        assert (a and (a.span(), a.group(0))) == (b and (b.span(), b.group(0))), text
    assert type(fresh) is type(cached) and fresh.pattern == cached.pattern and fresh.flags == cached.flags
    assert compile_uncached("ab", re.I).search("AB")
