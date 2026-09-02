"""Tests for the per-release eval aggregator's gate logic (scripts/run_release_eval.py).

The aggregator is what the release-topos skill runs; its tier-1 gate must FAIL the release on
any UAR/CER/sensitive-excess/deny-latency regression, and its marketing summary must only
surface publishable, pinned-ruler numbers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.private]

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run_release_eval.py"


def _load():
    spec = importlib.util.spec_from_file_location("run_release_eval", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rel = _load()


def _clean_report():
    return {
        "version": "9.9.9",
        "code_sha": "abc",
        "corpus_version": "privacy-corpus-1",
        "errors": {},
        "uar": {"uar": 0.0, "leaks": 0},
        "cer": {"cer": 0.0, "leaked_tokens": []},
        "minimality": {"firewall": {"sensitive_excess": 0}, "negotiated": {"sensitive_excess": 0}},
        "latency": {"paths": {"deny": {"p95": 1.0}}},
    }


def test_clean_report_passes():
    gates = rel._evaluate_gates(_clean_report())
    assert gates["passed"] is True and gates["failures"] == []


def test_uar_leak_fails_the_gate():
    r = _clean_report()
    r["uar"] = {"uar": 0.0833, "leaks": 1}
    gates = rel._evaluate_gates(r)
    assert gates["passed"] is False
    assert any("UAR" in f for f in gates["failures"])


def test_cer_leak_fails_the_gate():
    r = _clean_report()
    r["cer"] = {"cer": 0.05, "leaked_tokens": ["secret"]}
    assert rel._evaluate_gates(r)["passed"] is False


def test_sensitive_excess_fails_the_gate():
    r = _clean_report()
    r["minimality"]["negotiated"]["sensitive_excess"] = 1
    gates = rel._evaluate_gates(r)
    assert gates["passed"] is False
    assert any("sensitive_excess" in f for f in gates["failures"])


def test_slow_deny_fails_the_gate():
    r = _clean_report()
    r["latency"]["paths"]["deny"]["p95"] = 900.0
    assert rel._evaluate_gates(r)["passed"] is False


def test_pytest_gate_failure_fails_the_release():
    r = _clean_report()
    r["pytest_gate"] = {"passed": False, "returncode": 1, "summary": "1 failed, 126 passed"}
    gates = rel._evaluate_gates(r)
    assert gates["passed"] is False
    assert any("pytest" in f for f in gates["failures"])


def test_passing_pytest_gate_does_not_fail():
    r = _clean_report()
    r["pytest_gate"] = {"passed": True, "returncode": 0, "summary": "127 passed"}
    assert rel._evaluate_gates(r)["passed"] is True


def test_harness_error_fails_closed():
    r = _clean_report()
    r["errors"] = {"cer": "RuntimeError: boom"}
    gates = rel._evaluate_gates(r)
    assert gates["passed"] is False
    assert any("harness_error" in f for f in gates["failures"])


def test_summary_carries_n_and_wilson_bound():
    """§E1: the report schema gains n + upper_bound_95 beside each 0/N rate (additive)."""
    r = _clean_report()
    r["uar"].update({"n": 6, "upper_bound_95": 0.310784})
    r["cer"].update({"n": 270, "upper_bound_95": 0.009921})
    s = rel._marketing_summary(r)
    assert s["unauthorized_access_rate"] == 0.0
    assert s["unauthorized_access_n"] == 6
    assert s["unauthorized_access_upper_bound_95"] == 0.310784
    assert s["canary_extraction_n"] == 270
    assert s["canary_extraction_upper_bound_95"] == 0.009921


def test_summary_tolerates_batteries_without_bounds():
    """Older scorecards (no n/upper_bound_95) still summarize — fields are additive."""
    s = rel._marketing_summary(_clean_report())
    assert s["unauthorized_access_rate"] == 0.0
    assert s["unauthorized_access_n"] is None
    assert s["canary_extraction_upper_bound_95"] is None


def test_printed_rates_show_bound_pattern(capsys):
    """§E1: the printed line is `<rate> of <N> (95% UB X.X%)`, never a bare 0.0."""
    r = _clean_report()
    r["lane"] = "deterministic"
    r["uar"].update({"n": 6, "upper_bound_95": 0.310784})
    r["cer"].update({"n": 270, "upper_bound_95": 0.009921})
    r["gates"] = rel._evaluate_gates(r)
    rel._print_summary(r)
    out = capsys.readouterr().out
    assert "Unauthorized access rate : 0.0 of 6 (95% UB 31.1%)" in out
    assert "Canary extraction rate   : 0.0 of 270 (95% UB 1.0%)" in out


def test_printed_rates_fall_back_without_bounds(capsys):
    """A scorecard missing n/upper_bound_95 prints the bare rate, and an
    unmeasured rate still prints `unmeasured` (never `None`)."""
    r = _clean_report()
    r["lane"] = "deterministic"
    r["cer"] = {}
    r["gates"] = rel._evaluate_gates(r)
    rel._print_summary(r)
    out = capsys.readouterr().out
    assert "Unauthorized access rate : 0.0   (target 0)" in out
    assert "Canary extraction rate   : unmeasured" in out


def test_marketing_summary_only_has_publishable_fields():
    r = _clean_report()
    r.update({
        "negotiation": {"facts_reduction_ratio_open_over_negotiated": 4.0, "specificity_delta": 2},
        "gates": {"passed": True},
    })
    s = rel._marketing_summary(r)
    # Provenance is present (so numbers are traceable + comparable across versions).
    assert s["version"] and s["corpus_version"] and s["code_sha"]
    # The headline claims are present.
    assert s["unauthorized_access_rate"] == 0.0
    assert s["facts_reduction_open_over_negotiated"] == 4.0
    # No raw internal artifacts leak into the marketing record.
    assert "arm_results" not in s and "errors" not in s
