"""The social-graph charter: the invariants this feature is not allowed to lose.

Every test in this file is a REGRESSION GATE for a decision the owner made on purpose. They
are deliberately re-asserted here, independently of the feature tests scattered next to the
code, so that weakening one of these properties requires editing a file whose name says what
you are doing. The meta-test at the bottom fails if any of the distributed enforcement tests
is deleted — a guard on the guards.

If one of these fails, the right response is almost never to edit this file; it is to
understand which decision the change just reversed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from topos.analytics.messenger_communities import _compute_directed_lane
from topos.analytics.messenger_directed import (
    MESSENGER_DIRECTED_EDGES_TABLE,
    MESSENGER_DYAD_STATS_TABLE,
    SELF_KEY,
    create_directed_tables,
)

T0 = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
DS = "golden"


@pytest.fixture()
def golden(tmp_path):
    """A deterministic corpus small enough to hand-check: two humans, one shortcode.

    peer_a: 12 msgs each way across 3 weeks (real, reciprocal, above floor)
    peer_b: 5 inbound only (below floor — an event, not a relationship)
    262966: a shortcode (automated)
    """
    c = sqlite3.connect(str(tmp_path / "golden.db"))
    c.execute("""CREATE TABLE conversation_messages (
        conversation_id TEXT, message_id TEXT PRIMARY KEY, dataset_id TEXT,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT)""")
    n = 0
    def msg(conv, sender, minutes, is_self=0):
        nonlocal n
        n += 1
        c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?)",
                  (conv, f"m{n}", DS, sender,
                   (T0 + timedelta(minutes=minutes)).isoformat(), is_self, "imessage", None))
    for week in range(3):
        base = week * 7 * 24 * 60
        for i in range(4):
            msg("c_a", "peer_a", base + i * 30)
            msg("c_a", None, base + i * 30 + 5, is_self=1)
    for i in range(5):
        msg("c_b", "peer_b", i * 24 * 60)
    msg("c_s", "262966", 0)
    c.commit()
    yield c
    c.close()


# --- charter I: the numbers are conserved ---

def test_charter_volume_conservation(golden):
    """A1. Every DM message appears in exactly one directed row. If this drifts by one
    message, some contribution was counted without provenance or dropped without record."""
    _compute_directed_lane(golden, DS, None)
    raw = golden.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE conversation_id IN ('c_a','c_b','c_s')"
    ).fetchone()[0]
    directed = golden.execute(
        f"SELECT COALESCE(SUM(msgs),0) FROM {MESSENGER_DIRECTED_EDGES_TABLE} WHERE edge_kind='dm'"
    ).fetchone()[0]
    assert directed == raw == 30


def test_charter_direction_exists_and_mirrors(golden):
    """A2. Both directions of a reciprocal dyad exist as separate rows."""
    _compute_directed_lane(golden, DS, None)
    out = golden.execute(
        f"SELECT SUM(msgs) FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
        f" WHERE from_key=? AND to_key='peer_a'", (SELF_KEY,)).fetchone()[0]
    inb = golden.execute(
        f"SELECT SUM(msgs) FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
        f" WHERE from_key='peer_a' AND to_key=?", (SELF_KEY,)).fetchone()[0]
    assert out == 12 and inb == 12


def test_charter_initiations_sum_to_sessions(golden):
    """A3. Initiations are re-derivable: the two directions sum to the session count."""
    _compute_directed_lane(golden, DS, None)
    total = golden.execute(
        f"SELECT SUM(sessions_initiated) FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
        f" WHERE edge_kind='dm' AND (from_key='peer_a' OR to_key='peer_a')").fetchone()[0]
    assert total == 3, "three weekly bursts, one opener each"


def test_charter_streaks_bounded_by_calendar(golden):
    """A4. No streak may exceed the corpus's own calendar."""
    _compute_directed_lane(golden, DS, None)
    wk, mo = golden.execute(
        f"SELECT MAX(longest_contact_streak_weeks), MAX(longest_contact_streak_months)"
        f" FROM {MESSENGER_DYAD_STATS_TABLE}").fetchone()
    assert wk <= 3 and mo <= 1


# --- charter II: claims about people carry their basis ---

def test_charter_balance_is_owner_relative(golden):
    """Positive means THE OWNER sends more — never a function of how the peer's id sorts.
    ('peer_a' > 'self' and '+1512…' < 'self'; the sign must not care.)"""
    _compute_directed_lane(golden, DS, None)
    bal = golden.execute(
        f"SELECT balance FROM {MESSENGER_DYAD_STATS_TABLE}"
        f" WHERE a_key=? OR b_key=?", ("peer_a", "peer_a")).fetchone()[0]
    assert bal == 0.0, "12 each way is exactly even, from the owner's side"


def test_charter_the_floor_separates_events_from_relationships(golden):
    """peer_b (5 inbound, never answered) must never be banded, alarmed, or postured.
    116 of 151 live dyads were this shape; judging them made every number about noise."""
    from topos.features.derivation.social_kernels import (
        _dyad_rows, apply_evidence_floor, compute_drift, compute_reciprocity, compute_warmth)

    _compute_directed_lane(golden, DS, None)
    rows = _dyad_rows(golden)
    kept, excluded = apply_evidence_floor(rows)
    assert excluded >= 1
    judged = ({x["peer_key"] for x in compute_warmth(rows)}
              | {x["peer_key"] for x in compute_drift(rows)}
              | {x["peer_key"] for x in compute_reciprocity(rows)})
    assert "peer_b" not in judged


