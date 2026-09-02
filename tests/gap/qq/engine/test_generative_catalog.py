"""No-DB registration smoke for the GEN calibration lane (D1.7 / Wave B2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generative_eval_cases import (  # noqa: E402
    ANSWERABLE_TARGET,
    GENERATIVE_CASES,
    GENERATIVE_CATALOG_VERSION,
    UNANSWERABLE_TARGET,
    answerable_cases,
    unanswerable_cases,
)
from query_eval_cases import QUERY_CATALOG_VERSION  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-gen-calibration")]


def test_generative_catalog_version() -> None:
    assert GENERATIVE_CATALOG_VERSION == "qq-gen-2"
    assert QUERY_CATALOG_VERSION == "qq-catalog-18"


def test_calibration_lane_is_15_plus_15() -> None:
    ans = answerable_cases()
    unans = unanswerable_cases()
    assert len(ans) == ANSWERABLE_TARGET == 15
    assert len(unans) == UNANSWERABLE_TARGET == 15
    assert len(GENERATIVE_CASES) == 30


def test_case_ids_unique() -> None:
    ids = [c.id for c in GENERATIVE_CASES]
    assert len(ids) == len(set(ids)), f"duplicate case ids: {ids}"


def test_g1_scoped_to_messages_not_ai_conversations() -> None:
    """Q2 lesson: docker/container evidence lives in messages — ai_conversations
    made the honest answer 'unknown' while the old forced-yes/no prompt passed."""
    g1 = next(c for c in GENERATIVE_CASES if c.id == "G1")
    assert g1.scope_id == "messages:read"
    assert g1.answerable is True
    assert "docker" in g1.query_text(None).lower()  # type: ignore[arg-type]


def test_answerable_cases_have_needles_for_calibration() -> None:
    """Calibration ECE/Brier only gets (confidence, correct) pairs when needles
    can produce a deterministic correctness bit — bare answerable cases are judge-only."""
    for case in answerable_cases():
        assert case.answer_needles is not None, case.id
        # Static factories must return at least one non-empty group without a DB.
        # Live-oracle cases (G3) may return [] on an empty conn — that is ok.
        if case.id == "G3":
            continue
        groups = case.answer_needles(None)  # type: ignore[arg-type]
        assert groups and all(groups[0]), case.id


def test_unanswerable_cases_span_multiple_scopes() -> None:
    scopes = {c.scope_id for c in unanswerable_cases()}
    assert len(scopes) >= 10
    assert "messages:read" in scopes
    assert "places:read" in scopes
