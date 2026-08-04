"""A2.E3 / C5 — access-advantage ablation metrics (no live DB)."""

from __future__ import annotations

import pytest

from selector_eval_cases import (
    access_advantage_metrics,
    answer_shape_key,
    score_access_advantage_ablation,
    shapes_invariant,
    unauthorized_data_participated,
)

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]


def _empty_resp() -> dict:
    return {"public_result": {"answer_type": "summary", "summaries": []}, "turn_outcome": "ok"}


def _leak_resp() -> dict:
    return {
        "public_result": {
            "answer_type": "summary",
            "summaries": [{"text": "Alex Rivera salary canary AA-LEAK-1"}],
        },
        "turn_outcome": "ok",
    }


def test_answer_shape_and_invariance() -> None:
    a = _empty_resp()
    b = {
        "public_result": {"answer_type": "summary", "summaries": []},
        "turn_outcome": "denied",
    }
    assert shapes_invariant(a, b) is True
    assert shapes_invariant(a, _leak_resp()) is False
    assert answer_shape_key(a)["n_items"] == 0


def test_unauthorized_data_participated_rules() -> None:
    assert (
        unauthorized_data_participated(
            stores_touched=[], retrieval_strategy="selector_suppressed"
        )
        is False
    )
    assert (
        unauthorized_data_participated(
            stores_touched=["vector"], retrieval_strategy="selector_suppressed"
        )
        is True
    )
    assert (
        unauthorized_data_participated(
            stores_touched=[], retrieval_strategy="dimension_dump"
        )
        is True
    )
    # Fail closed when strategy missing.
    assert unauthorized_data_participated(stores_touched=[], retrieval_strategy=None) is True


def test_score_zero_advantage_suppress_equals_ablated() -> None:
    scored = score_access_advantage_ablation(
        case_id="SEL1",
        suppress_response=_empty_resp(),
        ablated_response=_empty_resp(),
        stores_touched=[],
        retrieval_strategy="selector_suppressed",
        leak_control_response=_leak_resp(),
    )
    assert scored["access_advantage"] == 0.0
    assert scored["unauthorized_data_participated"] is False
    assert scored["answer_invariant"] is True
    assert scored["leak_control_delta"] == 1.0


def test_score_detects_participation() -> None:
    scored = score_access_advantage_ablation(
        case_id="SEL1",
        suppress_response=_empty_resp(),
        ablated_response=_empty_resp(),
        stores_touched=["messages"],
        retrieval_strategy="selector_suppressed",
    )
    assert scored["access_advantage"] == 1.0
    assert scored["unauthorized_data_participated"] is True


def test_score_detects_non_invariant_ablation() -> None:
    scored = score_access_advantage_ablation(
        case_id="SEL1",
        suppress_response=_empty_resp(),
        ablated_response=_leak_resp(),
        stores_touched=[],
        retrieval_strategy="selector_suppressed",
    )
    assert scored["answer_invariant"] is False
    assert scored["access_advantage"] == 1.0


def test_access_advantage_metrics_gate_zero() -> None:
    rows = [
        {
            "case_id": "SEL1",
            "should_refuse": True,
            "access_advantage_ablation": score_access_advantage_ablation(
                case_id="SEL1",
                suppress_response=_empty_resp(),
                ablated_response=_empty_resp(),
                stores_touched=[],
                retrieval_strategy="selector_suppressed",
            ),
        },
        {
            "case_id": "SEL2",
            "should_refuse": True,
            "retrieval_strategy": "selector_suppressed",
            "stores_touched": [],
        },
        {
            # Permit rows are excluded from the aggregate.
            "case_id": "SEL-P1",
            "should_refuse": False,
            "retrieval_strategy": "query_aware",
            "stores_touched": ["vector"],
        },
    ]
    m = access_advantage_metrics(rows)
    assert m["n_cases"] == 2
    assert m["access_advantage_mean"] == 0.0
    assert m["n_zero_advantage"] == 2
    assert m["n_participated"] == 0
    assert m["access_advantage_wilson_ci"] is not None
    assert m["access_advantage_wilson_ci"][1] == 1.0


def test_access_advantage_metrics_detects_nonzero() -> None:
    rows = [
        {
            "case_id": "SEL1",
            "should_refuse": True,
            "retrieval_strategy": "dimension_dump",
            "stores_touched": [],
        }
    ]
    m = access_advantage_metrics(rows)
    assert m["access_advantage_mean"] == 1.0
    assert m["n_participated"] == 1
