"""Mutation run over the v3 existing-row reader (RD12: inline replies and Messages' chain pointer).

Every new rule must be killed by a test: the reader's two accepted forms and what it still refuses, the
comparison's thread agreement, the capture's contents, the ledger's two reconciliation lanes, the refresh's
move from v2 to v3, the owner door's first recovery, the sync's enrolled-dataset guard, and the count-only
observations.

Each mutant is one textual patch. As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/`, `fixtures/`
and `scripts/` are copied into a scratch directory and each mutant is applied there, one at a time; the
worktree is never modified. A patch that no longer applies is reported as such, never as killed, and a
mutant is killed only by a failing test after a clean baseline.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/imessage_forms_mutants.py --out forms-mutants.json
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
READER = "topos/ingestion/owner_snapshot.py"
COMPARISON = "topos/permissions_v2/imessage_reconciliation.py"
CAPTURE = "topos/permissions_v2/native_imessage_probe.py"
SERVICE = "topos/permissions_v2/reconciliation_provenance.py"
LEDGER = "topos/permissions_v2/ingest_provenance.py"
SYNC = "topos/ingestion/local_sync.py"
DOOR = "topos/api/permissions_native_probe.py"
TESTS = ["tests/permissions_v2/test_imessage_provenance_forms.py", "tests/permissions_v2/test_native_imessage_probe.py",
         "tests/permissions_v2/test_imessage_reconciliation.py", "tests/sources/test_imessage_sync_guards.py",
         "tests/sources/test_imessage_since_last_sync.py", "tests/ingestion/test_owner_snapshot.py"]

MUTANTS = [
    # The reader.
    ("thread_fields_unvalidated", READER,
     '    if not _identifier(guid) or (part not in (None, "") and not _identifier(part)):\n'
     '        _reject("snapshot_message_form_unsupported")\n    return guid, (part or None)\n',
     '    return guid, (part or None)\n'),
    ("part_without_originator_accepted", READER,
     '        if part not in (None, ""):\n            _reject("snapshot_message_form_unsupported")\n        return None, None\n',
     '        return None, None\n'),
    ("v2_reads_replies_too", READER,
     '                    if value not in (None, "") and not thread_replies:\n',
     '                    if False:\n'),
    ("v3_rejects_replies_like_v2", READER,
     '                    if value not in (None, "") and not thread_replies:\n',
     '                    if value not in (None, ""):\n'),
    ("v3_drops_the_thread", READER,
     '                records[-1].update(thread_originator_guid=originator, thread_originator_part=part)\n',
     '                pass\n'),
    ("thread_reply_keeps_an_empty_part", READER,
     '    return guid, (part or None)\n',
     '    return guid, part\n'),
    # The comparison's reader table and native columns.
    ("v3_parsed_by_the_v2_parser", COMPARISON,
     '            FORMS_CONTRACT: parse_imessage_forms_snapshot}\n',
     '            FORMS_CONTRACT: parse_imessage_attributed_snapshot}\n'),
    ("v3_parse_refuses_the_chain", COMPARISON,
     '        if reader_contract == FORMS_CONTRACT:\n'
     '            # Messages\' own chain to the preceding message is not a form of the message (see FORMS_CONTRACT).\n'
     '            empty_columns.discard("reply_to_guid")\n',
     ''),
    ("every_reader_ignores_the_chain", COMPARISON,
     '        if reader_contract == FORMS_CONTRACT:\n'
     '            # Messages\' own chain to the preceding message is not a form of the message (see FORMS_CONTRACT).\n',
     '        if True:\n'),
    # The comparison.
    ("names_ignores_the_native_thread", COMPARISON,
     '    if native is None:\n        return stored in (None, "")\n    return type(stored) is str and stored == native\n',
     '    return True\n'),
    ("stored_reply_not_compared", COMPARISON,
     '    if not _names(row.get("reply_to_message_id"), thread):\n        refuse("message_form")\n',
     ''),
    ("stored_originator_not_compared", COMPARISON,
     '    if (not _names(metadata.get("thread_originator_guid"), thread) or not _names(metadata.get("thread_originator_part"), part)\n',
     '    if (False or not _names(metadata.get("thread_originator_part"), part)\n'),
    ("stored_part_not_compared", COMPARISON,
     '    if (not _names(metadata.get("thread_originator_guid"), thread) or not _names(metadata.get("thread_originator_part"), part)\n',
     '    if (not _names(metadata.get("thread_originator_guid"), thread)\n'),
    ("reaction_metadata_allowed_on_replies", COMPARISON,
     '            or metadata.get("associated_message_guid") not in (None, "")\n',
     ''),
    ("v3_skips_the_native_time_check", COMPARISON,
     '    if native.reader_contract in RECONCILIATION_CONTRACTS:\n',
     '    if native.reader_contract == ATTRIBUTED_CONTRACT:\n'),
    ("v2_accepts_thread_fields", COMPARISON,
     '    if native.reader_contract != FORMS_CONTRACT:\n        if thread is not None or part is not None:\n'
     '            refuse("input_invalid")\n    elif',
     '    if native.reader_contract != FORMS_CONTRACT:\n        pass\n    elif'),
    ("v3_accepts_a_malformed_thread_observation", COMPARISON,
     '    elif (thread, part) != (None, None) and (not _identifier(thread) or not (part is None or _identifier(part))):\n'
     '        refuse("input_invalid")\n',
     ''),
    # The capture.
    ("thread_columns_not_captured", CAPTURE,
     "        selected = sorted(_REQUIRED | ((_EMPTY | _ZERO | _THREAD) & columns))\n",
     "        selected = sorted(_REQUIRED | ((_EMPTY | _ZERO) & columns))\n"),
    ("chain_pointer_captured", CAPTURE,
     "_THREAD = frozenset(THREAD_COLUMNS)\n",
     "_THREAD = frozenset(THREAD_COLUMNS) | {'reply_to_guid'}\n"),
    ("malformed_thread_is_not_a_form", CAPTURE,
     "            if (thread is None\n                    or any(row.get(key) not in (None, '') for key in _EMPTY",
     "            if (False\n                    or any(row.get(key) not in (None, '') for key in _EMPTY"),
    ("chain_counted_on_refused_rows", CAPTURE,
     "            pointer = seen.get('reply_to_guid') not in (None, '')\n            thread = _thread(row)\n",
     "            pointer = seen.get('reply_to_guid') not in (None, '')\n            thread = _thread(row)\n"
     "            if pointer:\n                counts['native_observed_reply_pointer'] += 1\n"),
    ("reply_counts_as_a_chain_too", CAPTURE,
     "            if replied:\n                counts['native_observed_thread_reply'] += 1\n            elif pointer:\n",
     "            if replied:\n                counts['native_observed_thread_reply'] += 1\n            if pointer:\n"),
    ("whitespace_count_takes_any_mismatch", CAPTURE,
     "    return type(stored) is str and type(native) is str and stored != native and stored == native.strip()\n",
     "    return type(stored) is str and type(native) is str and stored != native\n"),
    ("observation_labelled_v2", CAPTURE,
     "                row['guid'], chats[0][1], chats[0][2], event, True, content, FORMS_CONTRACT, row['date'], *thread)\n",
     "                row['guid'], chats[0][1], chats[0][2], event, True, content, 'imessage-existing-comparison/v2', row['date'], *thread)\n"),
    ("capture_verified_under_v2", CAPTURE,
     "        parsed = parse_reconciliation_snapshot(data, now=now, reader_contract=FORMS_CONTRACT)\n",
     "        parsed = parse_reconciliation_snapshot(data, now=now, reader_contract='imessage-existing-comparison/v2')\n"),
    # The ledger service.
    ("refresh_capture_labelled_v2", SERVICE,
     "    actual, data = service._snapshot(snapshot_id, FORMS_CONTRACT)\n",
     "    actual, data = service._snapshot(snapshot_id, ATTRIBUTED_CONTRACT)\n"),
    ("refresh_parses_under_v2", SERVICE,
     "    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=FORMS_CONTRACT)\n",
     "    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=ATTRIBUTED_CONTRACT)\n"),
    ("refresh_requires_the_v2_lane", SERVICE,
     "            if enrollment['lane'].reader_contract not in RECONCILIATION_CONTRACTS:\n",
     "            if enrollment['lane'].reader_contract != ATTRIBUTED_CONTRACT:\n"),
    ("unchanged_check_sees_a_relabel_as_a_change", SERVICE,
     "            if {**previous, 'reader_contract': actual['reader_contract']} == actual:\n",
     "            if previous == actual:\n"),
    ("publish_parses_under_the_current_reader_not_the_enrollments", SERVICE,
     "    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=contract)\n",
     "    native = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=FORMS_CONTRACT)\n"),
    ("publish_lane_not_rechecked_in_its_transaction", SERVICE,
     "        if enrollment['lane'].reader_contract != contract:\n            raise PolicyError('reconciliation_lane_required')\n",
     ""),
    ("publish_requires_the_v2_lane", SERVICE,
     "    if contract not in RECONCILIATION_CONTRACTS:\n        raise PolicyError('reconciliation_lane_required')\n    expected",
     "    if contract != ATTRIBUTED_CONTRACT:\n        raise PolicyError('reconciliation_lane_required')\n    expected"),
    ("links_validate_under_v2_only", SERVICE,
     "    if (enrollment['lane'].reader_contract not in RECONCILIATION_CONTRACTS\n",
     "    if (enrollment['lane'].reader_contract != ATTRIBUTED_CONTRACT\n"),
    ("validation_rehashes_under_v2", SERVICE,
     "    if service._snapshot(snapshot['snapshot_id'], snapshot['reader_contract'])[0] != snapshot:\n",
     "    if service._snapshot(snapshot['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != snapshot:\n"),
    ("pass_rehashes_under_v2", SERVICE,
     "                    actual = self._service._snapshot(snapshot_id, pinned[0]['reader_contract'])[0]\n",
     "                    actual = self._service._snapshot(snapshot_id, ATTRIBUTED_CONTRACT)[0]\n"),
    ("v3_lane_missing", LEDGER,
     "RECONCILIATION_CONTRACTS = ('imessage-existing-comparison/v2', 'imessage-existing-comparison/v3')\n",
     "RECONCILIATION_CONTRACTS = ('imessage-existing-comparison/v2',)\n"),
    ("revoke_moves_the_clock_for_v2_only", LEDGER,
     "                if row['lane'].reader_contract in RECONCILIATION_CONTRACTS:\n",
     "                if row['lane'].reader_contract == 'imessage-existing-comparison/v2':\n"),
    # The owner door's recovery.
    ("recovery_enrolls_v2", DOOR,
     "                reader_contract=FORMS_CONTRACT)\n            derived = {}\n",
     "                reader_contract='imessage-existing-comparison/v2')\n            derived = {}\n"),
    ("recovery_reads_its_capture_as_v2", DOOR,
     "            records = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=FORMS_CONTRACT)\n",
     "            records = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract='imessage-existing-comparison/v2')\n"),
    # The sync's guard.
    ("guard_counts_only_the_snapshot_lane", SYNC,
     '    "imessage-owner-snapshot/v1",\n    "imessage-existing-comparison/v2",\n    "imessage-existing-comparison/v3",\n',
     '    "imessage-owner-snapshot/v1",\n'),
    ("guard_counts_revoked_enrollments", SYNC,
     "        \"SELECT dataset_id, snapshot_json FROM ingest_provenance_enrollments WHERE state='active'\"\n",
     "        \"SELECT dataset_id, snapshot_json FROM ingest_provenance_enrollments\"\n"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="imessage-forms-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(base)}
        command = [sys.executable, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider", "-m", "not e2e and not live"]
        # A mutant is killed only by a failing test. Without a clean baseline, a broken
        # environment would read as every mutant killed.
        baseline = subprocess.run(command, cwd=base, env=env, capture_output=True, text=True, timeout=1800)
        if baseline.returncode != 0:
            tail = [line for line in baseline.stdout.splitlines() if "passed" in line or "failed" in line or "error" in line][-1:]
            print(json.dumps({"baseline": "failed", "returncode": baseline.returncode, "summary": tail}))
            return 2
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text()
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                print(results[-1], flush=True)
                continue
            target.write_text(original.replace(old, new))
            try:
                run = subprocess.run(command, cwd=base, env=env, capture_output=True, text=True, timeout=1800)
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                # pytest exits 1 only when tests ran and some failed; 2-5 mean the run itself broke.
                status = ("killed" if run.returncode == 1 and failing else
                          "SURVIVED" if run.returncode == 0 else f"run_broken_exit_{run.returncode}")
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
