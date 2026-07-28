"""Badges: persistent artifacts of aha moments (USER_JOURNEY_ATLAS addendum,
PLAN_NEWSLETTER_UNLOCK.md §3).

Awarded idempotently by `award_badges` (piggybacked on the attention_triage
job — no new machinery), stored as owner-only signal objects with an
earned-at snapshot of the criteria. `TIERS` defines the hierarchy; the
highest earned badge is the one worn in the app header (quiet, IYKYK).
Badges are scenery, never gates: nothing checks a badge to grant function.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from ..signal.signal_object_store import SignalObjectStore
from .readiness import newsletter_readiness

# ascending rank: the last earned-tier wins the header slot
TIERS: List[Dict[str, str]] = [
    {"id": "first_signal", "label": "First Signal", "glyph": "·",
     "blurb": "your first connector synced real data"},
    {"id": "triangulated", "label": "Triangulated", "glyph": "△",
     "blurb": "three connectors feeding your node"},
    {"id": "steady_stream", "label": "Steady Stream", "glyph": "≋",
     "blurb": "a seven-day recency streak"},
    {"id": "signal_unlocked", "label": "Signal Unlocked", "glyph": "◈",
     "blurb": "your Daily Signal digest is live"},
    {"id": "first_pin", "label": "First Pin", "glyph": "📍",
     "blurb": "you declared a future"},
    {"id": "steered", "label": "Steered", "glyph": "⤳",
     "blurb": "a seed arrived via your own pin"},
    {"id": "calibrated", "label": "Calibrated", "glyph": "⚖",
     "blurb": "25 verdict labels given"},
    {"id": "two_way_street", "label": "Two-Way Street", "glyph": "⇄",
     "blurb": "you inspected the ledger"},
    {"id": "chronicler", "label": "Chronicler", "glyph": "✎",
     "blurb": "one hundred journal entries kept"},
    {"id": "cartographer", "label": "Cartographer", "glyph": "🗺",
     "blurb": "a thousand entities on your map"},
    {"id": "ten_thousand_things", "label": "Ten Thousand Things", "glyph": "∞",
     "blurb": "ten thousand items triaged"},
]
_RANK = {t["id"]: i for i, t in enumerate(TIERS)}

# ---- two-currency mechanics (PLAN_NEWSLETTER_UNLOCK.md §9) --------------------
# Achievement points; network-effect actions weigh highest so rank tracks the
# flywheel. host/lighthouse are CP-awarded (cross-topos events) — points
# reserved here so rank math lives in one place.
POINTS: Dict[str, int] = {
    "first_signal": 10, "triangulated": 20, "steady_stream": 30, "first_pin": 15,
    "steered": 25, "signal_unlocked": 50, "calibrated": 40, "two_way_street": 20,
    "chronicler": 30, "cartographer": 40, "ten_thousand_things": 75,
    "host": 60, "lighthouse": 100,
}

# Public rank tiers: absolute, log-spaced thresholds; computed ON-NODE — no
# leaderboard, no central comparison. Status without surveillance.
RANK_TIERS: List[Dict[str, Any]] = [
    {"id": "trace", "label": "Trace", "glyph": "·", "threshold": 10},
    {"id": "signal", "label": "Signal", "glyph": "◆", "threshold": 60},
    {"id": "resonance", "label": "Resonance", "glyph": "≋", "threshold": 150},
    {"id": "beacon", "label": "Beacon", "glyph": "◈", "threshold": 300},
    {"id": "constellation", "label": "Constellation", "glyph": "✦", "threshold": 500},
]


def points_total(conn: sqlite3.Connection) -> int:
    return sum(POINTS.get(b.get("badge_id", ""), 0) for b in earned_badges(conn))


def rank(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The public layer: point tally -> tier. Worn chip defaults to the tier
    glyph; achievement-glyph wearing stays the owner's IYKYK option."""
    pts = points_total(conn)
    tier = None
    for t in RANK_TIERS:
        if pts >= t["threshold"]:
            tier = t
    nxt = next((t for t in RANK_TIERS if pts < t["threshold"]), None)
    return {
        "points": pts,
        "tier": tier,
        "next_tier": None if nxt is None else {**nxt, "points_needed": nxt["threshold"] - pts},
    }


def earned_badges(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    import json
    try:
        rows = conn.execute(
            "SELECT payload_json FROM signal_objects WHERE object_type='badge' "
            "AND valid_to IS NULL").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for (pj,) in rows:
        try:
            out.append(json.loads(pj))
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: _RANK.get(b.get("badge_id", ""), -1))


def current_badge(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    earned = earned_badges(conn)
    return earned[-1] if earned else None


def _award(store: SignalObjectStore, badge_id: str, snapshot: Dict[str, Any]) -> None:
    tier = next(t for t in TIERS if t["id"] == badge_id)
    store.upsert_object(
        "intentions", "badge", f"badge:{badge_id}",
        {"badge_id": badge_id, "label": tier["label"], "glyph": tier["glyph"],
         "blurb": tier["blurb"], "rank": _RANK[badge_id],
         "criteria_snapshot": snapshot, "disclosure": "owner_only"},
        source_refs=[{"awarded_by": "attention_triage"}],
        confidence=1.0, extractor_version="badges_v1")


def award_badges(conn: sqlite3.Connection) -> List[str]:
    """Idempotent criteria sweep; returns newly-awarded badge ids."""
    have = {b.get("badge_id") for b in earned_badges(conn)}
    store = SignalObjectStore(conn)
    new: List[str] = []
    r = newsletter_readiness(conn)

    def maybe(badge_id: str, condition: bool, snapshot: Dict[str, Any]) -> None:
        if condition and badge_id not in have:
            _award(store, badge_id, snapshot)
            new.append(badge_id)

    maybe("first_signal", r["connectors"]["have"] >= 1, {"connectors": r["connectors"]["have"]})
    maybe("triangulated", r["connectors"]["have"] >= 3, {"connectors": r["connectors"]["have"]})
    maybe("steady_stream", r["streak_days"]["have"] >= 7, {"streak": r["streak_days"]["have"]})
    maybe("signal_unlocked", r["ready"], {"readiness": {k: r[k] for k in ("connectors", "streak_days", "total_items")}})

    pins = conn.execute(
        "SELECT COUNT(*) FROM signal_objects WHERE object_type='declared_intent' "
        "AND valid_to IS NULL").fetchone()[0]
    maybe("first_pin", pins >= 1, {"pins": pins})

    import json as _json
    steered = False
    for (pj,) in conn.execute(
            "SELECT payload_json FROM signal_objects WHERE object_type='attention_summary' "
            "AND valid_to IS NULL"):
        try:
            if any(s.get("via_intent") for s in _json.loads(pj).get("seeds", [])):
                steered = True
                break
        except (TypeError, ValueError):
            continue
    maybe("steered", steered, {})

    labels = conn.execute(
        "SELECT COUNT(*) FROM triage_verdicts WHERE user_label IS NOT NULL").fetchone()[0]
    maybe("calibrated", labels >= 25, {"labels": labels})

    def _count(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return 0

    journals = _count("SELECT COUNT(*) FROM journal_entries")
    maybe("chronicler", journals >= 100, {"journal_entries": journals})
    entities = _count("SELECT COUNT(*) FROM entities")
    maybe("cartographer", entities >= 1000, {"entities": entities})
    triaged = _count("SELECT COUNT(*) FROM triage_verdicts")
    maybe("ten_thousand_things", triaged >= 10000, {"triaged": triaged})
    return new
