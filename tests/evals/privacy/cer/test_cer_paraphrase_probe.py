"""Paraphrase leak probe — proves literal CER can be false-green.

Not wired into run_release_eval hard gates. Markers:
  cer, private, check(C-quality-paraphrase-leak-probe)

Experiment: topos-research-wiki
  40_EXPERIMENTS/2026-08-03_paraphrase_leak_gate_false_green.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.evals.privacy.common.paraphrase_canaries import (
    CANARIES,
    DIAGNOSIS_SOFT,
    SALARY_BAND,
    THERAPY_THURSDAY,
    advisory_canary_report,
    classify_paraphrase_leak,
    public_result_with_text,
    synonym_discloses,
)
from tests.evals.privacy.common.probe import response_discloses

pytestmark = [
    pytest.mark.cer,
    pytest.mark.private,
    pytest.mark.check("C-quality-paraphrase-leak-probe"),
]


@pytest.mark.parametrize("canary", list(CANARIES), ids=lambda c: c.canary_id)
def test_literal_detector_blind_to_paraphrase_h_blind(canary):
    """H-blind: casefold substring misses the paraphrase for every canary family."""
    resp = public_result_with_text(canary.paraphrase)
    hits = response_discloses(resp, canary.secret_tokens)
    assert hits == [], (
        f"{canary.canary_id}: literal CER unexpectedly caught paraphrase tokens "
        f"{hits!r} — ConfAIde concern may already be narrowed for this family"
    )


@pytest.mark.parametrize("canary", list(CANARIES), ids=lambda c: c.canary_id)
def test_synonym_probe_catches_paraphrase_h_catchable(canary):
    """H-catchable: deterministic alias table flags the same response as a leak."""
    resp = public_result_with_text(canary.paraphrase)
    hits = synonym_discloses(resp, canary.aliases)
    assert hits, (
        f"{canary.canary_id}: synonym probe missed the paraphrase — widen aliases "
        "before claiming the instrument gap is actionable"
    )


@pytest.mark.parametrize("canary", list(CANARIES), ids=lambda c: c.canary_id)
def test_literal_detector_fires_on_verbatim_secret_nonvacuous(canary):
    """Control: planted secret still trips the gated detector."""
    resp = public_result_with_text(canary.verbatim_secret)
    hits = response_discloses(resp, canary.secret_tokens)
    assert hits, f"{canary.canary_id}: literal detector must remain non-vacuous"


@pytest.mark.parametrize("canary", list(CANARIES), ids=lambda c: c.canary_id)
def test_synonym_probe_quiet_on_clean_control(canary):
    """Control: clean text must not trip therapy/salary/diagnosis aliases."""
    resp = public_result_with_text(canary.clean_control)
    lit = response_discloses(resp, canary.secret_tokens)
    syn = synonym_discloses(resp, canary.aliases)
    assert lit == []
    assert syn == [], f"{canary.canary_id}: false positive on clean control: {syn!r}"


@pytest.mark.parametrize("canary", list(CANARIES), ids=lambda c: c.canary_id)
def test_false_green_conjunction(canary):
    """Success criterion: H-blind ∧ H-catchable on each family fixture."""
    resp = public_result_with_text(canary.paraphrase)
    literal_hits = response_discloses(resp, canary.secret_tokens)
    synonym_hits = synonym_discloses(resp, canary.aliases)
    classified = classify_paraphrase_leak(
        literal_hits=literal_hits, synonym_hits=synonym_hits
    )
    assert classified["false_green"], (
        f"{canary.canary_id}: need H-blind ∧ H-catchable "
        f"(tags={classified['tags']!r})"
    )
    assert "h_blind" in classified["tags"] and "h_catchable" in classified["tags"]


def test_advisory_report_tags_all_families() -> None:
    report = advisory_canary_report(CANARIES, response_discloses_fn=response_discloses)
    assert report["release_gate"] is False
    assert report["n"] == 3
    assert report["n_false_green"] == 3
    assert {r["canary_id"] for r in report["rows"]} == {
        THERAPY_THURSDAY.canary_id,
        SALARY_BAND.canary_id,
        DIAGNOSIS_SOFT.canary_id,
    }
    for row in report["rows"]:
        assert "false_green" in row["tags"]


def test_not_wired_into_release_eval_hard_gate() -> None:
    """Advisory policy: paraphrase probe must not appear as a release hard gate."""
    root = Path(__file__).resolve().parents[4]  # topos/
    release = root / "scripts" / "run_release_eval.py"
    text = release.read_text(encoding="utf-8")
    assert "paraphrase" not in text.lower()
    assert "C-quality-paraphrase-leak-probe" not in text
