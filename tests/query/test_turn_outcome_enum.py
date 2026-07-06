"""TurnOutcome enum includes all PRD §8.3 outcomes."""

from topos.query.session import TurnOutcome


def test_turn_outcome_enum_values() -> None:
    expected = {
        "memory_hit",
        "live_query",
        "expand_boundary",
        "requalify",
        "denied",
        # §C negotiation
        "narrow_request",
    }
    assert {outcome.value for outcome in TurnOutcome} == expected
