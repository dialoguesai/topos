"""Mutation run over the native evidence refresh's guards (RD8): every guard must be killed by a test.

Each mutant is one textual patch to the refresh service, its owner door, the capture it relies on,
or the pool probe. As in `p2c_mutants.py`, the engine's `topos/`, `tests/` and `fixtures/` are copied
into a scratch directory and each mutant is applied there, one at a time; the worktree is never
modified. A patch that no longer applies is reported as such, never as killed.

    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/p2c_refresh_mutants.py --out refresh-mutants.json
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
SERVICE = "topos/permissions_v2/reconciliation_provenance.py"
DOOR = "topos/api/permissions_native_probe.py"
CAPTURE = "topos/permissions_v2/native_imessage_probe.py"
PROBE = "scripts/permissions_v2/p2c_provenance_pool.py"
TESTS = ["tests/permissions_v2/test_reconciliation_refresh.py", "tests/permissions_v2/test_provenance_pool_probe.py",
         "tests/permissions_v2/test_native_imessage_probe.py", "tests/permissions_v2/test_reconciliation_provenance.py"]

MUTANTS = [
    ("service_owner_check", SERVICE,
     "    _owner(service.binding)\n    _identifier(dataset_id)\n    if owner_attestation",
     "    _identifier(dataset_id)\n    if owner_attestation"),
    ("service_attestation", SERVICE,
     "    if owner_attestation != OWNER_ATTESTATION:\n        raise PolicyError('ingest_owner_attestation_required')\n    if type(window_start_us)",
     "    if type(window_start_us)"),
    ("window_must_be_valid", SERVICE,
     "    if type(window_start_us) is not int or type(window_end_us) is not int or window_start_us >= window_end_us:\n"
     "        raise PolicyError('reconciliation_window_invalid')\n",
     ""),
    ("revoked_is_refused", SERVICE,
     "            if enrollment['state'] != 'active':\n                raise PolicyError('reconciliation_enrollment_revoked')\n",
     ""),
    ("unchanged_is_refused", SERVICE,
     "            if previous == actual:\n                raise PolicyError('reconciliation_refresh_unchanged')\n",
     ""),
    ("incomplete_is_refused", SERVICE,
     "            if [tuple(job) for job in jobs] != [('done', enrollment['revision'])]:\n"
     "                raise PolicyError('reconciliation_refresh_incomplete')\n",
     ""),
    ("ceiling_dropped_on_row_change", SERVICE,
     "                ceiling = before[0].get('classification') if before is not None else None",
     "                ceiling = (before[0].get('classification') if before is not None and before[0].get('row_revision') == match.canonical_revision else None)"),
    ("ceiling_never_carried", SERVICE,
     "                ceiling = before[0].get('classification') if before is not None else None",
     "                ceiling = None"),
    ("owned_elsewhere_is_refused", SERVICE,
     "                    raise PolicyError('reconciliation_row_owned_elsewhere')\n",
     "                    continue\n"),
    ("aged_links_are_deleted", SERVICE,
     "                conn.execute('DELETE FROM ingest_provenance_records WHERE message_id=? AND enrollment_id=?',\n"
     "                             (message_id, enrollment_id))\n",
     "                pass\n"),
    ("young_links_are_retired_not_deleted", SERVICE,
     "                if event_us is not None and event_us < keep_after_us:\n",
     "                if True:\n"),
    ("only_links_past_capture_reach_are_deleted", SERVICE,
     "                if event_us is not None and event_us < delete_before_us:\n",
     "                if event_us is not None and event_us < keep_after_us:\n"),
    ("band_links_are_retired_silently", SERVICE,
     "                if event_us is not None and event_us < keep_after_us:\n"
     "                    # Past every 30-day grant but still within a capture's reach: kept, unproven.\n"
     "                    counts['retired_aged'] += 1\n                    continue\n",
     ""),
    ("uncovered_is_refused", SERVICE,
     "            if uncovered and not accept_uncovered:\n                raise PolicyError('reconciliation_refresh_window_uncovered')\n",
     ""),
    ("early_end_counts_as_uncovered", SERVICE,
     "not window_start_us <= event_us <= window_end_us",
     "not window_start_us <= event_us"),
    ("mass_unproven_is_refused", SERVICE,
     "            if unmatched and unmatched * 2 > young_current and not accept_unproven:\n"
     "                raise PolicyError('reconciliation_refresh_mass_unproven')\n",
     ""),
    ("retired_links_never_count_toward_the_floor", SERVICE,
     "                if not current:\n                    counts['still_retired'] += 1\n                    continue\n",
     ""),
    ("changed_rows_do_not_count_toward_the_floor", SERVICE,
     "                if changed:\n                    counts['retired_row_changed'] += 1\n                else:",
     "                if False:\n                    counts['retired_row_changed'] += 1\n                else:"),
    ("capture_rehashed_after_writes", SERVICE,
     "            if service._snapshot(actual['snapshot_id'], ATTRIBUTED_CONTRACT)[0] != actual:\n"
     "                raise PolicyError('ingest_snapshot_changed')\n            service._enrollment(conn, enrollment_id, active=True",
     "            service._enrollment(conn, enrollment_id, active=True"),
    ("disabled_source_is_refused", SERVICE,
     "            service._enrollment(conn, enrollment_id, active=True, source_id='imessage')\n            result =",
     "            result ="),
    ("source_generation_is_refreshed", SERVICE,
     "(_json(actual), revision, generation, int(time.time())",
     "(_json(actual), revision, enrollment['source_generation'], int(time.time())"),
    ("clock_advances", SERVICE,
     "            conn.execute('UPDATE permissions_v2_protection_state SET generation=generation+1 WHERE singleton=1')\n"
     "            if dry_run:",
     "            if dry_run:"),
    ("dry_run_rolls_back", SERVICE,
     "            if dry_run:\n                raise _DryRun(dict(counts))\n",
     ""),
    ("named_capture_is_kept", SERVICE,
     "        if snapshot['snapshot_id'] in named:\n            return False\n",
     ""),
    ("discard_never_raises", SERVICE,
     "    except Exception:  # noqa: BLE001 -- best effort after the ledger decided; never fail the caller\n",
     "    except (OSError, KeyError):\n"),
    ("door_owner_socket_only", DOOR,
     "async def refresh(body: NativeRefreshRequest, principal=Depends(resolve_request_principal)):\n"
     '    """Re-prove',
     "async def refresh(body: NativeRefreshRequest, principal=Depends(resolve_request_principal)):\n"
     "    principal = principal if principal is None else __import__('dataclasses').replace(principal, channel='uds')\n"
     '    """Re-prove'),
    ("door_passes_the_window", DOOR,
     "window_start_us=window_start_us, window_end_us=window_end_us, dry_run=body.dry_run,",
     "window_start_us=0, window_end_us=2**62, dry_run=body.dry_run,"),
    ("door_discards_a_failed_capture", DOOR,
     "            except BaseException:\n                discard_capture(service, db, created)\n                raise\n",
     "            except BaseException:\n                raise\n"),
    ("door_discards_a_dry_run_capture", DOOR,
     "            if body.dry_run:\n                discard_capture(service, db, created)\n",
     "            if body.dry_run:\n"),
    ("door_skips_resync_on_dry_run", DOOR,
     "                return {'authority_created': False, 'counts': measured['counts'], 'refresh': result}\n",
     "                return {'authority_created': False, 'counts': measured['counts'], 'refresh': result, 'search': _resync_search(runtime)}\n"),
    ("door_checks_the_paired_owner", DOOR,
     "        if principal.acting_user and principal.acting_user != identity.owner_id:\n            raise PolicyError('owner_binding')\n"
     "        window_start_us",
     "        window_start_us"),
    ("door_one_at_a_time", DOOR,
     "    if not _RECOVERY_LOCK.acquire(blocking=False):\n        raise HTTPException(409, 'native_recovery_running')\n\n"
     "    def apply():\n        from dataclasses import replace\n        from topos.permissions_v2.runtime import get_runtime\n"
     "        from topos.permissions_v2.native_imessage_probe import capture_matching_snapshot\n"
     "        from topos.permissions_v2.fact_eligibility",
     "    _RECOVERY_LOCK.acquire(blocking=False)\n\n"
     "    def apply():\n        from dataclasses import replace\n        from topos.permissions_v2.runtime import get_runtime\n"
     "        from topos.permissions_v2.native_imessage_probe import capture_matching_snapshot\n"
     "        from topos.permissions_v2.fact_eligibility"),
    ("capture_skip_is_honoured", CAPTURE,
     "        if reason is not None:\n            excluded['excluded_' + reason] += 1\n            return\n",
     ""),
    ("probe_keeps_retired_links_out_of_the_pool", PROBE,
     "                    if link_revision != revision:\n",
     "                    if False:\n"),
    ("probe_refuses_the_live_tree", PROBE,
     '        raise SystemExit("refused: the probe reads copies only, never the live ~/.topos tree")',
     "        pass"),
    ("probe_refuses_hard_links", PROBE,
     '        raise SystemExit("refused: the probe reads single-link copies only")',
     "        pass"),
    ("probe_report_never_follows_a_symlink", PROBE,
     "os.O_TRUNC | os.O_NOFOLLOW, 0o600)",
     "os.O_TRUNC, 0o600)"),
    ("probe_imports_engine_only_with_scratch_db", PROBE,
     "    if not _engine_import_allowed():\n        return None\n",
     ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="p2c-refresh-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
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
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(base)}
                run = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider",
                                      "-m", "not e2e and not live"], cwd=base, env=env, capture_output=True, text=True,
                                     timeout=1800)
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                results.append({"mutant": name, "status": "killed" if run.returncode != 0 else "SURVIVED",
                                "summary": tail, "killed_by": failing})
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
