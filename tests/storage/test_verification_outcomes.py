import pytest
from topos.dashboard.verification import verification_outcome


@pytest.mark.parametrize("verdict,expected", [
    ("supported", "true"), ("contradicted", "false"), ("no_evidence", "unanswerable"),
    ("unable", None), ("future_verdict", None), (None, None), ({}, None),
])
def test_owner_verdict(verdict, expected):
    assert verification_outcome({"verdict": verdict}) == expected


@pytest.mark.parametrize("stances,expected", [
    (["supports", "no_evidence"], "true"), (["contradicts", "no_evidence"], "false"),
    (["supports", "contradicts"], "unanswerable"), (["no_evidence"], "unanswerable"),
    (["new_stance"], None), ([], None),
])
def test_legacy_lanes(stances, expected):
    assert verification_outcome({"lanes": {str(i): {"stance": s} for i, s in enumerate(stances)}}) == expected


def test_missing_and_error_never_become_abstentions():
    for value in (None, {}, {"abstained": True}, {"error": "failure", "verdict": "unable"}):
        assert verification_outcome(value) is None
