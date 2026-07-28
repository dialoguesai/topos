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
]
_RANK = {t["id"]: i for i, t in enumerate(TIERS)}


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
    return new
