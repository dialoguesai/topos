"""Mutation run over the fix for a node that never made one of the message tables (BL-108).

`evidence._copy_count` answers 0 for exactly one fact: the database's own catalog, asked inside the caller's read
transaction, holds nothing by the table's name. Each mutant here either takes the fix away or widens that one fact
into something a storage fault could also satisfy: every database error, every operational error, a catalog that
always says "absent", a catalog asked with no read transaction, a catalog that misses a name spelled in another
case, a catalog that sees tables only (so an unreadable view by the name reads as "absent"), and the index's own
count left on the old statement. Each must be killed by a failing test. Runs in a scratch copy of the engine, one
mutant at a time; the worktree is never modified. A mutant whose text no longer matches counts as a failure, not
a pass.

    <engine venv>/bin/python3 scripts/permissions_v2/fresh_node_tables_mutants.py --out fresh-node-mutants.json

Not listed because it is equivalent: asking the catalog before the count instead of after it fails (the same
answers; it would cost every read on every node one more statement).
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
EVIDENCE = "topos/permissions_v2/evidence.py"
INDEX = "topos/permissions_v2/search_index.py"
TESTS = ["tests/permissions_v2/test_fresh_node_message_tables.py",
         "tests/permissions_v2/test_lineage_fingerprint_copy_key.py",
         "tests/permissions_v2/test_bookkeeping_indexes.py"]

_GUARDED = ("    try:\n"
            "        return conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]\n"
            "    except sqlite3.OperationalError:\n"
            "        if conn.in_transaction and conn.execute(_CATALOG_NAME, (table,)).fetchone() is None:\n"
            "            return 0\n"
            "        raise\n")
_HANDLER = ("    except sqlite3.OperationalError:\n"
            "        if conn.in_transaction and conn.execute(_CATALOG_NAME, (table,)).fetchone() is None:\n"
            "            return 0\n"
            "        raise\n")

MUTANTS = [
    # The fix taken away: the count as it was on the live build.
    ("the_fix_reverted", EVIDENCE, _GUARDED,
     "    return conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]\n"),
    ("the_index_counts_the_old_way", INDEX, "    copies = sum(_copy_count(conn, table, content)\n",
     "    from .evidence import _COPY_COUNT\n"
     "    copies = sum(conn.execute(_COPY_COUNT.format(table=table), (content,)).fetchone()[0]\n"),
    # The one fact widened into what a storage fault also satisfies.
    ("every_database_error_is_no_copies", EVIDENCE, _HANDLER,
     "    except sqlite3.Error:\n        return 0\n"),
    ("every_operational_error_is_no_copies", EVIDENCE, _HANDLER,
     "    except sqlite3.OperationalError:\n        return 0\n"),
    ("the_catalog_always_says_absent", EVIDENCE,
     "        if conn.in_transaction and conn.execute(_CATALOG_NAME, (table,)).fetchone() is None:\n",
     "        if conn.in_transaction and True:\n"),
    ("the_catalog_is_asked_with_no_read_transaction", EVIDENCE,
     "        if conn.in_transaction and conn.execute(_CATALOG_NAME, (table,)).fetchone() is None:\n",
     "        if conn.execute(_CATALOG_NAME, (table,)).fetchone() is None:\n"),
    ("the_catalog_misses_a_name_in_another_case", EVIDENCE,
     '"SELECT 1 FROM sqlite_master WHERE name=?1 COLLATE NOCASE LIMIT 1"',
     '"SELECT 1 FROM sqlite_master WHERE name=?1 LIMIT 1"'),
    ("the_catalog_sees_tables_only", EVIDENCE,
     '"SELECT 1 FROM sqlite_master WHERE name=?1 COLLATE NOCASE LIMIT 1"',
     '"SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?1 COLLATE NOCASE LIMIT 1"'),
    ("an_unreadable_catalog_is_no_copies", EVIDENCE, _HANDLER,
     "    except sqlite3.OperationalError:\n"
     "        try:\n"
     "            listed = conn.in_transaction and conn.execute(_CATALOG_NAME, (table,)).fetchone() is None\n"
     "        except sqlite3.Error:\n"
     "            return 0\n"
     "        if listed:\n"
     "            return 0\n"
     "        raise\n"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def pytest(base, tests):
        run = subprocess.run([sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider"],
                             cwd=base, env={**env, "PYTHONPATH": os.pathsep.join(
                                 [str(base), *[part for part in env.get("PYTHONPATH", "").split(os.pathsep)
                                               if part and Path(part).resolve() != ROOT]])},
                             capture_output=True, text=True, timeout=1800)
        tail = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line][-1:]
        failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
        return run.returncode, tail, failing

    results = []
    with tempfile.TemporaryDirectory(prefix="fresh-node-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        tests = [test for test in TESTS if (base / test).exists()]
        code, tail, failing = pytest(base, tests)
        baseline = {"status": "pass" if code == 0 else "FAIL", "summary": tail, "failing": failing[:3], "tests": tests}
        print({"baseline": baseline}, flush=True)
        if code != 0:
            args.out.write_text(json.dumps({"baseline": baseline}, indent=2) + "\n")
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
                code, tail, failing = pytest(base, tests)
                # Killed only by a failing test: an error before any test ran proves nothing.
                status = "killed" if code != 0 and failing else "SURVIVED" if code == 0 else "ERRORED"
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by_count": len(failing),
                                "killed_by": [test.split("::", 1)[1] for test in failing][:6]})
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
