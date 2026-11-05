"""The pre-tag load gate's parser, verdict and guards, without a node.

The guard test arms the generated ``sitecustomize`` in a child interpreter with a
THROWAWAY "owner" home, so nothing here opens the real ``~/.topos``.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.load_gate import node, verdict
from scripts.load_gate.drive import sqlite_signature

# The 1.5.43 stress run's rollback line (5 Nov 2026), and the slow-section and watchdog shapes write_gate.py logs.
ROLLBACK_1543 = ("WARNING:topos.storage.db.write_gate:[WRITE_GATE] write at stores.py:1330 in put raised "
                 "DatabaseError: another row available; rolled back the transaction it opened so the connection "
                 "does not keep the write lock")
SLOW_AT = ("[WRITE_GATE] slow section at pipeline.py:912 in <lambda>: waited=12.5s held=31.3s — other writers "
           "were blocked for this long")
SLOW_BARE = "[WRITE_GATE] slow section: waited=61.0s held=2.0s — other writers were blocked for this long"
STUCK = ("[WRITE_GATE] transaction opened at stores.py:1330 in put (thread asyncio_3) has stayed open 41.0s "
         "without holding the gate — it holds SQLite's RESERVED write lock")


def clean_requests(**overrides):
    base = {"request_sqlite_errors": 0, "query_functional_rate": 1.0, "query_p95_s": 3.0, "health_failures": 0,
            "health_longest_s": 0.4, "transport_errors": 0, "recovery_ok": 5, "recovery_health_ok": True}
    return {**base, **overrides}


ISOLATED = {"serving_scratch": True, "live_db_unchanged": True, "guard_journal_empty": True}


class LogParserTests(unittest.TestCase):
    def test_reads_the_1543_rollback_with_site_and_error_type(self):
        parsed = verdict.parse_node_log(ROLLBACK_1543)
        self.assertEqual(parsed["rollbacks"],
                         [{"site": "stores.py:1330 in put", "error": "DatabaseError: another row available"}])
        # Counted once, as a rollback, not again as a SQLite error line.
        self.assertEqual(parsed["sqlite_error_lines"], 0)

    def test_longest_hold_and_wait_from_both_slow_section_shapes(self):
        parsed = verdict.parse_node_log("\n".join([SLOW_AT, SLOW_BARE, "unrelated line"]))
        self.assertEqual(parsed["longest_hold_s"], 31.3)
        self.assertEqual(parsed["longest_wait_s"], 61.0)
        self.assertEqual(parsed["slow_sections"][0]["site"], "pipeline.py:912 in <lambda>")

    def test_watchdog_and_sqlite_signatures_outside_the_gate(self):
        text = "\n".join([STUCK, "ERROR query failed: sqlite3.ProgrammingError: Recursive use of cursors not allowed",
                          "OperationalError: database is locked", "INFO all fine"])
        parsed = verdict.parse_node_log(text)
        self.assertEqual(parsed["stuck_transactions"], [{"site": "stores.py:1330 in put", "age_s": 41.0}])
        self.assertEqual(parsed["sqlite_error_lines"], 2)
        self.assertEqual(set(parsed["sqlite_error_signatures"]), {"recursive use of cursors", "database is locked"})

    def test_signature_matching_ignores_case_and_ordinary_text(self):
        self.assertEqual(sqlite_signature("DatabaseError: Another Row Available"), "another row available")
        self.assertIsNone(sqlite_signature("query request: scope='messages:read' mode='summary'"))


class VerdictTests(unittest.TestCase):
    def test_a_quiet_run_passes(self):
        result = verdict.evaluate(log=verdict.parse_node_log(""), requests=clean_requests(), isolation=ISOLATED)
        self.assertTrue(result["pass"], result["failures"])

    def test_one_rollback_fails_the_gate(self):
        result = verdict.evaluate(log=verdict.parse_node_log(ROLLBACK_1543), requests=clean_requests(),
                                  isolation=ISOLATED)
        self.assertEqual(result["failures"], ["write_gate_rollbacks"])

    def test_isolation_failure_and_missing_numbers_fail(self):
        result = verdict.evaluate(log=verdict.parse_node_log(""), requests=clean_requests(query_p95_s=None),
                                  isolation={**ISOLATED, "live_db_unchanged": False})
        self.assertEqual(set(result["failures"]), {"isolation", "query_p95_s"})
        missing = next(g for g in result["gates"] if g["gate"] == "query_p95_s")
        self.assertEqual(missing["status"], "missing")

    def test_recovery_needs_every_probe_and_a_healthy_node(self):
        result = verdict.evaluate(log=verdict.parse_node_log(""), requests=clean_requests(recovery_health_ok=False),
                                  isolation=ISOLATED)
        self.assertEqual(result["failures"], ["recovery_probes"])

    def test_a_hung_node_fails_its_own_gate(self):
        result = verdict.evaluate(log=verdict.parse_node_log(""), requests=clean_requests(hung_at_s=60.1),
                                  isolation=ISOLATED)
        self.assertEqual(result["failures"], ["node_hung"])

    def test_record_summary(self):
        records = [
            {"kind": "query", "phase": "load", "ok": True, "elapsed_s": 2.0},
            {"kind": "query", "phase": "load", "ok": False, "elapsed_s": 0.1, "sqlite_error": "another row available"},
            {"kind": "health", "phase": "load", "ok": True, "elapsed_s": 0.3},
            {"kind": "health", "phase": "load", "ok": False, "elapsed_s": 12.0, "transport_error": "ReadTimeout"},
            {"kind": "query", "phase": "recovery", "ok": True, "elapsed_s": 1.0},
            {"kind": "health", "phase": "recovery", "ok": True, "elapsed_s": 0.1},
        ]
        summary = verdict.summarize_records(records)
        self.assertEqual((summary["queries"], summary["queries_ok"]), (2, 1))
        self.assertEqual(summary["query_functional_rate"], 0.5)
        self.assertEqual(summary["request_sqlite_errors"], 1)
        self.assertEqual(summary["transport_errors"], 1)
        self.assertEqual((summary["health_failures"], summary["health_longest_s"]), (1, 12.0))
        self.assertEqual(summary["recovery_ok"], 1)

    def test_outcomes_count_only_answered_load_queries(self):
        records = [
            {"kind": "query", "phase": "load", "ok": True, "elapsed_s": 1.0, "outcome": "live_query"},
            {"kind": "query", "phase": "load", "ok": True, "elapsed_s": 1.0, "outcome": "denied"},
            {"kind": "query", "phase": "load", "ok": True, "elapsed_s": 1.0, "outcome": "live_query"},
            {"kind": "query", "phase": "load", "ok": False, "elapsed_s": 1.0, "outcome": None},
        ]
        self.assertEqual(verdict.summarize_records(records)["outcomes"], {"live_query": 2, "denied": 1})


# A faulthandler dump of the 1.5.43 tree under load (5 Nov 2026): a pool worker purging sessions through the
# event loop's connection while the loop reads a session on it. Paths shortened.
HUNG_STACKS = """Thread 0x0000007006043000 (most recent call first):
  File "/wt/topos/storage/adapters/sqlite/stores.py", line 1413 in _write
  File "/wt/topos/storage/db/write_gate.py", line 573 in retry_stale_snapshot
  File "/wt/topos/query/pipeline.py", line 372 in <lambda>
  File "/py/lib/python3.12/concurrent/futures/thread.py", line 58 in run

