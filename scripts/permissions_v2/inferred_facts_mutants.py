"""Mutation run over IF-6 v1 (derived facts): the value guards, fact_projection's step 7, the index basis key, the
refresh loop's `facts_changed` cause and the census what-if's discovery.

Each mutant weakens one decision. Each must be killed by a failing test. Runs in a scratch copy of the engine, one
mutant at a time; the worktree is never modified. A mutant whose text no longer matches counts as a failure, not a
pass.

    export TOPOS_DATABASE_PATH=<scratch>/db.sqlite TOPOS_ENV_FILE=<scratch>/.env
    .venv/bin/python3 scripts/permissions_v2/inferred_facts_mutants.py --out inferred-facts-mutants.json

Not listed because they are equivalent:
- dropping guard 3's control and format character test: `fact_contract.atomic_label_syntax` (guard 3's own earlier
  test) already refuses every character outside letters, marks, digits and " -'&", so no value reaches it;
- dropping `value_refusal`'s `boundary is None` test: `None.mentions_protected` raises, and the guard's own
  `except` already answers `inferred_boundary_unavailable`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARDS = "topos/permissions_v2/inferred_facts.py"
PROJECTION = "topos/permissions_v2/knowledge_projections.py"
INDEX = "topos/permissions_v2/search_index.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
CENSUS = "scripts/permissions_v2/grant_census.py"
TESTS = ["tests/permissions_v2/test_inferred_facts.py", "tests/permissions_v2/test_inferred_facts_refresh.py",
         "tests/permissions_v2/test_grant_census.py::test_the_derived_facts_what_if_releases_exactly_what_the_build_releases"]

MUTANTS = [
    # The flag.
    ("enabled_ignores_the_journal_family", GUARDS, '    return family("journal_entries").enabled(env)\n',
     "    return True\n"),
    ("enabled_reads_true_only", GUARDS, 'not in ("1", "true", "yes", "on"):', 'not in ("true",):'),
    # Guards 1-2: the entry's own labels.
    ("labels_authorship_unchecked", GUARDS,
     'if not _labels_are(labels, "authorship", "owner_authored") or not _labels_are(labels, "speech", "original_message")',
     'if not _labels_are(labels, "speech", "original_message")'),
    ("labels_protected_unchecked", GUARDS, '\\\n            or not _labels_are(labels, "protected_content", "none"):',
     ":"),
    ("sensitivity_special_allowed", GUARDS, 'not in ("none", "personal"):\n        return "inferred_entry_sensitivity"',
     'not in ("none", "personal", "special"):\n        return "inferred_entry_sensitivity"'),
    # Guard 3: shape.
    ("shape_length_unbounded", GUARDS, "not 2 <= len(value.strip()) <= 200", "not 2 <= len(value.strip()) <= 2000"),
    ("shape_word_count_unbounded", GUARDS, "not 1 <= len(eg.tokens(value)) <= 12", "not 1 <= len(eg.tokens(value)) <= 120"),
    ("shape_label_syntax_skipped", GUARDS, "        atomic_label_syntax(value)\n", "        pass\n"),
    ("shape_nfkc_skipped", GUARDS, 'if unicodedata.normalize("NFKC", value) != value:', "if False:"),
    ("shape_combining_marks_allowed", GUARDS, 'if category[0] in ("C", "M"):', 'if category[0] in ("C",):'),
    ("shape_non_latin_letters_allowed", GUARDS, 'not unicodedata.name(ch, "").startswith("LATIN ")', "False"),
    # Guard 4: Off-limits.
    ("protected_wire_content_unread", GUARDS, "boundary.mentions_protected(value, wire_content(predicate, value))",
     "boundary.mentions_protected(value)"),
    ("protected_name_parts_unread", GUARDS, " or _name_part(boundary, value):", ":"),
    ("protected_error_passes", GUARDS,
     '    except Exception:  # noqa: BLE001 -- an Off-limits check that cannot answer withholds\n'
     '        return "inferred_boundary_unavailable"\n',
     '    except Exception:  # noqa: BLE001 -- an Off-limits check that cannot answer withholds\n        pass\n'),
    # Guard 5: special categories.
    ("special_skipped", GUARDS, "    if jgf._special(plain, None):\n", "    if False:\n"),
    ("special_verb_slot", GUARDS, "    if jgf._special(plain, None):\n", "    if jgf._special(plain, 0):\n"),
    # Guard 6: questions and quotes.
    ("question_word_unchecked", GUARDS, "    return opening < len(low) and low[opening] in jgf.QUESTION_START\n",
     "    return False\n"),
    ("stray_apostrophe_allowed", GUARDS, 'if not (before.isalnum() and (after.isalnum() or before in "sS")):',
     "if False:"),
    # Guard 7: not a value.
    ("placeholders_unchecked", GUARDS,
     "if jgf.PLACEHOLDERS & set(plain) or any(jgf._has(plain, phrase) for phrase in jgf.PLACEHOLDER_PHRASES):",
     "if False:"),
    ("echo_unchecked", GUARDS, "    if value.strip().casefold() in echoed:\n", "    if False:\n"),
    ("no_letter_allowed", GUARDS, "    return not any(ch.isalpha() for ch in value)", "    return False"),
    ("url_unchecked", GUARDS,
     'if "://" in value or "www." in folded or any(ch in value for ch in "/@#") or _DOMAIN.search(value):',
     "if False:"),
    # Guard 8: a person.
    ("known_people_unread", GUARDS, "if (frozenset(people) | jgf._names_in(listed)) & words:",
     "if jgf._names_in(listed) & words:"),
    ("people_column_unread", GUARDS, "if (frozenset(people) | jgf._names_in(listed)) & words:",
     "if frozenset(people) & words:"),
    ("unreadable_people_pass", GUARDS, "    if people is None:\n        return True", "    if people is None:\n        return False"),
    ("relations_unchecked", GUARDS,
     "if jgf._PEOPLE & words or any(len(word) > 3 and eg.stem(word) in jgf._PEOPLE_STEMS for word in plain):",
     "if False:"),
    ("honorifics_unchecked", GUARDS, "    if HONORIFICS & words:\n", "    if False:\n"),
    ("possessive_unchecked", GUARDS, """if any(word.endswith("'s") and word[:-2] not in jgf.TIME_WORDS for word in plain):""",
     "if False:"),
    ("capitalised_words_allowed", GUARDS,
     "if predicate not in PROPER_NOUN_PREDICATES and any(jgf._name_like(word) for word in raw[1:]):", "if False:"),
    ("trades_unchecked", GUARDS,
     "if any(word.endswith(jgf.PERSON_SUFFIXES) and word not in jgf.PERSON_SUFFIX_EXEMPT and len(word) > 4",
     "if any(False"),
    # Step 7.
    ("step7_flag_ignored", PROJECTION, "            if not inferred_facts.enabled():\n", "            if False:\n"),
    ("step7_scope_unchecked", PROJECTION,
     "    if len(sources)!=1 or sources[0][0].snapshot.message.identity.table!=JOURNAL:\n", "    if False:\n"),
    ("step7_message_sources_allowed", PROJECTION,
     "    if len(sources)!=1 or sources[0][0].snapshot.message.identity.table!=JOURNAL:\n",
     "    if len(sources)!=1:\n"),
    ("step7_option_unchecked", PROJECTION,
     "    if 'journal_entry' not in policy.search.result_types:\n        raise PolicyError('journal_citation_needs_record_option')"
     "\n    if predicate not in CLASSES:", "    if predicate not in CLASSES:"),
    ("step7_class_unchecked", PROJECTION, "    if predicate not in CLASSES:\n        raise PolicyError('fact_projection_unsupported')",
     "    if False:\n        raise PolicyError('fact_projection_unsupported')"),
    ("step7_unreadable_people_pass", PROJECTION,
     "    except sqlite3.Error:\n        raise PolicyError('evidence_storage_unavailable') from None\n    code=inferred_facts",
     "    except sqlite3.Error:\n        people=frozenset()\n    code=inferred_facts"),
    ("step7_marks_owner_stated", PROJECTION, "            assertion='inferred'\n", "            assertion='owner_stated'\n"),
    ("step7_guards_skipped", PROJECTION, "    if code is not None:\n        raise PolicyError(code)\n",
     "    if False:\n        raise PolicyError(code)\n"),
    # The index basis.
    ("basis_key_missing", INDEX, '        revisions["inferred_facts"] = inferred_facts.VERSION\n', "        pass\n"),
    # The refresh loop.
    ("facts_any_grant", LOOP, 'and {"journal_entry", "fact"} <= set(policy.search.result_types)', "and True"),
    ("facts_unbuilt_grant", LOOP,
     "and (index_path(self.root, grant_id).name in names or grant_id in owed)]\n\n    def _request_fact_rebuilds",
     "and True]\n\n    def _request_fact_rebuilds"),
    ("facts_flag_ignored", LOOP, "        if not self.settings.facts:\n            return\n", ""),
    ("facts_restart_forgets", LOOP,
     'before = self._facts if self._facts is not None else self._load_state().get("fact_digest")',
     "before = self._facts"),
    ("facts_running_rebuild_finishes_the_entry", LOOP, '                if again and state in ("ready", "over_cap"):',
     "                if False:"),
    # The census what-if.
    ("census_journal_members_skipped", CENSUS,
     "                discovered += journal_members(resolver, conn, floor, frozen, policy, lower, upper)\n",
     "                pass\n"),
    ("census_journal_window_skipped", CENSUS, "if is_record_nsfw(row) or not within(table, row, lower_us, upper_us):",
     "if is_record_nsfw(row):"),
    ("census_journal_decision_skipped", CENSUS,
     '            if source_message_decision(policy, qualified).verdict != "permit":\n                continue\n', ""),
    ("census_grounding_unread", CENSUS,
     '                        typed.grounding = "inferred" if projected.fields.get("assertion") == "inferred" else "stated"',
     '                        typed.grounding = "stated"'),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def pytest(base, tests):
        run = subprocess.run([sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
                             cwd=base, env=env, capture_output=True, text=True, timeout=1800)
        tail = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line][-1:]
        failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
        return run.returncode, tail, failing

    results = []
    with tempfile.TemporaryDirectory(prefix="inferred-facts-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        tests = [test for test in TESTS if (base / test.split("::")[0]).exists()]
        code, tail, failing = pytest(base, tests)
        baseline = {"status": "pass" if code == 0 else "FAIL", "summary": tail, "failing": failing, "tests": tests}
        print({"baseline": baseline}, flush=True)
        if code != 0:
            args.out.write_text(json.dumps({"baseline": baseline}, indent=2) + "\n")
            return 2
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text() if target.exists() else ""
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                print(results[-1], flush=True)
                continue
            target.write_text(original.replace(old, new))
            try:
                code, tail, failing = pytest(base, tests)
                # Killed only by a failing test: an error before any test ran proves nothing.
                status = "killed" if code != 0 and failing else "SURVIVED" if code == 0 else "ERRORED"
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"baseline": baseline, "mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
