"""L5 kernels over L1's directed edges — warmth, drift, reciprocity and the archetype.

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
           " recent_gap_days, drift_ratio, median_gap_days, tie_state,"
           " max_gap_days, longest_contact_streak_weeks"
           " FROM messenger_dyad_stats WHERE involves_self = 1 AND peer_class = 'human'")
    args: List[Any] = []
    if dataset_id:
        sql += " AND dataset_id = ?"
        args.append(dataset_id)
    keys = ["dataset_id", "a_key", "b_key", "total_msgs", "a_to_b", "b_to_a", "balance",
            "reciprocal_periods", "active_periods", "reciprocal_streak_weeks",
            "recent_gap_days", "drift_ratio", "median_gap_days", "tie_state",
            "max_gap_days", "contact_streak_weeks"]
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

#: Below this many above-floor dyads, quantile self-calibration is a tautology — a single
#: dyad is its own p75 and bands 'warm' against itself.
MIN_DYADS_FOR_CALIBRATION = 3


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
    if len(rows) < MIN_DYADS_FOR_CALIBRATION:
        # Quantiles of a distribution of one are that one value: the single dyad becomes
        # its own p75 and bands 'warm' by tautology. Below the floor, describe rather than
        # rank — every dyad reads 'steady' and the basis says why.
        basis = {"n_dyads": len(rows), "excluded_below_floor": excluded,
                 "calibration": "insufficient_dyads"}
        return [{"peer_key": _peer(r), "dataset_id": r["dataset_id"],
                 "warmth_band": "steady",
                 "reciprocal_periods": int(r["reciprocal_periods"] or 0),
                 "total_msgs": int(r["total_msgs"] or 0),
                 "recent_gap_days": r["recent_gap_days"],
                 "threshold_basis": basis} for r in rows]
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
        gap_known = r["recent_gap_days"] is not None
        gap = float(r["recent_gap_days"]) if gap_known else 0.0
        vol = float(r["total_msgs"] or 0)
        if recip <= 0:
            band = "never_direct"
        elif not gap_known:
            # Unknown recency used to coerce to 0.0 — "we don't know when you last spoke"
            # scored as "you spoke moments ago", the most favourable possible reading of
            # missing data. Unknown caps at steady: never warm on ignorance.
            band = "steady"
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


# --------------------------------------------------------------------------- archetype

#: The relational archetype — one sentence about how a relationship BEHAVES, from the owner's
#: seat. Not a personality type: the pack catalog excluded MBTI on validity grounds and Big Five
#: inference is gated behind a validation this corpus cannot pass (4 of 216 peers clear the
#: 200-message stylometry floor). What the directed edges can honestly say is who opens, how
#: fast each side answers, where it happens, how steadily, and in what tone — and that is a
#: type of the RELATIONSHIP, which is the card's subject.
#:
#: Five axes. Each abstains on its own floor, so a sentence never fills a gap with a guess;
#: the unmeasured axes are returned by name and the card says them.

#: A share of openings below this many openings is coin-flipping, not a pattern.
MIN_OPENINGS = 4
#: A reply-speed median over fewer replies than this is one afternoon, not a habit.
MIN_REPLIES = 3
#: Tone needs this many labelled (non-neutral) messages — measured on this node, 80 of 154
#: edge-months rest on three messages or fewer, and two faces from three messages is noise.
MIN_TONE_LABELLED = 10
#: Initiation is a share and interprets itself: past these it is a role, between them it is
#: shared. Fixed on purpose — "starts 70% of your conversations" means the same in any network.
OPENER_AT = 0.6
RESPONDER_AT = 0.4
#: Venue likewise: a DM share is a fact about where the relationship lives.
ONE_TO_ONE_AT = 0.8
GROUP_AT = 0.2
#: Reply speed and persistence are calibrated against the OWNER'S OWN dyads (own_baseline):
#: whether twenty minutes is quick depends on how everyone else answers this owner.
LATENCY_QUICK_Q = 0.33
LATENCY_SLOW_Q = 0.67

ARCHETYPE_KERNEL_VERSION = "1"


def _weighted_median(pairs: List[tuple]) -> Optional[float]:
    """Median of per-period medians, weighted by how many replies each period carried."""
    pts = [(float(v), max(int(w or 0), 1)) for v, w in pairs if v is not None and float(v) > 0]
    if not pts:
        return None
    pts.sort()
    total = sum(w for _v, w in pts)
    acc = 0
    for v, w in pts:
        acc += w
        if acc * 2 >= total:
            return v
    return pts[-1][0]


def _edge_rows(conn: sqlite3.Connection, dataset_id: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Per-peer aggregates over `messenger_directed_edges`, both directions, every kind.

    Initiation and reply speed are read from DM edges ONLY. In a room, "who spoke first" is
    who posted first that morning, not who opened a conversation with THIS person — measured
    live, one group-only peer carried 268 "initiations" against zero direct messages. Venue
    reads every kind, because the split between them IS the venue. Affect is the PEER'S
    messages (from_key = peer), so tone is how they sound to the owner, never the reverse.

    `affect_counts_json` arrived after the table did; select it only when present.
    """
    import json as _json

    from ...analytics.messenger_directed import MESSENGER_DIRECTED_EDGES_TABLE, SELF_KEY

    try:
        present = {r[1] for r in
                   conn.execute(f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})")}
    except sqlite3.Error:
        return {}
    if not present:
        return {}
    has_affect = "affect_counts_json" in present
    cols = "from_key, to_key, edge_kind, msgs, sessions_initiated, replies, median_reply_latency_s"
    if has_affect:
        cols += ", affect_counts_json"
    sql = f"SELECT {cols} FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
    args: List[Any] = []
    if dataset_id:
        sql += " WHERE dataset_id = ?"
        args.append(dataset_id)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        return {}

    out: Dict[str, Dict[str, Any]] = {}

    def _slot(peer: str) -> Dict[str, Any]:
        return out.setdefault(peer, {
            "their": {"opens": 0, "replies": 0, "dm_msgs": 0, "latencies": []},
            "mine": {"opens": 0, "replies": 0, "dm_msgs": 0, "latencies": []},
            "dm_msgs": 0, "group_msgs": 0, "their_affect": {},
        })

    for r in rows:
        r = tuple(r)
        from_key, to_key, kind = str(r[0]), str(r[1]), str(r[2] or "dm")
        msgs, opens, replies, lat = int(r[3] or 0), int(r[4] or 0), int(r[5] or 0), r[6]
        if from_key == SELF_KEY and to_key != SELF_KEY:
            peer, side = to_key, "mine"
        elif to_key == SELF_KEY and from_key != SELF_KEY:
            peer, side = from_key, "their"
        else:
            continue  # peer<->peer or self<->self: not this owner's relationship
        slot = _slot(peer)
        if kind == "dm":
            slot["dm_msgs"] += msgs
            s = slot[side]
            s["dm_msgs"] += msgs
            s["opens"] += opens
            s["replies"] += replies
            if lat is not None:
                s["latencies"].append((lat, replies))
            if side == "their" and has_affect and r[7]:
                try:
                    counts = _json.loads(r[7]) or {}
                except (ValueError, TypeError):
                    counts = {}
                for label, n in counts.items():
                    slot["their_affect"][str(label)] = (
                        slot["their_affect"].get(str(label), 0) + int(n or 0))
        else:
            slot["group_msgs"] += msgs
    return out


