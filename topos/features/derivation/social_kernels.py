"""L5 kernels over L1's directed edges — warmth, drift and reciprocity.

These are the first lenses whose substrate is the social graph rather than the fact store,
and they exist because L1 finally made the inputs real: a dyad now carries counts each way,
streaks at two grains, gaps, and an own-baseline drift ratio.

Each is registered under its own `kind`, which is the lens contract working as designed —
new maths arrives as an engine kernel a pack may then NAME, rather than as a bespoke lane
wired to one call site.

**Calibration is against the owner's own distribution, never a global constant.** A warmth
band drawn at "more than 40 messages a month" says more about how the owner texts than about
which relationships are close. Every threshold here is a quantile of the owner's own dyads,
and every row records the thresholds it was computed under so a recompute can be compared
rather than merely trusted.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from .kernels import register_kernel

#: Warmth bands, coldest to warmest. `never_direct` is separate from `dormant` on purpose:
#: someone you have never exchanged messages with both ways is not a lapsed relationship,
#: and ranking them together is how a contact list starts looking like a friendship list.
WARMTH_BANDS = ("never_direct", "dormant", "cooling", "steady", "warm")


def _quantile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(int(len(xs) * q), len(xs) - 1)
    return float(xs[idx])


def _dyad_rows(conn: sqlite3.Connection, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Human, owner-involving dyads. Automated peers are excluded here rather than filtered
    later — a carrier shortcode in the distribution moves every quantile."""
    sql = ("SELECT dataset_id, a_key, b_key, total_msgs, a_to_b, b_to_a, balance,"
           " reciprocal_periods, active_periods, longest_reciprocal_streak_weeks,"
           " recent_gap_days, drift_ratio, median_gap_days, tie_state"
           " FROM messenger_dyad_stats WHERE involves_self = 1 AND peer_class = 'human'")
    args: List[Any] = []
    if dataset_id:
        sql += " AND dataset_id = ?"
        args.append(dataset_id)
    keys = ["dataset_id", "a_key", "b_key", "total_msgs", "a_to_b", "b_to_a", "balance",
            "reciprocal_periods", "active_periods", "reciprocal_streak_weeks",
            "recent_gap_days", "drift_ratio", "median_gap_days", "tie_state"]
    try:
        return [dict(zip(keys, tuple(r))) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.Error:
        return []


def _peer(row: Dict[str, Any]) -> str:
    return row["b_key"] if row["a_key"] == "self" else row["a_key"]


#: Below this, a dyad is an event rather than a relationship.
#:
#: Measured on the first live corpus, and the reason this exists: without a floor, 100 of 151
#: dyads read `peer_carries` — almost all of them single inbound messages the owner never
#: answered, each scoring a perfect -1.00 balance. Technically true, and useless: it made
#: "who carries our relationships" a report about one-off texts. Thin dyads also dragged the
#: quantiles (volume p75 was TEN messages), so they distorted the bands for everyone else too.
#:
#: The floor is applied BEFORE the thresholds are drawn, not after, so excluded dyads cannot
#: move the distribution they are excluded from.
DEFAULT_MIN_MESSAGES = 8
DEFAULT_MIN_RECIPROCAL_PERIODS = 1


def apply_evidence_floor(rows: List[Dict[str, Any]], *, min_messages: int = DEFAULT_MIN_MESSAGES,
                         min_reciprocal: int = DEFAULT_MIN_RECIPROCAL_PERIODS) -> tuple:
    """Split dyads into those that can support a claim and those that cannot.

    Returns (kept, excluded_count). Abstaining on the thin ones is the honest outcome: the
    node has met this person, and that is all it knows.
    """
    kept = [r for r in rows
            if int(r["total_msgs"] or 0) >= min_messages
            and int(r["reciprocal_periods"] or 0) >= min_reciprocal]
    return kept, len(rows) - len(kept)


def compute_warmth(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Band every dyad by calibrated thresholds drawn from the owner's own distribution.

    This replaces a measurably degenerate artifact. `synthesize_closeness` assigned
    `rel.closeness_tier` by FIXED RANK CUTOFF — top 3 `inner_circle`, next 5 `close`, next 12
    `regular` — so position alone decided the band whatever the weight distribution did, and
    the third-warmest relationship was "inner circle" on a node with three relationships or
    three hundred.

    Warmth here is reciprocity first, then recency, then volume, because those are the order
    in which they mean something: a thousand messages you never answered is not warmth, and
    a warm relationship that stopped six months ago is a memory.
    """
    rows, excluded = apply_evidence_floor(rows)
    if not rows:
        return []
    volumes = [float(r["total_msgs"] or 0) for r in rows]
    gaps = [float(r["recent_gap_days"] or 0) for r in rows]
    vol_hi, vol_mid = _quantile(volumes, 0.75), _quantile(volumes, 0.4)
    gap_lo, gap_hi = _quantile(gaps, 0.33), _quantile(gaps, 0.66)
    basis = {"volume_p75": vol_hi, "volume_p40": vol_mid,
             "recent_gap_p33": gap_lo, "recent_gap_p66": gap_hi, "n_dyads": len(rows),
             "excluded_below_floor": excluded}

    out = []
    for r in rows:
        recip = int(r["reciprocal_periods"] or 0)
        gap = float(r["recent_gap_days"] or 0)
        vol = float(r["total_msgs"] or 0)
        if recip <= 0:
            band = "never_direct"
        elif gap > max(gap_hi, 60.0):
            band = "dormant"
        elif gap > gap_lo and vol < vol_mid:
            band = "cooling"
        elif vol >= vol_hi and gap <= gap_hi:
            band = "warm"
        else:
            band = "steady"
        out.append({"peer_key": _peer(r), "dataset_id": r["dataset_id"], "warmth_band": band,
                    "reciprocal_periods": recip, "total_msgs": int(vol),
                    "recent_gap_days": gap, "threshold_basis": basis})
    return out


def compute_drift(rows: List[Dict[str, Any]], *, alarm_ratio: float = 0.4) -> List[Dict[str, Any]]:
    """Relationships running below their OWN historical rate.

    Own-baseline is the whole point. A global "you haven't talked in 30 days" alarm fires on
    every monthly correspondent and stays silent on the daily one who just went quiet — which
    is exactly backwards, and is why this compares each dyad only against itself.
    """
    rows, _ = apply_evidence_floor(rows)
    out = []
    for r in rows:
        drift = r["drift_ratio"]
        if drift is None or int(r["reciprocal_periods"] or 0) <= 0:
            continue
        if float(drift) < alarm_ratio:
            out.append({"peer_key": _peer(r), "dataset_id": r["dataset_id"],
                        "drift_ratio": float(drift), "alarm_ratio": alarm_ratio,
                        "recent_gap_days": r["recent_gap_days"],
                        "total_msgs": int(r["total_msgs"] or 0),
                        "basis": "own_baseline"})
    return sorted(out, key=lambda d: d["drift_ratio"])


def compute_reciprocity(rows: List[Dict[str, Any]], *, one_sided_at: float = 0.5) -> List[Dict[str, Any]]:
    """Who carries each relationship.

    `balance` is owner-relative — positive means the owner sends more — and the two streak
    counts are what make one-sidedness visible: sustained CONTACT with little sustained
    RECIPROCITY is someone talking at you, or you at them.
    """
    rows, _ = apply_evidence_floor(rows)
    out = []
    for r in rows:
        bal = r["balance"]
        if bal is None or not int(r["total_msgs"] or 0):
            continue
        bal = float(bal)
        if bal >= one_sided_at:
            posture = "owner_carries"
        elif bal <= -one_sided_at:
            posture = "peer_carries"
        else:
            posture = "mutual"
        out.append({"peer_key": _peer(r), "dataset_id": r["dataset_id"], "balance": bal,
                    "posture": posture, "sent": int(r["a_to_b"] if r["a_key"] == "self" else r["b_to_a"]),
                    "received": int(r["b_to_a"] if r["a_key"] == "self" else r["a_to_b"]),
                    "reciprocal_streak_weeks": int(r["reciprocal_streak_weeks"] or 0),
                    "threshold": one_sided_at})
    return out


# --------------------------------------------------------------------------- registration

@register_kernel("warmth_banding", version="1")
def _warmth(conn, pack, lens, owner):
    return compute_warmth(_dyad_rows(conn))


@register_kernel("drift_alarm", version="1")
def _drift(conn, pack, lens, owner):
    return compute_drift(_dyad_rows(conn))


@register_kernel("reciprocity_profile", version="1")
def _reciprocity(conn, pack, lens, owner):
    return compute_reciprocity(_dyad_rows(conn))
