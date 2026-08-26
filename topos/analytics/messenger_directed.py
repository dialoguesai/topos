"""L1 — directed dyadic edges and lifetime dyad stats.

`messenger_social_edges` records that two people share a conversation. It cannot say who
spoke first, who answers, or who has gone quiet — its edges are undirected co-participation,
and its primary key has no room for a direction. This module adds the half that carries
direction, so the questions Rings 6 and 7 actually ask become answerable:

    "In my top relationships, who initiates — me or them?"          (catalog #18)
    "Who am I losing touch with, compared to how often we used to?" (catalog #17)
    "Which friendships have become one-sided in the last months?"   (catalog #19)

Two tables, because they answer at two grains:

  * `messenger_directed_edges` — per period, per connector, per ORDERED pair. Counts,
    session initiations and reply latency, each direction stored separately.
  * `messenger_dyad_stats` — lifetime, per UNORDERED pair. Streaks, gaps, balance and tie
    state are properties of a relationship rather than of a direction, so the dyad is keyed
    canonically (a_key < b_key) and the per-direction totals ride along as columns.

**No content columns, by construction.** Neither table has a column for content, snippet,
subject, body or hash. The only free text stored is handles, ids and enum strings. This is a
schema-level privacy invariant, not a convention, and `test_l1_schema.py` fails if it is ever
violated — a directed-edge table is exactly the shape that tempts someone to cache "the last
thing they said" next to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MESSENGER_DIRECTED_EDGES_TABLE = "messenger_directed_edges"
MESSENGER_DYAD_STATS_TABLE = "messenger_dyad_stats"

#: The gap that ends a conversation session, so the next message opens one.
#:
#: 6 hours, chosen from the corpus rather than from taste. Measured over 7,392
#: within-conversation gaps the distribution has a clear elbow — p85 = 3.4 h, p90 = 16.7 h, a
#: 5x jump — and 6 h sits inside it, leaving 86.8% of gaps within a session.
#:
#: Deliberately FIXED rather than per-dyad self-calibrated: catalog #18 compares initiation
#: across relationships, and a per-dyad threshold makes those numbers incommensurable. The
#: value is written onto every row (`session_gap_seconds`) so a later recalibration can tell
#: which rows were produced under which threshold.
DEFAULT_SESSION_GAP_SECONDS = 6 * 3600

#: Peers that are not people. Kept rather than dropped (so "what is cluttering my inbox" stays
#: answerable) and filtered at read (so they never pollute a relationship ranking).
PEER_CLASS_HUMAN = "human"
PEER_CLASS_AUTOMATED = "automated"

#: What produced a directed edge. In the PRIMARY KEY on purpose.
#:
#: A single message to a 10-person room would otherwise mint 9 directed edges, and group
#: broadcast would swamp DM signal in every ranking that reads this table — the same failure
#: the undirected lane already has, where co-participation in a big thread outweighs a real
#: correspondence. Keeping the kinds in separate rows means volume conservation stays
#: checkable per kind, and a reader can ask for correspondence without asking for noise.
EDGE_KIND_DM = "dm"              # a two-person conversation: unambiguous direction
EDGE_KIND_GROUP_REPLY = "group_reply"      # reply_to_message_id: a HARD directed edge
EDGE_KIND_GROUP_BROADCAST = "group_broadcast"  # sender -> room: soft, and kept apart


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_peer(key: str) -> str:
    """Shortcodes are carriers, 2FA and delivery notices — 29 of 179 DM peers on the first
    live corpus checked. A 5-digit 'sender' is not a relationship."""
    k = str(key or "").strip().lstrip("+")
    if k.isdigit() and len(k) <= 6:
        return PEER_CLASS_AUTOMATED
    return PEER_CLASS_HUMAN


def dyad_key(a: str, b: str) -> tuple:
    """A dyad is an unordered pair, ordered canonically so it has one row.

    Returned as (a_key, b_key) with a_key <= b_key. Streaks, gaps and tenure belong to the
    relationship; storing them twice (once per direction) would let the two copies disagree.
    """
    x, y = str(a or ""), str(b or "")
    return (x, y) if x <= y else (y, x)


def create_directed_tables(conn: Any) -> None:
    """DDL for both L1 tables.

    Called from `_create_messenger_analytics_tables`, which `ensure_messenger_analytics_tables`
    already wraps in the write gate. Deliberately NOT a registry migration: every messenger_*
    table is feature-owned and created this way, and routing this through the registry would
    bump `user_version` past what an installed engine understands and fence the node out of
    every write — which is what happened on 2026-08-25.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DIRECTED_EDGES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            connector TEXT NOT NULL,
            edge_kind TEXT NOT NULL DEFAULT 'dm',
            from_key TEXT NOT NULL,
            to_key TEXT NOT NULL,
            msgs INTEGER NOT NULL DEFAULT 0,
            sessions_initiated INTEGER NOT NULL DEFAULT 0,
            replies INTEGER NOT NULL DEFAULT 0,
            median_reply_latency_s REAL,
            first_ts TEXT,
            last_ts TEXT,
            session_gap_seconds INTEGER NOT NULL,
            from_person_id TEXT,
            to_person_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, connector, edge_kind, from_key, to_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_period
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, period_key, connector)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DIRECTED_EDGES_TABLE}_from
        ON {MESSENGER_DIRECTED_EDGES_TABLE}(dataset_id, from_key)
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_DYAD_STATS_TABLE} (
            dataset_id TEXT NOT NULL,
            a_key TEXT NOT NULL,
            b_key TEXT NOT NULL,
            involves_self INTEGER NOT NULL DEFAULT 0,
            peer_class TEXT NOT NULL DEFAULT '{PEER_CLASS_HUMAN}',
            total_msgs INTEGER NOT NULL DEFAULT 0,
            a_to_b INTEGER NOT NULL DEFAULT 0,
            b_to_a INTEGER NOT NULL DEFAULT 0,
            balance REAL,
            first_ts TEXT,
            last_ts TEXT,
            active_periods INTEGER NOT NULL DEFAULT 0,
            reciprocal_periods INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_contact_streak_weeks INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_months INTEGER NOT NULL DEFAULT 0,
            longest_reciprocal_streak_weeks INTEGER NOT NULL DEFAULT 0,
            max_gap_days REAL,
            median_gap_days REAL,
            recent_gap_days REAL,
            drift_ratio REAL,
            tie_state TEXT,
            a_person_id TEXT,
            b_person_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, a_key, b_key)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_DYAD_STATS_TABLE}_self
        ON {MESSENGER_DYAD_STATS_TABLE}(dataset_id, involves_self, peer_class)
        """
    )


# --------------------------------------------------------------------------- extraction

#: The owner's endpoint. A literal rather than an entity id, so L1 never blocks on L0 —
#: `from_person_id` is the nullable column L0 backfills once the spine exists.
SELF_KEY = "self"


def _parse_ts(value: Any) -> Any:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None


def _period_of(dt: Any) -> str:
    return dt.strftime("%Y-%m")


class _Acc:
    """One ordered pair, in one period, from one connector, of one kind."""

    __slots__ = ("msgs", "sessions_initiated", "replies", "latencies", "first_ts", "last_ts")

    def __init__(self) -> None:
        self.msgs = 0
        self.sessions_initiated = 0
        self.replies = 0
        self.latencies: list = []
        self.first_ts = None
        self.last_ts = None

    def observe(self, dt) -> None:
        self.msgs += 1
        if self.first_ts is None or dt < self.first_ts:
            self.first_ts = dt
        if self.last_ts is None or dt > self.last_ts:
            self.last_ts = dt


def _median(xs: list):
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    return float(ys[n // 2]) if n % 2 else float((ys[n // 2 - 1] + ys[n // 2]) / 2)


def load_messages(conn: Any, dataset_id: str, connector: Any = None) -> list:
    """Ordered by conversation then time — the order the session walk depends on.

    Rows with no timestamp are dropped rather than defaulted: a message with no time cannot
    be placed in a period, cannot open or continue a session, and defaulting it to now would
    silently invent a conversation that happened at derivation time.
    """
    sql = ("SELECT conversation_id, message_id, sender_id, event_at, is_from_self,"
           " source_id, reply_to_message_id FROM conversation_messages"
           " WHERE dataset_id = ? AND event_at IS NOT NULL")
    args = [dataset_id]
    if connector:
        sql += " AND source_id = ?"
        args.append(connector)
    sql += " ORDER BY conversation_id, event_at"
    return conn.execute(sql, args).fetchall()


def classify_conversations(rows: list) -> dict:
    """DM iff exactly one distinct non-self sender ever spoke in it.

    Measured on the first live corpus: 181 of 202 conversations are DM-shaped. Note this is
    a property of the CORPUS, not of a room's roster — a group where only one other person
    ever spoke reads as a DM here, which is the honest answer for a direction metric built
    from messages rather than from membership.
    """
    senders: dict = {}
    for conv, _mid, sender, _ea, is_self, _src, _rt in rows:
        if not is_self and sender:
            senders.setdefault(conv, set()).add(str(sender))
    return {conv: (EDGE_KIND_DM if len(s) == 1 else EDGE_KIND_GROUP_BROADCAST)
            for conv, s in senders.items()}


def extract_directed_dyadic_edges(
    conn: Any,
    dataset_id: str,
    *,
    session_gap_seconds: int = DEFAULT_SESSION_GAP_SECONDS,
    connector: Any = None,
) -> dict:
    """Single pass over the corpus producing directed per-period aggregates.

    Sessions, initiations and replies all fall out of one walk, because they are three
    readings of the same fact — where the silences are:

      * a gap >= threshold ends a session, so the next message OPENS one and its sender is
        credited with an initiation;
      * a message from a different speaker than the previous one, inside a session, is a
        REPLY, and the elapsed time is its latency;
      * a message from the same speaker is neither — consecutive texts from one person are
        one turn, and counting each as an initiation is how "who initiates" becomes a
        measure of who is chattiest.

    Returns {(period, connector, edge_kind, from_key, to_key): _Acc}.
    """
    rows = load_messages(conn, dataset_id, connector)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _mid, sender, _ea, is_self, _src, _rt in rows:
        if not is_self and sender:
            peers.setdefault(conv, set()).add(str(sender))

    acc: dict = {}
    by_id: dict = {}

    def bucket(period, conn_id, kind, a, b):
        key = (period, conn_id, kind, a, b)
        if key not in acc:
            acc[key] = _Acc()
        return acc[key]

    cur_conv = None
    last_dt = None
    last_speaker = None
    for conv, mid, sender, ea, is_self, src, reply_to in rows:
        dt = _parse_ts(ea)
        if dt is None:
            continue
        by_id[str(mid)] = (str(sender or ""), bool(is_self), dt)
        speaker = SELF_KEY if is_self else str(sender or "")
        if not speaker:
            continue
        if conv != cur_conv:
            cur_conv, last_dt, last_speaker = conv, None, None

        opens_session = last_dt is None or (dt - last_dt).total_seconds() >= session_gap_seconds
        is_reply = (not opens_session) and last_speaker is not None and last_speaker != speaker
        latency = (dt - last_dt).total_seconds() if (is_reply and last_dt) else None

        period = _period_of(dt)
        kind = kinds.get(conv, EDGE_KIND_DM)
        conv_peers = peers.get(conv) or set()

        if kind == EDGE_KIND_DM:
            peer = next(iter(conv_peers)) if conv_peers else ""
            if peer:
                a, b = (SELF_KEY, peer) if is_self else (peer, SELF_KEY)
                e = bucket(period, src, EDGE_KIND_DM, a, b)
                e.observe(dt)
                if opens_session:
                    e.sessions_initiated += 1
                if is_reply:
                    e.replies += 1
                    if latency is not None:
                        e.latencies.append(latency)
        else:
            # A hard directed edge exists only where the corpus states one.
            if reply_to and str(reply_to) in by_id:
                tgt_sender, tgt_self, _tdt = by_id[str(reply_to)]
                target = SELF_KEY if tgt_self else tgt_sender
                if target and target != speaker:
                    e = bucket(period, src, EDGE_KIND_GROUP_REPLY, speaker, target)
                    e.observe(dt)
                    e.replies += 1
                    if latency is not None:
                        e.latencies.append(latency)
            # Broadcast: speaker -> everyone else who has ever spoken in the room. Soft by
            # construction, kept in its own rows so it can be excluded wholesale.
            room = set(conv_peers) | {SELF_KEY}
            for other in room:
                if other == speaker:
                    continue
                e = bucket(period, src, EDGE_KIND_GROUP_BROADCAST, speaker, other)
                e.observe(dt)
                if opens_session:
                    e.sessions_initiated += 1

        last_dt, last_speaker = dt, speaker
    return acc


def rows_for_persist(acc: dict, dataset_id: str, session_gap_seconds: int) -> list:
    now = _utc_now()
    out = []
    for (period, conn_id, kind, a, b), e in acc.items():
        out.append((dataset_id, period, str(conn_id or ""), kind, a, b, e.msgs,
                    e.sessions_initiated, e.replies, _median(e.latencies),
                    e.first_ts.isoformat() if e.first_ts else None,
                    e.last_ts.isoformat() if e.last_ts else None,
                    int(session_gap_seconds), None, None, now, now))
    return out


# --------------------------------------------------------------------------- persistence

def persist_directed_edges(conn: Any, dataset_id: str, rows: list, periods: Any = None) -> int:
    """Replace the directed rows for the periods this pass computed.

    Pruning is scoped to the periods actually recomputed, never a blanket delete: a partial
    pass (one connector, one month) must not silently erase everything else. The write and
    its commit both sit inside the gate — taking SQLite's write lock outside it is the
    lock-order inversion the write gate exists to prevent.
    """
    from ..storage.db.write_gate import batched_writes

    touched = set(periods) if periods is not None else {r[1] for r in rows}
    with batched_writes(conn):
        for period in sorted(touched):
            conn.execute(
                f"DELETE FROM {MESSENGER_DIRECTED_EDGES_TABLE}"
                " WHERE dataset_id = ? AND period_key = ?", (dataset_id, period))
        conn.executemany(
            f"""INSERT OR REPLACE INTO {MESSENGER_DIRECTED_EDGES_TABLE}
                (dataset_id, period_key, connector, edge_kind, from_key, to_key, msgs,
                 sessions_initiated, replies, median_reply_latency_s, first_ts, last_ts,
                 session_gap_seconds, from_person_id, to_person_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


def persist_dyad_stats(conn: Any, dataset_id: str, rows: list) -> int:
    """The lifetime rollup is a full replacement for this dataset — every row is derived
    from the whole corpus, so a partial update would leave rows describing a corpus that no
    longer exists."""
    from ..storage.db.write_gate import batched_writes

    with batched_writes(conn):
        conn.execute(f"DELETE FROM {MESSENGER_DYAD_STATS_TABLE} WHERE dataset_id = ?",
                     (dataset_id,))
        conn.executemany(
            f"""INSERT OR REPLACE INTO {MESSENGER_DYAD_STATS_TABLE}
                (dataset_id, a_key, b_key, involves_self, peer_class, total_msgs, a_to_b,
                 b_to_a, balance, first_ts, last_ts, active_periods, reciprocal_periods,
                 longest_contact_streak_months, longest_contact_streak_weeks,
                 longest_reciprocal_streak_months, longest_reciprocal_streak_weeks,
                 max_gap_days, median_gap_days, recent_gap_days, drift_ratio, tie_state,
                 a_person_id, b_person_id, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    return len(rows)


# --------------------------------------------------------------------------- dyad rollup

TIE_ACTIVE = "active"
TIE_COOLING = "cooling"
TIE_DORMANT = "dormant"
TIE_ONE_SIDED = "one_sided"
TIE_BROADCAST_ONLY = "broadcast_only"


def _longest_run(buckets: set, universe: list) -> int:
    """Longest run of CONSECUTIVE occupied buckets, measured against the corpus's own
    calendar rather than against the dyad's — a dyad that starts in month three cannot have
    a five-month streak, and indexing off its own first bucket would say it could."""
    idx = {b: i for i, b in enumerate(universe)}
    xs = sorted(idx[b] for b in buckets if b in idx)
    best = cur = 0
    prev = None
    for x in xs:
        cur = cur + 1 if prev is not None and x == prev + 1 else 1
        best = max(best, cur)
        prev = x
    return best


def build_dyad_stats(conn: Any, dataset_id: str, *, now: Any = None,
                     recent_window_days: int = 30) -> list:
    """Lifetime rollup, one row per unordered pair.

    Streaks are stored at BOTH grains and in BOTH flavours, which is not indecision. On a
    4.5-month corpus a monthly streak maxes at 5 — too coarse to see drift — while weekly
    carries 21 buckets at the same peer coverage. And the contrast between *contact* (either
    direction) and *reciprocal* (both directions in the bucket) is the whole of catalog #19,
    "which friendships have become one-sided": one number cannot express it.

    `drift_ratio` compares the dyad against ITS OWN baseline rate, never against a global
    one. A monthly correspondent is not drifting because a daily one exists.
    """
    rows = load_messages(conn, dataset_id)
    kinds = classify_conversations(rows)
    peers: dict = {}
    for conv, _m, s, _e, sf, _src, _rt in rows:
        if not sf and s:
            peers.setdefault(conv, set()).add(str(s))

    per: dict = {}
    all_months: set = set()
    all_weeks: set = set()
    for conv, _mid, sender, ea, is_self, _src, _rt in rows:
        if kinds.get(conv) != EDGE_KIND_DM:
            continue
        dt = _parse_ts(ea)
        if dt is None:
            continue
        cp = peers.get(conv) or set()
        if not cp:
            continue
        peer = next(iter(cp))
        a, b = dyad_key(SELF_KEY, peer)
        d = per.setdefault((a, b), {
            "times": [], "a_to_b": 0, "b_to_a": 0, "months": set(), "weeks": set(),
            "m_out": set(), "m_in": set(), "w_out": set(), "w_in": set()})
        speaker = SELF_KEY if is_self else peer
        mo, wk = dt.strftime("%Y-%m"), dt.strftime("%G-W%V")
        all_months.add(mo)
        all_weeks.add(wk)
        d["times"].append(dt)
        d["months"].add(mo)
        d["weeks"].add(wk)
        if speaker == a:
            d["a_to_b"] += 1
            d["m_out"].add(mo)
            d["w_out"].add(wk)
        else:
            d["b_to_a"] += 1
            d["m_in"].add(mo)
            d["w_in"].add(wk)

    months = sorted(all_months)
    weeks = sorted(all_weeks)
    ref = now or (max((t for d in per.values() for t in d["times"]), default=None))
    stamp = _utc_now()
    out = []
    for (a, b), d in per.items():
        times = sorted(d["times"])
        total = len(times)
        gaps = [(times[i] - times[i - 1]).total_seconds() / 86400.0
                for i in range(1, len(times))]
        peer = b if a == SELF_KEY else a
        recent_gap = ((ref - times[-1]).total_seconds() / 86400.0) if ref else None
        # own-baseline drift: recent rate vs this dyad's own lifetime rate, never a global one
        span_days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1.0)
        baseline = total / span_days
        recent = sum(1 for t in times
                     if ref and (ref - t).total_seconds() / 86400.0 <= recent_window_days)
        # Divide by the OBSERVABLE window, not the nominal one. A dyad three weeks old has
        # only three weeks in which recent messages could have happened; dividing those by 30
        # understates its rate and reports every new relationship as already cooling.
        observable = recent_window_days
        if ref is not None:
            age_days = (ref - times[0]).total_seconds() / 86400.0
            observable = max(min(recent_window_days, age_days), 1.0)
        drift = round((recent / observable) / baseline, 4) if baseline > 0 else None
        recip_m = d["m_out"] & d["m_in"]
        recip_w = d["w_out"] & d["w_in"]
        # BALANCE IS OWNER-RELATIVE, and it has to be stated because the naive definition is
        # silently wrong. `dyad_key` sorts canonically, so for a phone peer ('+1512…' < 's')
        # the owner is b_key, while for an email peer ('zoe@…' > 's') the owner is a_key.
        # Defining balance as (a_to_b - b_to_a) therefore FLIPS SIGN depending on how the
        # counterparty's identifier happens to sort — and "who is one-sided" would invert
        # between two peers for no reason but their phone number.
        #
        # Positive means the OWNER sends more. For a dyad with no owner in it the pair has no
        # privileged side, so the a->b convention stands and a_key/b_key are stored alongside.
        if not total:
            balance = None
        elif a == SELF_KEY:
            balance = round((d["a_to_b"] - d["b_to_a"]) / total, 4)
        elif b == SELF_KEY:
            balance = round((d["b_to_a"] - d["a_to_b"]) / total, 4)
        else:
            balance = round((d["a_to_b"] - d["b_to_a"]) / total, 4)

        if not recip_m:
            tie = TIE_BROADCAST_ONLY
        elif recent_gap is not None and recent_gap > 90:
            tie = TIE_DORMANT
        elif abs(balance or 0) >= 0.6:
            tie = TIE_ONE_SIDED
        elif drift is not None and drift < 0.5:
            tie = TIE_COOLING
        else:
            tie = TIE_ACTIVE

        out.append((
            dataset_id, a, b, 1 if SELF_KEY in (a, b) else 0, classify_peer(peer),
            total, d["a_to_b"], d["b_to_a"], balance,
            times[0].isoformat(), times[-1].isoformat(),
            len(d["months"]), len(recip_m),
            _longest_run(d["months"], months), _longest_run(d["weeks"], weeks),
            _longest_run(recip_m, months), _longest_run(recip_w, weeks),
            round(max(gaps), 4) if gaps else None,
            round(_median(gaps), 4) if gaps else None,
            round(recent_gap, 4) if recent_gap is not None else None,
            drift, tie, None, None, stamp, stamp))
    return out
