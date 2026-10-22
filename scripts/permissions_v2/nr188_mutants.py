"""Mutation run over NR188 (DR-31): a member moved only by new data is withheld and the rest of its share index keeps
serving (`TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS`). Every mutant weakens one decision: the switch (on by default,
ignored), what counts as a narrow failure (a missing dependency set, a veto, a deleted row, any context move, a
fingerprint or lineage move, a dependency left unchecked, the duplicate-context rule's two conditions), the member not
being withheld, the sweep's narrowed publish (never made, made without BUILD_SLOT, without the file identity or the
authority re-checked under the gate, reported as a publish, owing no rebuild, copying a withheld member or its vectors,
keeping an index it could not narrow), the request path (serving or shredding on a narrow-only failure, at the search,
batch, answer and send-check doors, the send check not comparing what it sends, the transport not passing it), the
rebuild's discard path, the refresh loop's cause, and the attribution words; and, after the independent review, the
narrowed copy's successor record (#2), its on-disk mark, the restamp and restart carry and the busy-slot bound (#3), the
authority at the present (#4), the send check's identity and the copy's integrity and identity checks (#6); and fix 192
(DR-34, `TOPOS_PERMISSIONS_V2_INDEX_NARROW_OWN_ROW_EDITS`): every condition of `_own_row_edit`, its line said once, and
the replaced file's zeroing. Each must be killed by at least one test.

As `nr187_mutants.py` (and `n3c_mutants.py`): a scratch copy of the engine, one mutant at a time, the worktree never
modified. A mutant is one or more exact edits; one whose text does not match exactly once counts as a failure, not a
pass. A run that hangs past its timeout is recorded as HUNG, never as killed.

    TOPOS_ENV_FILE=/dev/null .venv/bin/python3 scripts/permissions_v2/nr188_mutants.py --out nr188-mutants.json
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
INDEX = "topos/permissions_v2/search_index.py"
RELEASE = "topos/permissions_v2/search_release.py"
TRANSPORT = "topos/permissions_v2/search_transport.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
SWITCHES = "topos/permissions_v2/switches.py"
TIMING = "topos/permissions_v2/search_timing.py"
TESTS = ["tests/permissions_v2/test_nr188_narrow_member_drop.py", "tests/permissions_v2/test_switches.py",
         "tests/permissions_v2/test_bl155_index_survives_bookkeeping.py",
         "tests/permissions_v2/test_index_hold_moved_member_192.py"]

STALE_HERE = '                    raise PolicyError("search_index_stale")   # NR188: not served now; the sweep withholds, never here\n'
BATCH_SITE = "                if withheld:\n" + STALE_HERE + "                read_now = self.clock()\n"
SEARCH_SITE = ("                if withheld:\n" + STALE_HERE
               + "                # The grant's rolling window, from the clock of this very read (never the earlier one).\n")
ANSWER_SITE = ('                        # one of its items was withheld, or is delivered.\n'
               '                        raise PolicyError("search_index_stale")\n'
               '                    started = self._stage("answer_recheck", started, **_lap_fields(laps))\n')
CHECK_OWN_SITE = ('        if withheld:\n'
                  '            raise PolicyError("search_index_stale")   # NR188: kept for the sweep to narrow, never shredded here\n')
MEMBER_EXCEPT = ('            except (PolicyError, sqlite3.Error, KeyError):\n'
                 '                return stale("member_unavailable")\n'
                 '        if provenance is not None:\n')
GATED_IDENTITY = ('                    if _file_state(path) != checked:\n'
                  '                        return False      # replaced by a publish, or removed, since the check read it\n')
GATED_AUTHORITY = ('                    if again is None or again != authority:\n'
                   '                        return False\n'
                   '                    retired = open(path, "r+b", buffering=0)\n')
OWED = '                        self._narrowed_owed[path.name] = successor\n'
#: Fix 192 (DR-34): the fingerprint step's one admitted mismatch, and `_own_row_edit`'s conditions.
FINGERPRINT_SITE = ('                    if not (moved and narrowed is not None\n'
                    '                            and self._own_row_edit(member, rows, facts, fingerprint, dependency_word, why)):\n'
                    '                        return stale("fingerprint")\n')
OWN_FIRST = '            if not _narrow_own_row_edits() or dependency_word != "row_revision" or fingerprint is None:\n'
OWN_SHAPE = ('            if len(rows) != 1 or facts or member.get("facts") or member.get("projection") '
             'or member.get("interest"):\n')
OWN_KEY = '            if (why.get("moved") or {}).get(self._own_key(member)) != "row_revision":\n                return False\n'
OWN_LAST = '            return not is_record_nsfw(row) and isinstance(content, str) and bool(content.strip())\n'

MUTANTS = [
    # The switch.
    ("switch_off_by_default", [
        (SWITCHES, '    "INDEX_NARROW_MEMBERS", "bool", True, True,\n', '    "INDEX_NARROW_MEMBERS", "bool", False, False,\n')]),
    ("switch_ignored", [
        (INDEX, "        narrowing = narrowed is not None and _narrow_members()\n",
                "        narrowing = narrowed is not None\n")]),
    # What a narrow failure is: only a clean revision mismatch at the two classes, every other check passing.
    ("member_not_withheld", [
        (INDEX, "                    narrowed.add(opaque)   # NR188: withheld; every other member is still checked\n",
                "                    pass\n")]),
    ("missing_dependencies_narrow", [
        (INDEX, 'NARROW_DEPENDENCY_WORDS = frozenset({"row_revision", "row_binding", "conversation_context"})\n',
                'NARROW_DEPENDENCY_WORDS = frozenset({"row_revision", "row_binding", "conversation_context", "missing"})\n')]),
    ("first_moved_dependency_ends_the_check", [
        (INDEX, "                                                                every=narrowed is not None)\n",
                "                                                                every=False)\n")]),
    ("veto_or_unreadable_row_narrows", [
        (INDEX, MEMBER_EXCEPT, MEMBER_EXCEPT.replace(
            '                return stale("member_unavailable")\n',
            '                if narrowed is None:\n                    return stale("member_unavailable")\n'
            '                narrowed.add(opaque)\n'))]),
    ("deleted_row_narrows", [
        (INDEX, '                    if len(rows) != 1:\n'
                '                        return stale("classification_context", "row_unavailable")\n',
                '                    if len(rows) != 1:\n'
                '                        if narrowed is None:\n'
                '                            return stale("classification_context", "row_unavailable")\n'
                '                        narrowed.add(opaque)\n'
                '                        continue\n')]),
    ("any_context_move_forgiven", [
        (INDEX, "                        moved and self._same_drift(member, entity_context, why, checked)):\n",
                "                        moved):\n")]),
    ("duplicate_rule_sealed_equality_dropped", [
        (INDEX, '                and sealed.get("context") == member.get("entity_context_revision")\n',
                '                and True\n')]),
    ("duplicate_rule_current_value_dropped", [
        (INDEX, "                and own in checked and checked[own][1] == current_context)\n",
                "                and own in checked)\n")]),
    # Fix 192 re-anchored: any fingerprint move of a moved member forgiven, not only the own-row edit DR-34 admits.
    ("fingerprint_move_of_a_moved_member_narrows", [
        (INDEX, FINGERPRINT_SITE, '                    if not moved:\n                        return stale("fingerprint")\n')]),
    ("lineage_move_of_a_moved_member_narrows", [
        (INDEX, '                if deep and _lineage_fingerprint(conn, member, dict(rows[0]).get("content")) != member["lineage"]:\n',
                '                if deep and not moved and _lineage_fingerprint(conn, member, dict(rows[0]).get("content")) != member["lineage"]:\n')]),
    # The sweep's narrowed publish.
    ("sweep_never_narrows", [
        (INDEX, "                    elif checked is not None and withheld:\n", "                    elif False:\n")]),
    ("build_slot_not_taken", [
        (INDEX, "        held = BUILD_SLOT.acquire(blocking=False)\n", "        held = False and BUILD_SLOT.acquire(blocking=False)\n"),
        (INDEX, "        if not held and waited < NARROW_SLOT_WAITS:\n", "        if False:\n")]),
    ("file_identity_not_checked_under_the_gate", [(INDEX, GATED_IDENTITY, "")]),
    ("authority_not_checked_under_the_gate", [
        (INDEX, GATED_AUTHORITY, '                    retired = open(path, "r+b", buffering=0)\n')]),
    ("narrowed_publish_reported_as_a_publish", [
        (INDEX, OWED, OWED + "                        self._published.add(path.name)\n")]),
    ("narrowed_publish_owes_no_rebuild", [(INDEX, OWED, "                        pass\n")]),
    ("withheld_members_copied", [
        (INDEX, "        kept = [row for row in rows if row[0] not in withheld]\n        temporary = _build_file(self.root)\n",
                "        kept = list(rows)\n        temporary = _build_file(self.root)\n")]),
    ("withheld_vectors_copied", [
        (INDEX, "                                 [vector for vector in vectors if vector[0] not in withheld])\n",
                "                                 list(vectors))\n")]),
    ("index_kept_when_it_cannot_be_narrowed", [
        (INDEX, "                with _gated(holds):\n                    if _file_state(path) == checked:\n"
                "                        _shred(path)\n                return False\n",
                "                return False\n")]),
    # The request path: never serves, never shreds, on a narrow-only failure.
    ("search_serves_a_moved_member", [(RELEASE, SEARCH_SITE, SEARCH_SITE.replace("                if withheld:\n" + STALE_HERE, ""))]),
    ("search_shreds_on_a_moved_member", [
        (RELEASE, SEARCH_SITE, SEARCH_SITE.replace("                if withheld:\n",
                                                   "                if withheld:\n                    deciding.purge_stale()\n"))]),
    ("batch_serves_a_moved_member", [(RELEASE, BATCH_SITE, BATCH_SITE.replace("                if withheld:\n" + STALE_HERE, ""))]),
    ("batch_shreds_on_a_moved_member", [
        (RELEASE, BATCH_SITE, BATCH_SITE.replace("                if withheld:\n",
                                                 "                if withheld:\n                    deciding.purge_stale()\n"))]),
    ("answer_retrieval_serves_a_moved_member", [
        (RELEASE, ANSWER_SITE, ANSWER_SITE.replace('                        raise PolicyError("search_index_stale")\n',
                                                   "                        pass\n"))]),
    ("answer_retrieval_shreds_on_a_moved_member", [
        (RELEASE, ANSWER_SITE, ANSWER_SITE.replace('                        raise PolicyError("search_index_stale")\n',
                                                   '                        deciding.purge_stale()\n'
                                                   '                        raise PolicyError("search_index_stale")\n'))]),
    ("check_own_serves_a_moved_member", [(INDEX, CHECK_OWN_SITE, "")]),
    ("check_own_shreds_on_a_moved_member", [
        (INDEX, CHECK_OWN_SITE, CHECK_OWN_SITE.replace("        if withheld:\n",
                                                       "        if withheld:\n            _shred(path)\n"))]),
    ("send_check_ignores_what_it_sends", [
        (INDEX, "        if released is not None and _narrow_members() and not self._holds(path, checked_state, released):\n",
                "        if False:\n")]),
    ("transport_single_passes_nothing_sent", [
        (TRANSPORT, '                                                released=frozenset(record["record_id"] for record in output["records"]))\n',
                    "                                                released=None)\n")]),
    ("transport_batch_passes_nothing_sent", [
        (TRANSPORT, '                                                released=frozenset(record["record_id"] for _result, sent in answered\n'
                    '                                                                   for record in sent["records"]))\n',
                    "                                                released=None)\n")]),
    # A build that new data kept from publishing.
    ("discard_purges_a_narrowing_index", [
        (INDEX, "                        if self._current(path, grant_id, authority, clock_state(conn), conn, narrowed=set()):\n",
                "                        if self._current(path, grant_id, authority, clock_state(conn), conn):\n")]),
    # The refresh loop.
    ("refresh_loop_queues_nothing", [
        (LOOP, "        take = getattr(service, \"take_narrowed\", None)\n        owed = set(take()) if callable(take) else set()\n",
               "        owed = set()\n")]),
    ("a_build_publish_does_not_settle_it", [
        (LOOP, '                         "members_narrowed"})\n', "                         })\n")]),
    # Review #2: a removal also takes the narrowed copy in the file's place.
    ("successor_never_followed", [
        (INDEX, "        if current == state:\n            return True\n        with self._published_lock:\n"
                "            chain = dict(self._successors.get(Path(path).name, {}))\n",
                "        if current == state:\n            return True\n        return False\n        with self._published_lock:\n"
                "            chain = dict(self._successors.get(Path(path).name, {}))\n")]),
    ("successor_not_recorded", [(INDEX, "                        chain[checked] = successor\n", "                        pass\n")]),
    # Review #3: the owed rebuild survives a restamp and a restart; a busy slot holds the publish back only so long.
    ("narrowed_copy_unmarked", [
        (INDEX, '                conn.execute(_NARROWED_DDL)\n                conn.execute("INSERT INTO narrowed VALUES (1)")',
                '                pass\n                pass')]),
    ("restamp_drops_the_owed_rebuild", [
        (INDEX, "            if narrowed:\n                self._narrowed_owed[path.name] = _file_state(path)\n",
                "            if False:\n                self._narrowed_owed[path.name] = _file_state(path)\n")]),
    ("restart_forgets_the_owed_rebuild", [(LOOP, "        if restart and callable(listing):\n", "        if False:\n")]),
    ("busy_slot_holds_the_publish_back_for_ever", [
        (INDEX, "        if not held and waited < NARROW_SLOT_WAITS:\n", "        if not held:\n")]),
    ("busy_slot_never_waited_for", [
        (INDEX, "        if not held and waited < NARROW_SLOT_WAITS:\n", "        if not held and waited < 1:\n")]),
    # Review #4: the authority at the present.
    ("authority_read_at_the_sweep_start", [
        (INDEX, "                            again = self.ledger._authority(db, grant_id, current_now)[0]\n",
                "                            again = self.ledger._authority(db, grant_id, now)[0]\n")]),
    # Review #6: the send check's identity, the copy's integrity and identity.
    ("send_check_ignores_a_replaced_file", [
        (INDEX, "        return (_file_state(path) == checked_state\n                and set(released)",
                "        return (True\n                and set(released)")]),
    ("narrowed_copy_integrity_unchecked", [
        (INDEX, "        if meta is None or meta[0] != FORMAT or len(rows) != meta[5] or not withheld or not withheld <= present:\n",
                "        if meta is None:\n")]),
    ("narrowed_copy_identity_unchecked", [
        (INDEX, "        if _file_state(path) != checked:\n            return None\n        source = sqlite3.connect(path.as_uri()",
                "        source = sqlite3.connect(path.as_uri()"),
        (INDEX, "        finally:\n            source.close()\n        if _file_state(path) != checked:\n            return None\n",
                "        finally:\n            source.close()\n")]),
    # Fix 192 (DR-34): what `_own_row_edit` admits, condition by condition; its line; the replaced file zeroed.
    ("own_row_edit_switch_not_read", [
        (INDEX, OWN_FIRST, OWN_FIRST.replace("not _narrow_own_row_edits() or ", ""))]),
    ("dependency_word_not_required", [
        (INDEX, OWN_FIRST, OWN_FIRST.replace('dependency_word != "row_revision" or ', ""))]),
    ("fingerprint_none_condition_dropped", [
        (INDEX, OWN_FIRST, OWN_FIRST.replace(" or fingerprint is None", ""))]),
    ("own_key_not_required", [(INDEX, OWN_KEY, "")]),
    ("row_count_condition_dropped", [(INDEX, OWN_SHAPE, OWN_SHAPE.replace("len(rows) != 1 or ", ""))]),
    ("facts_condition_dropped", [
        (INDEX, OWN_SHAPE, OWN_SHAPE.replace('facts or member.get("facts") or ', ""))]),
    ("projection_condition_dropped", [(INDEX, OWN_SHAPE, OWN_SHAPE.replace(' or member.get("projection")', ""))]),
    ("interest_condition_dropped", [(INDEX, OWN_SHAPE, OWN_SHAPE.replace(' or member.get("interest")', ""))]),
    ("journal_rows_admitted", [
        (INDEX, '            if member.get("table") not in OWN_ROW_EDIT_TABLES:\n                return False\n', "")]),
    ("nsfw_condition_dropped", [(INDEX, OWN_LAST, OWN_LAST.replace("not is_record_nsfw(row) and ", ""))]),
    ("blank_content_condition_dropped", [(INDEX, OWN_LAST, OWN_LAST.replace(" and bool(content.strip())", ""))]),
    ("own_row_edit_exception_admits", [
        (INDEX, "        except Exception:  # noqa: BLE001 -- what cannot be told is not an own-row edit: the index goes\n"
                "            return False\n",
                "        except Exception:  # noqa: BLE001\n            return True\n")]),
    ("own_row_edit_said_every_time", [
        (INDEX, '                    if "fingerprint" not in said:\n', "                    if True:\n")]),
    ("retired_file_not_zeroed", [
        (INDEX, "                _zero_open(handle)        # DR-34: outside the gate; the unlinked inode the publish replaced\n",
                "                handle.close()\n")]),
    # The attribution words.
    ("inserted_neighbour_never_said", [
        (INDEX, '                    return "neighbour_inserted"\n', '                    return "neighbour_changed"\n')]),
    ("row_binding_never_said", [(INDEX, '                return "row_binding"\n', '                return "row_revision"\n')]),
    ("stale_line_without_its_word", [
        (INDEX, "            if sub is not None:\n                _log.warning(\"message search index member drift",
                "            if False:\n                _log.warning(\"message search index member drift")]),
    ("timing_line_without_its_word", [
        (TIMING, '        for name in ("sub", "scope"):', "        for name in ():")]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="nr188-mutants-") as scratch:
        base = Path(scratch).resolve() / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        for name, edits, *selected in MUTANTS:
            if args.only and name not in args.only:
                continue
            originals = {path: (base / path).read_text() for path, _old, _new in edits}
            patched, applicable = dict(originals), True
            for path, old, new in edits:
                if patched[path].count(old) != 1:
                    applicable = False
                    results.append({"mutant": name, "status": "patch_not_applicable", "count": patched[path].count(old)})
                    break
                patched[path] = patched[path].replace(old, new)
            if not applicable:
                print(results[-1], flush=True)
                continue
            try:
                for path, text in patched.items():
                    (base / path).write_text(text)
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
                try:
                    run = subprocess.run([sys.executable, "-m", "pytest", *(selected[0] if selected else TESTS), "-q",
                                          "-p", "no:cacheprovider", "-m", "not e2e and not live"], cwd=base, env=env,
                                         capture_output=True, text=True, timeout=900)
                except subprocess.TimeoutExpired:
                    results.append({"mutant": name, "status": "HUNG", "summary": [], "killed_by": []})
                    print(results[-1], flush=True)
                    continue
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
                errored = "error" in " ".join(tail) and not failing
                results.append({"mutant": name, "status": "killed" if run.returncode != 0 and not errored else
                                ("ERRORED" if errored else "SURVIVED"), "summary": tail, "killed_by": failing[:6],
                                "killed_by_count": len(failing)})
            finally:
                for path, text in originals.items():
                    (base / path).write_text(text)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"mutants": len(results), "killed": killed, "tests": TESTS, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
