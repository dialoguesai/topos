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
    MIN_OPENINGS,
    MIN_REPLIES,
    MIN_TONE_LABELLED,
    apply_evidence_floor,
    compose_archetype_sentence,
    compute_archetype,
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


# --- the archetype ---

def _edges(their_opens=6, my_opens=2, their_lat=120.0, my_lat=600.0, their_replies=10,
           my_replies=10, dm=100, group=0, affect=None):
    """Per-peer aggregate in the shape `_edge_rows` returns."""
    return {"their": {"opens": their_opens, "replies": their_replies, "dm_msgs": dm // 2,
                      "latencies": [(their_lat, their_replies)]},
            "mine": {"opens": my_opens, "replies": my_replies, "dm_msgs": dm // 2,
                     "latencies": [(my_lat, my_replies)]},
            "dm_msgs": dm, "group_msgs": group, "their_affect": affect or {}}


def _circle(n=6, **over):
    """Enough above-floor dyads to calibrate, all alike unless overridden."""
    rows = [_dyad(f"+c{i}", msgs=100, streak=4) for i in range(n)]
    for r in rows:
        r["max_gap_days"] = 10.0
        r.update(over)
    edges = {f"+c{i}": _edges() for i in range(n)}
    return rows, edges


def test_archetype_is_registered_as_a_kernel_kind():
    assert "archetype" in registered_kinds()


def test_every_axis_abstains_on_its_own_floor_and_says_so():
    """A sentence never fills a gap with a guess: the unmeasured axes come back BY NAME."""
    rows, edges = _circle()
    thin = _dyad("+thin", msgs=20, streak=1)
    thin["max_gap_days"] = 40.0
    rows.append(thin)
    edges["+thin"] = _edges(their_opens=1, my_opens=1, their_replies=MIN_REPLIES - 1,
                            dm=0, group=0)
    out = {r["peer_key"]: r for r in compute_archetype(rows, edges)}
    row = out["+thin"]
    assert row["axes"]["initiation"]["band"] is None
    assert row["axes"]["responsiveness"]["band"] is None
    assert row["axes"]["venue"]["band"] is None
    assert row["axes"]["tone"]["band"] is None
    assert set(row["unmeasured"]) == {"initiation", "reply speed", "venue", "tone"}
    assert "starts" not in row["sentence"] and "answer" not in row["sentence"]
    # every abstention carries its reason
    for ax in ("initiation", "responsiveness", "venue", "tone"):
        assert row["axes"][ax]["reason"]


def test_initiation_is_a_share_with_semantic_cutpoints():
    rows, edges = _circle()
    edges["+c0"] = _edges(their_opens=7, my_opens=3)   # 0.70 -> opener
    edges["+c1"] = _edges(their_opens=2, my_opens=8)   # 0.20 -> responder
    edges["+c2"] = _edges(their_opens=5, my_opens=5)   # 0.50 -> shared
    out = {r["peer_key"]: r["axes"]["initiation"]["band"] for r in compute_archetype(rows, edges)}
    assert (out["+c0"], out["+c1"], out["+c2"]) == ("opener", "responder", "shared")
    assert MIN_OPENINGS > 1, "one opening is not a pattern"


def test_reply_speed_is_calibrated_against_the_owners_own_ties():
    """Ten minutes is quick in one circle and slow in another. The band comes from the
    owner's p33/p67, and the sentence says which world the word comes from."""
    rows, edges = _circle(n=9)
    for i in range(9):
        edges[f"+c{i}"] = _edges(their_lat=float(60 * (i + 1)))   # 1m .. 9m
    out = {r["peer_key"]: r for r in compute_archetype(rows, edges)}
    assert out["+c0"]["axes"]["responsiveness"]["band"] == "quick"
    assert out["+c8"]["axes"]["responsiveness"]["band"] == "slow_burn"
    assert "quicker than most" in out["+c0"]["sentence"]
    assert "slower than most" in out["+c8"]["sentence"]
    basis = out["+c0"]["threshold_basis"]
    assert basis["calibration"] == "own_baseline"
    assert basis["latency_quick_p33"] < basis["latency_slow_p67"]


