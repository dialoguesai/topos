"""Mutation run over the guards bookkeeping batch 3 adds (plan §9.3).

Each mutant is one edit to a scratch copy of this tree, exported from git so the copy is
the committed code. Its targeted tests then run: a mutant is KILLED when they fail. A
survivor is either a missing test or an equivalent mutant, and has to be argued for by
name in the report.

    TOPOS_ENV_FILE=<scratch> TMPDIR=<non-symlinked> python scripts/permissions_v2/bk3_mutants.py [--only NAME]
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
LINEAGE = "topos/storage/db/migrations/permissions_fact_lineage_keys_v1.py"
EVIDENCE = "topos/permissions_v2/evidence.py"
FLOOR = "topos/permissions_v2/canonical_floor.py"
RELEASE = "topos/permissions_v2/release.py"
FACT_RELEASE = "topos/permissions_v2/fact_release.py"
LEDGER = "topos/permissions_v2/ledger.py"
RETENTION = "topos/permissions_v2/ledger_retention.py"
INGEST = "topos/permissions_v2/ingest_provenance.py"

KEYS = "tests/permissions_v2/test_bk3_lineage_keys.py"
MIGRATION = "tests/storage/test_permissions_fact_lineage_keys_migration.py"
FLOOR_TESTS = "tests/permissions_v2/test_bk3_floor_checkpoint.py"
GATE = "tests/permissions_v2/test_bk3_gate_release.py"
IDS = "tests/permissions_v2/test_bk3_opaque_ids.py"
CONTRACT = "tests/permissions_v2/test_bk3_p2a_v3_contract.py"
RETENTION_TESTS = "tests/permissions_v2/test_bk3_ledger_retention.py"
CLOCK_TESTS = "tests/permissions_v2/test_bk3_ingest_source_clock.py"
OPERATIONAL = "tests/permissions_v2/test_bk3_operational_errors.py"
RELEASE_TESTS = "tests/permissions_v2/test_release.py"
FACT_TESTS = "tests/permissions_v2/test_fact_release.py"

# name -> (file, find, replace, targeted tests)
MUTANTS: dict[str, tuple] = {
    # R2/R3 candidate keys: each drops one reason a row is a candidate, or one exactness rule.
    "keys_drop_opaque_union": (EVIDENCE, "UNION SELECT object_id FROM permissions_v2_fact_key_opaque WHERE family='claim' AND state<>1)", ")", [KEYS]),
    "keys_drop_sibling_opaque": (LINEAGE, "\"UNION SELECT object_id FROM permissions_v2_fact_key_opaque WHERE family='refs' AND state<>1\")", "\"\")", [KEYS]),
    "keys_drop_substring_family": (LINEAGE, "\"UNION SELECT object_id FROM permissions_v2_fact_key_completion WHERE family='refs_substring' AND ({ranges}) \"", "\"\"", [KEYS]),
    "keys_ignore_an_id_past_int64": (LINEAGE, "f\"WHEN EXISTS (SELECT 1 FROM json_each({safe}) e WHERE json_type({_OBJECT},'$.id') IN ('text','integer') \"\n            f\"AND NOT {_usable(_OBJECT, '$.id')}) THEN 0 \"", "\"\"", [KEYS]),
    "keys_ignore_id_field": (LINEAGE, "f\"UNION SELECT trim(CAST(json_extract({_OBJECT},'$.id') AS TEXT), {_WS}) FROM json_each({safe}) e \"\n            f\"WHERE {_usable(_OBJECT, '$.id')}\"", "\"\"", [KEYS]),
    "keys_trim_without_charset": (LINEAGE, "trim(CAST(json_extract({_OBJECT},'$.record_id') AS TEXT), {_WS})", "trim(CAST(json_extract({_OBJECT},'$.record_id') AS TEXT))", [KEYS]),
    "keys_skip_duplicate_rule": (LINEAGE, "f\"WHEN EXISTS (SELECT 1 FROM json_tree({column}) t WHERE t.key IS NOT NULL GROUP BY t.parent, t.key \"\n            f\"HAVING count(*)>1) THEN 0 ELSE 1 END\")", "f\"ELSE 1 END\")", [KEYS]),
    "keys_accept_blob": (LINEAGE, "f\"CASE WHEN typeof({column})<>'text' THEN 0 \"", "f\"CASE WHEN 0 THEN 0 \"", [KEYS]),
    "keys_claim_without_ascii_guard": (LINEAGE, "f\"WHEN NOT {_ascii_clean(predicate)} OR NOT {_ascii_clean(value)} THEN 0 ELSE 1 END)\"", "f\"ELSE 1 END)\"", [KEYS]),
    "keys_ignore_object_type_updates": (LINEAGE, "AFTER UPDATE OF object_id, signal_dimension, object_type, ", "AFTER UPDATE OF object_id, signal_dimension, ", [KEYS]),
    "keys_no_replaced_row_cleanup": (LINEAGE, "+ _forget(\"NEW.object_id\") + _forget_replaced() + _key_new() + \" END\")", "+ _forget(\"NEW.object_id\") + _key_new() + \" END\")", [KEYS]),
    "keys_trust_without_trigger_check": (EVIDENCE, "if lineage_keys.installed(conn):\n            # Candidates by index", "if True:\n            # Candidates by index", [KEYS]),
    "keys_migration_keeps_stale_rows": (LINEAGE, "    for name in TABLES:\n        conn.execute(f\"DROP TABLE IF EXISTS {name}\")", "    pass", [KEYS, MIGRATION]),
    "keys_candidates_ignore_valid_to": (EVIDENCE, "\"SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL \"\n                            \"AND object_id<>? AND object_id IN (SELECT object_id FROM permissions_v2_fact_claim_keys WHERE claim_key=? \"", "\"SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' \"\n                            \"AND object_id<>? AND object_id IN (SELECT object_id FROM permissions_v2_fact_claim_keys WHERE claim_key=? \"", [KEYS, RELEASE_TESTS]),
    # R4 rollback floor checkpoint.
    "floor_ignores_the_schema_version": (FLOOR, "                or verified[3] != _schema_version(conn)\n", "", [FLOOR_TESTS]),
    "floor_skip_boundary_rows": (FLOOR, "or _boundary(conn, verified[0]) != verified[2]", "or False", [FLOOR_TESTS]),
    "floor_never_folds_fully_again": (FLOOR, "or self._monotonic() - self._full_fold_at >= FULL_FOLD_SECONDS", "or False", [FLOOR_TESTS]),
    "floor_checkpoint_without_verification": (FLOOR, "verified = self._verified\n        if (verified is None", "verified = (self._resume or (0, CHAIN_SEED)) + ((),)\n        if (verified is None", [FLOOR_TESTS]),
    "floor_publish_skips_full_fold": (FLOOR, "self._verified = None  # every consent write folds the whole prefix", "pass  # every consent write folds the whole prefix", [FLOOR_TESTS]),
    # R12 gate release and its post-checkpoint re-read.
    "gate_send_before_authority_reread": (RELEASE, "if self._authority_after_checkpoint(signed) != checkpointed:\n            raise PolicyError(\"authority_stale\")", "pass", [GATE]),
    "gate_reread_without_protection_sync": (RELEASE, "        with ledger._transaction() as db:\n            self.protocol._sync_protection(db)\n            return ledger._authority(db, signed.grant_id, now)[0]", "        with ledger._transaction() as db:\n            return ledger._authority(db, signed.grant_id, now)[0]", [GATE]),
    "gate_fact_door_sends_without_reread": (FACT_RELEASE, "if self._authority_after_checkpoint(signed) != checkpointed:\n            raise PolicyError(\"authority_stale\")", "pass", [FACT_TESTS]),
    # F1 opaque ids.
    "ids_from_the_canonical_counter": (RELEASE, "record_id = identity.record_id if key is None else opaque_record_id(key, grant_id=signed.grant_id,", "record_id = identity.record_id if True else opaque_record_id(key, grant_id=signed.grant_id,", [IDS]),
    "ids_one_key_for_every_grant": (RELEASE, "RecordKeys(self.record_keys).get(signed.grant_id, create=True)", "RecordKeys(self.record_keys).get(\"shared\", create=True)", [IDS]),
    "ids_ordinal_capability_still_releases": (RELEASE, "if signed.capability_version in self.retired:\n            raise PolicyError(\"capability_retired\")", "pass", [IDS]),
    "ids_order_follows_the_canonical_ids": (RELEASE, "                if key is not None:", "                if False:", [IDS]),
    "ids_door_takes_the_ordinal_default": (RELEASE, "        if signed.capability_version not in SOURCE_VIEWS:", "        if False:", [IDS]),
    "ids_v3_releases_the_v1_view": (RELEASE, "def source_view(capability: str) -> tuple:\n    return SOURCE_VIEWS.get(capability, (VIEW, MessageDisclosure))", "def source_view(capability: str) -> tuple:\n    return (VIEW, MessageDisclosure)", [IDS, CONTRACT]),
    # F3/F4 retention.
    "retention_deletes_the_tombstone": (RETENTION, "\"UPDATE p2a_requests SET envelope_json='' WHERE rowid IN (SELECT rowid FROM p2a_requests \"", "\"DELETE FROM p2a_requests WHERE rowid IN (SELECT rowid FROM p2a_requests \"", [RETENTION_TESTS]),
    "retention_ignores_expiry": (RETENTION, "WHERE envelope_json<>'' AND json_extract(envelope_json,'$.expires_at') < ? LIMIT ?)", "WHERE envelope_json<>'' AND ? IS NOT NULL LIMIT ?)", [RETENTION_TESTS]),
    "retention_unbounded_batch": (LEDGER, "compact_expired(conn, now=now)", "compact_expired(conn, now=now + 10 ** 9)", [RETENTION_TESTS]),
    # W3 ingest source clock.
    "clock_update_without_column_list": (INGEST, "operation_sql = \"UPDATE OF \" + \", \".join(_WATCHED_UPDATE_COLUMNS[table])", "operation_sql = \"UPDATE\"", [CLOCK_TESTS]),
    "clock_stops_watching_posture": (INGEST, "\"user_ingestion_sources\": (\"dataset_id\", \"source_id\", \"enabled\", \"posture\"),", "\"user_ingestion_sources\": (\"dataset_id\", \"source_id\", \"enabled\"),", [CLOCK_TESTS]),
    "clock_accepts_either_schema": (INGEST, "if (version not in (1, 2) or found != self._schema(conn, version)", "if (version not in (1, 2) or (found != self._schema(conn, 1) and found != self._schema(conn, 2))", [CLOCK_TESTS]),
    "clock_upgrade_without_generation_bump": (INGEST, "conn.execute(\"UPDATE ingest_provenance_state SET generation=generation+1 WHERE singleton=1\")\n                generation = conn.execute", "generation = conn.execute", [CLOCK_TESTS]),
    # F3/F5 node uniformity.
    "operational_error_escapes_the_transport": ("topos/permissions_v2/release_transport.py", "    except Exception:\n        # Recipient errors reveal no fact existence", "    except PolicyError:\n        # Recipient errors reveal no fact existence", [OPERATIONAL]),
}


def run(name: str, spec: tuple, export: Path, interpreter: str, env: dict) -> dict:
    target, find, replace, tests = spec
    with tempfile.TemporaryDirectory(prefix=f"bk3-mutant-{name}-", dir=str(export.parent)) as scratch:
        tree = Path(scratch) / "tree"
        shutil.copytree(export, tree, symlinks=True)
        path = tree / target
        source = path.read_text()
        if find not in source:
            return {"mutant": name, "status": "NOT_APPLIED", "reason": "pattern absent", "file": target}
        path.write_text(source.replace(find, replace, 1))
        result = subprocess.run([interpreter, "-m", "pytest", *tests, "-x", "-q", "-p", "no:cacheprovider"],
                                cwd=tree, env={**env, "PYTHONPATH": str(tree)}, capture_output=True, text=True)
        killed = result.returncode != 0
        tail = [line for line in result.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))][:3]
        return {"mutant": name, "file": target, "tests": tests, "status": "KILLED" if killed else "SURVIVED",
                "first_failures": tail}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--interpreter", default=sys.executable)
    args = parser.parse_args()
    env = {key: value for key, value in os.environ.items()}
    env.setdefault("TOPOS_KEY", "synthetic-mutation-key")
    with tempfile.TemporaryDirectory(prefix="bk3-mutants-") as staging:
        export = Path(staging) / "export"
        export.mkdir()
        subprocess.run(f"git -C {ROOT} archive HEAD | tar -x -C {export}", shell=True, check=True)
        results = []
        for name, spec in MUTANTS.items():
            if args.only and name not in args.only:
                continue
            outcome = run(name, spec, export, args.interpreter, env)
            results.append(outcome)
            print(f"{outcome['status']:12} {name}", flush=True)
    summary = {"total": len(results), "killed": sum(1 for r in results if r["status"] == "KILLED"),
               "survived": [r["mutant"] for r in results if r["status"] == "SURVIVED"],
               "not_applied": [r["mutant"] for r in results if r["status"] == "NOT_APPLIED"], "results": results}
    print(json.dumps({k: summary[k] for k in ("total", "killed", "survived", "not_applied")}, indent=2))
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if not summary["survived"] and not summary["not_applied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