Thread 0x00000001726c7000 (most recent call first):
  File "/wt/topos/storage/adapters/sqlite/stores.py", line 1255 in get
  File "/wt/topos/query/pipeline.py", line 378 in _execute_turn

Current thread 0x00000001f0d1c0c0 (most recent call first):
  File "/py/lib/python3.12/selectors.py", line 566 in select
"""


class HangEvidenceTests(unittest.TestCase):
    def test_innermost_topos_frame_per_thread(self):
        from scripts.node_load_gate import _innermost_topos_frames

        self.assertEqual(_innermost_topos_frames(HUNG_STACKS),
                         ["storage/adapters/sqlite/stores.py:1255 in get",
                          "storage/adapters/sqlite/stores.py:1413 in _write"])


_GUARD_PROBE = r"""
import json, os, socket, sqlite3, subprocess, sys
out = {"armed": "topos_live_db_watch" in sys.modules}
owner_db = os.path.join(os.environ["TOPOS_LOAD_GATE_REAL_HOME"], ".topos", "database.db")
try:
    sqlite3.connect(owner_db)
    out["owner_write"] = "allowed"
except Exception as exc:
    out["owner_write"] = type(exc).__name__
scratch_db = os.path.join(os.environ["HOME"], ".topos", "database.db")
sqlite3.connect(scratch_db).close()
out["scratch_write"] = "allowed"
def connect(port):
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port))
        return "connected"
    except Exception as exc:
        return type(exc).__name__
    finally:
        s.close()
