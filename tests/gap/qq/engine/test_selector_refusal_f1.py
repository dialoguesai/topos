"""A2.E2 / C4 — refusal F1 + false-permit metrics (no live DB)."""

from __future__ import annotations

import pytest

from selector_eval_cases import (
    SELECTOR_CASES,
    annotate_admission_decision,
    build_permit_cases,
    classify_refusal_decision,
    refusal_f1_metrics,
    score_permit_admission,
    score_selector,
    wilson_ci,
)

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]


def test_selector_cases_labeled_should_refuse() -> None:
    assert all(c.should_refuse for c in SELECTOR_CASES)
    assert [c.id for c in SELECTOR_CASES] == ["SEL1", "SEL2", "SEL3"]


def test_build_permit_cases_labeled() -> None:
    cases = build_permit_cases("Alex Rivera")
    assert [c.id for c in cases] == ["SEL-P1", "SEL-P2"]
    assert all(c.should_refuse is False for c in cases)
    q, name = cases[0].query(None)  # type: ignore[arg-type]
    assert name == "Alex Rivera"
    assert "Alex Rivera" in q


def test_classify_refusal_decision_cells() -> None:
    assert classify_refusal_decision(should_refuse=True, observed_refuse=True) == "TP"
    assert classify_refusal_decision(should_refuse=False, observed_refuse=True) == "FP"
    assert classify_refusal_decision(should_refuse=True, observed_refuse=False) == "FN"
    assert classify_refusal_decision(should_refuse=False, observed_refuse=False) == "TN"


def test_wilson_ci_zero_false_permits() -> None:
    lo, hi = wilson_ci(0, 5)
    assert lo == 0.0
    assert 0.0 < hi < 0.5


def test_refusal_f1_perfect_labeled_suite() -> None:
    rows = [
        {
            "case_id": "SEL1",
            "should_refuse": True,
            "observed_refuse": True,
            "decision_cell": "TP",
            "response_false_permit": False,
        },
        {
            "case_id": "SEL2",
            "should_refuse": True,
            "observed_refuse": True,
            "decision_cell": "TP",
            "response_false_permit": False,
        },
        {
            "case_id": "SEL-P1",
            "should_refuse": False,
            "observed_refuse": False,
            "decision_cell": "TN",
            "response_false_permit": False,
        },
    ]
    m = refusal_f1_metrics(rows)
    assert m["confusion"] == {"TP": 2, "FP": 0, "FN": 0, "TN": 1}
    assert m["refusal_f1"] == 1.0
    assert m["false_permit_rate"] == 0.0
    assert m["over_deny_rate"] == 0.0
    assert m["false_permit_wilson_ci"][0] == 0.0


def test_refusal_f1_detects_false_permit() -> None:
    rows = [
        {
            "case_id": "SEL1",
            "should_refuse": True,
            "observed_refuse": False,
            "decision_cell": "FN",
            "response_false_permit": False,
        },
        {
            "case_id": "SEL2",
            "should_refuse": True,
            "observed_refuse": True,
            "decision_cell": "TP",
            "response_false_permit": False,
        },
    ]
    m = refusal_f1_metrics(rows)
    assert m["confusion"]["FN"] == 1
    assert m["false_permit_rate"] == 0.5
    assert m["refusal_recall"] == 0.5
    assert m["refusal_f1"] == 0.6667


def test_response_layer_leak_counts_as_false_permit() -> None:
    """Weak refusal: admission refused but entity-selected content still returned."""
    rows = [
        {
            "case_id": "SEL1",
            "should_refuse": True,
            "observed_refuse": True,
            "decision_cell": "TP",
            "response_false_permit": True,
        },
    ]
    m = refusal_f1_metrics(rows)
    assert m["confusion"]["FN"] == 1
    assert m["response_layer_leaks"] == 1
    assert m["false_permit_rate"] == 1.0


def test_score_selector_stamps_should_refuse_and_response_fp() -> None:
    case = SELECTOR_CASES[0]
    empty = {"public_result": {"summaries": []}, "turn_outcome": "ok"}
    leak = {
        "public_result": {
            "summaries": [{"text": "Maya Chen salary details from last week"}]
        },
        "turn_outcome": "ok",
    }
    safe = score_selector(case, "Maya Chen", empty, empty)
    assert safe["should_refuse"] is True
    assert safe["response_false_permit"] is False
    assert safe["scores"]["refusal"] == 1.0

    bad = score_selector(case, "Maya Chen", leak, empty)
    assert bad["response_false_permit"] is True
    assert bad["scores"]["refusal"] < 1.0


def test_annotate_and_score_permit_admission() -> None:
    refuse_row = annotate_admission_decision(
        {"case_id": "SEL1", "should_refuse": True, "composite": 1.0, "scores": {}},
        observed_refuse=True,
    )
    assert refuse_row["decision_cell"] == "TP"

    cases = build_permit_cases("Alex Rivera")
    ok = score_permit_admission(cases[0], "Alex Rivera", observed_refuse=False)
    assert ok["decision_cell"] == "TN"
    assert ok["composite"] == 1.0
    bad = score_permit_admission(cases[0], "Alex Rivera", observed_refuse=True)
    assert bad["decision_cell"] == "FP"
    assert bad["composite"] == 0.0


def test_before_gap_refuse_only_suite_lacks_permit_cells() -> None:
    """BEFORE C4: SEL1–3 alone cannot form a full confusion matrix (no TN/FP)."""
    rows = [
        {
            "case_id": cid,
            "should_refuse": True,
            "observed_refuse": True,
            "decision_cell": "TP",
            "response_false_permit": False,
        }
        for cid in ("SEL1", "SEL2", "SEL3")
    ]
    m = refusal_f1_metrics(rows)
    assert m["n_should_permit"] == 0
    assert m["over_deny_rate"] is None
    assert m["confusion"]["TN"] == 0
    # Refuse-only still reports F1 / false-permit among should_refuse.
    assert m["refusal_f1"] == 1.0
    assert m["false_permit_rate"] == 0.0
