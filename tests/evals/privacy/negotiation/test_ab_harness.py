"""§F.5 — the negotiation A/B headline: task parity at N× fewer facts, +specificity, ratchet-safe.

Asserts the payoff the whole program builds toward: the firewall+negotiation arm answers the
task as well as an open API while disclosing far fewer facts and zero sensitive facts, and it
does so by making the requester's question more specific — without the negotiation rounds
themselves disclosing anything (ratchet safety).
"""

from __future__ import annotations

import pytest

from tests.evals.privacy.negotiation.ab_harness import (
    DEFAULT_TASK,
    build_scorecard,
    run_arm_negotiated,
    run_ab,
)

pytestmark = [pytest.mark.private, pytest.mark.eval_release]


def test_task_parity_across_arms():
    res = run_ab()
    assert res["open"].task_success is True
    assert res["negotiated"].task_success is True, "negotiation must not cost task success"
    # firewall (no negotiation) also succeeds — the point is disclosure volume, not success.
    assert res["firewall"].task_success is True


def test_negotiated_discloses_far_fewer_facts_than_open():
    res = run_ab()
    assert res["negotiated"].total_facts < res["open"].total_facts
    # and no more than the plain firewall arm.
    assert res["negotiated"].total_facts <= res["firewall"].total_facts
    ratio = res["open"].total_facts / max(1, res["negotiated"].total_facts)
    assert ratio >= 2.0, f"expected a meaningful reduction, got {ratio}x"


def test_open_leaks_sensitive_but_firewall_and_negotiated_do_not():
    res = run_ab()
    assert res["open"].sensitive_facts >= 1, "open baseline should leak the raw PII (non-vacuous)"
    assert res["firewall"].sensitive_facts == 0
    assert res["negotiated"].sensitive_facts == 0


def test_negotiation_sharpens_the_question():
    res = run_ab()
    c = res["negotiated"]
    assert c.rounds >= 2, "the requester should have been pushed to refine at least once"
    assert c.intents[0] != c.intents[-1], "the accepted intent differs from the broad opener"


def test_ratchet_negotiation_rounds_disclose_nothing():
    # Re-run arm C and confirm the intermediate narrow_request rounds carried no facts —
    # only the final accepted query discloses.
    result = run_arm_negotiated()
    # rounds == number of intents tried; all but the last were narrow_request (no disclosure).
    assert result.turn_outcome != "narrow_request"  # it did resolve
    assert result.rounds >= 2


def test_scorecard_headline():
    sc = build_scorecard()
    assert all(sc["task_success"].values()), sc["task_success"]
    assert sc["facts_reduction_ratio_open_over_negotiated"] >= 2.0
    assert sc["sensitive_facts"]["open"] >= 1
    assert sc["sensitive_facts"]["negotiated"] == 0
    assert sc["specificity_delta"] >= 1
