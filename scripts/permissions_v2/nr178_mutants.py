"""Mutation run over NR178: the regular expressions built from the Off-limits list kept by the request or the pass that
made them, never by the process. Every mutant weakens one decision and must be killed by at least one real test.

Whether a process-wide cache comes back on `_name_pattern`, `_identifier_pattern` or `_term_pattern`; whether any of
them compiles through `re`'s own cache again, or `compile_uncached` does; whether the clean-up's pass keeps a term
set's pattern under its own terms, is reset, emptied and marked ended, and is opened by the clean-up and by its
preview at all; whether `identifiers_found` keeps its reader on its object; whether a pattern hides where a module
global does not show it (a function's default argument, an attribute of a class). `interest_family`'s name keys are
mutated in `interest_family_mutants.py` (`name_keys_kept_by_the_module`, `name_keys_kept_by_count`); the ones that hide
them in a default argument or on a class are here, since they take two edits or must still compute at every build.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr178_mutants.py --out nr178-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

READER = "topos/features/lifecycle/blackhole.py"
CLEAN_UP = "topos/features/lifecycle/blackhole_rebuild.py"
COMPILER = "topos/features/lifecycle/uncached_patterns.py"
FAMILY = "topos/permissions_v2/interest_family.py"
TESTS = ["tests/topos/test_nr178_offlimits_patterns.py"]
FAMILY_TESTS = ["tests/permissions_v2/test_interest_family.py"]
ENTITY_KEYS = "def _entity_keys(conn, tombstones: dict, mentioned: frozenset) -> tuple:"
KEPT = "    kept: dict = {}   # NR178: this call's name keys, and nothing past it\n"
LRU = '@__import__("functools").lru_cache(maxsize=256)\n'
IDENTIFIER_RETURN = '    return compile_uncached(rf"(?<![^\\W_])(?:{body})(?![^\\W_])")\n\n\n#: A name of this many letters'
NAME_RETURN = '    return compile_uncached(rf"(?<![^\\W_])(?:{body}){possessive}(?![^\\W_])")\n'
TERM_RETURN = '        return compile_uncached(rf"(?<![^\\W_])(?:{body})(?![^\\W_])")\n    if not spaced:\n'

MUTANTS = [
    ("name_pattern_cached_by_the_process", [
        (READER, "\ndef _name_pattern(names: frozenset)", "\n" + LRU + "def _name_pattern(names: frozenset)")], TESTS),
    ("identifier_pattern_cached_by_the_process", [
        (READER, "\ndef _identifier_pattern(identifiers: frozenset)",
                 "\n" + LRU + "def _identifier_pattern(identifiers: frozenset)")], TESTS),
    ("term_pattern_cached_by_the_process", [
        (CLEAN_UP, "\ndef _term_pattern(terms: frozenset)", "\n" + LRU + "def _term_pattern(terms: frozenset)")], TESTS),
    ("identifier_pattern_in_re_cache", [
        (READER, IDENTIFIER_RETURN, IDENTIFIER_RETURN.replace("compile_uncached(", "re.compile("))], TESTS),
    ("name_pattern_in_re_cache", [(READER, NAME_RETURN, NAME_RETURN.replace("compile_uncached(", "re.compile("))],
     TESTS),
    ("term_pattern_in_re_cache", [(CLEAN_UP, TERM_RETURN, TERM_RETURN.replace("compile_uncached(", "re.compile("))],
     TESTS),
    ("compile_uncached_uses_re_compile", [
        (COMPILER, "    return _sre_compiler.compile(pattern, int(flags))\n", "    return re.compile(pattern, int(flags))\n")],
     TESTS),
    ("a_pass_keys_patterns_by_size", [(CLEAN_UP, "    key = frozenset(terms)\n", "    key = len(terms)\n")], TESTS),
    ("a_pass_never_reset", [(CLEAN_UP, "        _PASS_PATTERNS.reset(token)\n", "        pass\n")], TESTS),
    ("a_pass_not_emptied_or_marked", [(CLEAN_UP, "        memory.clear()\n        memory[_ENDED] = True\n", "")], TESTS),
    ("the_ended_mark_ignored", [
        (CLEAN_UP, "    if memory is None or _ENDED in memory:\n        return _term_pattern(key)\n",
                   "    if memory is None:\n        return _term_pattern(key)\n")], TESTS),
    ("the_clean_up_opens_no_pass", [
        (CLEAN_UP, "@_one_pass_of_patterns\ndef rebuild_for_blackhole(", "def rebuild_for_blackhole(")], TESTS),
    ("the_preview_opens_no_pass", [
        (CLEAN_UP, "@_one_pass_of_patterns\ndef preview_for_blackhole(", "def preview_for_blackhole(")], TESTS),
    ("identifiers_reader_made_per_call", [
        (READER, "        if self._identifiers_only is None:\n", "        if True:\n")], TESTS),
    # From WS0's review (15 Oct): a pattern kept where the "held nowhere" walk did not look until then.
    ("name_pattern_kept_in_a_default_argument", [
        (READER, "\ndef _name_pattern(names: frozenset) -> ", "\ndef _name_pattern(names: frozenset, _kept={}) -> "),
        (READER, NAME_RETURN, NAME_RETURN.replace("return compile_uncached(", "return _kept.setdefault(names, compile_uncached(")
                                         .replace('")\n', '"))\n'))], TESTS),
    ("clean_up_pattern_kept_in_a_default_argument", [
        (CLEAN_UP, "\ndef _pattern_for(terms: Set[str]) -> ", "\ndef _pattern_for(terms: Set[str], _kept={}) -> "),
        (CLEAN_UP, "    if memory is None or _ENDED in memory:\n        return _term_pattern(key)\n",
                   "    if memory is None or _ENDED in memory:\n        return _kept.setdefault(key, _term_pattern(key))\n")],
     TESTS),
    ("reader_patterns_kept_on_the_class", [
        (READER, '"_whole", "_identifiers_only")\n', '"_whole", "_identifiers_only")\n    _made: dict = {}\n'),
        (READER, "        self._identifiers_only = None   # NR178",
                 "        OffLimitsTerms._made[self.names] = (self._named, self._whole)\n"
                 "        self._identifiers_only = None   # NR178")], TESTS),
    ("entity_name_keys_kept_in_a_default_argument", [
        (FAMILY, ENTITY_KEYS, ENTITY_KEYS.replace("mentioned: frozenset)", "mentioned: frozenset, _kept={})")),
        (FAMILY, KEPT, "    kept = _kept\n")], FAMILY_TESTS),
    # Each build still computes its own keys (so no count sees them), and the names are also left behind.
    ("entity_name_keys_also_left_in_a_default_argument", [
        (FAMILY, ENTITY_KEYS, ENTITY_KEYS.replace("mentioned: frozenset)", "mentioned: frozenset, _last=[])")),
        (FAMILY, KEPT, KEPT + "    _last[:] = [kept]\n")], FAMILY_TESTS),
    ("entity_name_keys_also_left_on_a_class", [(FAMILY, KEPT, KEPT + "    Snapshot._last = kept\n")], FAMILY_TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