def _latency_word(seconds: float) -> str:
    if seconds < 90:
        return f"{int(round(seconds))}s"
    if seconds < 5400:
        return f"{int(round(seconds / 60))}m"
    if seconds < 172800:
        return f"{int(round(seconds / 3600))}h"
    return f"{int(round(seconds / 86400))}d"


def _join_clauses(parts: List[str]) -> str:
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def compose_archetype_sentence(axes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The sentence, from the axes. Deterministic English, no model.

    Shape: `<who they are in this exchange>: <the measured details>. <Venue>. <Tone>.`
    Every clause is present only when its axis measured; the caller receives the unmeasured
    axes by name so the card can say them quietly instead of the sentence guessing.
    """
    init = axes.get("initiation") or {}
    resp = axes.get("responsiveness") or {}
    venue = axes.get("venue") or {}
    pers = axes.get("persistence") or {}
    tone = axes.get("tone") or {}

    i_band = init.get("band")
    p_band = pers.get("band")
    verb = {"steady": "keeps a steady rhythm", "bursty": "runs hot and cold",
            "intermittent": "comes and goes"}.get(p_band or "", "")
    if i_band == "opener":
        lead = "An opener" + (f" who {verb}" if verb else "")
    elif i_band == "responder":
        lead = "A responder" + (f" who {verb}" if verb else "")
    elif i_band == "shared":
        lead = "Someone who shares the opening" + (f" and {verb}" if verb else "")
    elif verb:
        lead = f"Someone who {verb}"
    else:
        lead = "A relationship the record cannot yet type"

    details: List[str] = []
    if i_band:
        pct = int(round(float(init.get("their_share") or 0) * 100))
        if i_band == "opener":
            details.append(f"starts {pct}% of your conversations")
        elif i_band == "responder":
            details.append(f"you start {100 - pct}% of the conversations")
        else:
            details.append("starts about half of your conversations")
    if resp.get("band"):
        # Bands are the owner's own p33/p67, so a ten-minute reply can be "slow" in a circle
        # that answers in two. The comparative says which world the word comes from.
        word = _latency_word(float(resp["their_median_s"]))
        details.append({"quick": f"answers within {word}, quicker than most of your ties",
                        "slow_burn": f"answers in {word}, slower than most of your ties"}
                       .get(str(resp["band"]), f"answers in {word}"))
    if p_band == "steady":
        details.append(f"{int(pers['reciprocal_streak_weeks'])} weeks without a break both ways")
    elif p_band == "bursty":
        details.append(f"goes quiet for up to {int(round(float(pers['max_gap_days'])))} days at a time")
    elif p_band == "intermittent":
        details.append(f"longest two-way run {int(pers['reciprocal_streak_weeks'])} weeks")

    sentence = lead
    if details:
        sentence += ": " + _join_clauses(details)
    sentence += "."
    if venue.get("band") == "one_to_one":
        sentence += " Mostly one-to-one."
    elif venue.get("band") == "group":
        sentence += " Mostly in groups you are both in."
    elif venue.get("band") == "mixed":
        share = int(round((1.0 - float(venue.get("dm_share") or 0)) * 100))
        sentence += f" {share}% in groups you are both in."
    if tone.get("top"):
        labels = [str(t["label"]).replace("_", " ") for t in tone["top"]]
        n = int(tone.get("labelled") or 0)
        sentence += (f" {labels[0].capitalize()}" + (f" and {labels[1]}" if len(labels) > 1 else "")
                     + f", from {n} message{'' if n == 1 else 's'} that carried one.")

    short = [
        {"opener": "Opener", "responder": "Responder", "shared": "Shared opening"}.get(i_band or "", None),
        {"steady": "steady", "bursty": "bursty", "intermittent": "intermittent"}.get(p_band or "", None),
        {"one_to_one": "one-to-one", "group": "in groups", "mixed": "mixed venue"}.get(venue.get("band") or "", None),
    ]
    return {"sentence": sentence, "short": " · ".join(x for x in short if x)}


def compute_archetype(rows: List[Dict[str, Any]],
                      edges_by_peer: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Type every above-floor dyad by how it behaves, calibrated against the owner's own dyads.

    Returns one row per dyad above the evidence floor with `axes`, `unmeasured`, the
    composed `sentence`, and the `threshold_basis` it was drawn under.
    """
    rows, excluded = apply_evidence_floor(rows)
    if not rows:
        return []

    def _their_latency(peer: str) -> tuple:
        e = (edges_by_peer.get(peer) or {}).get("their") or {}
        if int(e.get("replies") or 0) < MIN_REPLIES:
            return None, int(e.get("replies") or 0)
        return _weighted_median(e.get("latencies") or []), int(e.get("replies") or 0)

    def _my_latency(peer: str) -> Optional[float]:
        e = (edges_by_peer.get(peer) or {}).get("mine") or {}
        if int(e.get("replies") or 0) < MIN_REPLIES:
            return None
        return _weighted_median(e.get("latencies") or [])

    lat_pool = [v for v in (_their_latency(_peer(r))[0] for r in rows) if v is not None]
    streak_pool = [float(r.get("reciprocal_streak_weeks") or 0) for r in rows]
    gap_pool = [float(r["max_gap_days"]) for r in rows if r.get("max_gap_days") is not None]
    calibrated = len(rows) >= MIN_DYADS_FOR_CALIBRATION
    basis: Dict[str, Any] = {
        "n_dyads": len(rows), "excluded_below_floor": excluded,
        "initiation": {"opener_at": OPENER_AT, "responder_at": RESPONDER_AT, "min_openings": MIN_OPENINGS},
        "venue": {"one_to_one_at": ONE_TO_ONE_AT, "group_at": GROUP_AT},
        "tone": {"min_labelled": MIN_TONE_LABELLED},
        "kernel_version": ARCHETYPE_KERNEL_VERSION,
    }
    if calibrated:
        basis["latency_quick_p33"] = _quantile(lat_pool, LATENCY_QUICK_Q) if lat_pool else None
        basis["latency_slow_p67"] = _quantile(lat_pool, LATENCY_SLOW_Q) if lat_pool else None
        basis["streak_p50"] = _quantile(streak_pool, 0.5)
        basis["gap_p50"] = _quantile(gap_pool, 0.5) if gap_pool else None
        basis["gap_p67"] = _quantile(gap_pool, 0.67) if gap_pool else None
        basis["calibration"] = "own_baseline"
    else:
        # Quantiles of a distribution of one are that one value. Below the floor, describe:
        # reply speed and persistence are reported as measured, never banded.
        basis["calibration"] = "insufficient_dyads"

    out = []
    for r in rows:
        peer = _peer(r)
        e = edges_by_peer.get(peer) or {}
        their, mine = e.get("their") or {}, e.get("mine") or {}
        axes: Dict[str, Dict[str, Any]] = {}
        unmeasured: List[str] = []

        # INITIATION — who opens. A share, so the cutpoints are semantic.
        opens_t, opens_m = int(their.get("opens") or 0), int(mine.get("opens") or 0)
        total_opens = opens_t + opens_m
        if total_opens >= MIN_OPENINGS:
            share = opens_t / total_opens
            band = "opener" if share >= OPENER_AT else "responder" if share <= RESPONDER_AT else "shared"
            axes["initiation"] = {"band": band, "their_share": round(share, 3),
                                  "their_openings": opens_t, "your_openings": opens_m}
        else:
            axes["initiation"] = {"band": None, "their_openings": opens_t, "your_openings": opens_m,
                                  "reason": f"fewer than {MIN_OPENINGS} conversation openings on record"}
            unmeasured.append("initiation")

        # RESPONSIVENESS — their reply speed, against how everyone else answers this owner.
        their_med, their_replies = _their_latency(peer)
        my_med = _my_latency(peer)
        if their_med is not None:
            if calibrated and lat_pool and len(lat_pool) >= MIN_DYADS_FOR_CALIBRATION:
                band = ("quick" if their_med <= basis["latency_quick_p33"]
                        else "slow_burn" if their_med >= basis["latency_slow_p67"] else "measured")
            else:
                band = "measured"
            axes["responsiveness"] = {"band": band, "their_median_s": round(their_med, 1),
                                      "your_median_s": round(my_med, 1) if my_med is not None else None,
                                      "their_replies": their_replies}
        else:
            axes["responsiveness"] = {"band": None, "their_replies": their_replies,
                                      "reason": f"fewer than {MIN_REPLIES} replies from them on record"}
            unmeasured.append("reply speed")

        # VENUE — where it happens.
        dm_msgs, group_msgs = int(e.get("dm_msgs") or 0), int(e.get("group_msgs") or 0)
        if dm_msgs + group_msgs > 0:
            dm_share = dm_msgs / (dm_msgs + group_msgs)
            band = "one_to_one" if dm_share >= ONE_TO_ONE_AT else "group" if dm_share <= GROUP_AT else "mixed"
            axes["venue"] = {"band": band, "dm_share": round(dm_share, 3),
                             "dm_msgs": dm_msgs, "group_msgs": group_msgs}
        else:
            axes["venue"] = {"band": None, "reason": "no directed edges on record"}
            unmeasured.append("venue")

        # PERSISTENCE — streak against gap, each against the owner's other dyads.
        streak = float(r.get("reciprocal_streak_weeks") or 0)
        gap = r.get("max_gap_days")
        if gap is not None and calibrated and gap_pool:
            gap = float(gap)
            if streak >= basis["streak_p50"] and gap <= basis["gap_p50"]:
                band = "steady"
            elif gap >= basis["gap_p67"] and streak <= basis["streak_p50"]:
                band = "bursty"
            else:
                band = "intermittent"
            axes["persistence"] = {"band": band, "reciprocal_streak_weeks": int(streak),
                                   "max_gap_days": round(gap, 1)}
        else:
            axes["persistence"] = {"band": None, "reciprocal_streak_weeks": int(streak),
                                   "max_gap_days": gap,
                                   "reason": ("too few dyads to calibrate" if gap is not None
                                              else "no gap statistics on record")}
            unmeasured.append("persistence")

        # TONE — how THEY sound, from their own messages, with the count that carries it.
        affect = e.get("their_affect") or {}
        labelled = sum(int(n) for k, n in affect.items() if k != "neutral" and n)
        if labelled >= MIN_TONE_LABELLED:
            top = sorted(((k, int(n)) for k, n in affect.items() if k != "neutral" and n),
                         key=lambda kv: (-kv[1], kv[0]))[:2]
            axes["tone"] = {"band": "measured", "labelled": labelled,
                            "top": [{"label": k, "n": n} for k, n in top]}
        else:
            axes["tone"] = {"band": None, "labelled": labelled,
                            "reason": f"fewer than {MIN_TONE_LABELLED} messages from them carried a tone"}
            unmeasured.append("tone")

        composed = compose_archetype_sentence(axes)
        out.append({
            "peer_key": peer, "dataset_id": r["dataset_id"],
            "sentence": composed["sentence"], "short": composed["short"],
            "axes": axes, "unmeasured": unmeasured,
            "total_msgs": int(r["total_msgs"] or 0),
            "threshold_basis": basis,
        })
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


@register_kernel("archetype", version=ARCHETYPE_KERNEL_VERSION)
def _archetype(conn, pack, lens, owner):
    return compute_archetype(_dyad_rows(conn), _edge_rows(conn))
