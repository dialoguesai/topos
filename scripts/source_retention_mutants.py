"""Mutation run over the source retention floor (topos/sources/retention.py and the doors that read it).

Every mutant weakens one rule: what "older than the floor" means, what the removal keeps, that a batch is
one transaction, that the floor is persisted before anything goes, that a failed recompute stays due, that
every write door and the iMessage sync refuse older rows, that the dry run writes nothing, that the owner
door is the owner socket and a dry run by default, and that compaction checks the volume first. Each must be
killed by at least one test.

As `permissions_v2/n3c_mutants.py`: a scratch copy of the engine, one mutant at a time, the worktree never
modified. A mutant is one or more exact edits; one whose text does not match exactly once counts as a failure,
not a pass. A run that hangs past its timeout is recorded as HUNG, never as killed.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/source_retention_mutants.py --out retention-mutants.json
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

ROOT = Path(__file__).resolve().parents[1]
RET = "topos/sources/retention.py"
ROUTE = "topos/api/source_retention.py"
STORE = "topos/storage/canonical/canonical_store.py"
CONV = "topos/storage/canonical/conversations_tables.py"
SYNC = "topos/ingestion/local_sync.py"
TESTS = ["tests/sources/test_source_retention.py"]

MUTANTS = [
    # What "older than the floor" means.
    ("floor_is_inclusive", [
        (RET, "when is not None and limit is not None and when < limit",
              "when is not None and limit is not None and when <= limit")]),
    ("undated_counts_as_older", [
        (RET, "when is not None and limit is not None and when < limit",
              "limit is not None and (when is None or when < limit)")]),
    ("compared_as_text", [
        (RET, "WHERE source_id=? AND event_at < ? AND julianday(event_at) < julianday(?) ORDER BY",
              "WHERE source_id=? AND event_at < ? AND event_at < ? ORDER BY")]),
    ("future_floor_allowed", [
        (RET, "    if instant > current:\n", "    if False:\n")]),
    # What the removal keeps.
    ("attested_rows_removed", [
        (RET, "first = [r for r in _candidates(conn, sid, cutoff) if r[0] not in attested]",
              "first = list(_candidates(conn, sid, cutoff))")]),
    ("owner_only_markers_deleted", [
        (RET, '    "owner_only_records": "owner decision; deleting advances the protection clock",\n', "")]),
    ("quoted_evidence_kept", [
        (RET, "payload, dropped = _trim_payload_evidence(_load_any(row[1]), removed)",
              "payload, dropped = _load_any(row[1]), 0")]),
    ("emptied_conversation_kept", [
        (RET, "        _drop_emptied_conversations(conn, source_id, {(r[2], r[3]) for r in batch}, run)\n",
              "        pass\n")]),
    ("graph_projection_kept", [
        (RET, "        if graph:\n", "        if False:\n")]),
    ("entities_not_recounted", [
        (RET, '    "entity_mentions": "entities",\n', "")]),
    ("stats_never_flagged", [
        (RET, '    "stat_seen": "stats",\n', "")]),
    ("messenger_periods_kept", [
        (RET, 'f"DELETE FROM {table} WHERE dataset_id=? AND source_scope=? AND period_key < ? "',
              'f"SELECT 0 FROM {table} WHERE dataset_id=? AND source_scope=? AND period_key < ? "')]),
    # One transaction per batch; the floor first; a failed step stays due.
    ("batch_not_atomic", [
        (RET, "    add = run.add\n    with batched_writes(conn):\n",
              "    add = run.add\n    with with_db_write():\n")]),
    ("floor_after_removal", [
        (RET, "            set_retention_floor(conn, sid, cutoff)\n            attested = _attested_ids(conn)\n",
              "            attested = _attested_ids(conn)\n"),
        (RET, "            recompute, due = _finish(conn, sid, cutoff)\n",
              "            set_retention_floor(conn, sid, cutoff)\n            recompute, due = _finish(conn, sid, cutoff)\n")]),
    ("failed_step_cleared", [
        (RET, '            report[flag] = {"status": "failed", "error": type(exc).__name__}\n',
              '            report[flag] = {"status": "failed", "error": type(exc).__name__}\n'
              '            pending.pop(flag, None)\n')]),
    ("dry_run_writes_the_floor", [
        (RET, "    rows = _candidates(conn, sid, cutoff)\n    attested = _attested_ids(conn)\n",
              "    set_retention_floor(conn, sid, cutoff)\n    rows = _candidates(conn, sid, cutoff)\n"
              "    attested = _attested_ids(conn)\n")]),
    # No re-import, at every door.
    ("canonical_writer_ungated", [
        (STORE, "refusal = self._retention_refusal(message_id, record) or self._conversation_writer_gate(",
                "refusal = None or self._conversation_writer_gate(")]),
    ("batch_writer_ungated", [
        (CONV, "        records, below_floor = split_below_floor(self.conn, source_id, records)\n",
               "        below_floor = []\n")]),
    ("sync_reads_past_the_floor", [
        (SYNC, "        rows, below_floor = _drop_below_floor(batch.rows, floor_unix)\n",
               "        rows, below_floor = batch.rows, 0\n")]),
    ("preview_counts_older_rows", [
        (SYNC, "        kept = [when for when in new_times if when is None or when >= floor_unix]\n",
               "        kept = list(new_times)\n")]),
    # The owner door, and compaction.
    ("route_not_a_dry_run_by_default", [
        (ROUTE, '    dry_run = body.get("dry_run", True)\n    clear = body.get("clear", False)\n',
                '    dry_run = body.get("dry_run", False)\n    clear = body.get("clear", False)\n')]),
    ("route_any_principal", [
        (ROUTE, '    if principal is None or principal.cls != OWNER_APP or principal.channel != "uds":\n',
                '    if principal is None:\n')]),
    ("compaction_ignores_free_space", [
        (RET, "    elif free_disk < needed:\n", "    elif False:\n")]),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="retention-mutants-") as scratch:
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
                                          "-p", "no:cacheprovider"], cwd=base, env=env, capture_output=True, text=True,
                                         timeout=600)
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
