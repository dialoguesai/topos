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
