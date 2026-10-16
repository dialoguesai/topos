"""Mutation run over NR189: `ai_chat_messages.share_excluded` at every sharing read of `permissions_v2` (D-17).

Each mutant drops, or weakens, the filter at one site, or one decision of the two helpers it uses
(`topos.storage.share_excluded`): the evidence load, the member re-check, the owner's review queue, the automatic
review pass's page, the share page's counts, the share editor's catalog, the owner-attested snapshot's fact
selection; the row check's reading of the column, a row without the column, and a probe the caller already made.
Each must be killed by at least one test.

The n3c runner (`n3c_mutants.py`): a scratch copy of the engine, one mutant at a time, the worktree never modified.
A mutant is one or more exact edits; one whose text does not match exactly once counts as a failure, not a pass. A
run that hangs past its timeout is recorded as HUNG, never as killed.

    TOPOS_ENV_FILE=/dev/null .venv/bin/python3 scripts/permissions_v2/nr189_mutants.py --out nr189-mutants.json
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
HELPERS = "topos/storage/share_excluded.py"
EVIDENCE = "topos/permissions_v2/evidence.py"
INDEX = "topos/permissions_v2/search_index.py"
QUEUE = "topos/permissions_v2/message_evidence.py"
WORKER = "topos/permissions_v2/automatic_review_worker.py"
COUNTS = "topos/permissions_v2/share_counts.py"
CATALOG = "topos/permissions_v2/share_catalog.py"
SNAPSHOT = "topos/permissions_v2/ingest_snapshot_facts.py"
TESTS = ["tests/permissions_v2/test_nr189_share_excluded_sites.py", "tests/ingestion/test_share_excluded_selection.py"]

MUTANTS = [
    # The sites: each drops its filter.
    ("evidence_load_keeps_a_flagged_row", [
        (EVIDENCE, '        if table == "ai_chat_messages" and row_share_excluded(row):\n',
                   '        if False:\n')]),
    ("member_recheck_keeps_a_flagged_row", [
        (INDEX, "        rows = [row for row in rows if not row_share_excluded(row)]\n",
                "        rows = list(rows)\n")]),
    ("review_queue_lists_a_flagged_prompt", [
        (QUEUE, "               f\"WHERE m.sender_type IN ('human','user') AND {window}{kept}\")",
                "               f\"WHERE m.sender_type IN ('human','user') AND {window}\")")]),
    ("review_queue_ignores_the_column_it_read", [
        (QUEUE, 'kept = share_excluded_clause(conn, alias="m", present=SHARE_EXCLUDED in columns)',
                'kept = share_excluded_clause(conn, alias="m", present=False)')]),
    ("review_pass_page_holds_a_flagged_row", [
        (WORKER, "f\"AND julianday(event_at)<=julianday(?,'unixepoch'){kept}{scope} ORDER BY message_id LIMIT 200\",",
                 "f\"AND julianday(event_at)<=julianday(?,'unixepoch'){scope} ORDER BY message_id LIMIT 200\",")]),
    ("review_pass_page_filters_the_wrong_table", [
        (WORKER, '                    if table == "ai_chat_messages" else "")',
                 '                    if table == "conversation_messages" else "")')]),
    ("share_counts_count_a_flagged_row", [
        (COUNTS, "            where += share_excluded_clause(self.conn)\n",
                 "            where += \"\"\n")]),
    ("catalog_counts_a_flagged_row", [
        (CATALOG, '        where = f" WHERE {kept}" if kept else ""\n',
                  '        where = ""\n')]),
    ("catalog_filters_the_wrong_table", [
        (CATALOG, '                if table == "ai_chat_messages" else "")',
                  '                if table == "journal_entries" else "")')]),
    ("snapshot_facts_read_a_flagged_row", [
        (SNAPSHOT, "               f\"{share_excluded_clause(conn, alias='m')} ORDER BY m.event_at, m.message_id\")",
                   "               \" ORDER BY m.event_at, m.message_id\")")]),
    # The helpers.
    ("row_check_reads_only_a_true_bool", [
        (HELPERS, "    return value not in (None, 0)\n",
                  "    return value is True\n")]),
    ("row_check_reads_a_missing_column_as_flagged", [
        (HELPERS, "    except (KeyError, IndexError, TypeError):\n        return False\n    return value not in (None, 0)\n",
                  "    except (KeyError, IndexError, TypeError):\n        return True\n    return value not in (None, 0)\n")]),
    ("clause_probes_again_whatever_the_caller_knew", [
        (HELPERS, "    if not (share_excluded_present(conn, table) if present is None else present):\n",
                  "    if not share_excluded_present(conn, table):\n")]),
    ("clause_trusts_a_missing_probe_as_present", [
        (HELPERS, "    if not (share_excluded_present(conn, table) if present is None else present):\n",
                  "    if not (True if present is None else present):\n")]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="nr189-mutants-") as scratch:
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
                    run = subprocess.run([sys.executable, "-m", "pytest", *(selected[0] if selected else TESTS), "-q", "-x",
                                          "-p", "no:cacheprovider", "-m", "not e2e and not live"], cwd=base, env=env,
                                         capture_output=True, text=True, timeout=600)
                except subprocess.TimeoutExpired:
                    results.append({"mutant": name, "status": "HUNG", "summary": [], "killed_by": []})
                    print(results[-1], flush=True)
                    continue
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                errored = "error" in " ".join(tail) and not failing
                results.append({"mutant": name, "status": "killed" if run.returncode != 0 and not errored else
                                ("ERRORED" if errored else "SURVIVED"), "summary": tail, "killed_by": failing})
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
