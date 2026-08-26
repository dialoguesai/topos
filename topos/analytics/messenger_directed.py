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
