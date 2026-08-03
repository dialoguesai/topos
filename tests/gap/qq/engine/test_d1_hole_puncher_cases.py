"""No-DB registration smoke for D1.1–D1.5 hole-punchers."""

from __future__ import annotations

import pytest

from d1_hole_puncher_cases import (
    D1_1_COMMON_WORD_CASES,
    D1_2_MIXED_REAL_FAB_CASES,
    D1_3_DENIAL_ABSENCE_CASES,
    D1_4_PARAPHRASE_CASES,
    D1_5_INJECTION_CASES,
    D1_HOLE_PUNCHER_CASES,
    paraphrase_family_variance,
)
from query_eval_cases import QUERY_CATALOG_VERSION

pytestmark = [pytest.mark.check("C-quality-d1-hole-punchers")]


def test_catalog_version_includes_d1_hole_punchers() -> None:
    assert QUERY_CATALOG_VERSION == "qq-catalog-12"


def test_d1_1_reexports_nh_cases() -> None:
    ids = [c.id for c in D1_1_COMMON_WORD_CASES]
    assert ids == ["NH1", "NH2", "NH3"]
    assert all(c.negative and c.layer.startswith("negative_hard:") for c in D1_1_COMMON_WORD_CASES)


def test_d1_families_registered() -> None:
    assert [c.id for c in D1_2_MIXED_REAL_FAB_CASES] == ["D12-1", "D12-2", "D12-3"]
    assert [c.id for c in D1_3_DENIAL_ABSENCE_CASES] == ["D13-1", "D13-2", "D13-3"]
    assert [c.id for c in D1_4_PARAPHRASE_CASES] == [f"D14-{i}" for i in range(1, 6)]
    assert [c.id for c in D1_5_INJECTION_CASES] == ["D15-1", "D15-2", "D15-3"]

    assert all(c.negative for c in D1_HOLE_PUNCHER_CASES)
    assert all(c.layer.startswith("d1.") for c in D1_HOLE_PUNCHER_CASES)
    assert len({c.id for c in D1_HOLE_PUNCHER_CASES}) == len(D1_HOLE_PUNCHER_CASES)


def test_d1_4_family_fields_and_variance_helper() -> None:
    assert all(c.family_id == "D14F1" for c in D1_4_PARAPHRASE_CASES)
    assert [c.variant_index for c in D1_4_PARAPHRASE_CASES] == [1, 2, 3, 4, 5]
    fake_rows = [
        {"case_id": "D14-1", "scores": {"groundedness": 1.0}},
        {"case_id": "D14-2", "scores": {"groundedness": 0.5}},
        {"case_id": "D14-3", "scores": {"groundedness": 0.0}},
        {"case_id": "D14-4", "scores": {"groundedness": 1.0}},
        {"case_id": "D14-5", "scores": {"groundedness": 0.5}},
    ]
    var = paraphrase_family_variance(fake_rows)
    assert "D14F1" in var
    assert var["D14F1"]["n"] == 5
    assert var["D14F1"]["pstdev"] > 0


def test_score_pack_includes_family_id() -> None:
    from composition_eval_cases import score_composition
    from d1_hole_puncher_cases import D1_4_PARAPHRASE_CASES

    case = D1_4_PARAPHRASE_CASES[0]
    oracle = case.oracle(None)
    packed = score_composition(case, {"turn_outcome": "ok", "public_result": {"summaries": []}}, oracle)
    assert packed["family_id"] == "D14F1"
    assert packed["variant_index"] == 1
    assert packed["scores"]["groundedness"] == 1.0
