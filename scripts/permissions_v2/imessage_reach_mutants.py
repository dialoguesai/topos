"""Mutation run over the proof's reach (owner decision, 1 Oct 2026: the reach follows the longest grant window).

Every new rule must be killed by a test: the coverage, reach and deletion bounds and their refusals; the coverage
the node's grants ask for; ceilings outliving every reach; the capture's slices, splits and bounds; the v3
reader's twelve reads' worth with every row checked; and the owner doors taking their bounds from the grants.

Each mutant is one textual patch. As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/`, `fixtures/`
and `scripts/` are copied into a scratch directory and each mutant is applied there, one at a time; the
worktree is never modified. A patch that no longer applies is reported as such, never as killed, and a
mutant is killed only by a failing test after a clean baseline.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/imessage_reach_mutants.py --out reach-mutants.json
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
DOOR = "topos/api/permissions_native_probe.py"
TESTS = ["tests/permissions_v2/test_imessage_proof_reach.py", "tests/permissions_v2/test_reconciliation_refresh.py",
         "tests/permissions_v2/test_native_imessage_probe.py", "tests/permissions_v2/test_imessage_reconciliation.py",
         "tests/ingestion/test_owner_snapshot.py"]

MUTANTS = [
    # The bounds.
    ("bounds_floor_not_enforced", SERVICE,
     "            or not REFRESH_MINIMUM_COVERAGE_SECONDS <= coverage_seconds <= PROOF_COVERAGE_CAP_SECONDS):\n",
     "            or not 0 <= coverage_seconds <= PROOF_COVERAGE_CAP_SECONDS):\n"),
    ("bounds_cap_not_enforced", SERVICE,
     "            or not REFRESH_MINIMUM_COVERAGE_SECONDS <= coverage_seconds <= PROOF_COVERAGE_CAP_SECONDS):\n",
     "            or not REFRESH_MINIMUM_COVERAGE_SECONDS <= coverage_seconds):\n"),
    ("bounds_take_any_type", SERVICE,
     "    if (type(coverage_seconds) is not int\n",
     "    if (False\n"),
    ("reach_has_no_margin", SERVICE,
     "    return coverage_seconds, coverage_seconds + DAY_SECONDS, coverage_seconds + 2 * DAY_SECONDS\n",
     "    return coverage_seconds, coverage_seconds, coverage_seconds + 2 * DAY_SECONDS\n"),
    ("deletion_at_the_reach", SERVICE,
     "    return coverage_seconds, coverage_seconds + DAY_SECONDS, coverage_seconds + 2 * DAY_SECONDS\n",
     "    return coverage_seconds, coverage_seconds + DAY_SECONDS, coverage_seconds + DAY_SECONDS\n"),
    ("keep_after_from_the_floor", SERVICE,
     "    keep_after_us = (now_seconds - coverage) * 1_000_000\n",
     "    keep_after_us = (now_seconds - REFRESH_MINIMUM_COVERAGE_SECONDS) * 1_000_000\n"),
    ("reach_from_the_floor", SERVICE,
     "            if window_start_us < (max(now_seconds, authorized_at) - reach) * 1_000_000:\n",
     "            if window_start_us < (max(now_seconds, authorized_at) - REFRESH_CAPTURE_REACH_SECONDS) * 1_000_000:\n"),
    ("deletion_from_the_floor", SERVICE,
     "    delete_before_us = (now_seconds - delete_after) * 1_000_000\n",
     "    delete_before_us = (now_seconds - REFRESH_DELETE_AFTER_SECONDS) * 1_000_000\n"),
    # Ceilings are never deleted; a legacy-refreshed v2 enrollment is not refreshed.
    ("ceiling_links_deleted_at_the_horizon", SERVICE,
     "                if identity.get('classification') is None and event_us is not None and event_us < delete_before_us:\n",
     "                if event_us is not None and event_us < delete_before_us:\n"),
    ("links_without_a_ceiling_kept_too", SERVICE,
     "                if identity.get('classification') is None and event_us is not None and event_us < delete_before_us:\n",
     "                if False:\n"),
    ("legacy_enrollment_refreshed", SERVICE,
     "            if enrollment['lane'].reader_contract == ATTRIBUTED_CONTRACT and enrollment['revision'] > 1:\n"
     "                raise PolicyError('reconciliation_refresh_legacy_enrollment')\n",
     ""),
    ("legacy_enrollment_from_its_third_revision", SERVICE,
     "            if enrollment['lane'].reader_contract == ATTRIBUTED_CONTRACT and enrollment['revision'] > 1:\n",
     "            if enrollment['lane'].reader_contract == ATTRIBUTED_CONTRACT and enrollment['revision'] > 2:\n"),
    ("legacy_refusal_for_every_v2_enrollment", SERVICE,
     "            if enrollment['lane'].reader_contract == ATTRIBUTED_CONTRACT and enrollment['revision'] > 1:\n",
     "            if enrollment['lane'].reader_contract == ATTRIBUTED_CONTRACT:\n"),
    # The coverage the grants ask for.
    ("coverage_ignores_the_grants", SERVICE,
     "                longest = max(longest, _longest_window(policy.model_dump()))\n",
     "                pass\n"),
    ("every_policy_refusal_reads_as_inactive", SERVICE,
     "                    if exc.code in _NOT_ACTIVE:\n",
     "                    if True:\n"),
    ("coverage_shrinks_on_an_unreadable_ledger", SERVICE,
     "        raise PolicyError('reconciliation_coverage_unavailable') from None\n",
     "        return REFRESH_MINIMUM_COVERAGE_SECONDS\n"),
    ("coverage_not_capped", SERVICE,
     "    return min(max(longest, REFRESH_MINIMUM_COVERAGE_SECONDS), PROOF_COVERAGE_CAP_SECONDS)\n",
     "    return max(longest, REFRESH_MINIMUM_COVERAGE_SECONDS)\n"),
    ("coverage_not_floored", SERVICE,
     "    return min(max(longest, REFRESH_MINIMUM_COVERAGE_SECONDS), PROOF_COVERAGE_CAP_SECONDS)\n",
     "    return min(longest, PROOF_COVERAGE_CAP_SECONDS)\n"),
    ("window_search_ignores_nesting", SERVICE,
     "        return max([found, *(_longest_window(item) for item in value.values())])\n",
     "        return found\n"),
    ("window_search_ignores_lists", SERVICE,
     "        return max([0, *(_longest_window(item) for item in value)])\n",
     "        return 0\n"),
    ("window_search_takes_any_value", SERVICE,
     "        found = own if type(own) is int and own > 0 else 0\n",
     "        found = int(own or 0)\n"),
    # The owner doors.
    ("refresh_door_ignores_the_grants", DOOR,
     "        coverage = proof_coverage_seconds(runtime.protocol.ledger, int(time.time()))\n",
     "        coverage = 30 * 86400\n"),
    ("refresh_door_keeps_the_floor_for_the_refresh", DOOR,
     "                    accept_uncovered=body.accept_uncovered_links, accept_unproven=body.accept_unproven_links,\n"
     "                    coverage_seconds=coverage)\n",
     "                    accept_uncovered=body.accept_uncovered_links, accept_unproven=body.accept_unproven_links)\n"),
    ("recovery_window_unbounded", DOOR,
     "        if window_start_us < (now_seconds - reach) * 1_000_000:\n            raise PolicyError('reconciliation_refresh_window_too_old')\n",
     ""),
    # The capture's slices.
    ("slices_longer_than_one_read", CAPTURE,
     "    step = SLICE_SECONDS * 1_000_000\n",
     "    step = 2 * SLICE_SECONDS * 1_000_000\n"),
    ("capture_window_unbounded", CAPTURE,
     "            or end > current or end - start > CAPTURE_MAX_SECONDS * 1_000_000):\n",
     "            or end > current):\n"),
    ("one_read_bound_unchanged", CAPTURE,
     "            or end > current or end - start > SLICE_SECONDS * 1_000_000):\n",
     "            or end > current or end - start > CAPTURE_MAX_SECONDS * 1_000_000):\n"),
    ("bounded_reads_never_split", CAPTURE,
     "            if exc.code not in _SPLIT_CODES or upper - lower <= _MIN_SLICE_SECONDS * 1_000_000:\n",
     "            if True:\n"),
    ("every_refusal_splits", CAPTURE,
     "            if exc.code not in _SPLIT_CODES or upper - lower <= _MIN_SLICE_SECONDS * 1_000_000:\n",
     "            if upper - lower <= _MIN_SLICE_SECONDS * 1_000_000:\n"),
    ("splits_below_a_day", CAPTURE,
     "            if exc.code not in _SPLIT_CODES or upper - lower <= _MIN_SLICE_SECONDS * 1_000_000:\n",
     "            if exc.code not in _SPLIT_CODES:\n"),
    ("a_refused_reads_rows_are_kept", CAPTURE,
     "            rows.append((row, chat))\n",
     "            captured.append((row, chat))\n"),
    ("reads_unbounded", CAPTURE,
     "        if reads > _CAPTURE_READS or time.monotonic() > deadline:\n",
     "        if time.monotonic() > deadline:\n"),
    ("reads_share_no_deadline", CAPTURE,
     "        if reads > _CAPTURE_READS or time.monotonic() > deadline:\n",
     "        if reads > _CAPTURE_READS:\n"),
    ("a_row_read_twice_is_kept", CAPTURE,
     "    if len({row['ROWID'] for row, _ in captured}) != len(captured):\n        raise PolicyError('native_probe_capture_changed')\n",
     ""),
    ("capture_rows_unbounded", CAPTURE,
     "    if len(captured) > FORMS_MAX_MESSAGES:\n        raise PolicyError('native_probe_capture_limit')\n",
     ""),
    ("capture_file_unbounded", CAPTURE,
     "        if len(data) > MAX_SNAPSHOT_BYTES:\n            raise PolicyError('native_probe_capture_limit')\n",
     ""),
    ("one_long_message_refuses_the_read", CAPTURE,
     "            if size > 64 * 1024:\n"
     "                # Past the reader's bound for one message (owner_snapshot.MAX_TEXT_BYTES): this row is not read,\n"
     "                # as a `text` column that long is not. It never refuses the read, which no split could help.\n"
     "                counts['native_text_unsupported'] += 1\n                continue\n",
     "            if size > 64 * 1024:\n                raise PolicyError('native_probe_text_limit')\n"),
    ("capture_bytes_counted_after_writing", CAPTURE,
     "        if captured_bytes > MAX_SNAPSHOT_BYTES:\n            raise PolicyError('native_probe_capture_limit')\n    return captured, counts\n",
     "    return captured, counts\n"),
    ("excluded_rows_not_counted", CAPTURE,
     "        counts.update(excluded)\n",
     ""),
    # The v3 reader: twelve reads' worth, every row checked.
    ("forms_reader_reads_one_read", READER,
     "    return _parse_snapshot(data, dataset_id, now=now, attributed=True, thread_replies=True, captions=True,\n"
     "                           slices=FORMS_SLICES)\n",
     "    return _parse_snapshot(data, dataset_id, now=now, attributed=True, thread_replies=True, captions=True,\n"
     "                           slices=1)\n"),
    ("reader_takes_any_slices", READER,
     "        if type(slices) is not int or not 1 <= slices <= FORMS_SLICES:\n            _reject(\"snapshot_size_unsupported\")\n",
     ""),
    ("reader_text_bound_one_read", READER,
     "        max_messages, max_text = MAX_MESSAGES * slices, MAX_TOTAL_TEXT_BYTES * slices\n",
     "        max_messages, max_text = MAX_MESSAGES * slices, MAX_TOTAL_TEXT_BYTES\n"),
    ("reader_checks_only_the_first_read", READER,
     "            for (value,) in db.execute(f'SELECT \"{column}\" FROM message LIMIT ?', (limit,)):\n",
     "            for (value,) in db.execute(f'SELECT \"{column}\" FROM message LIMIT 1001'):\n"),
    ("reader_message_bound_unscaled", READER,
     "        if len(native) > max_messages:\n",
     "        if len(native) > MAX_MESSAGES:\n"),
    ("comparison_checks_only_the_first_read", COMPARISON,
     "            for (value,) in db.execute(f'SELECT \"{column}\" FROM message LIMIT ?', (limit,)):\n",
     "            for (value,) in db.execute(f'SELECT \"{column}\" FROM message LIMIT 1001'):\n"),
    ("comparison_correspondence_one_read", COMPARISON,
     "        if len(native) != len(records) or len(native) > MAX_MESSAGES * slices:\n",
     "        if len(native) != len(records) or len(native) > MAX_MESSAGES:\n"),
]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="imessage-reach-mutants-") as scratch:
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
