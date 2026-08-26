"""L5 kernels over L1 — warmth, drift and reciprocity.

The first lenses whose substrate is the social graph rather than the fact store. They exist
because L1 made the inputs real: counts each way, streaks at two grains, gaps, and an
own-baseline drift ratio.
"""

from __future__ import annotations

import pytest

from topos.features.derivation.kernels import registered_kinds
from topos.features.derivation.social_kernels import (
    DEFAULT_MIN_MESSAGES,
    apply_evidence_floor,
    compute_drift,
    compute_reciprocity,
    compute_warmth,
)


def _dyad(peer, msgs=50, sent=25, recv=25, recip=3, gap=5.0, drift=1.0, streak=3):
    return {"dataset_id": "ds", "a_key": "self", "b_key": peer, "total_msgs": msgs,
            "a_to_b": sent, "b_to_a": recv,
            "balance": round((sent - recv) / msgs, 4) if msgs else None,
            "reciprocal_periods": recip, "active_periods": recip,
            "reciprocal_streak_weeks": streak, "recent_gap_days": gap,
            "drift_ratio": drift, "median_gap_days": 2.0, "tie_state": "active"}


# --- the evidence floor ---

def test_a_single_inbound_message_is_an_event_not_a_relationship():
    """Measured: without a floor, 100 of 151 dyads read 'peer_carries', almost all of them
    one inbound text the owner never answered, each scoring a perfect -1.00."""
    thin = _dyad("+1555", msgs=1, sent=0, recv=1, recip=0)
    kept, excluded = apply_evidence_floor([thin])
    assert kept == [] and excluded == 1


def test_the_floor_is_applied_before_the_thresholds_are_drawn():
    """Excluded dyads must not move the distribution they are excluded from. On live data
    thin dyads dragged the volume p75 down to TEN messages, banding everyone else wrongly."""
    real = [_dyad(f"+p{i}", msgs=200) for i in range(4)]
    noise = [_dyad(f"+n{i}", msgs=1, sent=0, recv=1, recip=0) for i in range(50)]
    out = compute_warmth(real + noise)
    assert out[0]["threshold_basis"]["n_dyads"] == 4
    assert out[0]["threshold_basis"]["excluded_below_floor"] == 50
    assert out[0]["threshold_basis"]["volume_p75"] == 200.0


def test_the_floor_needs_both_volume_and_reciprocity():
    assert apply_evidence_floor([_dyad("+a", msgs=DEFAULT_MIN_MESSAGES, recip=0)])[0] == []
    assert apply_evidence_floor([_dyad("+b", msgs=1, recip=5)])[0] == []
    assert len(apply_evidence_floor([_dyad("+c", msgs=DEFAULT_MIN_MESSAGES, recip=1)])[0]) == 1


# --- warmth ---

def test_someone_never_answered_is_not_a_lapsed_relationship():
    """`never_direct` is separate from `dormant` on purpose. Ranking them together is how a
    contact list starts looking like a friendship list."""
    rows = [_dyad(f"+p{i}", msgs=100) for i in range(4)] + [
        _dyad("+never", msgs=40, sent=40, recv=0, recip=0)]
    out = {r["peer_key"]: r["warmth_band"] for r in compute_warmth(rows)}
    assert "+never" not in out, "below the reciprocity floor entirely"


def test_a_warm_relationship_that_stopped_is_dormant_not_warm():
    rows = [_dyad(f"+p{i}", msgs=100, gap=2.0) for i in range(4)]
    rows.append(_dyad("+stopped", msgs=400, gap=300.0))
    out = {r["peer_key"]: r["warmth_band"] for r in compute_warmth(rows)}
    assert out["+stopped"] == "dormant", "volume is not warmth once it has stopped"


def test_warmth_is_calibrated_against_the_owners_own_distribution():
    """A band drawn at a global constant says more about how the owner texts than about
    which relationships are close."""
    quiet = compute_warmth([_dyad(f"+q{i}", msgs=10 + i, gap=1.0) for i in range(8)])
    loud = compute_warmth([_dyad(f"+l{i}", msgs=1000 + i, gap=1.0) for i in range(8)])
    assert "warm" in {r["warmth_band"] for r in quiet}
    assert "warm" in {r["warmth_band"] for r in loud}, (
        "a quiet correspondent should be able to be warm; a loud one should not all be")


def test_every_row_records_the_thresholds_it_was_computed_under():
    out = compute_warmth([_dyad(f"+p{i}", msgs=100) for i in range(4)])
    assert set(out[0]["threshold_basis"]) >= {"volume_p75", "recent_gap_p66", "n_dyads"}


# --- drift ---

def test_drift_compares_a_dyad_only_against_itself():
    """A global 'you haven't talked in 30 days' fires on every monthly correspondent and
    stays silent on the daily one who just went quiet — exactly backwards."""
    rows = [_dyad("+steady", drift=1.0), _dyad("+stalled", drift=0.05)]
    alarmed = {r["peer_key"] for r in compute_drift(rows)}
    assert alarmed == {"+stalled"}


def test_drift_output_is_ordered_worst_first():
    rows = [_dyad("+a", drift=0.3), _dyad("+b", drift=0.05), _dyad("+c", drift=0.2)]
    assert [r["peer_key"] for r in compute_drift(rows)] == ["+b", "+c", "+a"]


def test_a_dyad_that_never_went_both_ways_raises_no_alarm():
    """You cannot drift from a relationship you never had."""
    assert compute_drift([_dyad("+x", recip=0, drift=0.0)]) == []


# --- reciprocity ---

def test_reciprocity_states_who_carries_it():
    rows = [_dyad("+owner_side", msgs=100, sent=90, recv=10),
            _dyad("+peer_side", msgs=100, sent=10, recv=90),
            _dyad("+even", msgs=100, sent=50, recv=50)]
    out = {r["peer_key"]: r["posture"] for r in compute_reciprocity(rows)}
    assert out == {"+owner_side": "owner_carries", "+peer_side": "peer_carries",
                   "+even": "mutual"}


def test_sent_and_received_are_owner_relative():
    r = compute_reciprocity([_dyad("+p", msgs=100, sent=70, recv=30)])[0]
    assert r["sent"] == 70 and r["received"] == 30


# --- registration ---

def test_the_kernels_are_reachable_by_declaration():
    assert {"warmth_banding", "drift_alarm", "reciprocity_profile"} <= registered_kinds()
