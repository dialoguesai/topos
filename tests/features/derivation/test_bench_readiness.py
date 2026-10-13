"""Stable, synthetic decision cases; no claims about extractor or answer accuracy."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from topos.features.derivation.bench_readiness import assess_bench_candidate, assess_readiness

CATALOG = json.loads((Path(__file__).parent / "fixtures/bench_readiness_cases.json").read_text())


def evaluate(case):
    return assess_readiness(
        **{key: CATALOG[key] for key in ("person_id", "purpose_id", "dataset_id")},
        observations=case["observations"], coverage=case["coverage"],
        as_of=datetime.fromisoformat(CATALOG["as_of"]))


@pytest.mark.parametrize("case", CATALOG["cases"], ids=lambda case: case["id"])
def test_readiness_contract(case):
    result = evaluate(case)
    assert result["status"] == case["expected_status"], case["need"]
    if case.get("expected_reason"):
        assert case["expected_reason"] in result["reason_codes"]


def test_duplicate_evidence_is_not_double_counted():
    result = evaluate(next(c for c in CATALOG["cases"] if c["id"] == "BR-030"))
    assert all(len(d["evidence"]) == 1 for d in result["dimensions"].values())


def test_foreign_evidence_ids_are_not_echoed():
    case = {**CATALOG["cases"][0], "observations": [{
        **CATALOG["cases"][0]["observations"][0], "dataset_id": "another-dataset",
        "source": {"table": "conversation_messages", "record_id": "foreign-secret"}}]}
    assert "foreign-secret" not in json.dumps(evaluate(case))


def test_naive_reference_clock_rejected():
    with pytest.raises(ValueError, match="timezone"):
        assess_readiness(person_id="a", purpose_id="b", dataset_id="c", as_of=datetime(2026, 10, 13))


@pytest.mark.parametrize("dimension,value,reason", [
    ("need", "inferred_possible_help", "current_need_unverified"),
    ("fit", "inferred_skill_match", "purpose_fit_unverified"),
    ("capability", "unknown", "capability_unverified"),
    ("readiness", "unknown", "readiness_unknown"),
    ("identity", "other-person", "readiness_scope_mismatch"),
])
def test_ready_bench_requires_each_independent_claim(dimension, value, reason):
    purpose = {"role_shape_id": CATALOG["purpose_id"], "need_status": "supported_current_need"}
    person = {"person_id": CATALOG["person_id"], "fit_status": "supported_purpose_match",
              "capability_status": "demonstrated"}
    readiness = evaluate(CATALOG["cases"][0])
    assert assess_bench_candidate(purpose, person, readiness)["status"] == "ready"
    if dimension == "need":
        purpose["need_status"] = value
    elif dimension == "identity":
        person["person_id"] = value
    elif dimension == "readiness":
        readiness["status"] = value
    else:
        person[dimension + "_status"] = value
    result = assess_bench_candidate(purpose, person, readiness)
    assert result["status"] == "candidate"
    assert reason in result["reason_codes"]


def test_actual_slate_adapter_does_not_promote_capability_to_readiness(monkeypatch):
    from topos.features.derivation import bench_evidence, social_bench
    from topos.analytics import dataset_resolution
    monkeypatch.setattr(bench_evidence, "help_purposes", lambda *a, **kw: [{
        "role_shape_id": "purpose-review", "label": "Review the release",
        "need_status": "inferred_possible_help"}])
    monkeypatch.setattr(dataset_resolution, "resolve_messaging_dataset", lambda *a: ("private-messages", None))
    monkeypatch.setattr(social_bench, "work_engagement", lambda *a: {})
    monkeypatch.setattr(social_bench, "find_candidates", lambda *a, **kw: [{
        "person_id": "person-alex", "name": "Alex", "capability_status": "demonstrated",
        "fit_status": "inferred_skill_match", "warmth_band": "warm"}])
    result = social_bench.build_bench_slate(None, now=datetime.fromisoformat(CATALOG["as_of"]))
    candidate = result["roles"][0]["candidates"][0]
    assert candidate["readiness_status"] == "unknown"
    assert candidate["readiness"]["coverage"] == "not_integrated"
    assert candidate["bench_qualification"]["status"] == "candidate"
    assert result["coverage"]["ready_candidate_count"] == 0
