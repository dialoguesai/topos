"""No-DB registration smoke for the GEN-judged IMB phrasing lane (Wave B8)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from imb_generative_eval_cases import (  # noqa: E402
    IMB_GENERATIVE_CASES,
    IMB_GENERATIVE_CATALOG_VERSION,
    answerable_cases,
    poison_hit,
    unanswerable_cases,
)
from imbalance_eval_cases import IMBALANCE_CASES  # noqa: E402
from query_eval_cases import QUERY_CATALOG_VERSION  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-imb-phrasing")]


def test_imb_generative_catalog_version() -> None:
    assert IMB_GENERATIVE_CATALOG_VERSION == "qq-imb-gen-1"
    assert QUERY_CATALOG_VERSION == "qq-catalog-19"


def test_imb_generative_mirrors_imb_probes() -> None:
    assert len(IMB_GENERATIVE_CASES) == 10
    assert [c.id for c in IMB_GENERATIVE_CASES] == [f"IMBG{i}" for i in range(1, 11)]
    imb_queries = [c.query_text(None) for c in IMBALANCE_CASES]  # type: ignore[arg-type]
    gen_queries = [c.query_text(None) for c in IMB_GENERATIVE_CASES]  # type: ignore[arg-type]
    assert gen_queries == imb_queries
    assert {c.scope_id for c in IMB_GENERATIVE_CASES} == {
        c.scope_id for c in IMBALANCE_CASES
    }


def test_answerable_unanswerable_split() -> None:
    # Mirrors IMB negatives IMB1/IMB4/IMB8.
    assert {c.id for c in unanswerable_cases()} == {"IMBG1", "IMBG4", "IMBG8"}
    assert len(answerable_cases()) == 7
    for case in answerable_cases():
        assert case.answer_needles is not None, case.id
        assert case.anchors.ideal
        assert case.anchors.bad_confabulation


def test_poison_hit_helper() -> None:
    assert poison_hit("crypto is a scam per me", (("crypto is a scam",),)) == [
        "crypto is a scam"
    ]
    assert poison_hit("promising for remittances", (("crypto is a scam",),)) == []


def test_every_case_has_attribution_anchors() -> None:
    for case in IMB_GENERATIVE_CASES:
        assert case.anchors.ideal, case.id
        assert case.anchors.bad_confabulation or case.anchors.bad_noise, case.id
