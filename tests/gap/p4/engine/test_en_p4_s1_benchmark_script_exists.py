"""
Gap: Baselines — none → benchmark module runnable with --json output
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.gap

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "benchmarks" / "wiki_mvp_baselines.py"


def test_benchmark_script_runs_with_json_output() -> None:
    assert SCRIPT.is_file()
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--profile", "local", "--json"],
        capture_output=True,
        text=True,
        check=False,
        cwd=SCRIPT.parents[2],
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["profile"] == "local"
    assert "query_raw_p95_ms" in data
