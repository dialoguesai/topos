"""Mutation run over the owner's standing iMessage attestation (owner decision 1, 1 Oct 2026).

Every rule must be killed by a test: what the statement trusts and refuses (a second Apple ID, a row with no
account, a changed account list), the record's privacy, the standing principal's narrow reach, the automatic
enrollment and refresh (dry run first, no clock move for nothing, never a loss acknowledged), the scheduler's
hooks and the owner's settings surface.

Each mutant is one textual patch. As in `p2c_refresh_mutants.py`, the engine's `topos/`, `tests/`, `fixtures/`
and `scripts/` are copied into a scratch directory and each mutant is applied there, one at a time; the
worktree is never modified. A patch that no longer applies is reported as such, never as killed, and a
mutant is killed only by a failing test after a clean baseline.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/imessage_standing_mutants.py --out standing-mutants.json
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
STANDING = "topos/permissions_v2/imessage_standing.py"
EVIDENCE = "topos/permissions_v2/evidence.py"
LEDGER = "topos/permissions_v2/ingest_provenance.py"
SERVICE = "topos/permissions_v2/reconciliation_provenance.py"
SCHEDULE = "topos/ingestion/local_sync_schedule.py"
HANDLER = "topos/core/handlers/sources.py"
HTTP_DOOR = "topos/api/ingestion_sources.py"
TESTS = ["tests/permissions_v2/test_imessage_standing.py", "tests/permissions_v2/test_reconciliation_refresh.py",
         "tests/permissions_v2/test_imessage_provenance_forms.py", "tests/permissions_v2/test_ingest_provenance.py",
         "tests/ingestion/test_local_sync_schedule.py", "tests/sources/test_imessage_sync_settings_handlers.py"]

MUTANTS = [
    # What the statement refuses.
    ("an_unattested_account_passes", STANDING, "        if foreign:\n", "        if False:\n"),
    ("one_attested_identifier_suffices", STANDING,
     "                      if identifiers and not all(_digest(record[\"key\"], *item) in attested for item in identifiers))\n",
     "                      if identifiers and not any(_digest(record[\"key\"], *item) in attested for item in identifiers))\n"),
    ("rows_with_no_account_are_captured", STANDING,
     "        return None if accounts.get(rowid) else \"account_unknown\"\n", "        return None\n"),
    ("rows_another_enrollment_proves_are_captured", STANDING,
     "        if link is not None and link[0] != enrollment_id:\n            return \"row_owned_elsewhere\"\n", ""),
    ("any_statement_arms", STANDING, "    if statement != STANDING_STATEMENT:\n", "    if False:\n"),
    ("any_token_arms", STANDING,
     "    if type(accounts_token) is not str or not hmac.compare_digest(accounts_token, _token(record[\"key\"], previewed[\"accounts\"])):\n",
     "    if False:\n"),
    ("accounts_not_rechecked_at_the_statement", STANDING,
     "    if digests != previewed[\"accounts\"]:\n", "    if False:\n"),
    ("a_preview_with_no_account_is_kept", STANDING,
     "    if not digests:\n        raise PolicyError(\"standing_account_unavailable\")\n", ""),
    ("an_empty_identifier_counts", STANDING,
     "                                 if type(value) is str and value.strip() and len(value) <= 512)\n",
     "                                 if type(value) is str)\n"),
    ("the_scan_ignores_the_window", STANDING,
     "               + \" FROM message WHERE is_from_me=1 AND date>=? AND date<? ORDER BY ROWID LIMIT ?\")\n",
     "               + \" FROM message WHERE is_from_me=1 AND ?<=? ORDER BY ROWID LIMIT ?\")\n"),
    ("identifiers_kept_in_the_clear", STANDING,
     "    return hmac.new(bytes.fromhex(key), f\"{column}\\0{value}\".encode(\"utf-8\"), hashlib.sha256).hexdigest()\n",
     "    return value\n"),
    # Who may state.
    ("anyone_previews", STANDING,
     "    _principal, owner_id = _owner_principal(runtime)\n    now = int(time.time()) if now is None else now\n    path = record_path(runtime)\n    record = read_record(path)\n    if record is None or record[\"owner_id\"] != owner_id:\n        record = {",
     "    owner_id = runtime.protocol.ledger.identity.owner_id\n    now = int(time.time()) if now is None else now\n    path = record_path(runtime)\n    record = read_record(path)\n    if record is None or record[\"owner_id\"] != owner_id:\n        record = {"),
    ("any_channel_states", STANDING,
     "    if (principal is None or principal.cls != OWNER_APP or principal.channel not in {\"uds\", \"cp_relay\"}\n",
     "    if (principal is None or principal.cls != OWNER_APP\n"),
    ("the_relay_needs_no_owner", STANDING,
     "            or (principal.channel == \"cp_relay\" and principal.acting_user != owner_id)):\n", "            ):\n"),
    # The record.
    ("record_mode_unchecked", STANDING,
     "    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077\n",
     "    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1\n"),
    ("record_hard_link_accepted", STANDING,
     "    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077\n",
     "    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077\n"),
    ("record_accounts_unsorted_accepted", STANDING,
     "            or record[\"accounts\"] != sorted(set(record[\"accounts\"]))\n", ""),
    ("armed_without_accounts_accepted", STANDING,
     "            or (record[\"state\"] == \"armed\" and not record[\"accounts\"])):\n", "            ):\n"),
    ("record_written_readable_by_others", STANDING,
     "    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)\n",
     "    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o644)\n"),
    ("record_directory_unchecked", STANDING,
     "    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:\n        raise PolicyError(\"standing_record_invalid\")\n", ""),
    # The run.
    ("a_disarmed_record_runs", STANDING,
     "    if record is None or record[\"state\"] != \"armed\":\n        return {\"ran\": False, \"reason\": \"not_armed\"}\n",
     "    if record is None:\n        return {\"ran\": False, \"reason\": \"not_armed\"}\n"),
    ("another_owners_record_runs", STANDING, "    if record[\"owner_id\"] != owner_id:\n        run.update(", "    if False:\n        run.update("),
    ("runs_beside_the_owners_door", STANDING,
     "    if not _RECOVERY_LOCK.acquire(blocking=False):\n        return {\"ran\": False, \"reason\": \"busy\"}\n",
     "    if False:\n        return {\"ran\": False, \"reason\": \"busy\"}\n"),
    ("the_outcome_is_not_recorded", STANDING,
     "        if \"outcome\" in run:\n            _record_run(path, record, run)\n", "        pass\n"),
    ("enrolled_without_the_exact_check", STANDING,
     "            compare_existing_message(_canonical_row(db, record.message_id), record, dataset_id=dataset_id,\n"
     "                                     owner_id=owner_id)\n",
     "            pass\n"),
    ("refreshed_without_a_dry_run", STANDING,
     "        preview = refresh_existing(service, db, dry_run=True, **arguments)\n",
     "        preview = {}\n"),
    ("a_refresh_for_nothing_is_made", STANDING, "        if not changes and current:\n", "        if False:\n"),
    ("a_stale_enrollment_reads_as_current", STANDING,
     "        current = found[0][3] == generation and _read_json(found[0][1]).get(\"reader_contract\") == FORMS_CONTRACT\n",
     "        current = _read_json(found[0][1]).get(\"reader_contract\") == FORMS_CONTRACT\n"),
    ("a_v2_enrollment_reads_as_current", STANDING,
     "        current = found[0][3] == generation and _read_json(found[0][1]).get(\"reader_contract\") == FORMS_CONTRACT\n",
     "        current = found[0][3] == generation\n"),
    ("a_disabled_dataset_is_attempted", STANDING,
     "        if exc.code == \"ingest_source_disabled\":\n", "        if False:\n"),
    ("a_revoked_enrollment_is_refreshed", STANDING,
     "    if found and found[0][2] != \"active\":\n        return {\"skipped\": \"revoked\"}  # the owner revoked it; only the owner enrolls it again\n", ""),
    ("another_lane_is_refreshed", STANDING,
     "    if found and _read_json(found[0][1]).get(\"reader_contract\") not in RECONCILIATION_CONTRACTS:\n        return {\"skipped\": \"another_lane\"}\n", ""),
    ("nothing_to_prove_is_a_refusal", STANDING,
     "        if exc.code == \"reconciliation_empty\":\n", "        if False:\n"),
    ("a_refused_capture_is_kept", STANDING,
     "        if created is not None:\n            _discard(service, db, created)\n", "        pass\n"),
    ("a_capture_before_any_ledger_is_kept", STANDING,
     "            (service.root / (created[\"snapshot_id\"] + _lane(created[\"reader_contract\"]).suffix)).unlink()\n            return True\n",
     "            return False\n"),
    # When.
    ("a_sync_that_imported_nothing_runs", STANDING, "    if outcome != \"imported\":\n", "    if False:\n"),
    ("the_statement_does_not_make_a_run_due", STANDING,
     "    if type(record.get(\"attested_at\")) is int and last[\"at\"] < record[\"attested_at\"]:\n        return True\n", ""),
    ("never_due_weekly", STANDING, "    return now - last[\"at\"] >= CADENCE_SECONDS\n", "    return False\n"),
    ("retried_at_once", STANDING, "        return now - last[\"at\"] >= RETRY_SECONDS\n", "        return True\n"),
    # The standing principal's reach.
    ("standing_passes_every_owner_check", EVIDENCE,
     "    channels = {\"uds\", \"cp_relay\", STANDING_CHANNEL} if standing else {\"uds\", \"cp_relay\"}\n",
     "    channels = {\"uds\", \"cp_relay\", STANDING_CHANNEL}\n"),
    ("standing_enrolls_any_lane", LEDGER,
     "        _owner(self.binding, standing=reader_contract in RECONCILIATION_CONTRACTS)\n",
     "        _owner(self.binding, standing=True)\n"),
    ("standing_cannot_install", LEDGER,
     "        _owner(self.binding, standing=True)  # also the first enrollment the owner's standing statement makes\n",
     "        _owner(self.binding)\n"),
    ("standing_publishes_with_a_derivation", SERVICE,
     "    _owner(service.binding, standing=derive is None and not classifications)\n",
     "    _owner(service.binding, standing=True)\n"),
    ("standing_acknowledges_a_loss", SERVICE,
     "    _owner(service.binding, standing=not accept_uncovered and not accept_unproven)\n",
     "    _owner(service.binding, standing=True)\n"),
    # The scheduler's hooks.
    ("no_hand_over_after_a_sync", SCHEDULE,
     "    if schedule.get(\"source_id\") == \"imessage\":\n        _prove_after_sync(str(schedule.get(\"dataset_id\") or \"\"), status)\n", ""),
    ("hand_over_for_every_source", SCHEDULE,
     "    if schedule.get(\"source_id\") == \"imessage\":\n        _prove_after_sync(", "    if True:\n        _prove_after_sync("),
    ("the_tick_never_asks", SCHEDULE, "        summary[\"proof\"] = _prove_when_due()\n", ""),
    # The owner's surface.
    ("the_surface_is_anyones", HANDLER, "    if schedule_provided or standing_provided:\n", "    if schedule_provided:\n"),
    ("the_status_is_anyones", HANDLER,
     "        if getattr(current_principal(), \"cls\", None) == OWNER_APP:\n            sync_data[\"proof_standing\"]",
     "        if True:\n            sync_data[\"proof_standing\"]"),
    ("any_action_is_accepted", HANDLER,
     "            if action not in (\"status\", \"preview\", \"arm\", \"disarm\"):\n", "            if False:\n"),
    # The HTTP twin of the settings door.
    ("the_http_door_is_anyones", HTTP_DOOR,
     "    if schedule_provided or standing_provided:\n        # Raises 403 owner_mode_required for any principal but the owner's.\n",
     "    if schedule_provided:\n        # Raises 403 owner_mode_required for any principal but the owner's.\n"),
    ("the_http_status_is_anyones", HTTP_DOOR,
     "        if principal is not None and principal.cls == \"owner_app\":\n", "        if True:\n"),
    ("the_http_door_takes_any_action", HTTP_DOOR,
     "    if standing_provided and standing_request.get(\"action\") not in (\"status\", \"preview\", \"arm\", \"disarm\"):\n",
     "    if False:\n"),
]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="imessage-standing-mutants-") as scratch:
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
