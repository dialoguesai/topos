"""NR166 R0: one command loads both sharing doors on invented rows and writes one table.

`scripts/permissions_v2/request_load_harness.py` runs in a process of its own (it replaces the transport's runtime
and switches for the whole process), at its smallest: one share of eight members, two askers per door, a writer and
the sweep beside them. Pinned: it exits clean, every request releases the bytes of its reference pass (taken with
timing and attribution off), nothing is refused, and each cell carries what the table is made of.

Invented evidence only; no model, port or network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.public
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2" / "request_load_harness.py"


def run(tmp_path, *args):
    env = {**os.environ, "TOPOS_DATABASE_PATH": str(tmp_path / "scratch.sqlite"), "TOPOS_ENV_FILE": str(tmp_path / "env"),
           "TOPOS_KEY": "synthetic-load-harness-key", "PYTHONDONTWRITEBYTECODE": "1"}
    for name in [name for name in env if name.startswith("TOPOS_PERMISSIONS_V2_")] + ["LOG_FORMAT"]:
        env.pop(name, None)
    return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, cwd=SCRIPT.parents[2], capture_output=True,
                          text=True, timeout=300)


def test_one_command_loads_both_doors_and_writes_the_table(tmp_path):
    out = tmp_path / "out"
    done = run(tmp_path, "--out", str(out), "--scratch", str((tmp_path / "fixtures").resolve()), "--members", "8",
               "--askers", "2", "--cells", "grid", "--requests", "4", "--min-per-asker", "2", "--units", "3",
               "--spare", "2", "--sweep-interval", "1", "--writer-period", "0.05", "--universe", "1")
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads((out / "report.json").read_text())
    assert report["schema"] == "NR166-R0-load/v1" and [fixture["members"] for fixture in report["fixtures"]] == [8]
    assert report["fixtures"][0]["universe"]["entities"] == 300   # the boundary reads a spine no member touches
    assert [(cell["door"], cell["askers"], cell["kind"]) for cell in report["cells"]] == [
        ("answers", 2, "grid"), ("batch", 2, "grid")]
    for cell in report["cells"]:
        assert cell["requests"] == cell["latency_ms"]["n"] == 4 and not cell["truncated"]
        assert cell["refused"] == cell["mismatches"] == 0 and not cell["asker_errors"] and not cell["stragglers"]
        assert cell["latency_ms"]["p50"] > 0 and cell["latency_ms"]["p95"] >= cell["latency_ms"]["p50"]
        assert cell["request_gate_hold_ms"]["n"] == 4 and cell["request_gate_hold_ms"]["p50"] > 0
        assert cell["probe_read_ms"]["n"] > 0 and cell["writer"]["commits"] > 0
        sites = {site["site"].rsplit(":", 1)[1] for site in cell["sites"]}
        assert {"ingest_writer", "_read", "_transaction"} <= sites
        assert cell["fast_paths"]["n3a_boundary"]["first"] == 4 and cell["fast_paths"]["bl171_guard"]["hit"] >= 1
        assert cell["attribution_dropped"] == 0
    answers, batch = report["cells"]
    assert {"retrieve_for_answer"} <= {site["site"].rsplit(":", 1)[1] for site in answers["sites"] if not site["nested"]}
    assert answers["stages"]["stages_ms_p50"]["answer_recheck"] > 0
    assert {"_decide_batch"} <= {site["site"].rsplit(":", 1)[1] for site in batch["sites"] if not site["nested"]}
    assert batch["stages"]["stages_ms_p50"]["recheck"] > 0 and "n5_send" in batch["fast_paths"]
    table = (out / "table.md").read_text()
    assert table.startswith("# NR166 R0 load table") and "| 8 | answers | grid | 2 | 4 |" in table
    assert "## Gate sections by site and caller" in table
    same = run(tmp_path, "--compare", str(out / "report.json"), str(out / "report.json"))
    assert same.returncode == 0 and "0 of 2 cells differ" in same.stdout


def test_through_the_client_the_batch_and_the_owner_probes_meet_the_inbound_queue(tmp_path):
    """NR183: `--through-client` sends the batch door's frames and the owner's probes (stamped owner_app) through a
    real ControlPlaneClient's inbound queue, once per owner-lane value; the same bytes are released either way."""
    out = tmp_path / "out"
    done = run(tmp_path, "--out", str(out), "--scratch", str((tmp_path / "fixtures").resolve()), "--members", "8",
               "--doors", "batch", "--askers", "2", "--cells", "grid", "--requests", "4", "--min-per-asker", "2",
               "--units", "3", "--spare", "2", "--sweep-interval", "1", "--writer-period", "0.05", "--universe", "1",
               "--through-client", "2", "--owner-lane", "off,on", "--owner-lane-slots", "1")
    assert done.returncode == 0, done.stderr[-2000:]
    report = json.loads((out / "report.json").read_text())
    assert [(cell["inbound_slots"], cell["owner_lane"]) for cell in report["cells"]] == [(2, "off"), (2, "on")]
    for cell in report["cells"]:
        assert cell["requests"] == cell["latency_ms"]["n"] == 4 and cell["refused"] == cell["mismatches"] == 0
        assert cell["probe_read_ms"]["n"] > 0 and cell["probe_write_ms"]["n"] > 0 and not cell["stragglers"]
    off, on = (cell["inbound"] for cell in report["cells"])
    assert (off["lane"], off["reserved"], off["granted"]) == (False, 0, {})
    assert on["lane"] is True and on["reserved"] == 1 and on["granted"]["shared_general"] == 4
    assert on["granted"]["owner_general"] + on["granted"]["owner_reserved"] > 0
    assert "| 8 | batch | grid, 2 slots, lane off | 2 | 4 |" in (out / "table.md").read_text()
    refused = run(tmp_path, "--out", str(out), "--doors", "answers", "--through-client", "2")
    assert refused.returncode != 0 and "batch door only" in refused.stderr