out["allowed_port"] = connect(int(sys.argv[1]))
out["other_port"] = connect(int(sys.argv[2]))
try:
    subprocess.run(["open", "-a", "Nothing"], capture_output=True)
    out["open"] = "ran"
except Exception as exc:
    out["open"] = type(exc).__name__
print(json.dumps(out))
"""


class GuardTests(unittest.TestCase):
    def test_guard_refuses_owner_writes_egress_and_app_launch(self):
        with tempfile.TemporaryDirectory(prefix="lg-guard-") as tmp:
            root, owner = Path(tmp) / "run", Path(tmp) / "owner"
            (root / "h" / ".topos").mkdir(parents=True)
            (owner / ".topos").mkdir(parents=True)
            guard = node.write_guard_site(root / "guard")
            allowed, other = node.free_port(), node.free_port()
            env = {**os.environ, "HOME": str(root / "h"), "PYTHONPATH": str(guard),
                   "TOPOS_LOAD_GATE_ROOT": str(root), "TOPOS_LOAD_GATE_REAL_HOME": str(owner),
                   "TOPOS_LOAD_GATE_LOOPBACK_PORTS": str(allowed),
                   "TOPOS_LOAD_GATE_EGRESS_LOG": str(root / "egress.txt"),
                   "TOPOS_LIVE_DB_WATCH_MODULE": str(node.WATCH_MODULE_PATH),
                   "TOPOS_LIVE_DB_JOURNAL": str(root / "journal.txt")}
            env.pop("TOPOS_TEST_ALLOW_OWNER_DB_WRITES", None)
            out = subprocess.run([sys.executable, "-c", _GUARD_PROBE, str(allowed), str(other)], env=env,
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            seen = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertTrue(seen["armed"])
            self.assertNotEqual(seen["owner_write"], "allowed")
            self.assertFalse((owner / ".topos" / "database.db").exists(), "refused only after SQLite made the file")
            self.assertEqual(seen["scratch_write"], "allowed")
            self.assertEqual(seen["allowed_port"], "ConnectionRefusedError")
            self.assertEqual(seen["other_port"], "PermissionError")
            self.assertEqual(seen["open"], "PermissionError")
            journal = (root / "journal.txt").read_text()
            self.assertIn("refused", journal)
            self.assertIn(str(owner), journal)
            self.assertEqual(len((root / "egress.txt").read_text().splitlines()), 2)


class HeavyRunTests(unittest.TestCase):
    def test_other_runs_are_found_and_this_runs_own_ancestors_are_not(self):
        me, parent = os.getpid(), os.getppid()
        table = "\n".join([
            f"{parent} 1 zsh -c uv run python scripts/node_load_gate.py",
            f"{me} {parent} python scripts/node_load_gate.py --repeats 3",
            "4242 1 python scripts/run_any_to_any_release.py",
            "4343 1 python scripts/stress_30.py --profile focused-45",
            "4444 1 python something_else.py",
        ])
        with patch.object(node.subprocess, "run") as run:
            run.return_value.stdout = table
            hits = node.other_heavy_runs()
        self.assertEqual(sorted(hits), ["4242 run_any_to_any_release.py", "4343 stress_30.py"])


class LiveNodeActionTests(unittest.TestCase):
    def test_actions_in_and_just_before_the_run_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(node.live_node_actions(0, 1, home=home))
            ledger = home / node.LIVE_LEDGER
            ledger.parent.mkdir(parents=True)
            conn = sqlite3.connect(ledger)
            conn.execute("CREATE TABLE p2a_system_actions (action_id TEXT, recorded_at REAL, receipt_json TEXT)")
            start = 1_000_000.0
            rows = [(start - 3600, {"action": "interest_refresh", "rebuild_requested": 2}),
                    (start - 600, {"action": "interest_refresh", "rebuild_requested": 2}),
                    (start + 60, {"action": "search_index_restore"}),
                    (start + 900, {"action": "interest_refresh", "rebuild_requested": 0})]
            conn.executemany("INSERT INTO p2a_system_actions VALUES (?, ?, ?)",
                             [(str(i), at, json.dumps(r)) for i, (at, r) in enumerate(rows)])
            conn.commit()
            conn.close()
            actions = node.live_node_actions(start, start + 300, home=home)
        # The hour-old refresh is outside the look-back; the one after the run ended is not reported.
        self.assertEqual([(a["action"], a["in_run"], a["rebuild_requested"]) for a in actions],
                         [("interest_refresh", False, 2), ("search_index_restore", True, None)])


if __name__ == "__main__":
    unittest.main()
