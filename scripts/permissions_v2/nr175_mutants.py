"""Mutation run over NR175: the forms of the Off-limits vocabulary remembered per boundary, `loose` per pass, nothing
by the process. Every mutant weakens one decision and must be killed by at least one real test.

Whether a process-wide cache comes back (on a function of a term set, of one word, or `loose`), or a memory is kept by
the function instead of the boundary; whether a kept form is looked up by its key; whether the variable is reset;
whether every boundary shares one memory (so an entry made of an old vocabulary is still there after it changed);
whether a match makes its boundary's memory current at all; whether the switch is obeyed; and, for `loose`, whether
its memory is emptied with the pass, keeps to the pass's limit, and stays out of the process outside a pass. For
`forms_memory` (a direct match's block, the message review's floors): whether the block resets the variable,
whether it replaces a memory already current, whether its dict is emptied and marked ended for a task
started inside it, whether the floors and ingestion's name check match inside one at all; and whether
two functions of one name share a table.

The runner is `n3c_mutants.py`'s (a scratch copy of the engine, one mutant at a time, the worktree never modified;
a mutant whose text does not match exactly once is a failure, not a pass; a hung run is HUNG, never killed).

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/nr175_mutants.py --out nr175-mutants.json
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import n3c_mutants as runner  # noqa: E402

BOUNDARY = "topos/permissions_v2/entity_boundary.py"
REVIEW = "topos/permissions_v2/automatic_message_review.py"
INGESTION = "topos/features/lifecycle/blackhole_llm.py"
TESTS = ["tests/permissions_v2/test_nr175_boundary_forms.py"]
LOOKUP = ("        try:\n            return table[key]\n        except KeyError:\n"
          "            found = table[key] = function(key)\n")
LOOSE_OUTSIDE = ("    if memory is None or type(word) is not str or not memory.usable():\n"
                 "        return _loose(word)\n")

MUTANTS = [
    ("a_set_function_cached_by_the_process", [
        (BOUNDARY, "@_per_boundary\ndef _v8_long(", "@functools.lru_cache(maxsize=256)\ndef _v8_long(")], TESTS),
    ("a_word_function_cached_by_the_process", [
        (BOUNDARY, "\ndef short_variants(term: str) -> frozenset:\n",
                   "\n@functools.lru_cache(maxsize=4096)\ndef short_variants(term: str) -> frozenset:\n")], TESTS),
    ("loose_cached_by_the_process", [
        (BOUNDARY, "\ndef _loose(word: str) -> str:\n", "\n@functools.lru_cache(maxsize=65536)\ndef _loose(word: str) -> str:\n")],
     TESTS),
    ("the_memory_kept_by_the_function", [
        (BOUNDARY, "        memory = _BOUNDARY_FORMS.get()\n        if memory is None or not FORMS_MEMORY or _ENDED in memory:\n",
                   '        memory = remembered.__dict__.setdefault("kept", {})\n'
                   "        if memory is None or not FORMS_MEMORY or _ENDED in memory:\n")], TESTS),
    ("a_kept_form_not_looked_up_by_its_key", [
        (BOUNDARY, LOOKUP, LOOKUP.replace("table[key]", "table[None]"))], TESTS),
    ("the_variable_never_reset", [
        (BOUNDARY, "            _BOUNDARY_FORMS.reset(token)\n", "            pass\n")], TESTS),
    ("every_boundary_shares_one_memory", [
        (BOUNDARY, "FORMS_MEMORY = True\n", "FORMS_MEMORY = True\n_EVERY_BOUNDARY = {}\n"),
        (BOUNDARY, "        self._forms = {}\n", "        self._forms = _EVERY_BOUNDARY\n")], TESTS),
    ("a_match_makes_no_memory_current", [
        (BOUNDARY, "        with self._forms_scope():\n            return self._hits_read(",
                   "        if True:\n            return self._hits_read(")], TESTS),
    ("the_switch_ignored", [
        (BOUNDARY, "        if memory is None or not FORMS_MEMORY or _ENDED in memory:\n",
                   "        if memory is None or _ENDED in memory:\n")], TESTS),
    ("loose_not_emptied_with_the_pass", [(BOUNDARY, "        self.loose.clear()\n", "")], TESTS),
    ("loose_limit_ignored", [
        (BOUNDARY, "        memory.keep(memory.loose, word, found)\n", "        memory.loose[word] = found\n")], TESTS),
    ("loose_kept_by_the_process_outside_a_pass", [
        (BOUNDARY, LOOSE_OUTSIDE, LOOSE_OUTSIDE.replace("return _loose(word)",
                                                        "return loose.__dict__.setdefault(word, _loose(word))"))],
     TESTS),
    ("a_block_never_reset", [
        (BOUNDARY, "    finally:\n        _BOUNDARY_FORMS.reset(token)\n", "    finally:\n        pass\n")], TESTS),
    ("a_block_replaces_the_memory_already_current", [
        (BOUNDARY, "    if _BOUNDARY_FORMS.get() is not None:\n        yield\n        return\n", "")], TESTS),
    ("the_floors_match_with_no_memory", [
        (REVIEW, "    with forms_memory():\n        labels = apply_floors(labels, inputs)\n",
                 "    if True:\n        labels = apply_floors(labels, inputs)\n")], TESTS),
    ("a_block_dict_not_emptied_or_marked", [
        (BOUNDARY, "        memory.clear()\n        memory[_ENDED] = True\n", "")], TESTS),
    ("the_ended_mark_ignored", [
        (BOUNDARY, "        if memory is None or not FORMS_MEMORY or _ENDED in memory:\n",
                   "        if memory is None or not FORMS_MEMORY:\n")], TESTS),
    ("tables_keyed_by_the_functions_name", [
        (BOUNDARY, "        table = memory.setdefault(function, {})\n",
                   "        table = memory.setdefault(function.__name__, {})\n")], TESTS),
    ("ingestion_matches_with_no_memory", [
        (INGESTION, "        with forms_memory():\n            return bool(text_hits(",
                    "        if True:\n            return bool(text_hits(")], TESTS),
]

if __name__ == "__main__":
    runner.MUTANTS, runner.TESTS = MUTANTS, TESTS
    raise SystemExit(runner.main())