def test_charter_a_shortcode_is_never_a_relationship(golden):
    _compute_directed_lane(golden, DS, None)
    cls = golden.execute(
        f"SELECT peer_class FROM {MESSENGER_DYAD_STATS_TABLE}"
        f" WHERE a_key='262966' OR b_key='262966'").fetchone()[0]
    assert cls == "automated"


def test_charter_thresholds_ride_on_the_rows():
    """Calibration must be auditable: warmth rows carry threshold_basis, edges carry
    session_gap_seconds, affect carries coverage."""
    from topos.features.derivation.social_kernels import compute_warmth

    dyad = {"dataset_id": "d", "a_key": "self", "b_key": "p", "total_msgs": 50,
            "a_to_b": 25, "b_to_a": 25, "balance": 0.0, "reciprocal_periods": 2,
            "active_periods": 2, "reciprocal_streak_weeks": 2, "recent_gap_days": 1.0,
            "drift_ratio": 1.0, "median_gap_days": 1.0, "tie_state": "active"}
    out = compute_warmth([dyad] * 4)
    assert "threshold_basis" in out[0]
    assert "excluded_below_floor" in out[0]["threshold_basis"]


# --- charter III: privacy and consent ---

def test_charter_no_content_columns(tmp_path):
    """Counts and timestamps only. A snippet cached beside an edge is a message body in an
    analytics table no disclosure rule covers."""
    c = sqlite3.connect(str(tmp_path / "p.db"))
    create_directed_tables(c)
    for table in (MESSENGER_DIRECTED_EDGES_TABLE, MESSENGER_DYAD_STATS_TABLE):
        for (_, col, *_rest) in c.execute(f"PRAGMA table_info({table})"):
            for bad in ("content", "snippet", "subject", "body", "text", "message", "preview"):
                assert bad not in col.lower(), f"{table}.{col}"
    c.close()


def test_charter_outward_surface_is_enumerated():
    """Exactly the packs the owner approved may describe non-owners, all first-party."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    outward = {p.pack: p for p in load_packs(bundled_pack_dir()).values()
               if p.net_subject == "allow"}
    assert set(outward) == {"net.capability"}
    assert all(p.first_party for p in outward.values())


def test_charter_outward_packs_are_disabled_by_default():
    """Describing third parties is the owner's decision, never a seed's."""
    from topos.features.derivation.registry import ENABLED_BY_DEFAULT

    assert "net.capability" not in ENABLED_BY_DEFAULT


def test_charter_spine_tables_stay_undeprecated():
    from topos.features.lifecycle.gc import DEPRECATED_TABLES

    assert not {"persons", "person_aliases", "relationship_edges"} & set(DEPRECATED_TABLES)


def test_charter_packs_carry_no_code():
    """A lens NAMES an engine kernel. The day a pack ships an algorithm, opening the format
    means accepting someone's code over your message history."""
    import yaml

    from topos.features.derivation.registry import bundled_pack_dir

    allowed = {"kind", "predicate", "inputs", "min_evidence", "subject", "over",
               "calibration", "null_model", "coverage", "disclosure", "description",
               "note", "window"}
    for f in sorted(bundled_pack_dir().glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        for entry in (yaml.safe_load(f.read_text()) or {}).get("synthesis") or []:
            assert set(entry) <= allowed, f"{f.name}: {set(entry) - allowed}"


# --- charter IV: the guards on the guards ---

_ENFORCEMENT = [
    ("tests/analytics/test_l1_schema.py", "test_no_content_column_may_ever_exist"),
    ("tests/analytics/test_l1_extract.py", "test_a_dm_produces_two_rows_that_sum_to_the_raw_count"),
    ("tests/analytics/test_l1_dyad_stats.py", "test_balance_sign_does_not_depend_on_how_the_peer_sorts"),
    ("tests/features/derivation/test_social_kernels.py", "test_the_floor_is_applied_before_the_thresholds_are_drawn"),
    ("tests/features/derivation/test_net_subject_policy.py", "test_a_third_party_pack_may_not_declare_net_subject_allow"),
    ("tests/features/derivation/test_net_subject_policy.py", "test_the_blackhole_still_stops_the_owner"),
    ("tests/features/test_l4_projection_guard.py", "test_a_third_party_fact_does_not_project"),
    ("tests/features/test_merge_honesty.py", "test_an_exclusion_survives_the_merge"),
    ("tests/features/test_owner_selector.py", "test_no_production_site_reads_is_self_unordered"),
    ("tests/analytics/test_l1_orchestrator.py", "test_no_new_trigger_was_introduced"),
]


def test_charter_the_enforcement_tests_still_exist():
    """A regression gate is only as durable as its own existence. Deleting one of the
    distributed guard tests now fails HERE, with a name that says which decision lost its
    enforcement."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    missing = [f"{f}::{t}" for f, t in _ENFORCEMENT
               if t not in (root / f.replace("tests/", "")).read_text(errors="ignore")]
    assert not missing, "enforcement deleted: " + ", ".join(missing)