def test_below_the_calibration_floor_the_kernel_describes_rather_than_bands():
    rows, edges = _circle(n=2)
    out = compute_archetype(rows, edges)
    assert out and all(r["threshold_basis"]["calibration"] == "insufficient_dyads" for r in out)
    assert all(r["axes"]["responsiveness"]["band"] == "measured" for r in out)
    assert all(r["axes"]["persistence"]["band"] is None for r in out)


def test_tone_is_the_peers_voice_and_needs_enough_labelled_messages():
    rows, edges = _circle()
    edges["+c0"] = _edges(affect={"neutral": 400, "curiosity": 9, "admiration": 6})
    edges["+c1"] = _edges(affect={"neutral": 400, "curiosity": 5})
    out = {r["peer_key"]: r for r in compute_archetype(rows, edges)}
    tone = out["+c0"]["axes"]["tone"]
    assert tone["band"] == "measured" and tone["labelled"] == 15
    assert [t["label"] for t in tone["top"]] == ["curiosity", "admiration"]
    assert "Curiosity and admiration, from 15 messages that carried one." in out["+c0"]["sentence"]
    assert out["+c1"]["axes"]["tone"]["band"] is None, "neutral never counts as a tone"
    assert MIN_TONE_LABELLED >= 10


def test_venue_reads_every_kind_and_names_the_group_share():
    rows, edges = _circle()
    edges["+c0"] = _edges(dm=90, group=10)    # one-to-one
    edges["+c1"] = _edges(dm=10, group=90)    # group
    edges["+c2"] = _edges(dm=60, group=40)    # mixed
    out = {r["peer_key"]: r for r in compute_archetype(rows, edges)}
    assert out["+c0"]["axes"]["venue"]["band"] == "one_to_one"
    assert "Mostly one-to-one." in out["+c0"]["sentence"]
    assert out["+c1"]["axes"]["venue"]["band"] == "group"
    assert "40% in groups you are both in." in out["+c2"]["sentence"]


def test_persistence_sets_streak_against_gap_on_the_owners_own_quantiles():
    rows, edges = _circle(n=8)
    for i, r in enumerate(rows):
        r["reciprocal_streak_weeks"] = 1 + i          # 1..8, p50 = 5
        r["max_gap_days"] = float(5 * (8 - i))        # 40..5 — long streak, short gap
    out = {r["peer_key"]: r for r in compute_archetype(rows, edges)}
    assert out["+c7"]["axes"]["persistence"]["band"] == "steady"
    assert out["+c0"]["axes"]["persistence"]["band"] == "bursty"
    assert "goes quiet for up to 40 days" in out["+c0"]["sentence"]
    assert "8 weeks without a break both ways" in out["+c7"]["sentence"]


def test_the_sentence_is_composed_only_from_measured_axes():
    full = compose_archetype_sentence({
        "initiation": {"band": "opener", "their_share": 0.7},
        "responsiveness": {"band": "quick", "their_median_s": 240},
        "venue": {"band": "one_to_one"},
        "persistence": {"band": "bursty", "max_gap_days": 21.0},
        "tone": {"top": [{"label": "curiosity", "n": 40}, {"label": "admiration", "n": 21}],
                 "labelled": 61},
    })
    assert full["sentence"] == (
        "An opener who runs hot and cold: starts 70% of your conversations, answers within 4m, "
        "quicker than most of your ties, and goes quiet for up to 21 days at a time. "
        "Mostly one-to-one. Curiosity and admiration, from 61 messages that carried one.")
    assert full["short"] == "Opener · bursty · one-to-one"
    empty = compose_archetype_sentence({k: {"band": None} for k in
                                        ("initiation", "responsiveness", "venue", "persistence", "tone")})
    assert empty["sentence"] == "A relationship the record cannot yet type."
    assert empty["short"] == ""


def test_the_archetype_keeps_the_evidence_floor():
    """Below eight messages or without a reciprocal period there is no type, same as warmth."""
    rows = [_dyad("+event", msgs=3, sent=0, recv=3, recip=0)]
    assert compute_archetype(rows, {"+event": _edges()}) == []
